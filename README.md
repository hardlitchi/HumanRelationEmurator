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

## AI アシスタントから使う（コネクタ）

Claude（claude.ai・デスクトップ・スマホ）・ChatGPT・Claude Code などに MCP サーバー `https://<HRE_DOMAIN>/mcp` を登録すると、チャットやエージェントから人間関係グラフを読み書きできる。AI 側のサーバーがインターネット越しに接続するので、外部のリバースプロキシで HTTPS 公開済みであることが前提。

接続は OAuth で行う。AI 側で接続を始めると Web UI の許可画面が開き、**接続する身元**を選んで **許可する** を押すだけでつながる（トークンの入力は要らない）。Web UI の **AI 連携** 画面に、接続先 URL・AI ごとの手順・接続中のアプリ（取り消しボタン付き）がまとまっている。

### 1. 身元（エージェント）を用意する

AITuber やエージェントごとに、Web UI の「アクセス管理」でエージェントを追加し、使わせたいワークスペースに `read`（書かせるなら `write` も）を付けておく（AITuber なら「アクセス」→「キャラのトークン」か `hre-admin add-persona` でまとめて作れる）。ここで表示されるトークンは、OAuth で接続するだけなら控えなくてよい。

エージェントを分けておくと、記録者（`recorded_by`）でどの AI から書いた内容か見分けられ、その AI だけ止めることもできる。

### 2. AI 側に登録する

| AI | 登録方法 |
|---|---|
| Claude（claude.ai / デスクトップ / スマホ） | **Customize > Connectors** → **Add custom connector** で URL に `https://<HRE_DOMAIN>/mcp`。OAuth Client ID / Secret は空のまま **Add** → **Connect**（Team / Enterprise では Owner が **Organization settings > Connectors** から追加） |
| ChatGPT | 設定 → アプリとコネクタ（開発者モードを有効化）→ 作成 → URL を入力し、認証は OAuth |
| Claude Code | `claude mcp add --transport http hre https://<HRE_DOMAIN>/mcp` のあと `/mcp` で認証（ブラウザで許可画面が開く） |
| OAuth 非対応のツール・サーバー | エージェントのトークンを `Authorization: Bearer hre_...` で送る（[docs/mcp-server.md](docs/mcp-server.md)） |

### 3. 許可画面で許可する

1. 開いた画面で Web UI にログインしていなければ、人間用のトークンでログインする（以後 30 日はログイン済み）
2. 「接続する身元」で 1 のエージェントを選ぶ（管理者は全エージェントから選べる。それ以外の人は自分だけ）
3. 閲覧だけにするなら「記録と更新も許可する」を外す
4. **許可する**

### 4. チャットで使う

Claude ならチャットの **+ > Connectors** で有効にして、「〇〇さんについて覚えていることを教えて」「今日の配信で△△さんと話した内容を記録して」のように頼む。スマホの Claude アプリでも、同じアカウントで追加したコネクタがそのまま使える。

書き込み系のツールは毎回の承認を残しておくのがおすすめ（**Always allow** にしない）。

人格フィールド（一人称・口調・背景設定など）と管理者関係（`operator_of`）は、エージェントからは信頼済みの出典がないと書けない。AI で AITuber の設定を作るときは次の流れになる:

1. AI に設定資料（キャラ設定・企画書など）を `add_source` で保存させる。返ってくる `review_url` を AI がチャットに出す
2. そのリンクを開き、内容を確かめて **信頼済みにする** を押す
3. AI にその出典を付けて人格フィールドを書かせる

信頼済みの出典なしで AI が書こうとした内容は、Web UI の **確認** に1件ずつ並ぶ。スマホで「書いてOK / 書かない / あとで」を押すだけで片づく（未確認の設定資料と、AI が提案した新しい項目もここに出る）。

配信コメントなどから人格が書き換わらないよう、信頼済みにできるのは人（Web UI）だけにしてある。許可画面で自分（人）を身元に選ぶとこの制限がかからないので、ふだんはエージェントを選ぶ。創作の人間関係の出典の示し方は [docs/data-model.md](docs/data-model.md) の「創作の人間関係」を参照。

### 接続を切る・やり直す

- Web UI の **AI 連携** で、接続ごとに **取り消す** を押す
- 身元のトークンを「アクセス管理」か `hre-admin rotate-token <name>` で再発行・無効化すると、その身元での接続はすべて切れる
- つなぎ直すときは AI 側でもう一度 **Connect** する。別の身元を選べば、別の AITuber として接続できる

## データベース

PostgreSQL 16 以上。

```sh
createdb hre
psql -v ON_ERROR_STOP=1 -d hre -f db/schema.sql
psql -v ON_ERROR_STOP=1 -d hre -f db/seed.sql
# 動作確認（ROLLBACK するのでデータは残らない）
psql -d hre -f db/tests/smoke.sql
```
