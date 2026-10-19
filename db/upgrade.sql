-- 既存の DB を今の seed.sql に追いつかせる変更。何度流しても同じ結果になるように書く。
-- hre-mcp --http が起動時に oauth.sql と一緒に毎回適用するので、手作業は要らない。

-- 運営・管理者は人だけでなく、組織（事務所・グループ）やグループも運営できる（2026-10-16）
UPDATE hre.relation_types SET target_kinds = '{person,organization,group}'
 WHERE key = 'operator_of' AND NOT (target_kinds @> '{organization,group}');

-- 人の確認（Web UI の「確認」）: 出典を確認済みにした時刻と、エージェントが書けなかった書き込みの確認待ち（2026-10-17）
ALTER TABLE hre.sources ADD COLUMN IF NOT EXISTS reviewed_at timestamptz;

-- 人の確認待ち: エージェントが保護フィールド・管理者関係を書こうとして拒否された書き込み。
-- 人が Web UI の「確認」で承認すると、その人の書き込みとして入る（却下なら入らない）
CREATE TABLE IF NOT EXISTS hre.review_requests (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  uuid NOT NULL REFERENCES hre.workspaces(id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('fact', 'relationship')),
    payload       jsonb NOT NULL,                  -- record_fact / add_relationship の引数
    entity_ids    uuid[] NOT NULL DEFAULT '{}',    -- 関係する人（完全削除のときにまとめて消す）
    requested_by  uuid REFERENCES hre.actors(id) ON DELETE SET NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by    uuid REFERENCES hre.actors(id) ON DELETE SET NULL,
    decided_at    timestamptz,
    result_id     uuid                             -- 承認で作った fact / relationship
);
CREATE INDEX IF NOT EXISTS review_requests_pending ON hre.review_requests (workspace_id) WHERE status = 'pending';

-- パスキー（WebAuthn）でのログイン（2026-10-17）。人間（kind = human）の actor だけが登録できる。
-- 公開鍵だけを保存する。ログインで発行するセッションは、その時点の actors.token_hash に結びつくので、
-- トークンを rotate すると既存のセッションは切れる（パスキーはそのまま使える）。revoke した actor は入れない
CREATE TABLE IF NOT EXISTS hre.passkeys (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id       uuid NOT NULL REFERENCES hre.actors(id) ON DELETE CASCADE,
    credential_id  bytea NOT NULL UNIQUE,
    public_key     bytea NOT NULL,                 -- COSE 形式
    sign_count     bigint NOT NULL DEFAULT 0,
    transports     text[] NOT NULL DEFAULT '{}',
    name           text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_used_at   timestamptz
);
CREATE INDEX IF NOT EXISTS passkeys_actor_idx ON hre.passkeys (actor_id);

-- 登録・ログインの途中で発行した challenge。1回使うと消す（id はハッシュだけ保存）
CREATE TABLE IF NOT EXISTS hre.passkey_challenges (
    id_hash     text PRIMARY KEY,
    purpose     text NOT NULL CHECK (purpose IN ('register', 'login')),
    actor_id    uuid REFERENCES hre.actors(id) ON DELETE CASCADE,   -- register のときの登録する人
    challenge   bytea NOT NULL,
    expires_at  timestamptz NOT NULL
);

-- ---------------------------------------------------------------------------
-- オントロジー整備（2026-10-19、docs/ontology.md）
-- 列・表・新しいフィールドの追加は何度流しても同じ。既存のマスターの値を直すものは
-- hre.schema_migrations に名前を残して一度だけ流す（管理者が後から変えた値を起動のたびに戻さないため）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hre.schema_migrations (
    name        text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- 関係の種類の説明（source / target がそれぞれ何か）と、相手の役割を表す other_label
ALTER TABLE hre.relation_types ADD COLUMN IF NOT EXISTS description text;

CREATE OR REPLACE VIEW hre.active_edges AS
SELECT r.workspace_id, r.id AS relationship_id, rt.key AS relation_type, rt.label, rt.directed,
       r.source_entity_id AS from_id, r.target_entity_id AS to_id, 'forward' AS direction,
       coalesce(rt.inverse_label, rt.label) AS other_label
  FROM hre.relationships r JOIN hre.relation_types rt ON rt.id = r.relation_type_id
 WHERE r.valid_to IS NULL OR r.valid_to > current_date
UNION ALL
SELECT r.workspace_id, r.id, rt.key, coalesce(rt.inverse_label, rt.label), rt.directed,
       r.target_entity_id, r.source_entity_id, 'inverse', rt.label
  FROM hre.relationships r JOIN hre.relation_types rt ON rt.id = r.relation_type_id
 WHERE r.valid_to IS NULL OR r.valid_to > current_date;

-- 新しいフィールド。同じキーが（エージェントの提案などで）既にあればそちらを残す。
-- 別名は、ほかのフィールドのキー・別名として使われていないものだけ足す
WITH seed (key, label, value_type, enum_options, applies_to, cardinality, sensitivity, ai_shareable,
           requires_trusted_source, description, aliases) AS (
    VALUES
    ('name_reading', '読み', 'text', NULL::text[], '{person,organization,group}'::text[], 'one', 'normal', true, false,
        '表示名の読み（ひらがな）。読み上げ（TTS）と検索に使う', '{よみ,読み方,ふりがな,フリガナ,furigana,kana,reading}'::text[]),
    ('legal_name', '本名', 'text', NULL, '{person}', 'one', 'sensitive', false, true,
        '戸籍・公的書類の氏名。芸名・キャラ名・呼び名は表示名か name 識別子に入れる。VTuber・AITuber・リスナーの本名は記録しない',
        '{本名,氏名,戸籍名,real_name}'),
    ('skills', 'スキル・特技', 'text', NULL, '{person}', 'many', 'personal', true, false,
        '1件につき1つ。仕事の技能も趣味の特技も含む。職種は occupation', '{スキル,特技,得意なこと,得意,skill}'),
    ('is_minor', '未成年', 'boolean', NULL, '{person}', 'one', 'personal', false, false,
        '本人の発言や明らかな描写から未成年と分かったら true。学生というだけでは付けない。true の人について新しい個人情報は記録しない',
        '{未成年,minor}'),
    ('stream_mention_policy', '配信での扱い', 'enum', '{ok,nickname_only,avoid}', '{person}', 'one', 'normal', true, false,
        'ok: 触れてよい / nickname_only: 呼び方（calls_them）だけで触れ、それ以外の情報は話さない / avoid: 配信では触れない。本人の希望を記録する',
        '{配信での扱い,配信で触れない,名前出しng,配信ng,mention_policy}'),
    ('topics_to_avoid', '避けたい話題（相手）', 'text', NULL, '{person}', 'many', 'personal', true, false,
        'この人と話すとき・この人について話すときに避ける話題。キャラクター自身の NG は ng_topics', '{触れない話題,avoid_topics}'),
    ('kinship_term', '続柄', 'text', NULL, '{relationship}', 'one', 'personal', true, false,
        'family / partner 関係に付ける、自分（ワークスペースの主）から見た相手の続柄。次の語のどれかで書く: '
        '父・母・兄・姉・弟・妹・祖父・祖母・子・孫・叔父・叔母・いとこ・甥・姪・義父・義母・義兄・義姉・義弟・義妹・婚約者・夫・妻・恋人',
        '{続柄,続き柄,kinship}')
)
INSERT INTO hre.field_definitions (key, label, value_type, enum_options, applies_to, cardinality, sensitivity,
                                   ai_shareable, requires_trusted_source, description, status)
SELECT key, label, value_type, enum_options, applies_to, cardinality, sensitivity,
       ai_shareable, requires_trusted_source, description, 'approved'
  FROM seed
ON CONFLICT (key) DO NOTHING;

INSERT INTO hre.field_aliases (alias, field_id)
SELECT DISTINCT ON (hre.normalize_label(a)) hre.normalize_label(a), fd.id
  FROM (VALUES
        ('name_reading', '{よみ,読み方,ふりがな,フリガナ,furigana,kana,reading}'::text[]),
        ('legal_name', '{本名,氏名,戸籍名,real_name}'),
        ('skills', '{スキル,特技,得意なこと,得意,skill}'),
        ('is_minor', '{未成年,minor}'),
        ('stream_mention_policy', '{配信での扱い,配信で触れない,名前出しng,配信ng,mention_policy}'),
        ('topics_to_avoid', '{触れない話題,avoid_topics}'),
        ('kinship_term', '{続柄,続き柄,kinship}')) v(key, aliases)
  JOIN hre.field_definitions fd ON fd.key = v.key, unnest(v.aliases) a
 WHERE hre.normalize_label(a) <> fd.key
   AND NOT EXISTS (SELECT 1 FROM hre.field_definitions o WHERE o.key = hre.normalize_label(a))
ON CONFLICT (alias) DO NOTHING;

-- 一度だけ: 既存のマスターの説明・保護を今の seed.sql にそろえる
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM hre.schema_migrations WHERE name = '2026-10-19-ontology') THEN
        -- エージェントが提案した本名を、身バレ・デマ・外部 AI への流出を防ぐ側に倒す（承認は人が「確認」で行う）
        UPDATE hre.field_definitions
           SET sensitivity = 'sensitive', ai_shareable = false, requires_trusted_source = true, updated_at = now()
         WHERE key = 'legal_name';
        UPDATE hre.field_definitions
           SET description = 'キャラクター（人格）自身が配信で扱わない話題。ほかの人について避ける話題は topics_to_avoid',
               updated_at = now()
         WHERE key = 'ng_topics' AND description IS NULL;
        UPDATE hre.relation_types t SET description = v.description
          FROM (VALUES
            ('knows', 'ほかのどれにも当てはまらない知り合い。より具体的な種類が分かればそちらを使う'),
            ('friend', '友人。向きはない'),
            ('family', '家族・親族。父・母・妹などの続柄は、この関係に kinship_term（自分から見た続柄）で付ける'),
            ('partner', '恋人・婚約者・配偶者。段階は kinship_term（恋人・婚約者・夫・妻）で表し、変わったら新しい主張を足す'),
            ('colleague', '同じ職場・チームで働く人'),
            ('manager_of', 'source が target の上司'),
            ('mentor_of', 'source が target を指導している'),
            ('business_partner', '仕事上の取引の相手'),
            ('works_at', 'source（人）が target（組織）に勤めている・所属している'),
            ('member_of', 'source（人）が target（グループ・ユニット・サークル）のメンバー'),
            ('part_of', 'source が target の一部（子会社・部署・下部ユニット）'),
            ('operator_of', 'source（人）が target（タレント・グループ・組織）を運営・管理している。人か信頼済みの出典からしか書けない'),
            ('collaborated_with', '配信・作品で共演したことがある'),
            ('listener_of', 'source（リスナー）が target（配信者）の配信を見ている・応援している')) v(key, description)
         WHERE t.key = v.key AND t.description IS NULL;
        INSERT INTO hre.schema_migrations (name) VALUES ('2026-10-19-ontology');
    END IF;
END $$;
