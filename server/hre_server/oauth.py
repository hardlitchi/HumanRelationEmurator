"""MCP サーバーの OAuth 認可サーバー。claude.ai のカスタムコネクタのように OAuth でしか接続できないクライアント向け。

流れ:
  1. クライアントが /register で自分を登録し（動的クライアント登録）、/authorize に人を送る
  2. /oauth/login で人が hre_ トークンを入力する。そのトークンの持ち主（actor）として認可する
  3. クライアントは認可コードを /token でアクセストークン（hrea_）とリフレッシュトークン（hrer_）に換える

発行したものはすべて入力された hre_ トークンに結び付く。そのトークンを rotate / revoke すると、
OAuth で発行したアクセストークンもリフレッシュトークンも使えなくなる。hre_ トークンを直接
Bearer で送る従来の接続（Claude Code など）もそのまま使える。
"""

from __future__ import annotations

import html
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from . import service
from .db import pool

DDL = Path(__file__).resolve().parents[2] / "db" / "oauth.sql"
LOGIN_PATH = "/oauth/login"

REQUEST_SECONDS = 10 * 60
CODE_SECONDS = 5 * 60
ACCESS_SECONDS = 60 * 60
REFRESH_SECONDS = 90 * 24 * 60 * 60


def apply_schema(conn: Connection) -> None:
    conn.execute(DDL.read_text())


def _row(conn: Connection, sql: str, params: Any) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchone()


def _db(fn, *args):
    def run():
        with pool().connection() as conn, conn.transaction():
            return fn(conn, *args)
    return anyio.to_thread.run_sync(run)


# 有効な grant だけを返す。発行元の hre_ トークンが rotate / revoke されていれば返さない
_GRANT = """SELECT g.client_id, g.actor_id::text, g.family, g.params, extract(epoch FROM g.expires_at)::bigint AS exp
              FROM hre.oauth_grants g JOIN hre.actors a ON a.id = g.actor_id
             WHERE g.token_hash = %s AND g.kind = %s AND g.expires_at > now()
               AND a.token_hash = g.actor_token_hash AND a.revoked_at IS NULL"""


def _issue(conn: Connection, kind: str, client_id: str, actor_id: str, actor_token_hash: str, family: str,
           params: dict, seconds: int, prefix: str) -> tuple[str, int]:
    token = prefix + secrets.token_urlsafe(32)
    expires_at = int(time.time()) + seconds
    conn.execute("""INSERT INTO hre.oauth_grants
                        (token_hash, kind, client_id, actor_id, actor_token_hash, family, params, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, to_timestamp(%s))""",
                 (service.hash_token(token), kind, client_id, actor_id, actor_token_hash, family,
                  Jsonb(params), expires_at))
    return token, expires_at


def _issue_tokens(conn: Connection, client_id: str, actor_id: str, family: str, scopes: list[str],
                  resource: str | None) -> OAuthToken:
    actor_token_hash = _row(conn, "SELECT token_hash FROM hre.actors WHERE id = %s", (actor_id,))["token_hash"]
    params = {"scopes": scopes, "resource": resource}
    access, _ = _issue(conn, "access", client_id, actor_id, actor_token_hash, family, params, ACCESS_SECONDS, "hrea_")
    refresh, _ = _issue(conn, "refresh", client_id, actor_id, actor_token_hash, family, params, REFRESH_SECONDS,
                        "hrer_")
    return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_SECONDS,
                      scope=" ".join(scopes) or None, refresh_token=refresh)


class HreOAuthProvider:
    """mcp SDK の OAuthAuthorizationServerProvider。状態はすべて DB に置く（再起動しても接続が切れない）。"""

    def __init__(self, issuer_url: str):
        self.login_url = issuer_url.rstrip("/") + LOGIN_PATH

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = await _db(lambda c: _row(c, "SELECT info FROM hre.oauth_clients WHERE client_id = %s", (client_id,)))
        return OAuthClientInformationFull.model_validate(row["info"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        info = client_info.model_dump(mode="json", exclude_none=True)
        await _db(lambda c: c.execute("INSERT INTO hre.oauth_clients (client_id, info) VALUES (%s, %s)",
                                      (client_info.client_id, Jsonb(info))))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(32)

        def save(conn: Connection) -> None:
            conn.execute("DELETE FROM hre.oauth_requests WHERE expires_at < now()")
            conn.execute("""INSERT INTO hre.oauth_requests (id_hash, client_id, params, expires_at)
                            VALUES (%s, %s, %s, now() + make_interval(secs => %s))""",
                         (service.hash_token(request_id), client.client_id,
                          Jsonb(params.model_dump(mode="json")), REQUEST_SECONDS))

        await _db(save)
        return construct_redirect_uri(self.login_url, request=request_id)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        row = await _db(lambda c: _row(c, _GRANT, (service.hash_token(authorization_code), "code")))
        if row is None or row["client_id"] != client.client_id:
            return None
        p = row["params"]
        return AuthorizationCode(code=authorization_code, scopes=p.get("scopes") or [], expires_at=row["exp"],
                                 client_id=row["client_id"], code_challenge=p["code_challenge"],
                                 redirect_uri=p["redirect_uri"],
                                 redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                                 resource=p.get("resource"), subject=row["actor_id"])

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        def run(conn: Connection) -> OAuthToken:
            # コードは1回だけ使える。消せなかった（同時に使われた）なら失敗させる
            row = _row(conn, """DELETE FROM hre.oauth_grants WHERE token_hash = %s AND kind = 'code'
                                RETURNING family""", (service.hash_token(authorization_code.code),))
            if row is None:
                raise TokenError("invalid_grant", "authorization code already used")
            return _issue_tokens(conn, client.client_id, authorization_code.subject, row["family"],
                                 authorization_code.scopes, authorization_code.resource)

        return await _db(run)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = await _db(lambda c: _row(c, _GRANT, (service.hash_token(refresh_token), "refresh")))
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["params"].get("scopes") or [],
                            expires_at=row["exp"], resource=row["params"].get("resource"), subject=row["actor_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        def run(conn: Connection) -> OAuthToken:
            # リフレッシュトークンは使うたびに取り替える
            row = _row(conn, """DELETE FROM hre.oauth_grants WHERE token_hash = %s AND kind = 'refresh'
                                RETURNING family""", (service.hash_token(refresh_token.token),))
            if row is None:
                raise TokenError("invalid_grant", "refresh token already used")
            return _issue_tokens(conn, client.client_id, refresh_token.subject, row["family"], scopes,
                                 refresh_token.resource)

        return await _db(run)

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith("hre_"):
            # 従来どおり hre_ トークンを直接 Bearer で送る接続
            def check(conn: Connection) -> str | None:
                actor = service.authenticate(conn, token)
                return actor.id if actor else None

            actor_id = await _db(check)
            return AccessToken(token=token, client_id=actor_id, subject=actor_id, scopes=[]) if actor_id else None
        row = await _db(lambda c: _row(c, _GRANT, (service.hash_token(token), "access")))
        if row is None:
            return None
        return AccessToken(token=token, client_id=row["client_id"], subject=row["actor_id"],
                           scopes=row["params"].get("scopes") or [], expires_at=row["exp"],
                           resource=row["params"].get("resource"))

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # 同じ認可から出たトークンはまとめて取り消す
        def run(conn: Connection) -> None:
            conn.execute("""DELETE FROM hre.oauth_grants WHERE family IN (
                                SELECT family FROM hre.oauth_grants WHERE token_hash = %s)""",
                         (service.hash_token(token.token),))

        await _db(run)

    # --- /oauth/login: 人が hre_ トークンを入力して認可する画面 -----------------------------------

    async def login(self, request: Request) -> Response:
        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            pending = await _db(_load_request, request_id)
            if pending is None:
                return _page("この認可リクエストは期限切れか無効です。claude.ai からもう一度接続し直してください。",
                             status=400)
            return _login_page(request_id, pending)

        form = await request.form()
        request_id, token = str(form.get("request", "")), str(form.get("token", "")).strip()

        def run(conn: Connection) -> tuple[str | None, dict | None]:
            pending = _load_request(conn, request_id)
            if pending is None:
                return None, None
            actor = service.authenticate(conn, token)
            if actor is None:
                return None, pending
            conn.execute("DELETE FROM hre.oauth_requests WHERE id_hash = %s", (service.hash_token(request_id),))
            p = pending["params"]
            code, _ = _issue(conn, "code", pending["client_id"], actor.id, service.hash_token(token),
                             secrets.token_urlsafe(16), p, CODE_SECONDS, "hrec_")
            return construct_redirect_uri(p["redirect_uri"], code=code, state=p.get("state")), pending

        redirect, pending = await _db(run)
        if pending is None:
            return _page("この認可リクエストは期限切れか無効です。claude.ai からもう一度接続し直してください。",
                         status=400)
        if redirect is None:
            return _login_page(request_id, pending, error="トークンが正しくないか、無効にされています。")
        return RedirectResponse(redirect, status_code=303)


def _load_request(conn: Connection, request_id: str) -> dict | None:
    if not request_id:
        return None
    return _row(conn, """SELECT r.client_id, r.params, c.info FROM hre.oauth_requests r
                           JOIN hre.oauth_clients c USING (client_id)
                          WHERE r.id_hash = %s AND r.expires_at > now()""", (service.hash_token(request_id),))


_STYLE = """body{font-family:system-ui,sans-serif;max-width:28rem;margin:2rem auto;padding:0 1rem;line-height:1.6}
input[type=password]{width:100%;box-sizing:border-box;padding:.6rem;font-size:1rem}
button{margin-top:1rem;padding:.6rem 1.2rem;font-size:1rem}.err{color:#b00020}.muted{color:#666;font-size:.9rem}"""


def _page(body: str, status: int = 200) -> HTMLResponse:
    doc = (f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
           f'<meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>HumanRelationEmurator への接続</title><style>{_STYLE}</style></head><body>{body}</body></html>')
    # 別サイトの枠に埋め込ませない（入力を盗むクリックジャッキング対策）
    return HTMLResponse(doc, status_code=status, headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
        "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


def _login_page(request_id: str, pending: dict, error: str | None = None) -> HTMLResponse:
    e = html.escape
    name = pending["info"].get("client_name") or pending["client_id"]
    host = urlsplit(pending["params"]["redirect_uri"]).netloc
    err = f'<p class="err">{e(error)}</p>' if error else ""
    return _page(f"""<h1>HumanRelationEmurator への接続</h1>
<p><b>{e(name)}</b>（戻り先: {e(host)}）が、このサーバーの人間関係データへの接続を求めています。</p>
<p>接続に使う <code>hre_</code> トークンを入力してください。このトークンの持ち主の権限で読み書きされます。</p>
{err}<form method="post">
<input type="hidden" name="request" value="{e(request_id)}">
<input type="password" name="token" placeholder="hre_..." autocomplete="off" required autofocus>
<button type="submit">接続を許可する</button></form>
<p class="muted">心当たりがなければこのページを閉じてください。許可を取り消すには、そのトークンを再発行か無効化します。</p>""")
