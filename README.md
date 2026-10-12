# HumanRelationEmurator

人間関係をネットワーク構造で記録・管理する、AIエージェントと人間のためのツール群。

- 人・組織・グループをノード、関係をエッジとして持ち、それぞれにコンテキスト（カスタムフィールド）を積む
- カスタムフィールドはマスターで管理し、エージェントが書いても表記ゆれしないようにする
- やり取りの記録から関係の強さ・クラスタを算出する

個人用。第三者の個人情報を扱うため、機微情報の扱い・外部AIへの送信・完全削除を設計に組み込んでいる。

## ドキュメント

- [要求の妥当性検証](docs/requirements-validation.md)
- [データモデル](docs/data-model.md)

## データベース

PostgreSQL 16 以上。

```sh
createdb hre
psql -v ON_ERROR_STOP=1 -d hre -f db/schema.sql
psql -v ON_ERROR_STOP=1 -d hre -f db/seed.sql
# 動作確認（ROLLBACK するのでデータは残らない）
psql -d hre -f db/tests/smoke.sql
```
