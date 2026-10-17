"""パスキー（WebAuthn）の登録・ログイン・削除を、ソフトウェアの認証器で通しで試す。"""

import base64
import hashlib
import json
import re
import struct
from urllib.parse import urlencode

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient

from hre_server import service, web

ORIGIN = "https://hre.example.com"
RP_ID = "hre.example.com"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    """ES256 の鍵を1つ持つ、attestation = none の認証器（ユーザー確認つき）。"""

    def __init__(self, rp_id: str = RP_ID, origin: str = ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = hashlib.sha256(repr(self.key).encode()).digest()
        self.rp_id, self.origin = rp_id, origin
        self.count = 0
        self.user_handle = b""

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin}).encode()

    def create(self, options: dict) -> dict:
        self.user_handle = unb64u(options["user"]["id"])
        n = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x45]) + struct.pack(">I", self.count)
                     + bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + cose)
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(self._client_data("webauthn.create", options["challenge"])),
                             "attestationObject": b64u(att), "transports": ["internal", "bogus"]},
                "clientExtensionResults": {}}

    def get(self, options: dict) -> dict:
        self.count += 1
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x05]) + struct.pack(">I", self.count)
        client_data = self._client_data("webauthn.get", options["challenge"])
        sig = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.credential_id), "rawId": b64u(self.credential_id), "type": "public-key",
                "response": {"clientDataJSON": b64u(client_data), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig), "userHandle": b64u(self.user_handle)},
                "clientExtensionResults": {}}


@pytest.fixture
def app(database_url, monkeypatch):
    monkeypatch.setenv("HRE_PUBLIC_URL", ORIGIN)
    monkeypatch.delenv("HRE_PASSKEY_RP_ID", raising=False)
    return web.create_app(secret="test-secret", secure_cookies=False)


def _login(app, token: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/login", data={"token": token, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def _csrf(c: TestClient) -> str:
    r = c.get("/account")
    assert r.status_code == 200, r.text
    return re.search(r'name="csrf" content="([^"]+)"', r.text).group(1)


def _register(c: TestClient, auth: SoftAuthenticator, name: str = "iPhone") -> dict:
    csrf = _csrf(c)
    opts = c.post("/account/passkeys/options", json={"csrf": csrf}).json()
    assert opts["options"]["rp"]["id"] == RP_ID
    assert opts["options"]["authenticatorSelection"]["residentKey"] == "required"
    return c.post("/account/passkeys", json={"csrf": csrf, "request": opts["request"],
                                            "credential": auth.create(opts["options"]), "name": name})


def _passkey_login(c: TestClient, auth: SoftAuthenticator, next_: str = "/"):
    opts = c.post("/login/passkey/options").json()
    return c.post("/login/passkey", json={"request": opts["request"], "credential": auth.get(opts["options"]),
                                          "next": next_})


def test_register_login_and_delete(app, conn, world):
    c = _login(app, world.tokens["owner"])
    auth = SoftAuthenticator()
    r = _register(c, auth)
    assert r.status_code == 200, r.text
    page = c.get("/account").text
    assert "iPhone" in page and "まだ使っていません" in page
    # 知らない transports は捨てる
    assert conn.execute("SELECT transports FROM hre.passkeys WHERE actor_id = %s",
                        (world.owner_human.id,)).fetchone()[0] == ["internal"]
    # 同じパスキーは二重に登録できない
    assert _register(c, auth).status_code == 400

    fresh = TestClient(app)
    login_page = fresh.get("/login").text
    assert "data-passkey-login" in login_page and "/static/passkey.js" in login_page
    r = _passkey_login(fresh, auth)
    assert r.status_code == 200 and r.json() == {"redirect": "/"}, r.text
    assert world.owner_human.name in fresh.get("/").text
    assert "まだ使っていません" not in fresh.get("/account").text

    # 削除すると入れなくなる
    pid = conn.execute("SELECT id::text FROM hre.passkeys WHERE actor_id = %s", (world.owner_human.id,)).fetchone()[0]
    r = c.post("/account/passkeys/delete", data={"csrf": _csrf(c), "id": pid}, follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    r = _passkey_login(TestClient(app), auth)
    assert r.status_code == 400 and "登録されていません" in r.json()["error"]


def test_login_returns_to_connect_page(app, world):
    """OAuth の許可画面（/connect）から来たときは、パスキーでログインしたあとそこへ戻る。"""
    c = _login(app, world.tokens["owner"])
    auth = SoftAuthenticator()
    assert _register(c, auth).status_code == 200

    fresh = TestClient(app)
    nxt = "/connect?" + urlencode({"request": "abc"})
    r = fresh.get(nxt, follow_redirects=False)
    assert r.status_code == 303
    login_url = r.headers["location"]
    assert 'data-next="/connect?request=abc"' in fresh.get(login_url).text
    assert _passkey_login(fresh, auth, nxt).json() == {"redirect": nxt}
    assert _passkey_login(fresh, auth, "//evil.example").json() == {"redirect": "/"}


def test_rejects_wrong_origin_replay_and_revoked(app, conn, world):
    c = _login(app, world.tokens["owner"])
    auth = SoftAuthenticator()
    assert _register(c, auth).status_code == 200

    # 別のサイト（フィッシング）で作った署名は通らない
    phish = SoftAuthenticator(origin="https://hre-example.evil")
    phish.key, phish.credential_id, phish.user_handle = auth.key, auth.credential_id, auth.user_handle
    r = _passkey_login(TestClient(app), phish)
    assert r.status_code == 400 and "hre_session" not in r.cookies

    # challenge は1回きり
    fresh = TestClient(app)
    opts = fresh.post("/login/passkey/options").json()
    body = {"request": opts["request"], "credential": auth.get(opts["options"])}
    assert fresh.post("/login/passkey", json=body).status_code == 200
    r = TestClient(app).post("/login/passkey", json=body)
    assert r.status_code == 400 and "時間切れ" in r.json()["error"]

    # トークンを rotate するとセッションは切れるが、パスキーではまた入れる
    service.rotate_token(conn, world.owner_human.name)
    assert fresh.get("/", follow_redirects=False).status_code == 303
    assert _passkey_login(fresh, auth).status_code == 200
    assert fresh.get("/", follow_redirects=False).status_code == 200
    # revoke した人は入れない
    service.revoke_actor(conn, world.owner_human.name)
    assert _passkey_login(TestClient(app), auth).status_code == 400


def test_registration_requires_login_csrf_and_human(app, world):
    anon = TestClient(app)
    assert anon.post("/account/passkeys/options", json={}).status_code == 401
    c = _login(app, world.tokens["owner"])
    assert c.post("/account/passkeys/options", json={"csrf": "x"}).status_code == 403
    # 登録の challenge は、それを出した人のものだけに使える
    other = _login(app, world.tokens["admin"])
    opts = c.post("/account/passkeys/options", json={"csrf": _csrf(c)}).json()
    r = other.post("/account/passkeys", json={"csrf": _csrf(other), "request": opts["request"],
                                              "credential": SoftAuthenticator().create(opts["options"])})
    assert r.status_code == 400
