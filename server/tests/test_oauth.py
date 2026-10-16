"""claude.ai のカスタムコネクタと同じ手順（動的クライアント登録 → 認可 → トークン）で OAuth 接続する。"""

import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from starlette.testclient import TestClient

from hre_server import service, web

CALLBACK = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def base(server_url):
    return server_url.removesuffix("/mcp")


@pytest.fixture
def browser(world):
    """許可画面（Web UI の /connect）を開く人のブラウザ。管理者でログイン済み。"""
    return _browser(world.tokens["admin"])


def _browser(token: str) -> TestClient:
    c = TestClient(web.create_app(secret="test-secret", secure_cookies=False))
    assert c.post("/login", data={"token": token, "next": "/"}, follow_redirects=False).status_code == 303
    return c


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


def _consent(browser: TestClient, consent_url: str, actor_id: str, write: bool = True,
             decision: str = "allow"):
    """許可画面を開いて、身元を選んで許可（か拒否）する。"""
    u = urlsplit(consent_url)
    page = browser.get(f"{u.path}?{u.query}")
    if page.status_code != 200:
        return page
    csrf = re.search(r'name="csrf" (?:value|content)="([^"]+)"', page.text).group(1)
    data = {"csrf": csrf, "request": parse_qs(u.query)["request"][0], "actor": actor_id, "decision": decision}
    if write:
        data["write"] = "on"
    return browser.post("/connect", data=data, follow_redirects=False)


def _token(base: str, **data) -> httpx2.Response:
    return httpx2.post(f"{base}/token", data=data)


def _connect(base: str, browser: TestClient, client_id: str, actor_id: str, client_secret: str | None = None,
             write: bool = True) -> dict:
    login_url, verifier = _authorize(base, client_id)
    r = _consent(browser, login_url, actor_id, write)
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


def test_oauth_connection_acts_as_chosen_agent(base, server_url, world, browser):
    client_id = _register(base)["client_id"]

    # /authorize は Web UI の許可画面に送る。ログインしていなければログイン画面へ（戻り先付き）
    consent_url, _ = _authorize(base, client_id)
    assert urlsplit(consent_url).path == "/connect"
    u = urlsplit(consent_url)
    anon = TestClient(web.create_app(secret="test-secret", secure_cookies=False))
    r = anon.get(f"{u.path}?{u.query}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=%2Fconnect%3Frequest%3D")

    # 許可画面にはクライアント名と戻り先が出て、埋め込みは禁止
    page = browser.get(f"{u.path}?{u.query}")
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
    csp = page.headers["content-security-policy"]
    # 戻り先がさらに別の場所へリダイレクトしても止めないよう、form-action は付けない
    assert "frame-ancestors 'none'" in csp and "form-action" not in csp
    # 管理者はエージェントを身元に選べる
    assert world.agent_a.name in page.text and world.agent_b.name in page.text

    t = _connect(base, browser, client_id, world.agent_a.id)
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

    # 別のエージェントを選べば、そのエージェントとして動く
    tb = _connect(base, browser, client_id, world.agent_b.id)
    assert anyio.run(_workspaces, server_url, tb["access_token"]) == {world.b, world.group}

    # hre_ トークンを直接 Bearer で送る従来の接続も使える
    assert anyio.run(_workspaces, server_url, world.tokens["agent-a"]) == {world.a, world.group}


def test_non_admin_can_only_connect_as_self(base, server_url, world):
    owner = _browser(world.tokens["owner"])
    client_id = _register(base)["client_id"]
    consent_url, _ = _authorize(base, client_id)
    u = urlsplit(consent_url)
    page = owner.get(f"{u.path}?{u.query}")
    assert world.agent_a.name not in page.text
    r = _consent(owner, consent_url, world.agent_a.id)
    assert r.status_code == 400 and "その身元では" in r.text
    # 同じリクエストのまま自分を選べば通る
    r = _consent(owner, consent_url, world.owner_human.id)
    assert r.status_code == 303 and r.headers["location"].startswith(CALLBACK)


def test_read_only_connection_cannot_write(base, server_url, world, browser):
    client_id = _register(base)["client_id"]
    t = _connect(base, browser, client_id, world.agent_a.id, write=False)
    assert anyio.run(_workspaces, server_url, t["access_token"]) == {world.a, world.group}

    async def create() -> bool:
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {t['access_token']}"})
        async with Client(streamable_http_client(server_url, http_client=http)) as c:
            result = await c.call_tool("create_entity", {"kind": "person", "display_name": "読み取り専用",
                                                         "workspace": world.a})
            return result.is_error and "閲覧だけ" in result.content[0].text
    assert anyio.run(create)

    # リフレッシュで write に広げることはできない
    r = _token(base, grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=client_id,
               scope="read write")
    assert r.status_code == 400


def test_deny_returns_access_denied(base, world, browser):
    client_id = _register(base)["client_id"]
    consent_url, _ = _authorize(base, client_id)
    r = _consent(browser, consent_url, world.agent_a.id, decision="deny")
    assert r.status_code == 303
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["st"]


def test_rotating_the_agent_token_cuts_oauth_access(base, server_url, world, conn, browser):
    client_id = _register(base)["client_id"]
    t = _connect(base, browser, client_id, world.agent_a.id)
    assert _accepted(server_url, t["access_token"])

    service.rotate_token(conn, world.agent_a.name)
    assert not _accepted(server_url, t["access_token"])
    r = _token(base, grant_type="refresh_token", refresh_token=t["refresh_token"], client_id=client_id)
    assert r.status_code == 400


def test_connections_page_lists_and_revokes(base, server_url, world, browser):
    client_id = _register(base)["client_id"]
    t = _connect(base, browser, client_id, world.agent_b.id, write=False)
    page = browser.get("/connectors")
    assert page.status_code == 200 and "/mcp" in page.text and "閲覧のみ" in page.text
    family = re.findall(r'name="family" value="([^"]+)"', page.text)
    assert family
    csrf = re.search(r'name="csrf" (?:value|content)="([^"]+)"', page.text).group(1)

    # 関係のない人には見えず、取り消せない
    owner = _browser(world.tokens["owner"])
    assert world.agent_b.name not in owner.get("/connectors").text

    for f in family:
        r = browser.post("/connectors/revoke", data={"csrf": csrf, "family": f}, follow_redirects=False)
        assert r.status_code == 303
    assert not _accepted(server_url, t["access_token"])


def test_revoke_drops_the_whole_grant(base, server_url, world, browser):
    # SDK の /revoke は client_secret を必須にしているので、秘密鍵ありのクライアントで確かめる
    client = _register(base, "client_secret_post")
    client_id = client["client_id"]
    t = _connect(base, browser, client_id, world.agent_b.id, client["client_secret"])
    r = httpx2.post(f"{base}/revoke", data={"token": t["refresh_token"], "client_id": client_id,
                                            "client_secret": client["client_secret"]})
    assert r.status_code == 200, r.text
    assert not _accepted(server_url, t["access_token"])


def test_unknown_or_used_request_is_rejected(base, world, browser):
    client_id = _register(base)["client_id"]
    consent_url, _ = _authorize(base, client_id)
    assert _consent(browser, consent_url, world.agent_a.id).status_code == 303
    assert _consent(browser, consent_url, world.agent_a.id).status_code == 400   # 同じリクエストは2度使えない
    assert browser.get("/connect?request=nope").status_code == 400
