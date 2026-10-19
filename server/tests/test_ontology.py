"""オントロジー（関係の種類・フィールドの使い分け）と、persona_context の配慮（docs/ontology.md）。"""

import uuid

import pytest

from hre_server import oauth, service
from hre_server.service import HreError


def _people(conn, world, *names):
    return [service.create_entity(conn, world.agent_a, "person", n, world.a)["id"] for n in names]


def test_kinship_and_inverse_words_point_to_the_right_relation(conn, world):
    a, b = _people(conn, world, "A", "B")
    with pytest.raises(HreError, match="family 関係.*kinship_term"):
        service.add_relationship(conn, world.agent_a, "父", a, b)
    with pytest.raises(HreError, match="partner 関係"):
        service.add_relationship(conn, world.agent_a, "婚約者", a, b)
    # 逆向きの言い方は入れ替えずに、正しい書き方を返す
    with pytest.raises(HreError, match="manager_of の逆向き"):
        service.add_relationship(conn, world.agent_a, "部下", a, b)
    with pytest.raises(HreError, match="listener_of の逆向き"):
        service.add_relationship(conn, world.agent_a, "配信者", a, b)
    with pytest.raises(HreError, match="list_relation_types"):
        service.add_relationship(conn, world.agent_a, "宿敵", a, b)

    # 家族は family＋続柄で書ける
    rel = service.add_relationship(conn, world.agent_a, "family", a, b)
    assert rel["reading"] == "A と B は家族"
    service.record_fact(conn, world.agent_a, "続柄", "父", "stated", relationship_id=rel["relationship_id"])


def test_reading_shows_the_direction(conn, world):
    x, me = _people(conn, world, "リスナーX", "配信者A")
    r = service.add_relationship(conn, world.agent_a, "リスナー", x, me)
    assert r["reading"] == "リスナーX は 配信者A のリスナー（配信者A は リスナーX の配信者）"


def test_undirected_relationship_is_not_duplicated_in_reverse(conn, world):
    a, b = _people(conn, world, "A", "B")
    first = service.add_relationship(conn, world.agent_a, "friend", a, b)
    again = service.add_relationship(conn, world.agent_a, "友達", b, a)
    assert again["already_exists"] and again["relationship_id"] == first["relationship_id"]
    # 有向の関係は逆向きを別の関係として持てる（お互いがお互いのリスナー）
    service.add_relationship(conn, world.agent_a, "listener_of", a, b)
    assert "already_exists" not in service.add_relationship(conn, world.agent_a, "listener_of", b, a)


def test_field_names_that_are_relation_types(conn, world):
    r = service.resolve_field(conn, "家族")
    assert r["found"] and r["field"]["key"] == "family_notes"
    assert "add_relationship(relation_type='family')" in r["relation_type_hint"]
    with pytest.raises(HreError, match="関係の種類 'partner'"):
        service.propose_field(conn, world.agent_a, "spouse_" + uuid.uuid4().hex[:6], "配偶者", "text", ["person"])


def test_ng_topics_rejection_points_to_topics_to_avoid(conn, world):
    (x,) = _people(conn, world, "リスナーX")
    with pytest.raises(HreError, match="topics_to_avoid"):
        service.record_fact(conn, world.agent_a, "ng_topics", "仕事の話", "stated", entity_id=x)
    service.record_fact(conn, world.agent_a, "触れない話題", "仕事の話", "stated", entity_id=x)


def test_legal_name_is_protected_and_not_shared_with_ai(conn, world):
    (x,) = _people(conn, world, "X")
    with pytest.raises(HreError, match="確認待ち"):
        service.record_fact(conn, world.agent_a, "本名", "山田太郎", "stated", entity_id=x)
    f = service.resolve_field(conn, "氏名")["field"]
    assert (f["key"], f["sensitivity"], f["ai_shareable"], f["requires_trusted_source"]) == \
        ("legal_name", "sensitive", False, True)
    assert service.resolve_field(conn, "full_name")["found"] is False   # 芸名のフルネームを吸い込まない


def _persona(conn, world):
    me = service.create_entity(conn, world.admin, "person", "莉央", world.a, is_self=True)["id"]

    def listener(name, calls=None):
        eid = service.create_entity(conn, world.agent_a, "person", name, world.a)["id"]
        rel = service.add_relationship(conn, world.agent_a, "listener_of", eid, me)["relationship_id"]
        service.record_fact(conn, world.agent_a, "共有している文脈", f"{name}の話", "stated", relationship_id=rel)
        if calls:
            service.record_fact(conn, world.agent_a, "呼び方", calls, "stated", relationship_id=rel)
        return eid
    return me, listener


def test_persona_context_respects_stream_mention_policy_and_minors(conn, world):
    me, listener = _persona(conn, world)
    listener("普通さん", "ふつうさん")
    avoid = listener("避けてさん")
    nick = listener("本名っぽい名前", "ニックさん")
    nick_without_calls = listener("呼び方なしさん")
    minor = listener("中学生さん", "ちゅうさん")
    service.record_fact(conn, world.agent_a, "配信での扱い", "ok", "stated", entity_id=avoid)
    service.record_fact(conn, world.agent_a, "配信での扱い", "avoid", "stated", entity_id=avoid)   # 厳しい方を採る
    service.record_fact(conn, world.agent_a, "stream_mention_policy", "nickname_only", "stated", entity_id=nick)
    service.record_fact(conn, world.agent_a, "stream_mention_policy", "nickname_only", "stated",
                        entity_id=nick_without_calls)
    service.record_fact(conn, world.agent_a, "未成年", True, "stated", entity_id=minor)

    rels = {r["other_name"]: r for r in service.persona_context(conn, world.agent_a)["relationships"]}
    assert set(rels) == {"普通さん", "ニックさん", "中学生さん"}   # avoid と、呼び方の無い nickname_only は渡さない
    assert "handling" not in rels["普通さん"] and len(rels["普通さん"]["facts"]) == 2
    assert rels["ニックさん"]["handling"] == ["nickname_only"]
    assert rels["ニックさん"]["facts"] == [{"key": "calls_them", "label": "呼び方（自分→相手）", "value": "ニックさん"}]
    assert rels["中学生さん"]["handling"] == ["minor"]
    assert [f["key"] for f in rels["中学生さん"]["facts"]] == ["calls_them"]
    # 相手の役割も返す（relation_label は自分の役割のまま）
    assert (rels["普通さん"]["relation_label"], rels["普通さん"]["other_label"]) == ("配信者", "リスナー")


def test_persona_context_leaves_out_unapproved_fields(conn, world):
    me, listener = _persona(conn, world)
    key = "character_setting_" + uuid.uuid4().hex[:6]
    service.propose_field(conn, world.agent_a, key, "キャラ設定" + key[-6:], "text", ["person"], "many")
    service.record_fact(conn, world.agent_a, key, "実は運営と知り合い", "stated", entity_id=me)
    service.record_fact(conn, world.agent_a, "好きなもの", "キャンプ", "stated", entity_id=me)
    ctx = service.persona_context(conn, world.agent_a)
    assert [p["key"] for p in ctx["profile"]] == ["likes"]
    assert ctx["omitted_unapproved"] == [key]
    # 人の画面・get_entity には出る
    assert key in {f["field_key"] for f in service.get_entity(conn, world.agent_a, me)["facts"]}


def test_upgrade_is_repeatable_and_keeps_admin_changes(conn):
    conn.execute("UPDATE hre.field_definitions SET ai_shareable = true WHERE key = 'legal_name'")
    try:
        oauth.apply_schema(conn)
        oauth.apply_schema(conn)
        row = conn.execute("SELECT ai_shareable FROM hre.field_definitions WHERE key = 'legal_name'").fetchone()
        assert row[0] is True   # 一度だけの変更は、管理者が後から変えた値を起動のたびに戻さない
        assert conn.execute("SELECT count(*) FROM hre.relation_types WHERE description IS NULL").fetchone()[0] == 0
    finally:
        conn.execute("UPDATE hre.field_definitions SET ai_shareable = false WHERE key = 'legal_name'")
