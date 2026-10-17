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
