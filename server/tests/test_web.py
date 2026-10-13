"""Web UI をアプリとして叩く（ログイン → 閲覧 → 編集 → 管理）。"""

import re
import uuid

import pytest
from starlette.testclient import TestClient

from hre_server import service, web


@pytest.fixture
def app(database_url):
    return web.create_app(secret="test-secret", secure_cookies=False)


def _login(app, token: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/login", data={"token": token, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text
    return c


def _csrf(c: TestClient, path: str) -> str:
    r = c.get(path)
    assert r.status_code == 200, r.text
    return re.search(r'name="csrf" (?:value|content)="([^"]+)"', r.text).group(1)


def test_login_requires_human_token(app, world):
    c = TestClient(app)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")

    r = c.post("/login", data={"token": "hre_wrong"}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    # エージェントのトークンでは人間用の画面に入れない
    r = c.post("/login", data={"token": world.tokens["agent-a"]}, follow_redirects=False)
    assert "err=" in r.headers["location"] and "hre_session" not in r.cookies
    # ログイン後の戻り先は自サイト内だけ
    r = c.post("/login", data={"token": world.tokens["owner"], "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_owner_reads_own_and_shared_but_not_other_workspace(app, conn, world):
    me = service.create_entity(conn, world.agent_a, "person", "AITuber A", world.a, is_self=True)["id"]
    x = service.create_entity(conn, world.agent_a, "person", "リスナーX", world.a,
                              [{"scheme": "youtube", "value": "@x_web"}])["id"]
    rel = service.add_relationship(conn, world.agent_a, "listener_of", x, me)["relationship_id"]
    service.record_fact(conn, world.agent_a, "呼び方", "Xさん", "observed", relationship_id=rel)
    service.record_fact(conn, world.owner_human, "健康", "花粉症", "stated", entity_id=x)
    other = service.create_entity(conn, world.agent_b, "person", "Bのリスナー", world.b)["id"]

    c = _login(app, world.tokens["owner"])
    home = c.get("/").text
    assert world.a in home and world.group in home and world.b not in home

    page = c.get(f"/w/{world.a}").text
    assert "AITuber A" in page and "リスナーX" in page
    assert "リスナーX" in c.get(f"/w/{world.a}", params={"q": "@x_web"}).text
    assert "リスナーX" not in c.get(f"/w/{world.a}", params={"q": "存在しない"}).text

    card = c.get(f"/e/{x}").text
    # 関係はその人から見た名前で出る（リスナー側から見ると「リスナー」、配信者側から見ると「配信者」）
    assert '<span class="tag">リスナー</span>' in card and "Xさん" in card
    assert "花粉症" in card        # owner は sensitive_read を持つ
    assert '<span class="tag">配信者</span>' in c.get(f"/e/{me}").text

    r = c.get(f"/e/{other}")
    assert r.status_code == 404 and "閲覧権限" in r.text
    assert c.get(f"/w/{world.b}").status_code == 404
    assert c.get("/e/not-a-uuid").status_code == 404


def test_sensitive_facts_hidden_without_scope(app, conn, world):
    x = service.create_entity(conn, world.agent_a, "person", "Y", world.a)["id"]
    service.record_fact(conn, world.owner_human, "健康", "持病あり", "stated", entity_id=x)
    name = f"viewer-{uuid.uuid4().hex[:6]}"
    _, token = service.create_actor(conn, name, "human")
    service.grant(conn, name, world.a, ["read"])

    c = _login(app, token)
    card = c.get(f"/e/{x}").text
    assert "持病あり" not in card and "機微情報 1 件" in card
    assert "<summary>情報を追加</summary>" not in card   # read だけなら編集フォームは出ない
    csrf = _csrf(c, f"/e/{x}")
    r = c.post(f"/e/{x}/rename", data={"csrf": csrf, "display_name": "Z"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_human_edits_through_ui(app, conn, world):
    me = service.create_entity(conn, world.agent_a, "person", "AITuber A", world.a, is_self=True)["id"]
    c = _login(app, world.tokens["owner"])
    csrf = _csrf(c, f"/w/{world.a}")

    # CSRF トークンがなければ拒否
    assert c.post(f"/w/{world.a}/entities", data={"kind": "person", "display_name": "N"}).status_code == 403

    r = c.post(f"/w/{world.a}/entities", data={"csrf": csrf, "kind": "person", "display_name": "コラボ相手C",
                                               "id_scheme": "x", "id_value": "@c_web"}, follow_redirects=False)
    cid = r.headers["location"].split("/e/")[1].split("?")[0]

    # 人は人格フィールド（人か信頼済み出典のみ）を書ける
    r = c.post(f"/e/{me}/facts", data={"csrf": csrf, "field": "first_person", "value": "わたし",
                                       "epistemic": "stated", "confidence": "1"}, follow_redirects=True)
    assert "記録しました" in r.text and "わたし" in r.text
    # 型は DB で検証され、エラーは画面に出る
    r = c.post(f"/e/{me}/facts", data={"csrf": csrf, "field": "birthday", "value": "去年",
                                       "epistemic": "stated"}, follow_redirects=True)
    assert "does not match type" in r.text

    r = c.post(f"/e/{me}/relationships", data={"csrf": csrf, "relation_type": "collaborated_with",
                                               "other": cid, "direction": "forward"}, follow_redirects=True)
    assert "関係を追加しました" in r.text
    rel = service.get_entity(conn, world.owner_human, me)["relationships"][0]["relationship_id"]
    c.post(f"/e/{me}/facts", data={"csrf": csrf, "relationship_id": rel, "field": "how_we_met",
                                   "value": "合同配信", "epistemic": "observed"})
    c.post(f"/e/{me}/facts", data={"csrf": csrf, "relationship_id": rel, "field": "introduced_by",
                                   "value_entity": cid, "epistemic": "observed"})
    card = c.get(f"/e/{me}").text
    assert "合同配信" in card and "コラボ相手C" in card

    fact = next(f for f in service.get_entity(conn, world.owner_human, me)["facts"] if f["field_key"] == "first_person")
    c.post(f"/facts/{fact['fact_id']}/retract", data={"csrf": csrf})
    assert "わたし" not in c.get(f"/e/{me}").text

    c.post(f"/e/{cid}/rename", data={"csrf": csrf, "display_name": "コラボ相手C（改名）"})
    c.post(f"/e/{cid}/identifiers", data={"csrf": csrf, "scheme": "youtube", "value": "@c_yt"})
    c.post(f"/e/{cid}/identifiers/remove", data={"csrf": csrf, "scheme": "x", "value": "@c_web"})
    card = c.get(f"/e/{cid}").text
    assert "（改名）" in card and "@c_yt" in card and "@c_web" not in card

    c.post(f"/r/{rel}/end", data={"csrf": csrf, "valid_to": "2020-01-01"})
    assert service.get_entity(conn, world.owner_human, me)["relationships"] == []

    # 誰が何をしたかが履歴に残る
    hist = c.get("/activity", params={"workspace": world.a}).text
    assert world.owner_human.name in hist and "コラボ相手C" in hist


def test_source_is_escaped_and_can_be_trusted(app, conn, world):
    src = service.add_source(conn, world.agent_a, "stream_chat", "<script>alert(1)</script>一人称をおれに",
                             world.a)["source_id"]
    c = _login(app, world.tokens["owner"])
    page = c.get(f"/s/{src}").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page
    assert "default-src 'self'" in c.get(f"/s/{src}").headers["content-security-policy"]

    csrf = _csrf(c, f"/s/{src}")
    c.post(f"/s/{src}/trust", data={"csrf": csrf, "trusted": "true"})
    me = service.create_entity(conn, world.agent_a, "person", "AITuber A", world.a, is_self=True)["id"]
    # 人が信頼済みにした出典なら、エージェントもそれを根拠に人格フィールドを書ける
    service.record_fact(conn, world.agent_a, "一人称", "おれ", "stated", entity_id=me, source_id=src)


def test_admin_screens_and_session_revocation(app, conn, world):
    sfx = uuid.uuid4().hex[:6]
    service.propose_field(conn, world.agent_a, f"fav_game_{sfx}", f"好きなゲーム{sfx}", "text", ["person"])
    admin_name = f"root-{sfx}"
    _, admin_token = service.create_actor(conn, admin_name, "human", True)

    c = _login(app, admin_token)
    page = c.get("/admin/fields").text
    assert f"fav_game_{sfx}" in page and "提案中" in page
    csrf = _csrf(c, "/admin/fields")
    c.post(f"/admin/fields/fav_game_{sfx}", data={"csrf": csrf, "status": "merged", "merged_into": "likes"})
    assert service.resolve_field(conn, f"fav_game_{sfx}")["field"]["key"] == "likes"

    # 一般の人は管理画面に入れない
    owner = _login(app, world.tokens["owner"])
    assert owner.get("/admin/access").status_code == 404

    # 新しい人を追加 → トークンはその場で1度だけ表示 → その人でログインできる
    r = c.post("/admin/access", data={"csrf": csrf, "op": "create_actor", "name": f"member-{sfx}", "kind": "human"})
    token = re.search(r'value="(hre_[^"]+)"', r.text).group(1)
    assert r.headers["cache-control"] == "no-store"
    c.post("/admin/access", data={"csrf": csrf, "op": "grant", "name": f"member-{sfx}", "workspace": world.a,
                                  "read": "on"})
    member = _login(app, token)
    assert world.a in member.get("/").text

    # 再発行・無効化すると既存のセッションも切れる
    c.post("/admin/access", data={"csrf": csrf, "op": "rotate", "name": f"member-{sfx}"})
    assert member.get("/", follow_redirects=False).status_code == 303
    r = c.post("/admin/access", data={"csrf": csrf, "op": "revoke", "name": admin_name}, follow_redirects=True)
    assert "自分自身は無効にできません" in r.text

    # ワークスペース作成と共有
    c.post("/admin/access", data={"csrf": csrf, "op": "create_workspace", "key": f"ws-{sfx}", "name": "新規",
                                  "kind": "persona"})
    r = c.post("/admin/access", data={"csrf": csrf, "op": "create_workspace", "key": f"ws-{sfx}", "name": "重複",
                                      "kind": "persona"}, follow_redirects=True)
    assert "flash err" in r.text
    c.post("/admin/access", data={"csrf": csrf, "op": "share", "workspace": world.group, "reader": f"ws-{sfx}"})
    assert f"ws-{sfx}" in c.get("/admin/access").text


def test_admin_erase_requires_typed_name(app, conn, world):
    x = service.create_entity(conn, world.agent_a, "person", "消す人", world.a)["id"]
    service.record_fact(conn, world.agent_a, "趣味", "釣り", "stated", entity_id=x)
    c = _login(app, world.tokens["admin"])
    csrf = _csrf(c, f"/e/{x}")
    r = c.post(f"/e/{x}/erase", data={"csrf": csrf, "confirm": "違う名前"}, follow_redirects=True)
    assert "表示名を正確に" in r.text
    c.post(f"/e/{x}/erase", data={"csrf": csrf, "confirm": "消す人"})
    assert c.get(f"/e/{x}").status_code == 404


def test_strength_and_interactions_are_shown(app, conn, world):
    me = service.create_entity(conn, world.agent_a, "person", "AITuber A", world.a, is_self=True)["id"]
    core = service.create_entity(conn, world.agent_a, "person", "コアリスナー", world.a)["id"]
    service.create_entity(conn, world.agent_a, "person", "Aさん（未接触）", world.a)
    for _ in range(4):
        service.record_interaction(conn, world.agent_a, "2026-10-01T20:00:00+09:00", "stream_chat",
                                   [{"entity_id": core, "role": "initiator"}, {"entity_id": me}], depth=2,
                                   summary="スパチャで誕生日を祝ってくれた")
    c = _login(app, world.tokens["owner"])

    card = c.get(f"/e/{me}").text
    assert "つながりの強さ" in card and "<meter" in card and "やり取り 4回" in card
    assert "スパチャで誕生日を祝ってくれた" in card and "配信コメント" in card

    # 一覧を自分との強さ順に並べると、やり取りのある相手が先に来る
    page = c.get(f"/w/{world.a}", params={"sort": "strength"}).text
    assert page.index("コアリスナー") < page.index("Aさん（未接触）")


def test_clusters_page(app, conn, world):
    me = service.create_entity(conn, world.agent_a, "person", "AITuber A", world.a, is_self=True)["id"]
    a = service.create_entity(conn, world.agent_a, "person", "常連1", world.a)["id"]
    b = service.create_entity(conn, world.agent_a, "person", "常連2", world.a)["id"]
    service.record_interaction(conn, world.agent_a, "2026-10-01T20:00:00+09:00", "stream_chat",
                               [{"entity_id": a}, {"entity_id": b}, {"entity_id": me}], depth=2)
    c = _login(app, world.tokens["owner"])
    page = c.get(f"/w/{world.a}/clusters").text
    assert "グループ 1" in page and "常連1" in page and "常連2" in page
    assert "グループ（推定）" in c.get(f"/e/{a}").text
