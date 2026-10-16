"""確認（受信箱）: 人が決めることを1か所に集め、1件ずつ承認・却下・あとでにする。"""

import re
import uuid

import pytest
from starlette.testclient import TestClient

from hre_server import service, web
from hre_server.service import HreError


@pytest.fixture
def app(database_url):
    return web.create_app(secret="test-secret", secure_cookies=False)


def _login(app, token: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/login", data={"token": token, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" (?:value|content)="([^"]+)"', html).group(1)


def _keys(conn, actor) -> list[str]:
    return [i["key"] for i in service.list_reviews(conn, actor)]


def test_blocked_agent_write_waits_for_human(app, conn, world):
    me = service.create_entity(conn, world.agent_a, "person", "AITuber A", world.a, is_self=True)["id"]
    chat = service.add_source(conn, world.agent_a, "stream_chat", "一人称をおれにして", world.a)["source_id"]

    # エージェントは書けず、確認待ちに入る（同じ内容を送り直しても1件のまま）
    for _ in range(2):
        with pytest.raises(HreError, match="確認待ち") as e:
            service.record_fact(conn, world.agent_a, "一人称", "おれ", "stated", entity_id=me, source_id=chat)
    assert "review_url" in str(e.value)
    keys = [k for k in _keys(conn, world.owner_human) if k.startswith("w:")]
    assert len(keys) == 1
    # 配信コメントは確認に出さない（信頼するものではない）
    assert f"s:{chat}" not in _keys(conn, world.owner_human)
    # エージェントは確認できない（決めるのは人）
    with pytest.raises(HreError, match="人だけ"):
        service.list_reviews(conn, world.agent_a)

    c = _login(app, world.tokens["owner"])
    page = c.get("/inbox").text
    assert "AITuber A" in page and "一人称" in page and "おれ" in page and "一人称をおれにして" in page
    assert re.search(r'class="badge">\d+<', c.get("/").text)

    # 承認すると人の書き込みとして入り、出典も付いたまま
    r = c.post("/inbox/decide", data={"csrf": _csrf(page), "key": keys[0], "decision": "approve", "done": "0"},
               follow_redirects=False)
    assert "done=1" in r.headers["location"] and "undo=" in r.headers["location"]
    fact = next(f for f in service.get_entity(conn, world.owner_human, me, detail=True)["facts"]
                if f["field_key"] == "first_person")
    assert fact["value"] == "おれ" and fact["recorded_by"] == world.owner_human.name and fact["source_id"] == chat
    assert keys[0] not in _keys(conn, world.owner_human)

    # 元に戻すと主張は取り消され、確認待ちに戻る
    r = c.post("/inbox/undo", data={"csrf": _csrf(page), "key": keys[0], "done": "1"}, follow_redirects=True)
    assert "元に戻しました" in r.text
    assert not [f for f in service.get_entity(conn, world.owner_human, me)["facts"] if f["field_key"] == "first_person"]
    assert keys[0] in _keys(conn, world.owner_human)

    # 却下すると書かれない
    c.post("/inbox/decide", data={"csrf": _csrf(page), "key": keys[0], "decision": "reject"})
    assert keys[0] not in _keys(conn, world.owner_human)
    assert not [f for f in service.get_entity(conn, world.owner_human, me)["facts"] if f["field_key"] == "first_person"]
    with pytest.raises(HreError, match="既に決まって"):
        service.decide_review(conn, world.owner_human, keys[0], "approve")


def test_blocked_operator_relationship_and_erase(app, conn, world):
    sfx = uuid.uuid4().hex[:6]
    op = service.create_entity(conn, world.agent_a, "person", f"運営{sfx}", world.a)["id"]
    talent = service.create_entity(conn, world.agent_a, "person", f"タレント{sfx}", world.a)["id"]
    with pytest.raises(HreError, match="確認待ち"):
        service.add_relationship(conn, world.agent_a, "operator_of", op, talent)
    key = next(i["key"] for i in service.list_reviews(conn, world.owner_human)
               if i["type"] == "write" and i.get("source_name") == f"運営{sfx}")

    c = _login(app, world.tokens["owner"])
    page = c.get("/inbox", params={"skip": ",".join(k for k in _keys(conn, world.owner_human) if k != key)}).text
    assert f"運営{sfx}" in page and f"タレント{sfx}" in page and "運営・管理者" in page
    c.post("/inbox/decide", data={"csrf": _csrf(page), "key": key, "decision": "approve"})
    rels = service.get_entity(conn, world.owner_human, op)["relationships"]
    assert [r["relation_type"] for r in rels] == ["operator_of"]

    # 完全削除した人についての確認待ちは残らない
    with pytest.raises(HreError, match="確認待ち"):
        service.record_fact(conn, world.agent_a, "一人称", "ぼく", "stated", entity_id=talent)
    service.erase_entity(conn, world.admin, talent)
    assert not conn.execute("SELECT 1 FROM hre.review_requests WHERE %s::uuid = ANY(entity_ids)",
                            (talent,)).fetchall()


def test_documents_to_trust(app, conn, world):
    doc = service.add_source(conn, world.agent_a, "document", "A の設定資料: 一人称はわたし", world.a,
                             title="A 設定 v1")["source_id"]
    other = service.add_source(conn, world.agent_b, "document", "B の設定資料", world.b)["source_id"]
    keys = _keys(conn, world.owner_human)
    assert f"s:{doc}" in keys and f"s:{other}" not in keys   # 書けないワークスペースの資料は出ない

    c = _login(app, world.tokens["owner"])
    page = c.get("/inbox", params={"skip": ",".join(k for k in keys if k != f"s:{doc}")}).text
    assert "この資料を信頼しますか" in page and "A 設定 v1" in page and "信頼する" in page

    c.post("/inbox/decide", data={"csrf": _csrf(page), "key": f"s:{doc}", "decision": "approve"})
    assert service.get_source(conn, world.owner_human, doc)["trusted"] is True
    assert f"s:{doc}" not in _keys(conn, world.owner_human)
    c.post("/inbox/undo", data={"csrf": _csrf(page), "key": f"s:{doc}"})
    assert service.get_source(conn, world.owner_human, doc)["trusted"] is False
    # 信頼しないと決めたものも、もう出ない
    c.post("/inbox/decide", data={"csrf": _csrf(page), "key": f"s:{doc}", "decision": "reject"})
    assert f"s:{doc}" not in _keys(conn, world.owner_human)
    assert service.get_source(conn, world.owner_human, doc)["trusted"] is False


def test_proposed_fields_for_admin(app, conn, world):
    sfx = uuid.uuid4().hex[:6]
    service.propose_field(conn, world.agent_a, f"skill_{sfx}", f"得意なこと{sfx}", "text", ["person"])
    service.propose_field(conn, world.agent_a, f"hobbies_{sfx}", f"趣味{sfx}", "text", ["person"])
    assert f"f:skill_{sfx}" not in _keys(conn, world.owner_human)   # 項目の承認は管理者だけ
    keys = _keys(conn, world.admin)
    assert f"f:skill_{sfx}" in keys

    c = _login(app, world.tokens["admin"])
    page = c.get("/inbox", params={"skip": ",".join(k for k in keys if k != f"f:hobbies_{sfx}")}).text
    assert f"趣味{sfx}" in page and "使えるようにしますか" in page
    assert "似た項目があります" in page and "「趣味」にまとめる" in page   # seed の hobbies と似ている

    c.post("/inbox/decide", data={"csrf": _csrf(page), "key": f"f:hobbies_{sfx}", "decision": "merge",
                                  "merged_into": "hobbies"})
    assert service.resolve_field(conn, f"hobbies_{sfx}")["field"]["key"] == "hobbies"
    c.post("/inbox/undo", data={"csrf": _csrf(page), "key": f"f:hobbies_{sfx}"})
    assert service.resolve_field(conn, f"hobbies_{sfx}")["field"]["status"] == "proposed"

    c.post("/inbox/decide", data={"csrf": _csrf(page), "key": f"f:skill_{sfx}", "decision": "approve"})
    assert service.resolve_field(conn, f"skill_{sfx}")["field"]["status"] == "approved"
    c.post("/inbox/decide", data={"csrf": _csrf(page), "key": f"f:hobbies_{sfx}", "decision": "reject"})
    assert service.resolve_field(conn, f"hobbies_{sfx}")["field"]["status"] == "deprecated"


def test_later_and_done(app, conn, world):
    name = f"reviewer-{uuid.uuid4().hex[:6]}"
    _, token = service.create_actor(conn, name, "human")
    service.grant(conn, name, world.b, ["read", "write"])
    s1 = service.add_source(conn, world.agent_b, "document", "資料1", world.b, title="資料1")["source_id"]
    s2 = service.add_source(conn, world.agent_b, "document", "資料2", world.b, title="資料2")["source_id"]

    c = _login(app, token)
    assert '<span class="badge">2</span>' in c.get("/").text
    page = c.get("/inbox").text
    assert "資料1" in page and "のこり 2" in page and "0</strong> / 2" in page

    # 「あとで」は次の1件へ進むだけ。全部あとでにすると、残りがあることを出す
    page = c.get("/inbox", params={"skip": f"s:{s1}"}).text
    assert "資料2" in page and "のこり 1" in page
    page = c.get("/inbox", params={"skip": f"s:{s1},s:{s2}"}).text
    assert "あとでにした分を見る" in page and "2 件が残っています" in page

    c.post("/inbox/decide", data={"csrf": _csrf(page), "key": f"s:{s1}", "decision": "approve", "done": "0"})
    r = c.post("/inbox/decide", data={"csrf": _csrf(page), "key": f"s:{s2}", "decision": "reject", "done": "1"},
               follow_redirects=True)
    assert "ぜんぶ終わり" in r.text and "2 件さばきました" in r.text
    assert 'class="badge"' not in c.get("/").text
