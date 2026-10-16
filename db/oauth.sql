-- claude.ai など OAuth で接続するクライアントのための表（MCP サーバーの OAuth 認可サーバー）。
-- 何度流しても同じ結果になるように書く。hre-mcp --http が起動時に毎回適用するので、
-- 既存の DB にも手作業なしで追加される。トークン・コードはハッシュだけを保存する。

CREATE SCHEMA IF NOT EXISTS hre;

-- 動的クライアント登録（RFC 7591）で登録されたクライアント
CREATE TABLE IF NOT EXISTS hre.oauth_clients (
    client_id   text PRIMARY KEY,
    info        jsonb NOT NULL,                    -- OAuthClientInformationFull
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- /authorize で受けて、人が Web UI の /connect で許可するのを待っている認可リクエスト
CREATE TABLE IF NOT EXISTS hre.oauth_requests (
    id_hash     text PRIMARY KEY,
    client_id   text NOT NULL REFERENCES hre.oauth_clients(client_id) ON DELETE CASCADE,
    params      jsonb NOT NULL,                    -- AuthorizationParams
    expires_at  timestamptz NOT NULL
);

-- 認可コード・アクセストークン・リフレッシュトークン。
-- actor_id は接続先の身元（許可した人が選んだ actor）、approved_by は許可した人。
-- actor_token_hash は発行時の actors.token_hash。トークンを rotate / revoke すると一致しなくなり、
-- ここから発行したものもすべて使えなくなる（Web UI のセッションと同じ扱い）
CREATE TABLE IF NOT EXISTS hre.oauth_grants (
    token_hash        text PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN ('code', 'access', 'refresh')),
    client_id         text NOT NULL REFERENCES hre.oauth_clients(client_id) ON DELETE CASCADE,
    actor_id          uuid NOT NULL REFERENCES hre.actors(id) ON DELETE CASCADE,
    actor_token_hash  text NOT NULL,
    family            text NOT NULL,                -- 1回の認可から続くコード・トークンの組。取り消しはこの単位
    params            jsonb NOT NULL DEFAULT '{}',  -- code: code_challenge / redirect_uri など。access・refresh: scopes / resource
    expires_at        timestamptz NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS oauth_grants_family_idx ON hre.oauth_grants (family);

-- 許可した人（Web UI の /connect でログインしていた人）。この列を足す前の行は NULL
ALTER TABLE hre.oauth_grants ADD COLUMN IF NOT EXISTS approved_by uuid REFERENCES hre.actors(id) ON DELETE SET NULL;
