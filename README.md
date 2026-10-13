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

## データベース

PostgreSQL 16 以上。

```sh
createdb hre
psql -v ON_ERROR_STOP=1 -d hre -f db/schema.sql
psql -v ON_ERROR_STOP=1 -d hre -f db/seed.sql
# 動作確認（ROLLBACK するのでデータは残らない）
psql -d hre -f db/tests/smoke.sql
```
