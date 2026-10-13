"""claude.ai のカスタムコネクタと同じ手順（動的クライアント登録 → 認可 → トークン）で OAuth 接続する。"""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from hre_server import service

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def base(server_url):
    return server_url.removesuffix("/mcp")


def _register(base: str, auth_method: str = "none") -> dict:
    r = httpx2.post(f"{base}/register", json={
        "redirect_uris": [CALLBACK], "client_name": "Claude", "token_endpoint_auth_method": auth_method,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
    assert r.status_code == 201, r.text
    return r.json()


def _authorize(base: str, client_id: str) -> tuple[str, str]:
    """/authorize を叩いて、ログイン画面の URL と PKCE の verifier を返す。"""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = httpx2.get(f"{base}/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "st",
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": f"{base}/mcp"})
    assert r.status_code == 302, r.text
    return r.headers["location"], verifier


def _login(login_url: str, token: str) -> httpx2.Response:
    request_id = parse_qs(urlsplit(login_url).query)["request"][0]
    return httpx2.post(login_url.split("?")[0], data={"request": request_id, "token": token})


def _token(base: str, **data) -> httpx2.Response:
    return httpx2.post(f"{base}/token", data=data)


def _connect(base: str, client_id: str, token: str, client_secret: str | None = None) -> dict:
    login_url, verifier = _authorize(base, client_id)
    r = _login(login_url, token)
    assert r.status_code == 303, r.text
    loc = urlsplit(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == CALLBACK
    q = parse_qs(loc.query)
    assert q["state"] == ["st"]
    r = _token(base, grant_type="authorization_code", code=q["code"][0], redirect_uri=CALLBACK,
               client_id=client_id, code_verifier=verifier,
               **({"client_secret": client_secret} if client_secret else {}))
    assert r.status_code == 200, r.text
    return {**r.json(), "code": q["code"][0], "verifier": verifier}


async def _workspaces(url: str, token: str) -> set[str]:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    async with Client(streamable_http_client(url, http_client=http)) as c:
        result = await c.call_tool("list_workspaces", {})
        assert not result.is_error, result.content
        return {w["key"] for w in json.loads(result.content[0].text)["workspaces"]}


def _accepted(url: str, token: str) -> bool:
    """トークンが通るか（通れば初期化前なので 400、通らなければ 401）。"""
    return httpx2.post(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                     "Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code != 401


def test_discovery_points_to_this_server(base, server_url):
    r = httpx2.post(server_url, json={})
    assert r.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]
    prm = httpx2.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == server_url
    asm = httpx2.get(f"{base}/.well-known/oauth-authorization-server").json()
    assert asm["issuer"].rstrip("/") == prm["authorization_servers"][0].rstrip("/") == base
    assert asm["registration_endpoint"] == f"{base}/register"
    assert asm["code_challenge_methods_supported"] == ["S256"]


def test_oauth_connection_acts_as_token_owner(base, server_url, world):
    client_id = _register(base)["client_id"]

    # ログイン画面にはクライアント名と戻り先が出て、埋め込みは禁止
    login_url, _ = _authorize(base, client_id)
    page = httpx2.get(login_url)
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    # 間違ったトークンでは認可されない
    r = _login(login_url, "hre_wrong")
    assert r.status_code == 200 and "トークンが正しくない" in r.text

    t = _connect(base, client_id, world.tokens["agent-a"])
    assert t["access_token"].startswith("hrea_") and t["refresh_token"].startswith("hrer_")
    assert anyio.run(_workspaces, server_url, t["access_token"]) == {world.a, world.group}

    # 認可コードは1回だけ
    r = _token(base, grant_type="authorization_code", code=t["code"], redirect_uri=CALLBACK,
               client_id=client_id, code_verifier=t["verifier"])
    assert r.status_code == 400

    # リフレッシュすると新しい組が出て、古いリフレッシュトークンは使えない
    r = _token(base, grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=client_id)
    assert r.status_code == 200, r.text
    t2 = r.json()
    assert anyio.run(_workspaces, server_url, t2["access_token"]) == {world.a, world.group}
    r = _token(base, grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=client_id)
    assert r.status_code == 400

    # 別のエージェントのトークンで認可すれば、そのエージェントとして動く
    tb = _connect(base, client_id, world.tokens["agent-b"])
    assert anyio.run(_workspaces, server_url, tb["access_token"]) == {world.b, world.group}

    # hre_ トークンを直接 Bearer で送る従来の接続も使える
    assert anyio.run(_workspaces, server_url, world.tokens["agent-a"]) == {world.a, world.group}


def test_rotating_the_hre_token_cuts_oauth_access(base, server_url, world, conn):
    client_id = _register(base)["client_id"]
    t = _connect(base, client_id, world.tokens["agent-a"])
    assert _accepted(server_url, t["access_token"])

    service.rotate_token(conn, world.agent_a.name)
    assert not _accepted(server_url, t["access_token"])
    r = _token(base, grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=client_id)
    assert r.status_code == 400


def test_revoke_drops_the_whole_grant(base, server_url, world):
    # SDK の /revoke は client_secret を必須にしているので、秘密鍵ありのクライアントで確かめる
    client = _register(base, "client_secret_post")
    client_id = client["client_id"]
    t = _connect(base, client_id, world.tokens["agent-b"], client["client_secret"])
    r = httpx2.post(f"{base}/revoke", data={"token": t["refresh_token"], "client_id": client_id,
                                            "client_secret": client["client_secret"]})
    assert r.status_code == 200, r.text
    assert not _accepted(server_url, t["access_token"])


def test_unknown_or_used_request_is_rejected(base, world):
    client_id = _register(base)["client_id"]
    login_url, _ = _authorize(base, client_id)
    assert _login(login_url, world.tokens["agent-a"]).status_code == 303
    assert _login(login_url, world.tokens["agent-a"]).status_code == 400   # 同じリクエストは2度使えない
    assert httpx2.get(f"{base}/oauth/login?request=nope").status_code == 400
