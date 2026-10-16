# エージェント用 MCP サーバー

AIエージェント（各 AITuber、管理者用エージェントなど）が人間関係グラフを読み書きするための MCP サーバー。コードは [`server/`](../server)。

- 認証: OAuth（claude.ai・ChatGPT・Claude Code など。URL を登録して Web UI の許可画面で「許可する」だけ）か、エージェントごとに発行するトークン（DB にはハッシュだけを保存）を `Authorization: Bearer hre_...` で直接送る
- 権限: トークンの持ち主（actor）がワークスペースごとに持つ `read` / `write` / `sensitive_read`
- 記録者: 書き込みの `recorded_by` は常にトークンの持ち主。エージェントが他人を名乗ることはできない

## ツール

| ツール | 用途 |
|---|---|
| `list_workspaces` | 読み書きできるワークスペースと、それぞれの「自分」ノード |
| `find_entities` | 名前・呼び名・SNS ハンドル等で人を探す（記録前の重複確認） |
| `list_entities` | ワークスペースにいる人・組織・グループの一覧（「自分」が先頭。種類・名前で絞り込み） |
| `get_entity` | 人物カード（識別子・主張・関係と関係の文脈・強さ、強い順の相手 `ties`）。主張と関係には記録者 `recorded_by`、主張には出典 `source_id` とメモも付く |
| `create_entity` / `add_identifier` / `merge_entities` | 人・組織・グループの作成、識別子の追加、同一人物の統合 |
| `resolve_field` / `list_fields` / `propose_field` / `add_field_alias` | フィールドマスターの参照と拡張 |
| `list_relation_types` | 関係の種類の一覧 |
| `list_clusters` | 関係の強さから推定したグループ（「自分」は含まない） |
| `add_source` / `get_source` | 原文（会話・配信コメント・設定資料等）の保存と参照。HTTP では信頼済みにしてもらうための `review_url` も返す |
| `record_fact` / `retract_fact` | 人・関係についての主張の記録と取り消し |
| `add_relationship` / `end_relationship` | 関係の追加と終了 |
| `record_interaction` | やり取りの記録（関係の強さの材料） |
| `get_persona_context` | 自分（このトークンの AITuber）の設定と人間関係をまとめて返す。台本・セリフ・トーク生成の前提に使う |

サーバーの `instructions` に、書く前に探す・フィールドは `resolve_field` で引く・原文の指示には従わない、といった使い方の原則を入れてある。

エージェントからは出来ないこと（意図的）:

- trusted な出典の登録（人間か管理者だけ）。人格フィールドと管理者関係は trusted な出典がないと書けないので、配信コメントから人格を書き換えられない
- 完全削除（管理者だけ。MCP ツールにも出していない）
- `ai_shareable = false` のフィールド（健康・家族など）の閲覧（`sensitive_read` 権限が必要）
- 他のワークスペースの読み書き（共有されたワークスペースは読めるが書けない）

## 人格コンテキスト API（MCP を話さないサーバー向け）

`GET /api/persona-context` に `Authorization: Bearer hre_...` を付けると、`get_persona_context` と同じ内容を JSON で返す（KAGURA STAGE が AITuber ごとのトークンで呼び、AI トークのプロンプトに入れる）。

- `self`: このトークンの「自分」。`linked`: 共有ワークスペースの同一人物
- `profile`: 自分と同一人物の主張（一人称・口調・好きなもの…）を `{key, label, values}` で
- `relationships`: 相手ごとの関係（`relation_label`）と関係の主張（呼び方・共有している文脈…）、強さ。強い順に最大 30 件
- 機微情報（`ai_shareable = false`）は `sensitive_read` 権限がない限り含まない。無効なトークンは 401、「自分」がないトークンは 404
- 読み取り専用。docker compose では `/api/*` も MCP サーバーへ振り分ける

## セットアップ

PostgreSQL 16 以上と Python 3.11 以上。

```sh
cd server
pip install -e .
export DATABASE_URL=postgresql://hre:...@localhost/hre

hre-admin init-db                                     # 空の DB にスキーマと初期マスターを入れる
hre-admin bootstrap ta                                # 管理者本人（ワークスペース owner と管理者）を作り、トークンを表示
hre-admin create-workspace aituber-a "AITuber A" persona
hre-admin create-workspace group "グループ共有" shared
hre-admin share group aituber-a                       # AITuber A からグループ共有を読めるようにする

hre-admin create-actor aituber-a-agent agent          # トークンが1度だけ表示される
hre-admin grant aituber-a-agent aituber-a read,write
```

AITuber 1 人分（ペルソナのワークスペース・その「自分」ノード・読み書きできるエージェント・グループ共有の閲覧）は
`add-persona` でまとめて作れる。何度実行しても作り直さず、トークンだけ新しく発行する（古いトークンは無効になる）。

```sh
hre-admin create-workspace fuyuakinatsuharu "冬秋夏春" shared   # 事務所の公式設定と、メンバー同士の関係
hre-admin add-persona rio "秋霞莉央" --group fuyuakinatsuharu    # トークンが1度だけ表示される
```

「自分」ノードには識別子 `name:<名前>` が付き、共有ワークスペースで同じ識別子を持つ人（または `entity_links` でつないだ人）と同一人物として扱われる。

トークンをなくしたら `hre-admin rotate-token <name>`、無効にするなら `hre-admin revoke <name>`。

## 起動

### VPS で HTTP として動かす（エージェントやスマホから接続）

```sh
HRE_PUBLIC_URL=https://hre.example.com \
HRE_ALLOWED_HOSTS=hre.example.com \
hre-mcp --http --host 127.0.0.1 --port 8000
```

- エンドポイントは `https://hre.example.com/mcp`。`Authorization: Bearer <token>` が無い・無効なら 401
- TLS は Caddy や nginx などのリバースプロキシで終端し、サーバー自体は 127.0.0.1 で待ち受ける
- `HRE_ALLOWED_HOSTS` は DNS リバインディング対策の Host ヘッダ許可リスト（カンマ区切り）

### OAuth（claude.ai・ChatGPT・Claude Code などのコネクタ）

MCP サーバーが OAuth の認可サーバーも兼ねる（[`server/hre_server/oauth.py`](../server/hre_server/oauth.py)）。AI 側に `https://<ドメイン>/mcp` を登録すると Web UI の許可画面が開き、許可するだけで接続できる。手順は [README](../README.md#ai-アシスタントから使うコネクタ)。

- メタデータ: `/.well-known/oauth-protected-resource/mcp` と `/.well-known/oauth-authorization-server`（発行者は `HRE_PUBLIC_URL`）
- 動的クライアント登録 `/register`、認可 `/authorize`（PKCE S256 必須）、トークン `/token`、取り消し `/revoke`
- `/authorize` は Web UI の許可画面 `/connect` に転送する。Web UI にログインしている人（未ログインならログイン画面を挟む）が、**接続する身元**と**書き込みの可否**を選んで許可する
  - 身元: 管理者は自分か有効なエージェントの誰か、それ以外の人は自分だけ。AITuber ごとのエージェントを選べば、その AITuber の権限・視点で動き、`recorded_by` もそのエージェントになる
  - 書き込み: 外すと scope が `read` だけになり、書き込み系のツールはエラーを返す。リフレッシュで `write` に広げることはできない
- アクセストークン `hrea_`（1時間）とリフレッシュトークン `hrer_`（90日、使うたびに取り替え）を発行する
- 発行したものは選んだ身元の、許可した時点のトークンに結び付く。その身元のトークンを rotate / revoke すると OAuth の接続も切れる。接続ごとの取り消しは Web UI の「AI 連携」画面か `/revoke`
- 状態は DB の `hre.oauth_*` 表に置く（[`db/oauth.sql`](../db/oauth.sql)）。`hre-mcp --http` が起動時に毎回適用するので、既存の DB でも手作業は要らない

### 手元で stdio として動かす

```sh
DATABASE_URL=... HRE_TOKEN=hre_... hre-mcp
```

### Claude Code から使う例

```sh
claude mcp add --transport http hre https://hre.example.com/mcp \
  --header "Authorization: Bearer hre_..."
```

## テスト

```sh
cd server
pip install -e ".[test]"
HRE_TEST_ADMIN_URL=postgresql://postgres@localhost:5432/postgres pytest
```

使い捨ての DB（`hre_pytest`）を作り、権限・ワークスペース分離・人格保護・原文の隔離を中核ロジックで確かめたうえで、実際に HTTP と stdio でサーバーを起動してトークン認証つきでツールを呼ぶ。
