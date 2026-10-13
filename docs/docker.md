# docker compose で動かす

PostgreSQL・エージェント用 MCP サーバー（`hre-mcp`）・人のための Web UI（`hre-web`）をまとめて起動する。VPS に Docker（Compose v2）が入っていればよい。

TLS は外部のリバースプロキシ（VPS の nginx・Caddy・Cloudflare Tunnel など）で終端する前提で、ホストに出すポートは `HRE_PORT`（既定 8080）の1つだけ。その中で `/mcp`・`/api/*` と OAuth の各エンドポイントは MCP サーバーへ、それ以外は Web UI へ振り分ける。

| サービス | 中身 | ホストに出すポート |
|---|---|---|
| `db` | PostgreSQL 16。データは `db-data` ボリューム | なし |
| `mcp` | `hre-mcp --http`。エンドポイントは `/mcp` | なし |
| `web` | `hre-web` | なし |
| `proxy` | Caddy（http のみ）。`/mcp`・`/api/*` と OAuth（`/.well-known/oauth-*`・`/authorize`・`/token`・`/register`・`/revoke`・`/oauth/*`）を `mcp` へ、それ以外を `web` へ | `127.0.0.1:${HRE_PORT}` |

## 1. 設定ファイルを作る

```sh
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
sed -i "s/^HRE_WEB_SECRET=.*/HRE_WEB_SECRET=$(openssl rand -hex 32)/" .env
```

`HRE_DOMAIN` を公開するドメインに書き換える。各項目の意味と生成方法は [`.env.example`](../.env.example) に書いてある。

## 2. 起動する

```sh
docker compose up -d --build
```

外部のリバースプロキシは、`https://<HRE_DOMAIN>/` へのアクセスをすべて `http://127.0.0.1:<HRE_PORT>` へ転送する。パスで分ける必要はない。Host ヘッダはそのまま渡すこと（`HRE_DOMAIN` と一致しないアクセスは拒否される）。

nginx の例:

```nginx
server {
    listen 443 ssl;
    server_name hre.example.com;
    # ssl_certificate ... は環境に合わせる

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_buffering off;          # MCP のストリーミング応答をため込まない
        proxy_read_timeout 1h;
    }
}
```

Caddy の例:

```
hre.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

DB のスキーマと初期マスターは、`db-data` ボリュームが空の初回起動時に自動で入る（`hre-admin init-db` は不要）。

## 3. 最初の管理者を作る

起動しただけでは誰もログインできない。最初の1回だけ次を実行すると、管理者本人のワークスペース `owner` と管理者が作られ、ログイン用のトークン（`hre_` で始まる文字列）が表示される。

```sh
docker compose exec mcp hre-admin bootstrap ta     # ta は管理者の名前（省略すると admin）
```

- トークンが表示されるのはこの1回だけ。パスワードマネージャーなどに保存する
- なくしたら `docker compose exec mcp hre-admin rotate-token ta` で再発行（古いトークンとそのログインは無効になる）
- 管理者がすでにいるときは何もしない。2人目以降の利用者やエージェントは、Web UI の「アクセス」から追加する

エージェント用のトークンをコマンドで作る場合:

```sh
docker compose exec mcp hre-admin create-actor owner-agent agent
docker compose exec mcp hre-admin grant owner-agent owner read,write
```

AITuber のワークスペースや共有の作り方は [MCP サーバー](mcp-server.md#セットアップ) と同じ。管理者を作ったあとは Web UI の「アクセス」からも追加できる。

- スマホ: `https://<HRE_DOMAIN>/` を開いて管理者のトークンでログイン
- エージェント: `claude mcp add --transport http hre https://<HRE_DOMAIN>/mcp --header "Authorization: Bearer hre_..."`

## 手元で試す

`.env` を次のようにすると、TLS なしで `http://localhost:8080/`（Web UI）と `http://localhost:8080/mcp` が使える。

```sh
HRE_DOMAIN=localhost
HRE_PUBLIC_URL=http://localhost:8080
HRE_INSECURE_COOKIES=1
```

## 運用

```sh
docker compose logs -f mcp web proxy                      # ログ
git pull && docker compose up -d --build             # 更新（DB のデータは残る）
docker compose exec -T db pg_dump -U hre -Fc hre > hre-$(date +%F).dump   # バックアップ
docker compose exec -T db pg_restore -U hre -d hre --clean < hre-YYYY-MM-DD.dump   # 復元
```

- `db/schema.sql` を変更しても、既存の DB には自動では反映されない（初回だけ実行されるため）
- `docker compose down -v` はボリュームごと消すので、DB の中身も消える。止めるだけなら `docker compose down`
- `.env` には DB のパスワードと Cookie の署名鍵が入るので、git に入れず、権限を絞る（`chmod 600 .env`）
