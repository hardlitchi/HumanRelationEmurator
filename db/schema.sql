-- HumanRelationEmurator: core schema (PostgreSQL 16+)
-- 設計の説明は docs/data-model.md を参照。

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS hre;
SET search_path = hre, public;

-- ---------------------------------------------------------------------------
-- 書き込み主体（人・AIエージェント・端末）
-- すべての書き込みは actor に紐づく。API トークンはハッシュのみ保存する。
-- ---------------------------------------------------------------------------
CREATE TABLE actors (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL CHECK (kind IN ('human', 'agent', 'importer')),
    name        text NOT NULL UNIQUE,
    token_hash  text UNIQUE,                       -- sha256(token)。平文は保存しない
    is_admin    boolean NOT NULL DEFAULT false,    -- 全ワークスペースの管理（マスター承認・横断閲覧）
    created_at  timestamptz NOT NULL DEFAULT now(),
    revoked_at  timestamptz
);

-- ---------------------------------------------------------------------------
-- ワークスペース = 「誰の視点の人間関係か」
-- 管理者本人・各 AITuber・グループ共有の情報を、それぞれ別のグラフとして持つ。
-- ワークスペースをまたぐ参照は複合外部キーで禁止している。
-- ---------------------------------------------------------------------------
CREATE TABLE workspaces (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key         text NOT NULL UNIQUE CHECK (key ~ '^[a-z][a-z0-9_-]*$'),
    name        text NOT NULL,
    -- owner: 管理者本人の視点 / persona: AITuber 等の人格の視点 / shared: グループで共有する公開情報
    kind        text NOT NULL CHECK (kind IN ('owner', 'persona', 'shared')),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- どの書き込み主体がどのワークスペースを扱えるか
CREATE TABLE workspace_grants (
    actor_id      uuid NOT NULL REFERENCES actors(id) ON DELETE CASCADE,
    workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    scopes        text[] NOT NULL DEFAULT '{read}'
                  CHECK (array_length(scopes, 1) > 0
                         AND scopes <@ ARRAY['read', 'write', 'sensitive_read']),
    PRIMARY KEY (actor_id, workspace_id)
);

-- reader のワークスペースから workspace の中身を読める（例: 各 AITuber → グループ共有）
CREATE TABLE workspace_shares (
    workspace_id         uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    reader_workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    PRIMARY KEY (workspace_id, reader_workspace_id),
    CHECK (workspace_id <> reader_workspace_id)
);

-- ---------------------------------------------------------------------------
-- フィールドマスター
-- ---------------------------------------------------------------------------
CREATE TABLE field_definitions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key             text NOT NULL UNIQUE CHECK (key ~ '^[a-z][a-z0-9_]*$'),
    label           text NOT NULL,                 -- 表示名（日本語可）
    description     text,
    value_type      text NOT NULL CHECK (value_type IN (
                        'text', 'number', 'integer', 'boolean', 'date', 'datetime',
                        'enum', 'entity_ref', 'url', 'email', 'phone', 'json')),
    enum_options    text[],                        -- value_type = 'enum' のとき必須
    applies_to      text[] NOT NULL
                    CHECK (array_length(applies_to, 1) > 0
                           AND applies_to <@ ARRAY['person', 'organization', 'group', 'relationship']),
    cardinality     text NOT NULL DEFAULT 'one' CHECK (cardinality IN ('one', 'many')),
    -- normal: 一般 / personal: 個人情報 / sensitive: 要配慮個人情報相当（推測で書かせない）
    sensitivity     text NOT NULL DEFAULT 'personal'
                    CHECK (sensitivity IN ('normal', 'personal', 'sensitive')),
    ai_shareable    boolean NOT NULL DEFAULT true, -- 外部 LLM に渡してよいか
    -- true なら、人が書くか信頼できる出典からしか書けない（人格設定・管理者などの乗っ取り防止）
    requires_trusted_source boolean NOT NULL DEFAULT false,
    status          text NOT NULL DEFAULT 'proposed'
                    CHECK (status IN ('proposed', 'approved', 'deprecated', 'merged')),
    merged_into_id  uuid REFERENCES field_definitions(id),
    created_by      uuid REFERENCES actors(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CHECK ((value_type = 'enum') = (enum_options IS NOT NULL)),
    CHECK ((status = 'merged') = (merged_into_id IS NOT NULL))
);

-- 表記ゆれ → 正規キーの対応表。alias は normalize_label() 済みの値で保存する。
CREATE TABLE field_aliases (
    alias     text PRIMARY KEY,
    field_id  uuid NOT NULL REFERENCES field_definitions(id) ON DELETE CASCADE
);

CREATE INDEX field_definitions_label_trgm ON field_definitions USING gin (label gin_trgm_ops);
CREATE INDEX field_aliases_trgm ON field_aliases USING gin (alias gin_trgm_ops);

-- 関係の種類もマスター管理する（友人・同僚・家族・所属…）
CREATE TABLE relation_types (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key           text NOT NULL UNIQUE CHECK (key ~ '^[a-z][a-z0-9_]*$'),
    label         text NOT NULL,
    directed      boolean NOT NULL,             -- false なら source/target の順序に意味はない
    inverse_label text,                         -- 有向のとき、逆向きから見た表示名（例: 上司 ↔ 部下）
    source_kinds  text[] NOT NULL DEFAULT '{person}',
    target_kinds  text[] NOT NULL DEFAULT '{person}',
    requires_trusted_source boolean NOT NULL DEFAULT false,
    status        text NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed', 'approved', 'deprecated')),
    created_by    uuid REFERENCES actors(id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE relation_type_aliases (
    alias             text PRIMARY KEY,
    relation_type_id  uuid NOT NULL REFERENCES relation_types(id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- エンティティ（人・組織・グループ）
-- ---------------------------------------------------------------------------
CREATE TABLE entities (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind            text NOT NULL CHECK (kind IN ('person', 'organization', 'group')),
    display_name    text NOT NULL,
    is_self         boolean NOT NULL DEFAULT false,  -- このワークスペースの視点の主（管理者本人 / AITuber 自身）
    merged_into_id  uuid,
    created_by      uuid REFERENCES actors(id) ON DELETE SET NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, merged_into_id) REFERENCES entities (workspace_id, id) ON DELETE CASCADE,
    CHECK (merged_into_id IS NULL OR merged_into_id <> id)
);
CREATE UNIQUE INDEX entities_single_self ON entities (workspace_id) WHERE is_self;
CREATE INDEX entities_display_name_trgm ON entities USING gin (display_name gin_trgm_ops);

-- 名寄せ用の識別子・別名（メール、電話、SNS ハンドル、呼び名など）
CREATE TABLE entity_identifiers (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  uuid NOT NULL,
    entity_id     uuid NOT NULL,
    scheme        text NOT NULL CHECK (scheme ~ '^[a-z][a-z0-9_]*$'),  -- email, x, youtube, twitch, discord, name ...
    value         text NOT NULL,                                       -- 正規化済みの値
    created_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities (workspace_id, id) ON DELETE CASCADE
);
-- name 以外の識別子はワークスペース内で一意（同じアカウントの人が2人いたら名寄せ漏れ）
CREATE UNIQUE INDEX entity_identifiers_unique ON entity_identifiers (workspace_id, scheme, value) WHERE scheme <> 'name';
CREATE UNIQUE INDEX entity_identifiers_per_entity ON entity_identifiers (entity_id, scheme, value);
CREATE INDEX entity_identifiers_value_trgm ON entity_identifiers USING gin (value gin_trgm_ops);

-- 別ワークスペースにいる同一人物の対応（例: AITuber A と B の両方のコアリスナー）。
-- 管理者だけが使う。各 AITuber は他の AITuber の記録を見ない。
CREATE TABLE entity_links (
    entity_a    uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b    uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    created_by  uuid REFERENCES actors(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entity_a, entity_b),
    CHECK (entity_a < entity_b)
);

-- ---------------------------------------------------------------------------
-- 出典（取り込んだ原文）
-- 原文は信頼できないデータとして隔離し、エージェントには指示として渡さない。
-- ---------------------------------------------------------------------------
CREATE TABLE sources (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN (
                     'manual', 'conversation', 'email', 'chat', 'calendar',
                     'sns', 'web', 'document', 'stream', 'stream_chat', 'agent_inference')),
    -- 信頼できる出典か（管理者が認証済みの経路で渡したもの等）。配信チャットや SNS は false のまま
    trusted      boolean NOT NULL DEFAULT false,
    uri          text,
    title        text,
    content      text,               -- 原文（不要なら NULL にして uri だけ残す）
    occurred_at  timestamptz,        -- 原文が書かれた/起きた時刻
    captured_by  uuid REFERENCES actors(id) ON DELETE SET NULL,
    captured_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id)
);

-- ---------------------------------------------------------------------------
-- 関係（エッジ）
-- 同じ2者の間に複数種類の関係を持てる（同僚かつ友人、など）。
-- ---------------------------------------------------------------------------
CREATE TABLE relationships (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid NOT NULL,
    relation_type_id  uuid NOT NULL REFERENCES relation_types(id),
    source_entity_id  uuid NOT NULL,
    target_entity_id  uuid NOT NULL,
    valid_from        date,              -- 関係が始まった日（不明なら NULL）
    valid_to          date,              -- 終わった日（継続中なら NULL）
    source_id         uuid,
    recorded_by       uuid REFERENCES actors(id) ON DELETE SET NULL,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, source_entity_id) REFERENCES entities (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, target_entity_id) REFERENCES entities (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, source_id) REFERENCES sources (workspace_id, id) ON DELETE SET NULL (source_id),
    CHECK (source_entity_id <> target_entity_id),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to)
);
CREATE INDEX relationships_source ON relationships (source_entity_id);
CREATE INDEX relationships_target ON relationships (target_entity_id);

-- ---------------------------------------------------------------------------
-- やり取り（関係の強さを算出する材料）
-- ---------------------------------------------------------------------------
CREATE TABLE interactions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    occurred_at   timestamptz NOT NULL,
    ended_at      timestamptz,
    -- stream: 配信（コラボ配信を含む）/ stream_chat: 配信コメント・スパチャへの反応
    channel       text NOT NULL CHECK (channel IN (
                      'in_person', 'call', 'video', 'chat', 'email', 'sns', 'event',
                      'stream', 'stream_chat', 'other')),
    -- 量の目安。チャット1往復とミーティング2時間を同列に数えないため
    depth         smallint CHECK (depth BETWEEN 1 AND 3),   -- 1:挨拶程度 2:用件 3:深い会話
    summary       text,
    source_id     uuid,
    recorded_by   uuid REFERENCES actors(id) ON DELETE SET NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, source_id) REFERENCES sources (workspace_id, id) ON DELETE SET NULL (source_id),
    CHECK (ended_at IS NULL OR ended_at >= occurred_at)
);
CREATE INDEX interactions_occurred_at ON interactions (occurred_at);

CREATE TABLE interaction_participants (
    workspace_id    uuid NOT NULL,
    interaction_id  uuid NOT NULL,
    entity_id       uuid NOT NULL,
    role            text NOT NULL DEFAULT 'participant'
                    CHECK (role IN ('initiator', 'participant', 'mentioned')),
    PRIMARY KEY (interaction_id, entity_id),
    FOREIGN KEY (workspace_id, interaction_id) REFERENCES interactions (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities (workspace_id, id) ON DELETE CASCADE
);
CREATE INDEX interaction_participants_entity ON interaction_participants (entity_id);

-- ---------------------------------------------------------------------------
-- 主張（Fact）
-- ノード・エッジのコンテキストは、すべて「誰が・何を根拠に・どの確度で言ったか」付きの
-- 主張として積む。上書きせず、訂正は retracted_at と新しい行で表す（二時間軸）。
-- ---------------------------------------------------------------------------
CREATE TABLE facts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL,
    entity_id        uuid,
    relationship_id  uuid,
    field_id         uuid NOT NULL REFERENCES field_definitions(id),
    value            jsonb,                       -- 型は field_definitions.value_type で検証
    value_entity_id  uuid,                        -- entity_ref 用（同じワークスペース内のノード）
    -- stated: 本人が言った / observed: 自分が見聞きした / reported: 第三者から聞いた / inferred: 推測
    epistemic        text NOT NULL CHECK (epistemic IN ('stated', 'observed', 'reported', 'inferred')),
    confidence       real NOT NULL DEFAULT 0.8 CHECK (confidence > 0 AND confidence <= 1),
    valid_from       date,                         -- 現実世界で真になった日
    valid_to         date,                         -- 真でなくなった日
    source_id        uuid,
    note             text,
    recorded_by      uuid REFERENCES actors(id) ON DELETE SET NULL,
    recorded_at      timestamptz NOT NULL DEFAULT now(),   -- DB に記録された時刻
    retracted_at     timestamptz,                          -- 誤りとして取り消された時刻
    retracted_by     uuid REFERENCES actors(id) ON DELETE SET NULL,
    FOREIGN KEY (workspace_id, entity_id) REFERENCES entities (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, relationship_id) REFERENCES relationships (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, value_entity_id) REFERENCES entities (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, source_id) REFERENCES sources (workspace_id, id) ON DELETE SET NULL (source_id),
    CHECK (num_nonnulls(entity_id, relationship_id) = 1),
    CHECK (num_nonnulls(value, value_entity_id) = 1),
    CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_from <= valid_to)
);
CREATE INDEX facts_entity ON facts (entity_id, field_id) WHERE retracted_at IS NULL;
CREATE INDEX facts_relationship ON facts (relationship_id, field_id) WHERE retracted_at IS NULL;
CREATE INDEX facts_value_entity ON facts (value_entity_id) WHERE value_entity_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 派生値（分析結果のキャッシュ）。いつでも消して再計算できる。
-- ---------------------------------------------------------------------------
CREATE TABLE analysis_runs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    algorithm    text NOT NULL,        -- 例: 'tie_strength_v1', 'louvain'
    params       jsonb NOT NULL DEFAULT '{}',
    computed_at  timestamptz NOT NULL DEFAULT now()
);

-- 2者間の強度。entity_a < entity_b に正規化して無向で持つ。
CREATE TABLE pair_metrics (
    run_id               uuid NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    entity_a             uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    entity_b             uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    interaction_count    integer NOT NULL,
    last_interaction_at  timestamptz,
    reciprocity          real,          -- 0..1。どちらか一方だけが働きかけていると低い
    common_neighbors     integer,       -- 埋め込み度
    derived_strength     real NOT NULL, -- 0..1。やり取りから算出
    manual_strength      real,          -- 0..1。closeness_rating 主張から正規化
    PRIMARY KEY (run_id, entity_a, entity_b),
    CHECK (entity_a < entity_b)
);

CREATE TABLE cluster_memberships (
    run_id      uuid NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    entity_id   uuid NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    cluster_id  integer NOT NULL,
    PRIMARY KEY (run_id, entity_id)
);

-- ---------------------------------------------------------------------------
-- 監査ログ（エージェントが何をしたかを人が追える）
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,
    actor_id    uuid REFERENCES actors(id) ON DELETE SET NULL,
    workspace_id uuid REFERENCES workspaces(id) ON DELETE CASCADE,
    action      text NOT NULL,          -- create / update / retract / merge / erase ...
    target      text NOT NULL,          -- テーブル名
    target_id   uuid,
    entity_ids  uuid[] NOT NULL DEFAULT '{}',  -- 削除時にまとめて消すため、関係する人を記録
    detail      jsonb,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_entity_ids ON audit_log USING gin (entity_ids);

-- ===========================================================================
-- 関数
-- ===========================================================================

-- フィールド名・関係名の揺れを吸収するための正規化（全角→半角、小文字、区切り統一）
CREATE FUNCTION normalize_label(t text) RETURNS text
LANGUAGE sql IMMUTABLE
SET search_path = hre, public AS $$
    SELECT trim(both '_' FROM
           regexp_replace(lower(normalize(t, NFKC)), '[\s\-\.・/]+', '_', 'g'))
$$;

-- 入力文字列（キー・別名・表示名）→ 使うべきフィールド。merged なら統合先まで辿る。見つからなければ NULL。
CREATE FUNCTION resolve_field(name text) RETURNS field_definitions
LANGUAGE plpgsql STABLE
SET search_path = hre, public AS $$
DECLARE
    f field_definitions;
BEGIN
    SELECT * INTO f FROM field_definitions WHERE key = normalize_label(name);
    IF NOT FOUND THEN
        SELECT fd.* INTO f FROM field_aliases a JOIN field_definitions fd ON fd.id = a.field_id
        WHERE a.alias = normalize_label(name);
    END IF;
    IF NOT FOUND THEN   -- 表示名そのものでも引ける（統合済みは除く）
        SELECT * INTO f FROM field_definitions
         WHERE normalize_label(label) = normalize_label(name) AND status <> 'merged'
         ORDER BY status = 'approved' DESC LIMIT 1;
    END IF;
    WHILE FOUND AND f.status = 'merged' LOOP
        SELECT * INTO f FROM field_definitions WHERE id = f.merged_into_id;
    END LOOP;
    IF f.id IS NULL THEN RETURN NULL; END IF;
    RETURN f;
END $$;

-- 未登録の名前に近い既存フィールドの候補（エージェントが新規作成する前に必ず呼ぶ）
CREATE FUNCTION suggest_fields(name text, max_results int DEFAULT 5)
RETURNS TABLE (key text, label text, matched text, score real)
LANGUAGE sql STABLE
SET search_path = hre, public AS $$
    SELECT DISTINCT ON (fd.key) fd.key, fd.label, c.matched, c.score
    FROM (
        SELECT id AS field_id, key AS matched, similarity(key, normalize_label(name)) AS score
          FROM field_definitions
        UNION ALL
        SELECT id, label, similarity(label, name) FROM field_definitions
        UNION ALL
        SELECT field_id, alias, similarity(alias, normalize_label(name)) FROM field_aliases
    ) c
    JOIN field_definitions fd ON fd.id = c.field_id
    WHERE fd.status IN ('proposed', 'approved') AND c.score > 0.2
    ORDER BY fd.key, c.score DESC
    LIMIT max_results
$$;

-- 人が書いたか、信頼できる出典に基づく書き込みか
CREATE FUNCTION is_trusted_write(recorded_by uuid, source_id uuid) RETURNS boolean
LANGUAGE sql STABLE
SET search_path = hre, public AS $$
    SELECT coalesce((SELECT kind = 'human' FROM actors WHERE id = recorded_by), false)
        OR coalesce((SELECT trusted FROM sources WHERE id = source_id), false)
$$;

-- facts の整合性チェック（型・適用対象・状態・機微情報の推測禁止）
CREATE FUNCTION check_fact() RETURNS trigger
LANGUAGE plpgsql
SET search_path = hre, public AS $$
DECLARE
    f field_definitions;
    subject_kind text;
    v jsonb := NEW.value;
    ok boolean;
BEGIN
    SELECT * INTO f FROM field_definitions WHERE id = NEW.field_id;

    IF f.status IN ('deprecated', 'merged') THEN
        RAISE EXCEPTION 'field "%" is %; use resolve_field() to find the current field', f.key, f.status;
    END IF;

    IF NEW.entity_id IS NOT NULL THEN
        SELECT kind INTO subject_kind FROM entities WHERE id = NEW.entity_id;
    ELSE
        subject_kind := 'relationship';
    END IF;
    IF (subject_kind = ANY (f.applies_to)) IS NOT TRUE THEN
        RAISE EXCEPTION 'field "%" does not apply to %', f.key, subject_kind;
    END IF;

    IF f.requires_trusted_source AND NOT is_trusted_write(NEW.recorded_by, NEW.source_id) THEN
        RAISE EXCEPTION 'field "%" can only be written by a human or from a trusted source', f.key;
    END IF;

    IF f.sensitivity = 'sensitive' AND NEW.epistemic = 'inferred' THEN
        RAISE EXCEPTION 'field "%" is sensitive and must not be inferred', f.key;
    END IF;

    IF f.value_type = 'entity_ref' THEN
        IF NEW.value_entity_id IS NULL THEN
            RAISE EXCEPTION 'field "%" requires value_entity_id', f.key;
        END IF;
        RETURN NEW;
    ELSIF NEW.value_entity_id IS NOT NULL THEN
        RAISE EXCEPTION 'field "%" is % and cannot take value_entity_id', f.key, f.value_type;
    END IF;

    ok := CASE f.value_type
        WHEN 'number'   THEN jsonb_typeof(v) = 'number'
        WHEN 'integer'  THEN jsonb_typeof(v) = 'number' AND (v #>> '{}')::numeric % 1 = 0
        WHEN 'boolean'  THEN jsonb_typeof(v) = 'boolean'
        -- YYYY / YYYY-MM / YYYY-MM-DD / --MM-DD（年不明）
        WHEN 'date'     THEN jsonb_typeof(v) = 'string'
                             AND (v #>> '{}') ~ '^(\d{4}(-\d{2}(-\d{2})?)?|--\d{2}-\d{2})$'
        WHEN 'datetime' THEN jsonb_typeof(v) = 'string' AND pg_input_is_valid(v #>> '{}', 'timestamptz')
        WHEN 'enum'     THEN jsonb_typeof(v) = 'string' AND (v #>> '{}') = ANY (f.enum_options)
        WHEN 'json'     THEN true
        ELSE                 jsonb_typeof(v) = 'string'   -- text, url, email, phone
    END;
    IF ok IS NOT TRUE THEN
        RAISE EXCEPTION 'value % does not match type % of field "%"', v, f.value_type, f.key;
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER facts_check BEFORE INSERT OR UPDATE OF field_id, value, value_entity_id, epistemic, entity_id, relationship_id, source_id, recorded_by
    ON facts FOR EACH ROW EXECUTE FUNCTION check_fact();

-- 関係の両端の種類チェック
CREATE FUNCTION check_relationship() RETURNS trigger
LANGUAGE plpgsql
SET search_path = hre, public AS $$
DECLARE
    rt relation_types;
    sk text;
    tk text;
BEGIN
    SELECT * INTO rt FROM relation_types WHERE id = NEW.relation_type_id;
    SELECT kind INTO sk FROM entities WHERE id = NEW.source_entity_id;
    SELECT kind INTO tk FROM entities WHERE id = NEW.target_entity_id;
    IF rt.status = 'deprecated' THEN
        RAISE EXCEPTION 'relation type "%" is deprecated', rt.key;
    END IF;
    IF rt.requires_trusted_source AND NOT is_trusted_write(NEW.recorded_by, NEW.source_id) THEN
        RAISE EXCEPTION 'relation type "%" can only be written by a human or from a trusted source', rt.key;
    END IF;
    IF (sk = ANY (rt.source_kinds) AND tk = ANY (rt.target_kinds)) IS NOT TRUE THEN
        RAISE EXCEPTION 'relation type "%" does not connect % -> %', rt.key, sk, tk;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER relationships_check BEFORE INSERT OR UPDATE
    ON relationships FOR EACH ROW EXECUTE FUNCTION check_relationship();

-- 同一人物の統合: from の記録をすべて into に付け替え、from は merged として残す
CREATE FUNCTION merge_entities(from_id uuid, into_id uuid, by_actor uuid) RETURNS void
LANGUAGE plpgsql
SET search_path = hre, public AS $$
BEGIN
    IF from_id = into_id THEN RAISE EXCEPTION 'cannot merge an entity into itself'; END IF;
    IF (SELECT workspace_id FROM entities WHERE id = from_id)
       IS DISTINCT FROM (SELECT workspace_id FROM entities WHERE id = into_id) THEN
        RAISE EXCEPTION 'cannot merge entities across workspaces; use entity_links instead';
    END IF;

    UPDATE entity_identifiers SET entity_id = into_id
     WHERE entity_id = from_id
       AND NOT EXISTS (SELECT 1 FROM entity_identifiers x
                        WHERE x.entity_id = into_id AND x.scheme = entity_identifiers.scheme
                          AND x.value = entity_identifiers.value);
    DELETE FROM entity_identifiers WHERE entity_id = from_id;

    UPDATE facts SET entity_id = into_id WHERE entity_id = from_id;
    UPDATE facts SET value_entity_id = into_id WHERE value_entity_id = from_id;

    UPDATE relationships SET source_entity_id = into_id WHERE source_entity_id = from_id;
    UPDATE relationships SET target_entity_id = into_id WHERE target_entity_id = from_id;
    DELETE FROM relationships WHERE source_entity_id = target_entity_id;  -- 統合で自己ループになったもの

    INSERT INTO interaction_participants (workspace_id, interaction_id, entity_id, role)
        SELECT workspace_id, interaction_id, into_id, role FROM interaction_participants WHERE entity_id = from_id
        ON CONFLICT DO NOTHING;
    DELETE FROM interaction_participants WHERE entity_id = from_id;

    UPDATE entities SET merged_into_id = into_id, updated_at = now() WHERE id = from_id;

    INSERT INTO audit_log (actor_id, workspace_id, action, target, target_id, entity_ids, detail)
        VALUES (by_actor, (SELECT workspace_id FROM entities WHERE id = into_id),
                'merge', 'entities', into_id, ARRAY[from_id, into_id],
                jsonb_build_object('from', from_id, 'into', into_id));
END $$;

-- 完全削除（「この人の情報を全部消す」）。関係・主張・参加記録・監査ログまで消し、
-- その人の記録からしか参照されなくなった出典の原文も消す。
-- include_linked = true なら entity_links でつながった他ワークスペースの同一人物もまとめて消す。
CREATE FUNCTION erase_entity(target uuid, include_linked boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql
SET search_path = hre, public AS $$
DECLARE
    ids uuid[];
    src uuid[];
    linked uuid;
BEGIN
    IF include_linked THEN
        FOR linked IN
            WITH RECURSIVE g(id) AS (
                SELECT target
                UNION
                SELECT CASE WHEN l.entity_a = g.id THEN l.entity_b ELSE l.entity_a END
                  FROM entity_links l JOIN g ON g.id IN (l.entity_a, l.entity_b)
            )
            SELECT id FROM g WHERE id <> target
        LOOP
            PERFORM erase_entity(linked, false);
        END LOOP;
    END IF;

    SELECT array_agg(id) INTO ids FROM entities WHERE id = target OR merged_into_id = target;
    IF ids IS NULL THEN RAISE EXCEPTION 'entity % not found', target; END IF;

    SELECT coalesce(array_agg(DISTINCT s), '{}') INTO src FROM (
        SELECT source_id FROM facts WHERE entity_id = ANY (ids) OR value_entity_id = ANY (ids)
        UNION ALL
        SELECT source_id FROM relationships WHERE source_entity_id = ANY (ids) OR target_entity_id = ANY (ids)
        UNION ALL
        SELECT i.source_id FROM interactions i JOIN interaction_participants p ON p.interaction_id = i.id
         WHERE p.entity_id = ANY (ids)
    ) x(s) WHERE s IS NOT NULL;

    -- 自分以外にはその人しか参加していないやり取りは丸ごと消す
    DELETE FROM interactions i
     WHERE EXISTS (SELECT 1 FROM interaction_participants p WHERE p.interaction_id = i.id AND p.entity_id = ANY (ids))
       AND NOT EXISTS (SELECT 1 FROM interaction_participants p JOIN entities e ON e.id = p.entity_id
                        WHERE p.interaction_id = i.id AND p.entity_id <> ALL (ids) AND NOT e.is_self);

    DELETE FROM entities WHERE id = ANY (ids);         -- 主張・関係・識別子・参加記録は CASCADE

    DELETE FROM sources s
     WHERE s.id = ANY (src)
       AND NOT EXISTS (SELECT 1 FROM facts WHERE source_id = s.id)
       AND NOT EXISTS (SELECT 1 FROM relationships WHERE source_id = s.id)
       AND NOT EXISTS (SELECT 1 FROM interactions WHERE source_id = s.id);

    DELETE FROM audit_log WHERE entity_ids && ids;
    INSERT INTO audit_log (action, target, detail)
        VALUES ('erase', 'entities', jsonb_build_object('count', array_length(ids, 1)));
END $$;

-- ===========================================================================
-- ビュー
-- ===========================================================================

-- 現在有効な主張（取り消されておらず、有効期間内）。人が参照するときの基本ビュー。
CREATE VIEW current_facts AS
SELECT f.*, fd.key AS field_key, fd.label AS field_label, fd.value_type,
       fd.cardinality, fd.sensitivity, fd.ai_shareable
  FROM facts f
  JOIN field_definitions fd ON fd.id = f.field_id
 WHERE f.retracted_at IS NULL
   AND (f.valid_from IS NULL OR f.valid_from <= current_date)
   AND (f.valid_to IS NULL OR f.valid_to > current_date);

-- cardinality = one のフィールドは、確度が高く新しいものを1件だけ採用した「人物カード」
CREATE VIEW entity_profile AS
SELECT DISTINCT ON (cf.entity_id, cf.field_id)
       cf.workspace_id, cf.entity_id, e.display_name, cf.field_key, cf.field_label,
       cf.value, cf.value_entity_id, cf.epistemic, cf.confidence, cf.recorded_at, cf.sensitivity
  FROM current_facts cf
  JOIN entities e ON e.id = cf.entity_id
 WHERE cf.cardinality = 'one' AND e.merged_into_id IS NULL
 ORDER BY cf.entity_id, cf.field_id,
          CASE cf.epistemic WHEN 'stated' THEN 0 WHEN 'observed' THEN 1 WHEN 'reported' THEN 2 ELSE 3 END,
          cf.confidence DESC, cf.recorded_at DESC;

-- 現在続いている関係を両方向から引けるようにしたもの
CREATE VIEW active_edges AS
SELECT r.workspace_id, r.id AS relationship_id, rt.key AS relation_type, rt.label, rt.directed,
       r.source_entity_id AS from_id, r.target_entity_id AS to_id, 'forward' AS direction
  FROM relationships r JOIN relation_types rt ON rt.id = r.relation_type_id
 WHERE r.valid_to IS NULL OR r.valid_to > current_date
UNION ALL
SELECT r.workspace_id, r.id, rt.key, coalesce(rt.inverse_label, rt.label), rt.directed,
       r.target_entity_id, r.source_entity_id, 'inverse'
  FROM relationships r JOIN relation_types rt ON rt.id = r.relation_type_id
 WHERE r.valid_to IS NULL OR r.valid_to > current_date;

-- ワークスペースから読める範囲（自分自身＋共有されたもの）。API はこれで絞り込む。
CREATE VIEW readable_workspaces AS
SELECT id AS reader_workspace_id, id AS workspace_id FROM workspaces
UNION ALL
SELECT reader_workspace_id, workspace_id FROM workspace_shares;
