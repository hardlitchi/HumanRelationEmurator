# docker compose で動かす

PostgreSQL・エージェント用 MCP サーバー（`hre-mcp`）・人のための Web UI（`hre-web`）をまとめて起動する。VPS に Docker（Compose v2）が入っていればよい。

| サービス | 中身 | ホスト側のポート |
|---|---|---|
| `db` | PostgreSQL 16。データは `db-data` ボリューム | 出さない |
| `mcp` | `hre-mcp --http`。エンドポイントは `/mcp` | `127.0.0.1:8000` |
| `web` | `hre-web` | `127.0.0.1:8001` |
| `caddy`（任意） | TLS 終端。`--profile caddy` のときだけ起動 | `80` / `443` |

## 1. 設定ファイルを作る

```sh
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^HRE_WEB_SECRET=.*/HRE_WEB_SECRET=$(openssl rand -hex 32)/" .env
```

`HRE_DOMAIN` を公開するドメインに書き換える。各項目の意味と生成方法は [`.env.example`](../.env.example) に書いてある。

## 2. 起動する

VPS に Caddy や nginx をまだ入れていないなら、同梱の Caddy ごと起動する（DNS の A レコードを VPS に向け、80/443 を開けておく。証明書は自動で取れる）。

```sh
docker compose --profile caddy up -d --build
```

すでにホスト側にリバースプロキシがあるなら、Caddy なしで起動して `127.0.0.1:8000`（`/mcp`）と `127.0.0.1:8001`（それ以外）へ流す（設定例は [Web UI](web-ui.md#vps-で-mcp-サーバーと同じドメインに置く例caddy)）。

```sh
docker compose up -d --build
```

DB のスキーマと初期マスターは、`db-data` ボリュームが空の初回起動時に自動で入る（`hre-admin init-db` は不要）。

## 3. 管理者とエージェントを作る

`hre-admin` はコンテナの中で実行する。トークンは1度だけ表示されるので控えておく。

```sh
docker compose exec mcp hre-admin create-workspace owner "管理者" owner
docker compose exec mcp hre-admin create-actor ta human --admin       # Web UI のログイン用
docker compose exec mcp hre-admin grant ta owner read,write,sensitive_read

docker compose exec mcp hre-admin create-actor owner-agent agent      # エージェント用
docker compose exec mcp hre-admin grant owner-agent owner read,write
```

AITuber のワークスペースや共有の作り方は [MCP サーバー](mcp-server.md#セットアップ) と同じ。管理者を作ったあとは Web UI の「アクセス」からも追加できる。

- スマホ: `https://<HRE_DOMAIN>/` を開いて管理者のトークンでログイン
- エージェント: `claude mcp add --transport http hre https://<HRE_DOMAIN>/mcp --header "Authorization: Bearer hre_..."`

## 手元で試す

`.env` を次のようにすると、TLS なしで `http://localhost:8001/`（Web UI）と `http://localhost:8000/mcp` が使える。

```sh
HRE_DOMAIN=localhost
HRE_PUBLIC_URL=http://localhost:8000
HRE_INSECURE_COOKIES=1
```

## 運用

```sh
docker compose logs -f mcp web                       # ログ
git pull && docker compose up -d --build             # 更新（DB のデータは残る）
docker compose exec -T db pg_dump -U hre -Fc hre > hre-$(date +%F).dump   # バックアップ
docker compose exec -T db pg_restore -U hre -d hre --clean < hre-YYYY-MM-DD.dump   # 復元
```

- `db/schema.sql` を変更しても、既存の DB には自動では反映されない（初回だけ実行されるため）
- `docker compose down -v` はボリュームごと消すので、DB の中身も消える。止めるだけなら `docker compose down`
- `.env` には DB のパスワードと Cookie の署名鍵が入るので、git に入れず、権限を絞る（`chmod 600 .env`）
