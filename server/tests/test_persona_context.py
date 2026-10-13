"""AITuber ごとのトークンで、自分の設定と人間関係をまとめて読む（persona_context と add-persona）。"""

import os
import subprocess
import sys

import httpx2
import pytest

from hre_server import service
from hre_server.service import HreError


def _agency(conn, world):
    """グループ共有に事務所の 2 人と関係、A のペルソナに「自分」とリスナーを作る。"""
    rio = service.create_entity(conn, world.admin, "person", "莉央", world.group,
                                [{"scheme": "name", "value": "莉央"}])["id"]
    neru = service.create_entity(conn, world.admin, "person", "音流", world.group)["id"]
    src = service.add_source(conn, world.admin, "document", "公式設定", world.group, trusted=True)["source_id"]
    service.record_fact(conn, world.admin, "一人称", "僕", "stated", entity_id=rio, source_id=src, confidence=1.0)
    service.record_fact(conn, world.admin, "好きなもの", "キャンプ", "stated", entity_id=rio, confidence=1.0)
    rel = service.add_relationship(conn, world.admin, "colleague", rio, neru)["relationship_id"]
    service.record_fact(conn, world.admin, "呼び方", "ねるくん", "stated", relationship_id=rel)
    service.record_fact(conn, world.admin, "共有している文脈", "ゲーム配信の定番コンビ", "stated", relationship_id=rel)
    me = service.create_entity(conn, world.admin, "person", "莉央", world.a, [{"scheme": "name", "value": "莉央"}],
                               is_self=True)["id"]
    listener = service.create_entity(conn, world.agent_a, "person", "リスナーX", world.a)["id"]
    service.add_relationship(conn, world.agent_a, "listener_of", listener, me)
    service.record_fact(conn, world.agent_a, "好きなもの", "整地", "stated", entity_id=me)
    return rio, neru, me


def test_persona_context_merges_self_and_shared_profile(conn, world):
    rio, neru, me = _agency(conn, world)
    ctx = service.persona_context(conn, world.agent_a)
    assert ctx["self"] == {"id": me, "name": "莉央", "workspace": world.a}
    assert [lk["id"] for lk in ctx["linked"]] == [rio]
    prof = {p["key"]: p["values"] for p in ctx["profile"]}
    assert prof["first_person"] == ["僕"]
    assert sorted(prof["likes"]) == ["キャンプ", "整地"]
    by_name = {r["other_name"]: r for r in ctx["relationships"]}
    assert set(by_name) == {"音流", "リスナーX"}
    facts = {f["key"]: f["value"] for f in by_name["音流"]["facts"]}
    assert facts == {"calls_them": "ねるくん", "shared_context": "ゲーム配信の定番コンビ"}
    assert by_name["音流"]["relation_label"] == "同僚"


def test_persona_context_needs_a_self_node(conn, world):
    with pytest.raises(HreError, match="自分"):
        service.persona_context(conn, world.agent_b)


def test_add_persona_creates_workspace_actor_and_reads_group(conn, world):
    rio, _, _ = _agency(conn, world)
    key = world.a + "-p"
    self_id, token = service.add_persona(conn, key, "莉央", world.group)
    actor = service.authenticate(conn, token)
    assert actor is not None and actor.kind == "agent"
    keys = {w["key"]: w for w in service.list_workspaces(conn, actor)}
    assert set(keys) == {key, world.group}
    assert keys[key]["self_entity_id"] == self_id
    ctx = service.persona_context(conn, actor)
    assert ctx["self"]["id"] == self_id and rio in [lk["id"] for lk in ctx["linked"]]
    # 共有ワークスペースは読めても書けない
    with pytest.raises(HreError, match="書き込み権限"):
        service.record_fact(conn, actor, "好きなもの", "雪", "stated", entity_id=rio)

    # 2 回目は作り直さず、トークンだけ新しくなる
    again, token2 = service.add_persona(conn, key, "莉央", world.group)
    assert again == self_id and token2 != token
    assert service.authenticate(conn, token) is None and service.authenticate(conn, token2) is not None

    with pytest.raises(HreError, match="共有ワークスペース"):
        service.add_persona(conn, key + "x", "誰か", "no-such-group")


def test_add_persona_cli(database_url, world):
    out = subprocess.run([sys.executable, "-m", "hre_server.admin", "add-persona", world.b + "-cli", "音流",
                          "--group", world.group], check=True, capture_output=True, text=True,
                         env=os.environ).stdout
    assert "hre_" in out.splitlines()[-1]


def test_persona_context_http(server_url, conn, world):
    _agency(conn, world)
    url = server_url.removesuffix("/mcp") + "/api/persona-context"
    assert httpx2.get(url).status_code == 401
    assert httpx2.get(url, headers={"Authorization": "Bearer hre_wrong"}).status_code == 401
    assert httpx2.get(url, headers={"Authorization": f"Bearer {world.tokens['agent-b']}"}).status_code == 404
    r = httpx2.get(url, headers={"Authorization": f"Bearer {world.tokens['agent-a']}"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["self"]["name"] == "莉央"


def test_enum_accepts_numbers_from_mcp_clients(conn, world):
    a = service.create_entity(conn, world.admin, "person", "甲", world.group)["id"]
    b = service.create_entity(conn, world.admin, "person", "乙", world.group)["id"]
    rel = service.add_relationship(conn, world.admin, "colleague", a, b)["relationship_id"]
    service.record_fact(conn, world.admin, "親しさ", 5, "observed", relationship_id=rel)
    facts = service.get_entity(conn, world.admin, a)["relationships"][0]["facts"]
    assert [f["value"] for f in facts if f["field_key"] == "closeness_rating"] == ["5"]
    with pytest.raises(HreError, match="enum"):
        service.record_fact(conn, world.admin, "親しさ", 6, "observed", relationship_id=rel)
