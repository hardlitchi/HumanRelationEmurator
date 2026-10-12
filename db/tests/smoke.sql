-- スキーマと初期マスターの動作確認。空の DB に schema.sql → seed.sql の後で流す。
-- 失敗すると例外で止まる。最後に ROLLBACK するのでデータは残らない。
\set ON_ERROR_STOP on
SET search_path = hre, public;
BEGIN;

CREATE FUNCTION pg_temp.expect_error(stmt text, pattern text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE stmt;
    RAISE EXCEPTION 'expected error matching "%" but statement succeeded: %', pattern, stmt;
EXCEPTION WHEN others THEN
    IF SQLERRM NOT LIKE pattern THEN
        RAISE EXCEPTION 'expected error matching "%", got "%"', pattern, SQLERRM;
    END IF;
END $$;

DO $$
DECLARE
    agent uuid;
    me uuid; tanaka uuid; tanaka2 uuid; sato uuid; acme uuid;
    rel_friend uuid; rel_work uuid;
    src uuid; ia uuid;
    n int;
BEGIN
    INSERT INTO actors (kind, name, scopes) VALUES ('agent', 'test-agent', '{read,write}') RETURNING id INTO agent;

    -- 表記ゆれの解決
    ASSERT (resolve_field('生年月日')).key = 'birthday', '生年月日 -> birthday';
    ASSERT (resolve_field('Date of Birth')).key = 'birthday', 'Date of Birth -> birthday';
    ASSERT (resolve_field('ＤＯＢ')).key = 'birthday', '全角 DOB -> birthday';
    ASSERT (resolve_field('job-title')).key = 'job_title', 'job-title -> job_title';
    ASSERT resolve_field('favorite_color') IS NULL, '未登録は NULL';
    ASSERT EXISTS (SELECT 1 FROM suggest_fields('birth day') WHERE key = 'birthday'), 'suggest: birth day';
    ASSERT EXISTS (SELECT 1 FROM suggest_fields('趣味・特技') WHERE key = 'hobbies'), 'suggest: 趣味・特技';

    -- 統合されたフィールドは統合先に解決される
    INSERT INTO field_definitions (key, label, value_type, applies_to, status, created_by)
        VALUES ('hobby_list', '趣味リスト', 'text', '{person}', 'proposed', agent);
    UPDATE field_definitions SET status = 'merged', merged_into_id = (SELECT id FROM field_definitions WHERE key = 'hobbies')
     WHERE key = 'hobby_list';
    ASSERT (resolve_field('hobby_list')).key = 'hobbies', 'merged -> hobbies';

    -- エンティティ
    INSERT INTO entities (kind, display_name, is_self) VALUES ('person', '自分', true) RETURNING id INTO me;
    INSERT INTO entities (kind, display_name) VALUES ('person', '田中太郎') RETURNING id INTO tanaka;
    INSERT INTO entities (kind, display_name) VALUES ('person', '田中さん') RETURNING id INTO tanaka2;
    INSERT INTO entities (kind, display_name) VALUES ('person', '佐藤花子') RETURNING id INTO sato;
    INSERT INTO entities (kind, display_name) VALUES ('organization', 'Acme株式会社') RETURNING id INTO acme;
    INSERT INTO entity_identifiers (entity_id, scheme, value) VALUES (tanaka, 'email', 't.tanaka@example.com');
    INSERT INTO entity_identifiers (entity_id, scheme, value) VALUES (tanaka2, 'name', 'たなっち');

    -- 関係
    INSERT INTO relationships (relation_type_id, source_entity_id, target_entity_id, recorded_by)
        SELECT id, me, tanaka, agent FROM relation_types WHERE key = 'friend' RETURNING id INTO rel_friend;
    INSERT INTO relationships (relation_type_id, source_entity_id, target_entity_id, valid_from, recorded_by)
        SELECT id, tanaka, acme, '2020-04-01', agent FROM relation_types WHERE key = 'works_at' RETURNING id INTO rel_work;
    INSERT INTO relationships (relation_type_id, source_entity_id, target_entity_id, recorded_by)
        SELECT id, tanaka2, sato, agent FROM relation_types WHERE key = 'colleague';

    -- 出典とやり取り
    INSERT INTO sources (kind, content, captured_by) VALUES ('conversation', '田中さんとランチ。来月が誕生日らしい', agent)
        RETURNING id INTO src;
    INSERT INTO interactions (occurred_at, channel, depth, summary, source_id, recorded_by)
        VALUES (now() - interval '3 days', 'in_person', 3, 'ランチ', src, agent) RETURNING id INTO ia;
    INSERT INTO interaction_participants VALUES (ia, me, 'initiator'), (ia, tanaka, 'participant');

    -- 主張
    INSERT INTO facts (entity_id, field_id, value, epistemic, confidence, source_id, recorded_by)
        VALUES (tanaka, (resolve_field('誕生日')).id, '"--11-03"', 'stated', 0.9, src, agent);
    INSERT INTO facts (entity_id, field_id, value, epistemic, confidence, recorded_by)
        VALUES (tanaka, (resolve_field('趣味')).id, '"釣り"', 'stated', 0.9, agent),
               (tanaka2, (resolve_field('hobby')).id, '"キャンプ"', 'reported', 0.6, agent);
    INSERT INTO facts (relationship_id, field_id, value, epistemic, recorded_by)
        VALUES (rel_friend, (resolve_field('親密度')).id, '"4"', 'observed', agent),
               (rel_friend, (resolve_field('共通の話題')).id, '"大学時代の山岳部"', 'observed', agent);
    INSERT INTO facts (entity_id, field_id, value, epistemic, confidence, recorded_by)
        VALUES (tanaka, (resolve_field('job_title')).id, '"課長"', 'reported', 0.5, agent),
               (tanaka, (resolve_field('job_title')).id, '"部長"', 'stated', 0.9, agent);

    -- 検証で弾かれるもの
    PERFORM pg_temp.expect_error(format(
        $q$INSERT INTO facts (entity_id, field_id, value, epistemic) VALUES (%L, %L, '123', 'stated')$q$,
        tanaka, (resolve_field('趣味')).id), '%does not match type%');
    PERFORM pg_temp.expect_error(format(
        $q$INSERT INTO facts (entity_id, field_id, value, epistemic) VALUES (%L, %L, '"2024/01/01"', 'stated')$q$,
        tanaka, (resolve_field('birthday')).id), '%does not match type%');
    PERFORM pg_temp.expect_error(format(
        $q$INSERT INTO facts (entity_id, field_id, value, epistemic) VALUES (%L, %L, '"4"', 'observed')$q$,
        tanaka, (resolve_field('closeness_rating')).id), '%does not apply to person%');
    PERFORM pg_temp.expect_error(format(
        $q$INSERT INTO facts (relationship_id, field_id, value, epistemic) VALUES (%L, %L, '"6"', 'observed')$q$,
        rel_friend, (resolve_field('closeness_rating')).id), '%does not match type%');
    PERFORM pg_temp.expect_error(format(
        $q$INSERT INTO facts (entity_id, field_id, value, epistemic) VALUES (%L, %L, '"糖尿病かも"', 'inferred')$q$,
        tanaka, (resolve_field('持病')).id), '%must not be inferred%');
    PERFORM pg_temp.expect_error(format(
        $q$INSERT INTO facts (entity_id, field_id, value, epistemic) VALUES (%L, %L, '"x"', 'stated')$q$,
        tanaka, (SELECT id FROM field_definitions WHERE key = 'hobby_list')), '%is merged%');
    PERFORM pg_temp.expect_error(format(
        $q$INSERT INTO relationships (relation_type_id, source_entity_id, target_entity_id) VALUES (%L, %L, %L)$q$,
        (SELECT id FROM relation_types WHERE key = 'works_at'), acme, tanaka), '%does not connect%');
    PERFORM pg_temp.expect_error(format(
        $q$INSERT INTO entity_identifiers (entity_id, scheme, value) VALUES (%L, 'email', 't.tanaka@example.com')$q$,
        sato), '%duplicate key%');

    -- 人物カード: 同じフィールドの主張が複数あれば stated・高確度・新しいものが勝つ
    ASSERT (SELECT value #>> '{}' FROM entity_profile WHERE entity_id = tanaka AND field_key = 'job_title') = '部長',
        'profile picks stated job_title';

    -- 取り消すと現在の値から外れる
    UPDATE facts SET retracted_at = now(), retracted_by = agent
     WHERE entity_id = tanaka AND value = '"部長"';
    ASSERT (SELECT value #>> '{}' FROM entity_profile WHERE entity_id = tanaka AND field_key = 'job_title') = '課長',
        'retracted fact drops out';

    -- 双方向に引ける
    ASSERT (SELECT label FROM active_edges WHERE from_id = acme AND to_id = tanaka) = '所属メンバー', 'inverse label';

    -- 名寄せ: 田中さん を 田中太郎 に統合
    PERFORM merge_entities(tanaka2, tanaka, agent);
    SELECT count(*) INTO n FROM current_facts WHERE entity_id = tanaka AND field_key = 'hobbies';
    ASSERT n = 2, 'hobbies merged: ' || n;
    ASSERT EXISTS (SELECT 1 FROM entity_identifiers WHERE entity_id = tanaka AND value = 'たなっち'), 'identifier moved';
    ASSERT EXISTS (SELECT 1 FROM active_edges WHERE from_id = tanaka AND to_id = sato), 'edge moved';

    -- 完全削除
    PERFORM erase_entity(tanaka);
    ASSERT NOT EXISTS (SELECT 1 FROM entities WHERE id IN (tanaka, tanaka2)), 'entities erased';
    ASSERT NOT EXISTS (SELECT 1 FROM facts WHERE entity_id IN (tanaka, tanaka2)), 'facts erased';
    ASSERT NOT EXISTS (SELECT 1 FROM relationships WHERE rel_friend = id OR rel_work = id), 'relationships erased';
    ASSERT NOT EXISTS (SELECT 1 FROM interactions WHERE id = ia), 'solo interaction erased';
    ASSERT NOT EXISTS (SELECT 1 FROM sources WHERE id = src), 'source erased';
    ASSERT NOT EXISTS (SELECT 1 FROM audit_log WHERE entity_ids && ARRAY[tanaka, tanaka2]), 'audit erased';
    ASSERT EXISTS (SELECT 1 FROM entities WHERE id = sato), 'others kept';

    RAISE NOTICE 'smoke: all checks passed';
END $$;

ROLLBACK;
