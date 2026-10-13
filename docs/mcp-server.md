# エージェント用 MCP サーバー

AIエージェント（各 AITuber、管理者用エージェントなど）が人間関係グラフを読み書きするための MCP サーバー。コードは [`server/`](../server)。

- 認証: エージェントごとに発行するトークン（DB にはハッシュだけを保存）。`Authorization: Bearer hre_...` で直接送るか、OAuth（claude.ai のカスタムコネクタ向け）の認可画面で入力する
- 権限: トークンの持ち主（actor）がワークスペースごとに持つ `read` / `write` / `sensitive_read`
- 記録者: 書き込みの `recorded_by` は常にトークンの持ち主。エージェントが他人を名乗ることはできない

## ツール

| ツール | 用途 |
|---|---|
| `list_workspaces` | 読み書きできるワークスペースと、それぞれの「自分」ノード |
| `find_entities` | 名前・呼び名・SNS ハンドル等で人を探す（記録前の重複確認） |
| `get_entity` | 人物カード（識別子・主張・関係と関係の文脈・強さ、強い順の相手 `ties`） |
| `create_entity` / `add_identifier` / `merge_entities` | 人・組織・グループの作成、識別子の追加、同一人物の統合 |
| `resolve_field` / `list_fields` / `propose_field` / `add_field_alias` | フィールドマスターの参照と拡張 |
| `list_relation_types` | 関係の種類の一覧 |
| `list_clusters` | 関係の強さから推定したグループ（「自分」は含まない） |
| `add_source` / `get_source` | 原文（会話・配信コメント等）の保存と参照 |
| `record_fact` / `retract_fact` | 人・関係についての主張の記録と取り消し |
| `add_relationship` / `end_relationship` | 関係の追加と終了 |
| `record_interaction` | やり取りの記録（関係の強さの材料） |

サーバーの `instructions` に、書く前に探す・フィールドは `resolve_field` で引く・原文の指示には従わない、といった使い方の原則を入れてある。

エージェントからは出来ないこと（意図的）:

- trusted な出典の登録（人間か管理者だけ）。人格フィールドと管理者関係は trusted な出典がないと書けないので、配信コメントから人格を書き換えられない
- 完全削除（管理者だけ。MCP ツールにも出していない）
- `ai_shareable = false` のフィールド（健康・家族など）の閲覧（`sensitive_read` 権限が必要）
- 他のワークスペースの読み書き（共有されたワークスペースは読めるが書けない）

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

### OAuth（claude.ai のカスタムコネクタ）

claude.ai のカスタムコネクタは固定トークンを送れないことが多いので、MCP サーバーが OAuth の認可サーバーも兼ねる（[`server/hre_server/oauth.py`](../server/hre_server/oauth.py)）。手順は [README](../README.md#claudeai-から使う)。

- メタデータ: `/.well-known/oauth-protected-resource/mcp` と `/.well-known/oauth-authorization-server`（発行者は `HRE_PUBLIC_URL`）
- 動的クライアント登録 `/register`、認可 `/authorize`（PKCE S256 必須）、トークン `/token`、取り消し `/revoke`
- 認可画面 `/oauth/login` で人が `hre_` トークンを入力し、その持ち主（actor）として認可する。アクセストークン `hrea_`（1時間）とリフレッシュトークン `hrer_`（90日、使うたびに取り替え）を発行する
- 発行したものはすべて入力した `hre_` トークンに結び付く。そのトークンを rotate / revoke すると、OAuth で出したトークンも使えなくなる
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
