"""MCP サーバーの OAuth 認可サーバー。claude.ai・ChatGPT・Claude Code など OAuth で接続するクライアント向け。

流れ（SubscriptionManager と同じ「URL を登録 → ログイン → 許可するだけ」）:
  1. クライアントが /register で自分を登録し（動的クライアント登録）、/authorize に人を送る
  2. Web UI の /connect（web.py）に転送される。Web UI にログインしている人が、
     どの身元（自分か、管理者なら各エージェント）で・書き込みも許すかを選んで「許可する」
  3. クライアントは認可コードを /token でアクセストークン（hrea_）とリフレッシュトークン（hrer_）に換える

発行したものは選んだ actor の、許可した時点のトークン（actors.token_hash）に結び付く。その actor の
トークンを rotate / revoke すると OAuth の接続も切れる。個々の接続は Web UI の「AI 連携」画面で取り消せる。
hre_ トークンを直接 Bearer で送る従来の接続（OAuth に対応していないツール）もそのまま使える。
"""

from __future__ import annotations

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
from . import service
from .db import pool

DDL = Path(__file__).resolve().parents[2] / "db" / "oauth.sql"
CONSENT_PATH = "/connect"   # Web UI 側の許可画面

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


# read だけなら閲覧のみ、write があれば登録・更新も。hre_ を直接送る接続と、この区別の前に出したトークンは scopes が空 = 全部
SCOPES = {"read": "人間関係データの閲覧", "write": "人・関係・主張・やり取りの記録と更新"}


def read_only(scopes: list[str] | None) -> bool:
    return bool(scopes) and "write" not in scopes


# 有効な grant だけを返す。発行元の hre_ トークンが rotate / revoke されていれば返さない
_GRANT = """SELECT g.client_id, g.actor_id::text, g.family, g.params, extract(epoch FROM g.expires_at)::bigint AS exp
              FROM hre.oauth_grants g JOIN hre.actors a ON a.id = g.actor_id
             WHERE g.token_hash = %s AND g.kind = %s AND g.expires_at > now()
               AND a.token_hash = g.actor_token_hash AND a.revoked_at IS NULL"""


def _issue(conn: Connection, kind: str, client_id: str, actor_id: str, actor_token_hash: str, family: str,
           params: dict, seconds: int, prefix: str, approved_by: str | None) -> tuple[str, int]:
    token = prefix + secrets.token_urlsafe(32)
    expires_at = int(time.time()) + seconds
    conn.execute("""INSERT INTO hre.oauth_grants
                        (token_hash, kind, client_id, actor_id, actor_token_hash, family, params, expires_at,
                         approved_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, to_timestamp(%s), %s)""",
                 (service.hash_token(token), kind, client_id, actor_id, actor_token_hash, family,
                  Jsonb(params), expires_at, approved_by))
    return token, expires_at


def _issue_tokens(conn: Connection, client_id: str, actor_id: str, family: str, scopes: list[str],
                  resource: str | None, approved_by: str | None) -> OAuthToken:
    # 発行元の token_hash は認可コードのものを引き継ぐ（その間に rotate されていれば _GRANT で弾かれている）
    actor_token_hash = _row(conn, "SELECT token_hash FROM hre.actors WHERE id = %s", (actor_id,))["token_hash"]
    params = {"scopes": scopes, "resource": resource}
    access, _ = _issue(conn, "access", client_id, actor_id, actor_token_hash, family, params, ACCESS_SECONDS, "hrea_",
                       approved_by)
    refresh, _ = _issue(conn, "refresh", client_id, actor_id, actor_token_hash, family, params, REFRESH_SECONDS,
                        "hrer_", approved_by)
    return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_SECONDS,
                      scope=" ".join(scopes) or None, refresh_token=refresh)


class HreOAuthProvider:
    """mcp SDK の OAuthAuthorizationServerProvider。状態はすべて DB に置く（再起動しても接続が切れない）。"""

    def __init__(self, issuer_url: str):
        self.consent_url = issuer_url.rstrip("/") + CONSENT_PATH

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
        return construct_redirect_uri(self.consent_url, request=request_id)

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
                                RETURNING family, approved_by::text""", (service.hash_token(authorization_code.code),))
            if row is None:
                raise TokenError("invalid_grant", "authorization code already used")
            return _issue_tokens(conn, client.client_id, authorization_code.subject, row["family"],
                                 authorization_code.scopes, authorization_code.resource, row["approved_by"])

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
                                RETURNING family, approved_by::text""", (service.hash_token(refresh_token.token),))
            if row is None:
                raise TokenError("invalid_grant", "refresh token already used")
            # 許可したときより広い範囲にはリフレッシュで広げさせない
            return _issue_tokens(conn, client.client_id, refresh_token.subject, row["family"],
                                 [x for x in scopes if x in refresh_token.scopes] if refresh_token.scopes else scopes,
                                 refresh_token.resource, row["approved_by"])

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


# --- Web UI の /connect（許可画面）から使う ------------------------------------------------------

def load_request(conn: Connection, request_id: str) -> dict | None:
    """許可を待っている認可リクエスト（client_id・params・client_name・戻り先）。期限切れ・不明なら None。"""
    if not request_id:
        return None
    row = _row(conn, """SELECT r.client_id, r.params, c.info FROM hre.oauth_requests r
                          JOIN hre.oauth_clients c USING (client_id)
                         WHERE r.id_hash = %s AND r.expires_at > now()""", (service.hash_token(request_id),))
    if row:
        row["client_name"] = row["info"].get("client_name") or row["client_id"]
    return row


def connectable_actors(conn: Connection, me: service.Actor) -> list[dict]:
    """この人が接続先の身元に選べる actor。管理者は自分と有効なエージェント全員、それ以外は自分だけ。"""
    if not me.is_admin:
        return [{"id": me.id, "name": me.name, "kind": me.kind}]
    with conn.cursor(row_factory=dict_row) as cur:
        agents = cur.execute("""SELECT id::text, name, kind FROM hre.actors
                                 WHERE kind = 'agent' AND revoked_at IS NULL ORDER BY name""").fetchall()
    return agents + [{"id": me.id, "name": me.name, "kind": me.kind}]


def approve(conn: Connection, me: service.Actor, request_id: str, actor_id: str, scopes: list[str]) -> str:
    """認可コードを出し、クライアントの戻り先 URL（code と state 付き）を返す。リクエストは1度だけ使える。"""
    pending = load_request(conn, request_id)
    if pending is None:
        raise service.HreError("この接続リクエストは期限切れか使用済みです。接続をやり直してください")
    if actor_id not in {a["id"] for a in connectable_actors(conn, me)}:
        raise service.HreError("その身元では接続を許可できません")
    actor = _row(conn, "SELECT token_hash FROM hre.actors WHERE id = %s AND revoked_at IS NULL", (actor_id,))
    if actor is None:
        raise service.HreError("その身元は無効にされています")
    conn.execute("DELETE FROM hre.oauth_requests WHERE id_hash = %s", (service.hash_token(request_id),))
    p = {**pending["params"], "scopes": [s for s in SCOPES if s in scopes] or ["read"]}
    code, _ = _issue(conn, "code", pending["client_id"], actor_id, actor["token_hash"], secrets.token_urlsafe(16),
                     p, CODE_SECONDS, "hrec_", me.id)
    return construct_redirect_uri(p["redirect_uri"], code=code, state=p.get("state"))


def deny(conn: Connection, request_id: str) -> str | None:
    """拒否: リクエストを消して、クライアントへ access_denied を返す URL。"""
    pending = load_request(conn, request_id)
    if pending is None:
        return None
    conn.execute("DELETE FROM hre.oauth_requests WHERE id_hash = %s", (service.hash_token(request_id),))
    p = pending["params"]
    return construct_redirect_uri(p["redirect_uri"], error="access_denied", state=p.get("state"))


def list_connections(conn: Connection, me: service.Actor) -> list[dict]:
    """有効な OAuth 接続（1回の許可ごと）。管理者は全部、それ以外は自分の身元か自分が許可したもの。"""
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("""
            SELECT DISTINCT ON (g.family) g.family, g.params->'scopes' AS scopes, g.created_at AS refreshed_at,
                   coalesce(c.info->>'client_name', g.client_id) AS client_name,
                   a.name AS actor_name, a.kind AS actor_kind, b.name AS approved_by
              FROM hre.oauth_grants g
              JOIN hre.oauth_clients c USING (client_id)
              JOIN hre.actors a ON a.id = g.actor_id
              LEFT JOIN hre.actors b ON b.id = g.approved_by
             WHERE g.kind = 'refresh' AND g.expires_at > now()
               AND a.token_hash = g.actor_token_hash AND a.revoked_at IS NULL
               AND (%(admin)s OR g.actor_id = %(me)s OR g.approved_by = %(me)s)
             ORDER BY g.family, g.created_at DESC""", {"admin": me.is_admin, "me": me.id}).fetchall()


def revoke_connection(conn: Connection, me: service.Actor, family: str) -> None:
    """1つの接続を切る（その許可から出たアクセストークン・リフレッシュトークンをすべて消す）。"""
    row = _row(conn, """DELETE FROM hre.oauth_grants
                         WHERE family = %(f)s AND (%(admin)s OR actor_id = %(me)s OR approved_by = %(me)s)
                        RETURNING family""", {"f": family, "admin": me.is_admin, "me": me.id})
    if row is None:
        raise service.HreError("その接続は見つかりません")
