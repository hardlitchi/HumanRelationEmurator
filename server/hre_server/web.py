"""人と管理者のための Web UI（スマホ対応）。MCP サーバーと同じ service 層・同じトークンを使う。

  HRE_WEB_SECRET=... python -m hre_server.web --host 127.0.0.1 --port 8001

- ログイン: 人間（kind = human）の actor のトークンを1度入力すると、署名つき Cookie のセッションになる。
  トークンを rotate / revoke すると既存のセッションも無効になる
- 権限は MCP と同じ（ワークスペースごとの read / write / sensitive_read、管理者）
- 人が書いた主張は「人間による書き込み」なので、人格フィールドや管理者関係もここから書ける
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import anyio.to_thread
from psycopg import Connection, errors
from starlette.applications import Starlette
from starlette.datastructures import FormData
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import service
from .db import pool

HERE = Path(__file__).resolve().parent
COOKIE = "hre_session"
SESSION_SECONDS = 30 * 24 * 3600

EPISTEMIC_LABELS = {"stated": "本人談", "observed": "見聞き", "reported": "伝聞", "inferred": "推測"}
KIND_LABELS = {"person": "人", "organization": "組織", "group": "グループ"}
WS_KIND_LABELS = {"owner": "管理者本人", "persona": "人格", "shared": "共有"}
SCOPE_LABELS = {"read": "閲覧", "write": "編集", "sensitive_read": "機微情報", "admin": "管理者",
                "shared_read": "共有で閲覧"}

ACTION_LABELS = {"create": "追加", "update": "変更", "retract": "取り消し", "end": "終了", "delete": "削除",
                 "merge": "統合", "erase": "完全削除", "add_identifier": "識別子を追加", "propose_field": "項目を提案",
                 "add_field_alias": "別名を追加", "set_status": "状態を変更"}
TARGET_LABELS = {"facts": "情報", "relationships": "関係", "entities": "人", "sources": "出典", "interactions": "やり取り",
                 "entity_identifiers": "識別子", "field_definitions": "項目", "field_aliases": "項目の別名"}

CHANNEL_LABELS = {"in_person": "対面", "call": "通話", "video": "ビデオ通話", "chat": "チャット", "email": "メール",
                  "sns": "SNS", "event": "イベント", "stream": "配信", "stream_chat": "配信コメント", "other": "その他"}
DEPTH_LABELS = {1: "挨拶程度", 2: "用件", 3: "深い会話"}

templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["fmt_value"] = lambda v: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
TZ = ZoneInfo(os.environ.get("HRE_TIMEZONE", "Asia/Tokyo"))   # 画面に出す時刻のタイムゾーン


def _dt(v: Any) -> str:
    if not hasattr(v, "strftime"):
        return v or ""
    return (v.astimezone(TZ) if getattr(v, "tzinfo", None) else v).strftime("%Y-%m-%d %H:%M")


templates.env.filters["dt"] = _dt

# 識別子の値が http(s) の URL なら人物カードでリンクにする。scheme ごとの見出し（無ければ URL をそのまま出す）
LINK_LABELS = {"stackchan": "スタックチャンで開く"}


def _link(v: Any) -> str:
    """http(s) の URL ならそのまま、それ以外は ""（javascript: などはリンクにしない）。"""
    if not isinstance(v, str) or not v.lower().startswith(("http://", "https://")):
        return ""
    return v if urlsplit(v).netloc else ""


templates.env.filters["link"] = _link
templates.env.filters["pct"] = lambda v: "—" if v is None else f"{round(float(v) * 100)}"
templates.env.globals.update(LINK_LABELS=LINK_LABELS, EPISTEMIC=EPISTEMIC_LABELS, KINDS=KIND_LABELS, WS_KINDS=WS_KIND_LABELS,
                             SCOPES=SCOPE_LABELS,
                             ACTIONS=ACTION_LABELS, TARGETS=TARGET_LABELS,
                             CHANNELS=CHANNEL_LABELS, DEPTHS=DEPTH_LABELS)


class LoginRequired(Exception):
    pass


# ---------------------------------------------------------------------------
# セッション（署名つき Cookie）
# ---------------------------------------------------------------------------

class Sessions:
    """Cookie = base64(json{actor_id, token_hash の先頭, 期限}) + "." + HMAC。

    token_hash を含めるので、トークンを rotate / revoke した時点でそのセッションは使えなくなる。
    """

    def __init__(self, secret: str):
        self.key = hashlib.sha256(("hre-web:" + secret).encode()).digest()

    def _sign(self, data: str) -> str:
        return hmac.new(self.key, data.encode(), hashlib.sha256).hexdigest()

    def issue(self, actor_id: str, token_hash: str) -> str:
        payload = json.dumps({"a": actor_id, "h": token_hash[:16], "e": int(time.time()) + SESSION_SECONDS})
        data = base64.urlsafe_b64encode(payload.encode()).decode()
        return f"{data}.{self._sign(data)}"

    def read(self, cookie: str | None) -> dict | None:
        if not cookie or "." not in cookie:
            return None
        data, sig = cookie.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(data)):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(data))
        except ValueError:
            return None
        return payload if payload.get("e", 0) > time.time() else None

    def csrf(self, cookie: str) -> str:
        return self._sign("csrf:" + cookie)


def _current(conn: Connection, request: Request) -> service.Actor:
    sessions: Sessions = request.app.state.sessions
    s = sessions.read(request.cookies.get(COOKIE))
    if s is None:
        raise LoginRequired
    row = service._one(conn, """SELECT id::text FROM hre.actors
                                 WHERE id = %s AND kind = 'human' AND revoked_at IS NULL
                                   AND left(token_hash, 16) = %s""", (s["a"], s["h"]))
    if row is None:
        raise LoginRequired
    return service.load_actor(conn, row["id"])


# ---------------------------------------------------------------------------
# 共通の入口
# ---------------------------------------------------------------------------

def _back(request: Request, path: str | None = None, fallback: str = "/", **params: str) -> RedirectResponse:
    # 戻り先はフォームの back（自サイト内のパスだけ）→ Referer → トップ
    back = str(getattr(request.state, "form", {}).get("back", ""))
    if not back.startswith("/") or back.startswith("//"):
        back = urlsplit(request.headers.get("referer", "")).path
    url = path or back or fallback
    if params:
        url += "?" + urlencode(params)
    return RedirectResponse(url, status_code=303)


def _home_of(path: str) -> str:
    """POST 先のパスから、エラー時に戻る画面（/e/{id}/facts → /e/{id}）。"""
    parts = path.strip("/").split("/")
    return f"/{parts[0]}/{parts[1]}" if len(parts) >= 2 and parts[0] in ("e", "s", "w", "admin") else "/"


def page(handler):
    """GET 用: ログイン確認・DB 接続・HreError を画面に出す。"""
    def endpoint(request: Request) -> Response:
        with pool().connection() as conn:
            try:
                actor = _current(conn, request)
            except LoginRequired:
                return RedirectResponse("/login?" + urlencode({"next": request.url.path}), status_code=303)
            try:
                name, ctx = handler(request, conn, actor)
            except service.HreError as e:
                name, ctx = "error.html", {"message": str(e)}
            except errors.DataError:   # UUID の形をしていない ID など
                name, ctx = "error.html", {"message": "見つかりません"}
            cookie = request.cookies[COOKIE]
            ctx.update(request=request, me=actor, csrf=request.app.state.sessions.csrf(cookie),
                       flash=request.query_params.get("ok"), error=request.query_params.get("err"))
            return templates.TemplateResponse(request, name, ctx,
                                              status_code=404 if name == "error.html" else 200)
    endpoint.__name__ = handler.__name__
    return endpoint


def action(handler):
    """POST 用: CSRF 確認のうえ実行し、元の画面にメッセージ付きで戻す。"""
    async def endpoint(request: Request) -> Response:
        form = await request.form()
        request.state.form = form

        def run() -> Response:
            with pool().connection() as conn:
                try:
                    actor = _current(conn, request)
                except LoginRequired:
                    return RedirectResponse("/login", status_code=303)
                expected = request.app.state.sessions.csrf(request.cookies[COOKIE])
                if not hmac.compare_digest(str(form.get("csrf", "")), expected):
                    return Response("CSRF token mismatch", status_code=403)
                try:
                    return handler(request, conn, actor, form)
                except service.HreError as e:
                    return _back(request, fallback=_home_of(request.url.path), err=str(e))
                except (ValueError, KeyError, errors.DataError) as e:
                    return _back(request, fallback=_home_of(request.url.path), err=f"入力が正しくありません: {e}")
        return await anyio.to_thread.run_sync(run)
    endpoint.__name__ = handler.__name__
    return endpoint


# ---------------------------------------------------------------------------
# ログイン
# ---------------------------------------------------------------------------

def login_form(request: Request) -> Response:
    with pool().connection() as conn:
        needs_setup = not service.has_admin(conn)   # 初回だけ、最初のトークンの出し方を案内する
    return templates.TemplateResponse(request, "login.html", {"next": request.query_params.get("next", "/"),
                                                              "error": request.query_params.get("err"),
                                                              "needs_setup": needs_setup})


async def login(request: Request) -> Response:
    form = await request.form()
    token = str(form.get("token", "")).strip()
    nxt = str(form.get("next", "/"))
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"

    def check() -> service.Actor | None:
        with pool().connection() as conn:
            return service.authenticate(conn, token) if token else None
    actor = await anyio.to_thread.run_sync(check)
    if actor is None:
        return RedirectResponse("/login?" + urlencode({"err": "トークンが無効です", "next": nxt}), status_code=303)
    if actor.kind != "human":
        return RedirectResponse("/login?" + urlencode({"err": "エージェント用のトークンではログインできません。"
                                                              "人間用（kind = human）のトークンを使ってください",
                                                       "next": nxt}), status_code=303)
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(COOKIE, request.app.state.sessions.issue(actor.id, service.hash_token(token)),
                    max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                    secure=request.app.state.secure_cookies)
    return resp


async def logout(request: Request) -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---------------------------------------------------------------------------
# 閲覧
# ---------------------------------------------------------------------------

@page
def home(request, conn, me):
    workspaces = service.list_workspaces(conn, me)
    return "home.html", {"workspaces": workspaces}


@page
def workspace(request, conn, me):
    key = request.path_params["key"]
    ws = service.workspace_access(conn, me, key)
    q = request.query_params.get("q") or None
    kind = request.query_params.get("kind") or None
    entities = service.list_entities(conn, me, key, q, kind)
    # 自分（このワークスペースの視点の主）との強さを並べる。コアリスナーや親しい相手が上に来るように並べ替えもできる
    self_e = next((x for x in entities if x["is_self"]), None) or service._one(
        conn, "SELECT id::text FROM hre.entities WHERE workspace_id = %s AND is_self", (ws["id"],))
    ties = {t["other_id"]: t for t in service.entity_ties(conn, ws["id"], self_e["id"])} if self_e else {}
    for x in entities:
        x["tie"] = ties.get(x["id"])
    sort = request.query_params.get("sort") or ""
    if sort == "strength":
        entities.sort(key=lambda x: (not x["is_self"], -(x["tie"]["strength"] if x["tie"] else -1)))
    return "workspace.html", {"ws": ws, "entities": entities, "q": q or "", "kind": kind or "", "sort": sort,
                              "has_self": bool(self_e)}


@page
def entity(request, conn, me):
    e = service.get_entity(conn, me, request.path_params["id"], detail=True)
    if "merged_into" in e:
        return "error.html", {"message": "この人は統合済みです", "link": f"/e/{e['merged_into']}"}
    ws = service.workspace_access(conn, me, e["workspace"])
    ctx: dict[str, Any] = {"e": e, "ws": ws}
    # cardinality = one は先頭（確度順で採用されるもの）だけを主に表示し、残りは「他の主張」に回す
    groups: dict[str, dict] = {}
    for f in e["facts"]:
        g = groups.setdefault(f["field_key"], {"label": f["field_label"], "main": [], "rest": []})
        g["rest" if f["cardinality"] == "one" and g["main"] else "main"].append(f)
    ctx["fact_groups"] = groups
    if ws["can_write"]:
        ctx["fields"] = service.list_fields(conn, e["kind"])
        ctx["rel_fields"] = service.list_fields(conn, "relationship")
        ctx["relation_types"] = service.list_relation_types(conn)
        ctx["others"] = [x for x in service.list_entities(conn, me, e["workspace"]) if x["id"] != e["id"]]
    ctx["interactions"] = service.list_interactions(conn, me, e["id"], limit=30)
    ctx["activity"] = service.list_activity(conn, me, entity_id=e["id"], limit=20)
    return "entity.html", ctx


@page
def source(request, conn, me):
    return "source.html", {"s": service.get_source_detail(conn, me, request.path_params["id"])}


@page
def clusters(request, conn, me):
    key = request.path_params["key"]
    return "clusters.html", {"ws": service.workspace_access(conn, me, key),
                             "clusters": service.list_clusters(conn, me, key)}


@page
def sources(request, conn, me):
    key = request.path_params["key"]
    return "sources.html", {"ws": service.workspace_access(conn, me, key),
                            "sources": service.list_sources(conn, me, key)}


@page
def activity(request, conn, me):
    key = request.query_params.get("workspace") or None
    return "activity.html", {"rows": service.list_activity(conn, me, workspace=key, limit=200), "workspace": key}


# ---------------------------------------------------------------------------
# 編集
# ---------------------------------------------------------------------------

def _parse_value(conn: Connection, field: str, form: FormData) -> Any:
    """フォームの文字列をフィールドの型に合わせる（型の最終チェックは DB のトリガー）。"""
    f = service._one(conn, "SELECT value_type FROM hre.resolve_field(%s) WHERE key IS NOT NULL", (field,))
    if f is None:
        raise service.HreError(f"フィールド {field!r} はマスターにありません")
    raw = str(form.get("value_entity" if f["value_type"] == "entity_ref" else "value", "")).strip()
    if raw == "":
        raise service.HreError("値を入力してください")
    match f["value_type"]:
        case "number":
            return float(raw)
        case "integer":
            return int(raw)
        case "boolean":
            return raw.lower() in ("true", "1", "はい", "yes")
        case "json":
            return json.loads(raw)
        case _:
            return raw


def _opt(form: FormData, name: str) -> str | None:
    v = str(form.get(name, "")).strip()
    return v or None


@action
def create_entity(request, conn, me, form):
    key = request.path_params["key"]
    idents = []
    if _opt(form, "id_value"):
        idents.append({"scheme": str(form["id_scheme"]), "value": str(form["id_value"]).strip()})
    e = service.create_entity(conn, me, str(form["kind"]), str(form["display_name"]).strip(), key, idents)
    return _back(request, f"/e/{e['id']}", ok="作成しました")


@action
def rename(request, conn, me, form):
    service.rename_entity(conn, me, request.path_params["id"], str(form["display_name"]))
    return _back(request, fallback=f"/e/{request.path_params['id']}", ok="名前を変更しました")


@action
def add_fact(request, conn, me, form):
    eid = request.path_params.get("id")
    rid = _opt(form, "relationship_id")
    field = str(form["field"])
    value = _parse_value(conn, field, form)
    service.record_fact(conn, me, field, value, str(form.get("epistemic", "observed")),
                        entity_id=None if rid else eid, relationship_id=rid,
                        confidence=float(form.get("confidence") or 1.0), source_id=_opt(form, "source_id"),
                        valid_from=_opt(form, "valid_from"), note=_opt(form, "note"))
    return _back(request, fallback=f"/e/{eid}", ok="記録しました")


@action
def retract(request, conn, me, form):
    service.retract_fact(conn, me, request.path_params["id"], _opt(form, "reason"))
    return _back(request, ok="取り消しました")


@action
def add_identifier(request, conn, me, form):
    service.add_identifier(conn, me, request.path_params["id"], str(form["scheme"]).strip(),
                           str(form["value"]).strip())
    return _back(request, fallback=f"/e/{request.path_params['id']}", ok="識別子を追加しました")


@action
def remove_identifier(request, conn, me, form):
    service.remove_identifier(conn, me, request.path_params["id"], str(form["scheme"]), str(form["value"]))
    return _back(request, fallback=f"/e/{request.path_params['id']}", ok="識別子を削除しました")


@action
def add_relationship(request, conn, me, form):
    me_id, other = request.path_params["id"], str(form["other"])
    src, dst = (other, me_id) if form.get("direction") == "inverse" else (me_id, other)
    r = service.add_relationship(conn, me, str(form["relation_type"]), src, dst, _opt(form, "valid_from"))
    return _back(request, fallback=f"/e/{me_id}",
                 ok="既にある関係です" if r.get("already_exists") else "関係を追加しました")


@action
def end_relationship(request, conn, me, form):
    service.end_relationship(conn, me, request.path_params["id"], str(form["valid_to"]))
    return _back(request, ok="関係を終了にしました")


@action
def merge(request, conn, me, form):
    into = str(form["into"])
    service.merge_entities(conn, me, request.path_params["id"], into)
    return _back(request, f"/e/{into}", ok="統合しました")


@action
def erase(request, conn, me, form):
    eid = request.path_params["id"]
    e = service.get_entity(conn, me, eid)
    if str(form.get("confirm", "")).strip() != e["display_name"]:
        raise service.HreError("確認のため表示名を正確に入力してください")
    service.erase_entity(conn, me, eid, include_linked=form.get("include_linked") == "on")
    return _back(request, f"/w/{e['workspace']}", ok=f"{e['display_name']} を完全に削除しました")


@action
def trust_source(request, conn, me, form):
    service.set_source_trusted(conn, me, request.path_params["id"], form.get("trusted") == "true")
    return _back(request, fallback=f"/s/{request.path_params['id']}", ok="出典の扱いを変更しました")


# ---------------------------------------------------------------------------
# 管理者
# ---------------------------------------------------------------------------

@page
def admin_fields(request, conn, me):
    fields = service.list_all_fields(conn, me)
    return "admin_fields.html", {"fields": fields,
                                 "relation_types": service.list_relation_types(conn)}


@action
def field_status(request, conn, me, form):
    service.set_field_status(conn, me, request.path_params["key"], str(form["status"]), _opt(form, "merged_into"))
    return _back(request, ok="フィールドを更新しました")


@page
def admin_access(request, conn, me):
    return "admin_access.html", {"actors": service.list_actors(conn, me),
                                 "workspaces": service.list_all_workspaces(conn, me),
                                 "new_token": None}


def _admin_only(me: service.Actor) -> None:
    if not me.is_admin:
        raise service.HreError("管理者だけが実行できます")


@action
def admin_post(request, conn, me, form):
    """アクセス管理の操作（CLI の hre-admin と同じ）。新しいトークンはこの画面で1度だけ表示する。"""
    _admin_only(me)
    op = str(form["op"])
    token = None
    try:
        match op:
            case "create_workspace":
                service.create_workspace(conn, str(form["key"]).strip(), str(form["name"]).strip(), str(form["kind"]))
                msg = "ワークスペースを作成しました"
            case "share":
                service.share(conn, str(form["workspace"]), str(form["reader"]))
                msg = "共有しました"
            case "unshare":
                service.unshare(conn, str(form["workspace"]), str(form["reader"]))
                msg = "共有を解除しました"
            case "create_actor":
                _, token = service.create_actor(conn, str(form["name"]).strip(), str(form["kind"]),
                                                form.get("is_admin") == "on")
                msg = f"{form['name']} を作成しました"
            case "rotate":
                token = service.rotate_token(conn, str(form["name"]))
                msg = f"{form['name']} のトークンを再発行しました"
            case "revoke":
                if str(form["name"]) == me.name:
                    raise service.HreError("自分自身は無効にできません")
                service.revoke_actor(conn, str(form["name"]))
                msg = f"{form['name']} を無効にしました"
            case "grant":
                scopes = [s for s in ("read", "write", "sensitive_read") if form.get(s) == "on"]
                if not scopes:
                    service.revoke_grant(conn, str(form["name"]), str(form["workspace"]))
                    msg = "権限を外しました"
                else:
                    service.grant(conn, str(form["name"]), str(form["workspace"]), scopes)
                    msg = "権限を設定しました"
            case _:
                raise service.HreError(f"不明な操作 {op}")
    except errors.IntegrityError as e:   # 同名の actor・ワークスペースなど
        raise service._db_error(e) from e
    if token is None:
        return _back(request, "/admin/access", ok=msg)
    # トークンは URL に載せず、その場で表示する
    ctx = {"actors": service.list_actors(conn, me), "workspaces": service.list_all_workspaces(conn, me),
           "new_token": {"name": str(form["name"]), "token": token}, "flash": msg, "error": None, "me": me,
           "csrf": request.app.state.sessions.csrf(request.cookies[COOKIE])}
    resp = templates.TemplateResponse(request, "admin_access.html", ctx)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# アプリ
# ---------------------------------------------------------------------------

async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if "text/html" in resp.headers.get("content-type", ""):
        resp.headers.setdefault("Cache-Control", "no-store")   # 個人情報を共有端末のキャッシュに残さない
    return resp


def create_app(secret: str | None = None, secure_cookies: bool = True,
               allowed_hosts: list[str] | None = None) -> Starlette:
    routes = [
        Route("/login", login_form, methods=["GET"]),
        Route("/login", login, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/", home),
        Route("/activity", activity),
        Route("/w/{key}", workspace),
        Route("/w/{key}/entities", create_entity, methods=["POST"]),
        Route("/w/{key}/sources", sources),
        Route("/w/{key}/clusters", clusters),
        Route("/e/{id}", entity),
        Route("/e/{id}/rename", rename, methods=["POST"]),
        Route("/e/{id}/facts", add_fact, methods=["POST"]),
        Route("/e/{id}/identifiers", add_identifier, methods=["POST"]),
        Route("/e/{id}/identifiers/remove", remove_identifier, methods=["POST"]),
        Route("/e/{id}/relationships", add_relationship, methods=["POST"]),
        Route("/e/{id}/merge", merge, methods=["POST"]),
        Route("/e/{id}/erase", erase, methods=["POST"]),
        Route("/facts/{id}/retract", retract, methods=["POST"]),
        Route("/r/{id}/end", end_relationship, methods=["POST"]),
        Route("/s/{id}", source),
        Route("/s/{id}/trust", trust_source, methods=["POST"]),
        Route("/admin/fields", admin_fields),
        Route("/admin/fields/{key}", field_status, methods=["POST"]),
        Route("/admin/access", admin_access, methods=["GET"]),
        Route("/admin/access", admin_post, methods=["POST"]),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)] if allowed_hosts else []
    middleware.append(Middleware(BaseHTTPMiddleware, dispatch=security_headers))
    app = Starlette(routes=routes, middleware=middleware)
    app.state.sessions = Sessions(secret or secrets.token_hex(32))
    app.state.secure_cookies = secure_cookies
    return app


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.add_argument("--insecure-cookies", action="store_true",
                   default=os.environ.get("HRE_INSECURE_COOKIES") == "1",
                   help="Cookie に Secure を付けない（http://localhost で試すときだけ。HRE_INSECURE_COOKIES=1 でも同じ）")
    args = p.parse_args()
    secret = os.environ.get("HRE_WEB_SECRET")
    if not secret:
        print("HRE_WEB_SECRET が未設定なので一時的な鍵を使います（再起動するとログインし直しになります）")
    allowed_hosts = [h for h in os.environ.get("HRE_ALLOWED_HOSTS", "").split(",") if h]
    if allowed_hosts:
        allowed_hosts += ["127.0.0.1", "localhost"]
    uvicorn.run(create_app(secret, not args.insecure_cookies, allowed_hosts or None), host=args.host, port=args.port,
                proxy_headers=True)


if __name__ == "__main__":
    main()
