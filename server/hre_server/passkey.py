"""パスキー（WebAuthn）でのログイン。Web UI（hre-web）から使う。

- 登録: ログイン中の人（kind = human）が「ログイン設定」画面で端末のパスキーを登録する
- ログイン: ログイン画面の「パスキーでログイン」。ユーザー名は聞かない（端末に保存されたパスキーを選んでもらう）
- RP ID は HRE_PASSKEY_RP_ID、無ければ HRE_PUBLIC_URL のホスト名、それも無ければアクセスされたホスト名。
  origin は HRE_PUBLIC_URL（無ければアクセスされた URL）。TLS は外部のリバースプロキシで終端する前提なので、
  リクエストの scheme ではなく公開 URL を正とする
- 保存するのは公開鍵だけ。challenge は DB に置き、1回使うと消す
"""

from __future__ import annotations

import json
import os
import secrets
from typing import Any
from urllib.parse import urlsplit

import webauthn
from psycopg import Connection
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, AuthenticatorTransport,
                                      PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

from . import service

CHALLENGE_SECONDS = 5 * 60
RP_NAME = "HRE 人間関係"
TRANSPORTS = {t.value for t in AuthenticatorTransport}


class PasskeyError(service.HreError):
    pass


def relying_party(request_url: str) -> tuple[str, str]:
    """(RP ID, origin)。request_url はアクセスされた URL（HRE_PUBLIC_URL が無いときだけ使う）。"""
    public = urlsplit(os.environ.get("HRE_PUBLIC_URL") or request_url)
    origin = f"{public.scheme}://{public.netloc}"
    return os.environ.get("HRE_PASSKEY_RP_ID") or public.hostname or "", origin


def _new_challenge(conn: Connection, purpose: str, challenge: bytes, actor_id: str | None = None) -> str:
    conn.execute("DELETE FROM hre.passkey_challenges WHERE expires_at < now()")
    request_id = secrets.token_urlsafe(24)
    conn.execute("""INSERT INTO hre.passkey_challenges (id_hash, purpose, actor_id, challenge, expires_at)
                    VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s))""",
                 (service.hash_token(request_id), purpose, actor_id, challenge, CHALLENGE_SECONDS))
    return request_id


def _take_challenge(conn: Connection, purpose: str, request_id: str, actor_id: str | None = None) -> bytes:
    row = service._one(conn, """DELETE FROM hre.passkey_challenges
                                 WHERE id_hash = %s AND purpose = %s AND expires_at > now()
                                   AND actor_id IS NOT DISTINCT FROM %s
                             RETURNING challenge""",
                       (service.hash_token(request_id), purpose, actor_id))
    if row is None:
        raise PasskeyError("時間切れです。もう一度やり直してください")
    return bytes(row["challenge"])


def _descriptors(conn: Connection, actor_id: str) -> list[PublicKeyCredentialDescriptor]:
    rows = service._q(conn, "SELECT credential_id, transports FROM hre.passkeys WHERE actor_id = %s", (actor_id,))
    return [PublicKeyCredentialDescriptor(id=bytes(r["credential_id"]),
                                          transports=[AuthenticatorTransport(t) for t in r["transports"]])
            for r in rows]


# ---------------------------------------------------------------------------
# 登録（ログイン中の人）
# ---------------------------------------------------------------------------

def registration_options(conn: Connection, me: service.Actor, rp: tuple[str, str]) -> dict:
    if me.kind != "human":
        raise PasskeyError("パスキーは人間の actor だけが登録できます")
    rp_id, _ = rp
    options = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_id=me.id.encode(), user_name=me.name, user_display_name=me.name,
        exclude_credentials=_descriptors(conn, me.id),   # 同じ端末の二重登録を防ぐ
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,   # ユーザー名なしでログインできるように
            user_verification=UserVerificationRequirement.REQUIRED))
    request_id = _new_challenge(conn, "register", options.challenge, me.id)
    return {"request": request_id, "options": json.loads(webauthn.options_to_json(options))}


def register(conn: Connection, me: service.Actor, rp: tuple[str, str], request_id: str,
             credential: dict[str, Any], name: str) -> dict:
    challenge = _take_challenge(conn, "register", request_id, me.id)
    rp_id, origin = rp
    try:
        v = webauthn.verify_registration_response(credential=credential, expected_challenge=challenge,
                                                  expected_rp_id=rp_id, expected_origin=origin,
                                                  require_user_verification=True)
    except WebAuthnException as e:
        raise PasskeyError(f"パスキーを確認できませんでした: {e}") from e
    transports = [t for t in (credential.get("response") or {}).get("transports") or [] if t in TRANSPORTS]
    name = name.strip()[:60] or "パスキー"
    row = service._one(conn, """INSERT INTO hre.passkeys (actor_id, credential_id, public_key, sign_count, transports, name)
                                VALUES (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT (credential_id) DO NOTHING RETURNING id::text""",
                       (me.id, v.credential_id, v.credential_public_key, v.sign_count, transports, name))
    if row is None:
        raise PasskeyError("このパスキーはすでに登録されています")
    return {"id": row["id"], "name": name}


def list_passkeys(conn: Connection, me: service.Actor) -> list[dict]:
    return service._q(conn, """SELECT id::text, name, created_at, last_used_at FROM hre.passkeys
                                WHERE actor_id = %s ORDER BY created_at""", (me.id,))


def delete_passkey(conn: Connection, me: service.Actor, passkey_id: str) -> None:
    row = service._one(conn, "DELETE FROM hre.passkeys WHERE id = %s AND actor_id = %s RETURNING id",
                       (passkey_id, me.id))
    if row is None:
        raise PasskeyError("パスキーが見つかりません")


# ---------------------------------------------------------------------------
# ログイン（未ログイン）
# ---------------------------------------------------------------------------

def login_options(conn: Connection, rp: tuple[str, str]) -> dict:
    rp_id, _ = rp
    options = webauthn.generate_authentication_options(rp_id=rp_id,
                                                       user_verification=UserVerificationRequirement.REQUIRED)
    request_id = _new_challenge(conn, "login", options.challenge)
    return {"request": request_id, "options": json.loads(webauthn.options_to_json(options))}


def login(conn: Connection, rp: tuple[str, str], request_id: str, credential: dict[str, Any]) -> tuple[str, str]:
    """検証できたら (actor_id, その actor の今の token_hash) を返す。セッションはこの token_hash に結びつける。"""
    challenge = _take_challenge(conn, "login", request_id)
    raw_id = str(credential.get("rawId") or credential.get("id") or "")
    try:
        credential_id = webauthn.base64url_to_bytes(raw_id)
    except ValueError as e:
        raise PasskeyError("パスキーの形式が正しくありません") from e
    row = service._one(conn, """SELECT p.id::text, p.public_key, p.sign_count, a.id::text AS actor_id, a.token_hash
                                  FROM hre.passkeys p JOIN hre.actors a ON a.id = p.actor_id
                                 WHERE p.credential_id = %s AND a.kind = 'human' AND a.revoked_at IS NULL""",
                       (credential_id,))
    if row is None:
        raise PasskeyError("このパスキーは登録されていません（削除済みか、別のサーバーのものです）")
    rp_id, origin = rp
    try:
        v = webauthn.verify_authentication_response(credential=credential, expected_challenge=challenge,
                                                    expected_rp_id=rp_id, expected_origin=origin,
                                                    credential_public_key=bytes(row["public_key"]),
                                                    credential_current_sign_count=row["sign_count"],
                                                    require_user_verification=True)
    except WebAuthnException as e:
        raise PasskeyError(f"パスキーを確認できませんでした: {e}") from e
    conn.execute("UPDATE hre.passkeys SET sign_count = %s, last_used_at = now() WHERE id = %s",
                 (v.new_sign_count, row["id"]))
    return row["actor_id"], row["token_hash"]

