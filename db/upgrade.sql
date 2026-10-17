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
