# HumanRelationEmurator

人間関係をネットワーク構造で記録・管理する、AIエージェントと人間のためのツール群。

- 人・組織・グループをノード、関係をエッジとして持ち、それぞれにコンテキスト（カスタムフィールド）を積む
- カスタムフィールドはマスターで管理し、エージェントが書いても表記ゆれしないようにする
- やり取りの記録から関係の強さ・クラスタを算出する

管理者本人に加えて、AITuber グループの各メンバーがそれぞれの人格と人間関係（管理者・メンバー・コラボ相手・リスナー）を持てるよう、ワークスペースで視点ごとにデータを分ける。第三者の個人情報を扱うため、機微情報の扱い・外部AIへの送信・完全削除を設計に組み込んでいる。

## ドキュメント

- [要求の妥当性検証](docs/requirements-validation.md)
- [データモデル](docs/data-model.md)
- [エージェント用 MCP サーバー](docs/mcp-server.md)
- [人と管理者のための Web UI](docs/web-ui.md)
- [docker compose で動かす](docs/docker.md)

## docker compose で起動する

```sh
cp .env.example .env   # パスワード・署名鍵・ドメインを埋める（生成方法はファイル内に記載）
docker compose up -d --build
```

詳しくは [docs/docker.md](docs/docker.md)。

## claude.ai から使う

claude.ai（Web・スマホアプリ・デスクトップアプリ）の「カスタムコネクタ」に MCP サーバーを登録すると、チャットから人間関係グラフを読み書きできる。claude.ai のサーバーがインターネット越しに `https://<HRE_DOMAIN>/mcp` へ接続するので、外部のリバースプロキシで HTTPS 公開済みであることが前提。

接続は OAuth で行う。claude.ai が接続を始めるとこのサーバーの認可画面が開き、そこに `hre_` トークンを入力すると、そのトークンの持ち主として読み書きできるようになる。

### 1. claude.ai 用のトークンを作る

Web UI の「アクセス管理」で claude.ai 用のエージェントを追加してトークン（`hre_...`）を発行し、使わせたいワークスペースに `read`（書かせるなら `write` も）を付ける。コマンドで作るなら:

```sh
docker compose exec mcp hre-admin create-actor claude-ai agent   # トークンが1度だけ表示される
docker compose exec mcp hre-admin grant claude-ai owner read,write
```

claude.ai 専用に分けておくと、記録者（`recorded_by`）で claude.ai から書いた内容を見分けられ、そのトークンだけ無効にもできる。

### 2. カスタムコネクタを追加する

1. claude.ai で **Customize > Connectors** を開き、**Add custom connector** を押す（Team / Enterprise プランでは Owner が **Organization settings > Connectors** から追加する）
2. **MCP server URL** に `https://<HRE_DOMAIN>/mcp`
3. 認証の選択肢が出る場合は **Sign in now**、OAuth クライアントは **Register automatically** を選ぶ（OAuth Client ID / Secret の欄は空のまま）
4. **Add** で保存し、**Connect** を押す
5. 開いた「HumanRelationEmurator への接続」画面で、1 のトークンを入力して **接続を許可する**

### 3. チャットで使う

チャットの **+ > Connectors** で有効にして、「〇〇さんについて覚えていることを教えて」「今日の配信で△△さんと話した内容を記録して」のように頼む。スマホの Claude アプリでも、同じアカウントで追加したコネクタがそのまま使える。

書き込み系のツールは毎回の承認を残しておくのがおすすめ（**Always allow** にしない）。人格フィールドや trusted な出典はエージェントからは書けないので、claude.ai から人格が書き換わることはない。

### 接続を切る・やり直す

- 1 のトークンを Web UI か `hre-admin rotate-token claude-ai` で再発行・無効化すると、claude.ai からの接続もすぐ使えなくなる。つなぎ直すときは claude.ai のコネクタでもう一度 **Connect** して新しいトークンを入れる
- 認可画面で別のトークンを入れれば、そのトークンの持ち主（別の AITuber など）として接続できる

## データベース

PostgreSQL 16 以上。

```sh
createdb hre
psql -v ON_ERROR_STOP=1 -d hre -f db/schema.sql
psql -v ON_ERROR_STOP=1 -d hre -f db/seed.sql
# 動作確認（ROLLBACK するのでデータは残らない）
psql -d hre -f db/tests/smoke.sql
```
