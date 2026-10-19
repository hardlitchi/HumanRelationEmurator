# オントロジー（語彙と使い分けの規約）

HumanRelationEmurator で「何を・どの名前で・どこに書くか」の決まり。テーブルの構造は [data-model.md](data-model.md)、値の実体は [`db/seed.sql`](../db/seed.sql)。

目的は2つ。

1. **揺れを防ぐ**: エージェント（LLM）が同じことを別の名前・別の場所・逆の向きで書かないようにする
2. **乗っ取りと漏洩を防ぐ**: 配信チャットのような誰でも書ける経路から、人格・運営関係を書き換えられたり、リスナーの情報が配信で口に出たりしないようにする

方針は「**語彙を増やすより、間違った書き方をしたときに正しい書き方を返す**」。選択肢が増えるほど LLM は近い意味のもの同士で迷うので、マスターは実データで必要になったものだけ足す。

## 1. エンティティの種類（`entities.kind`）

| kind | 使う対象 | 迷うとき |
|---|---|---|
| `person` | 実在の人、VTuber・AITuber・キャラクター | 実在か・キャラかは `persona_type` で区別する |
| `organization` | 法人・事務所・学校・店など、雇用や契約の主体 | 劇団・サークルは、法人格や雇用関係があれば organization、なければ group |
| `group` | ユニット・グループ・サークル・コミュニティ | AITuber グループ（冬秋夏春）は group |

## 2. 関係の種類（`relation_types`）

### 2.1 読み方

- 有向の関係は「**source は target の 〈label〉**」と読む。`inverse_label` は target の役割
  - 例: `listener_of`（リスナー / 配信者）: リスナーX → 配信者A は「X は A のリスナー（A は X の配信者）」
- 無向の関係（`directed = false`）は向きに意味がない。`A→B` と `B→A` は同じ関係として扱い、2本目は作らない
- `add_relationship` は記録した向きを文にした `reading` を返す。エージェントはこれで向きを確かめる
- 画面とエージェント向けの出力:
  - `label`: そのページの人（`from`）の役割。Web UI の関係のタグと、`get_entity` / `persona_context` の `label`・`relation_label` は今までどおりこれ
  - `other_label`（2026-10-19 追加）: 相手（`to`）の役割。`active_edges` ビューと `get_entity` / `persona_context` に新しいキーとして足した。既存のキーの意味は変えていない（KAGURA STAGE などの利用側が静かに壊れないように）

### 2.2 一覧

| key | label / inverse_label | 向き | source → target | 使うとき |
|---|---|---|---|---|
| `knows` | 知り合い | 無向 | 人 – 人 | ほかのどれにも当てはまらない知り合い |
| `friend` | 友人 | 無向 | 人 – 人 | |
| `family` | 家族 | 無向 | 人 – 人 | 家族・親族。**続柄は関係フィールド `kinship_term`** |
| `partner` | パートナー | 無向 | 人 – 人 | 恋人・婚約者・配偶者。**段階は `kinship_term`**（恋人・婚約者・夫・妻） |
| `colleague` | 同僚 | 無向 | 人 – 人 | |
| `manager_of` | 上司 / 部下 | 有向 | 人 → 人 | |
| `mentor_of` | メンター / メンティー | 有向 | 人 → 人 | |
| `business_partner` | 取引先 | 無向 | 人・組織 – 人・組織 | |
| `works_at` | 所属 / 所属メンバー | 有向 | 人 → 組織 | |
| `member_of` | メンバー / メンバー一覧 | 有向 | 人 → グループ | |
| `part_of` | 下部組織 / 上部組織 | 有向 | 組織・グループ → 組織・グループ | |
| `operator_of` 🔒 | 運営・管理者 / 担当タレント | 有向 | 人 → 人・組織・グループ | 人か信頼済みの出典からしか書けない |
| `collaborated_with` | コラボ相手 | 無向 | 人 – 人 | |
| `listener_of` | リスナー / 配信者 | 有向 | 人 → 人 | |

各種類の `description` にも同じ説明が入っていて、`list_relation_types` が返す。

### 2.3 関係の種類として受け付けない言葉

マスターに無い名前で `add_relationship` を呼ぶとエラーになる。そのとき、よくある取り違えには正しい書き方を返す（**自動で読み替えたり、向きを入れ替えたりはしない**）。

| 呼ばれた名前 | 返す案内 | 理由 |
|---|---|---|
| 続柄（父・母・妹・祖父・義母 など） | `family` を張り、その関係に `kinship_term` を付ける | 別名で `family` に読み替えると「父」という情報が黙って捨てられる |
| 婚約者・夫・妻・彼氏・彼女 など | `partner` を張り、`kinship_term` を付ける | 同上 |
| 逆向きの役割（部下・メンティー・配信者・担当タレント・上部組織 など。`inverse_label` と同じ名前） | どちらを source にして、どの key で書くか | 日本語の名詞は「A の部下 B」「A は B の部下」の両方に読めるので、入れ替えると五分で逆の事実が入る |

## 3. フィールド（`field_definitions`）

### 3.1 一覧

🔒 は人か信頼済みの出典からしか書けない（`requires_trusted_source`）。「AI 不可」は `ai_shareable = false`（`sensitive_read` 権限がないと返さない）。

| まとまり | フィールド（key） |
|---|---|
| 基本情報 | `birthday`, `hometown`, `residence_area`, `name_reading`（読み）, `legal_name`（本名、sensitive・AI 不可・🔒） |
| 仕事 | `occupation`, `job_title`, `skills`（スキル・特技） |
| 好み・人柄 | `hobbies`, `likes`, `dislikes`, `personality_notes` |
| 連絡 | `communication_preference` |
| 家族・健康 | `family_notes`（AI 不可）, `health_notes`（sensitive・AI 不可） |
| 配信での配慮 | `stream_mention_policy`（配信での扱い）, `is_minor`（未成年、AI 不可）, `topics_to_avoid`（避けたい話題（相手）） |
| 人格設定 🔒 | `persona_type`, `first_person`, `speech_style`, `catchphrases`, `backstory`, `persona_values`, `ng_topics` |
| 関係の文脈（関係に付く） | `closeness_rating`, `how_we_met`, `shared_context`, `inside_jokes`, `introduced_by`, `calls_them`, `called_by_them`, `support_notes`, `first_seen`, `kinship_term`（続柄） |
| 組織 | `industry`, `website` |
| その他 | `notes` |

### 3.2 使い分け

| 迷うこと | 決まり |
|---|---|
| 家族のメモか、家族という関係か | 人と人のつながりは関係（`family`）。`resolve_field('家族')` は `family_notes` を返すが、関係の種類と同じ名前なので `relation_type_hint` も返す。関係の種類と同じ名前のフィールドは提案できない |
| 父・母・婚約者 | `family` / `partner` 関係＋`kinship_term`。自分（ワークスペースの主）から見た続柄で、description にある語のどれかで書く。婚約→結婚のような変化は、古い主張に `valid_to` を入れて新しい主張を足す |
| NG の話題 | キャラクター自身が扱わない話題は `ng_topics`（🔒）。リスナーなど**ほかの人について**避ける話題は `topics_to_avoid`（保護なし、配信チャットからでも書ける）。エージェントが `ng_topics` を書いて拒否されたら、エラーが `topics_to_avoid` を案内する |
| 配信でその人に触れてよいか | `stream_mention_policy`: `ok` / `nickname_only`（呼び方だけで触れる）/ `avoid`（触れない）。本人の希望を記録する |
| 未成年 | `is_minor = true` は本人の発言や明らかな描写から分かったときだけ。「学生」だけでは付けない。true の人について新しい個人情報は記録しない |
| 本名 | `legal_name`。VTuber・AITuber・リスナーの本名は記録しない（演者とキャラクターを結びつけない）。芸名・キャラ名・呼び名は表示名か `name` 識別子 |

### 3.3 persona_context（AITuber のプロンプト）に渡すもの

`persona_context`（MCP の `get_persona_context`、`GET /api/persona-context`）は、配信で話す AITuber の前提になるので、ここで次を強制する（LLM 任せにしない）。

- **承認済み（`approved`）のフィールド・関係の種類の記録だけを渡す**。提案中のものは `omitted_unapproved` にキーだけ出る。保護つきのフィールド（`backstory` など）と同じ意味の別名のフィールドを提案して、保護を迂回させないため。人の画面と `get_entity` には提案中のものも出る
- `stream_mention_policy` は記録のうち最も厳しいものを採る。`avoid` の人は渡さない。`nickname_only` の人は呼び方（`calls_them`）を名前として渡し、それ以外の関係の情報は渡さない。呼び方が無ければ渡さない
- `is_minor` が1件でも true の人は、呼び方以外の関係の情報を渡さず、`handling: ["minor"]` の印だけを付ける（未成年であること自体はプロンプトに文字として入れない）

## 4. 主張の語彙（`facts`）

### 4.1 epistemic

| 値 | 意味 | 創作・設定資料のとき |
|---|---|---|
| `stated` | 本人が言った・書いた | 運営の設定資料に基づく人格フィールド（出典は信頼済みの document）／キャラの作中の発言 |
| `observed` | 記録者が直接見聞きした | 作中の描写・地の文 |
| `reported` | 第三者から聞いた | 他のキャラの発言 |
| `inferred` | 推測 | 考察・二次設定。sensitive のフィールドには書けない |

### 4.2 confidence の目安

細かい帯は決めず、2つだけ守る。

- 推測（`inferred`）は 0.5 以下
- 配信チャットや SNS での本人談（なりすましの余地がある）は 0.7 以下

### 4.3 関係は推測で張らない

関係（`relationships`）には epistemic・confidence が無い。「付き合っているらしい」は `partner` にせず、人の `notes` などに `inferred` で書く。

## 5. 出典の種類（`sources.kind`）とやり取りのチャネル（`interactions.channel`）

値は変えていない。

| sources.kind | 意味 | 「確認」に出るか |
|---|---|---|
| `manual` | 人が手で書いたメモ | 出ない |
| `conversation` | 対面・通話の会話の書き起こし・要約 | 出ない |
| `email` / `chat` / `calendar` | 各サービスから取り込んだもの | 出ない |
| `sns` / `web` | 公開 SNS の投稿・Web ページ | 出ない |
| `document` | 設定資料・企画書・原作など | **出る**（人が確認して信頼済みにする） |
| `stream` | 配信そのもの（アーカイブ・文字起こし） | 出ない |
| `stream_chat` | 配信のコメント・スパチャ。**信頼済みにしない** | 出ない |
| `agent_inference` | エージェントがまとめた推論 | 出ない |

| channel | 意味 | depth の目安 |
|---|---|---|
| `in_person` | 対面 | 2〜3 |
| `call` / `video` | 通話・ビデオ通話 | 2〜3 |
| `chat` / `email` / `sns` | 1対1・少人数のテキスト | 1〜2 |
| `event` | イベント・オフ会での同席 | 1〜2 |
| `stream` | 配信での共演（コラボ） | 2 |
| `stream_chat` | 配信コメントへの反応 | 1 |
| `other` | 上のどれでもない | — |

## 6. 識別子の scheme（`entity_identifiers.scheme`）

自由文字列のまま。使う名前をそろえる。

| scheme | 値 |
|---|---|
| `name` | 名前・呼び名・別名（ワークスペース内で一意にしない） |
| `email` / `phone` | メール・電話 |
| `x` | X（旧 Twitter）のハンドル。`twitter` は使わない |
| `youtube` / `twitch` / `instagram` / `tiktok` / `github` / `niconico` / `bluesky` / `discord` | 各サービスのハンドル・ユーザー名 |
| `url` | プロフィール URL |

## 7. 命名規則とマスターの運用

| 対象 | 規則 |
|---|---|
| フィールドの key | 英小文字のスネークケース。many は複数形（`hobbies`）、自由記述のメモは `_notes`、真偽値は `is_` / `has_`、段階・尺度の enum は `_stage` / `_rating` / `_policy` |
| 「自分から見た」フィールド | description に明記し、自分が当事者の関係にだけ付ける（`calls_them`, `kinship_term`） |
| 関係の種類の key | 有向は「source 〈key〉 target」と読める `<役割>_of` か `<動詞>_at/with`。無向は名詞。**逆向きの種類は作らない**（`subordinate_of` は作らず `manager_of`） |
| label | 日本語で短く。ほかのフィールドの label・別名や、関係の種類の名前と重ねない |
| 別名 | 一度登録した別名は別のものに付け替えない（既存の解決結果が変わるため） |

提案 → 承認 → 統合:

- エージェントは `resolve_field` で既存のものを引き、意味が同じなら `add_field_alias`、無ければ `propose_field`。提案中のフィールドもすぐ書けるが、**AITuber のプロンプトには承認されるまで入らない**
- 管理者（人）が受信箱で承認・統合・廃止する。統合は名前の解決（新しい書き込みの行き先）にだけ効き、既存の主張は統合元のまま残る
- 機微度・AI 送信可否・保護は厳しくする方向ならいつ変えてもよい。緩めるときは、既存の主張がどこに出ていくかを確かめてから

## 8. 本番 DB への反映

`db/upgrade.sql` は `hre-mcp --http` の起動のたびに流れる。

- 列・表・新しいフィールドの追加は `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` で何度流しても同じ。エージェントが同じキーで提案済みならそちらを残す
- 既存のマスターの値を直すもの（2026-10-19 は `legal_name` の保護と、`ng_topics`・関係の種類の説明）は `hre.schema_migrations` に名前を残して**一度だけ**流す。管理者が後から変えた値を、再起動のたびに戻さないため
- 2026-10-19 の変更で本番の提案中の `legal_name` は sensitive・AI 不可・保護ありになる。`legal_name` と `skills` の**承認は人が「確認」で行う**（提案中のままだと AITuber のプロンプトには入らない）

## 9. 決めた経緯（2026-10-19）

設計担当・利用者視点担当（シナリオ46件の要求カタログ）・デビルズアドボケイトの3役で案を作り、反論を受けて絞った。

**入れたもの**: §2.3 の案内エラー、`reading`、無向関係の重複防止、フィールドと関係の種類の名前の衝突検出、`kinship_term`、`topics_to_avoid`、`stream_mention_policy`・`is_minor` と persona_context での強制、承認済みだけを persona_context に渡すこと、`legal_name`（保護）・`skills`・`name_reading`、関係の種類の description、`other_label`、upgrade.sql の一度だけの変更。

**見送ったもの（理由）**:

| 案 | 判断 | 理由 |
|---|---|---|
| 関係の種類をエージェントが提案・統合できるようにする | 後回し | 保護なしの同義語（「プロデューサー」など）で `operator_of` を迂回できる。承認済みだけを AITuber に渡す仕組み（今回入れた）を前提に、提案は保護つきを既定にして入れる |
| 逆向きの別名で source / target を自動で入れ替える | 却下 | 向きの取り違えが増える（§2.3） |
| 続柄の語を family / partner の別名にする | 却下 | 呼び出しは成功するのに続柄が捨てられる |
| `partner_stage`（交際の段階）・`parent_of` / `sibling_of`・関係の種類の階層 | 却下 | `kinship_term` と同じことの二重表現になる |
| フィールドのカテゴリ表 | 後回し | 提案のたびに LLM に選択を課す。入れるなら表示専用で管理者だけが付ける |
| 識別子の scheme のマスター表と既存値の一括正規化 | 却下 | 正規化は persona_context の同一人物判定の当たり範囲を広げ、なりすましアカウントの記録が合流しうる。同一人物の判定を見直すときに一緒に考える |
| 関係に epistemic・confidence・取り消し | 後回し | 規模に対して波及が大きい。今は「推測で関係を張らない」規約で代える |
| ペルソナ対象の全フィールドを保護 | 後回し | AITuber 自身が覚える書き込みまで確認待ちになる。承認済みだけを渡すことで同義語の迂回は塞いだ |
| 記録を拒否する人のリスト（消去した人の再登録防止） | 別 PR | それ自体が個人データの保持になるので、プライバシー機能として設計する |
| `persona_type` に `fictional_character` | 後回し | 創作の実データが出てから |
| `works_at` / `member_of` の label・inverse_label の書き直し | 後回し | 利用側が `other_label` に移ってから |
