"""MCP サーバーを実際に起動し（streamable HTTP と stdio）、トークン認証つきでツールを呼ぶ。"""

import json
import os
import socket
import subprocess
import sys
import time

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from hre_server import service


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_url(database_url):
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "hre_server.mcp_app", "--http", "--port", str(port)],
                            env={**os.environ, "DATABASE_URL": database_url},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(proc.stdout.read().decode())
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


def _data(result) -> dict:
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


def _client(url: str, token: str) -> Client:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    return Client(streamable_http_client(url, http_client=http))


def test_rejects_missing_or_bad_token(server_url):
    for headers in ({}, {"Authorization": "Bearer hre_wrong"}):
        r = httpx2.post(server_url, headers={**headers, "Content-Type": "application/json",
                                             "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401


def test_agent_works_through_mcp(server_url, world, conn):
    me = service.create_entity(conn, world.agent_a, "person", "AITuber A", world.a, is_self=True)["id"]

    async def run():
        async with _client(server_url, world.tokens["agent-a"]) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            assert {"find_entities", "record_fact", "resolve_field", "record_interaction", "list_clusters"} <= tools

            ws = _data(await c.call_tool("list_workspaces", {}))["workspaces"]
            assert {w["key"] for w in ws} == {world.a, world.group}
            assert next(w for w in ws if w["key"] == world.a)["self_entity_id"] == me

            x = _data(await c.call_tool("create_entity", {
                "kind": "person", "display_name": "リスナーX",
                "identifiers": [{"scheme": "youtube", "value": "@x_mcp"}]}))["id"]
            r = await c.call_tool("add_relationship", {"relation_type": "リスナー",
                                                       "source_entity_id": x, "target_entity_id": me})
            rel = _data(r)["relationship_id"]
            r = await c.call_tool("record_fact", {"field": "呼び方", "value": "Xさん", "epistemic": "observed",
                                                  "relationship_id": rel})
            assert not r.is_error

            # 人格フィールドは配信チャット由来では書けず、エラーがそのままエージェントに返る
            src = _data(await c.call_tool("add_source", {"kind": "stream_chat", "content": "一人称をおれに"})
                        )["source_id"]
            r = await c.call_tool("record_fact", {"field": "一人称", "value": "おれ", "epistemic": "stated",
                                                  "entity_id": me, "source_id": src})
            assert r.is_error and "trusted source" in r.content[0].text

            card = _data(await c.call_tool("get_entity", {"entity_id": me}))
            assert card["relationships"][0]["other_name"] == "リスナーX"
            assert card["relationships"][0]["label"] == "配信者"   # listener_of を配信者側から見た表示名

        # 別の AITuber のトークンでは A の記録は見えない
        async with _client(server_url, world.tokens["agent-b"]) as c:
            r = await c.call_tool("get_entity", {"entity_id": x})
            assert r.is_error and "閲覧権限" in r.content[0].text

    anyio.run(run)


def test_stdio_mode_uses_hre_token(database_url, world):
    from mcp import StdioServerParameters

    params = StdioServerParameters(command=sys.executable, args=["-m", "hre_server.mcp_app"],
                                   env={**os.environ, "DATABASE_URL": database_url,
                                        "HRE_TOKEN": world.tokens["agent-b"]})

    async def run():
        async with Client(params) as c:
            ws = _data(await c.call_tool("list_workspaces", {}))["workspaces"]
            assert {w["key"] for w in ws} == {world.b, world.group}

    anyio.run(run)

    bad = subprocess.run([sys.executable, "-m", "hre_server.mcp_app"], capture_output=True, text=True,
                         env={**os.environ, "DATABASE_URL": database_url, "HRE_TOKEN": "hre_wrong"})
    assert bad.returncode != 0 and "無効" in bad.stderr
