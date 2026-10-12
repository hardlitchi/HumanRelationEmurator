-- 初期マスター（承認済みのフィールドと関係の種類）
-- エージェントはここに無いものを status = 'proposed' で追加できる。
SET search_path = hre, public;

-- key, label, value_type, enum_options, applies_to, cardinality, sensitivity, ai_shareable, description, aliases
WITH seed (key, label, value_type, enum_options, applies_to, cardinality, sensitivity, ai_shareable, description, aliases) AS (
    VALUES
    -- 人
    ('birthday', '誕生日', 'date', NULL::text[], '{person}'::text[], 'one', 'personal', true,
        'YYYY-MM-DD。年が不明なら --MM-DD（ISO 8601 の年省略形）', '{birth_date,date_of_birth,dob,生年月日,誕生日}'::text[]),
    ('occupation', '職業', 'text', NULL, '{person}', 'one', 'personal', true,
        '職種（エンジニア、営業など）。所属先は works_at 関係で表す', '{職種,仕事,job}'),
    ('job_title', '役職', 'text', NULL, '{person}', 'one', 'personal', true,
        '肩書き', '{title,position,肩書き,肩書,役職名}'),
    ('hometown', '出身地', 'text', NULL, '{person}', 'one', 'personal', true,
        NULL, '{birthplace,出身,地元}'),
    ('residence_area', '住んでいる地域', 'text', NULL, '{person}', 'one', 'personal', true,
        '市区町村程度まで。番地は書かない', '{居住地,住まい,住所,location}'),
    ('hobbies', '趣味', 'text', NULL, '{person}', 'many', 'personal', true,
        '1件につき1つ', '{hobby,趣味}'),
    ('likes', '好きなもの', 'text', NULL, '{person}', 'many', 'personal', true,
        '食べ物・話題・ブランドなど', '{好き,好物,favorite,favorites}'),
    ('dislikes', '苦手なもの', 'text', NULL, '{person}', 'many', 'personal', true,
        'アレルギー等の健康情報は health_notes へ', '{嫌い,嫌いなもの,苦手}'),
    ('communication_preference', '連絡の好み', 'text', NULL, '{person}', 'one', 'personal', true,
        '好む連絡手段・時間帯・文体', '{連絡手段,contact_preference,連絡方法}'),
    ('personality_notes', '人柄', 'text', NULL, '{person}', 'many', 'personal', true,
        '観察した振る舞い。推測なら epistemic = inferred', '{性格,人柄,personality}'),
    ('family_notes', '家族について', 'text', NULL, '{person}', 'many', 'personal', false,
        NULL, '{家族,family}'),
    ('health_notes', '健康について', 'text', NULL, '{person}', 'many', 'sensitive', false,
        '本人から聞いた事実のみ。推測禁止', '{健康,病歴,持病,アレルギー,health}'),
    ('notes', 'メモ', 'text', NULL, '{person,organization,group,relationship}', 'many', 'personal', true,
        'どのフィールドにも当てはまらない情報。多用されたら新フィールドを検討', '{memo,note,備考}'),
    -- 組織・グループ
    ('industry', '業種', 'text', NULL, '{organization}', 'one', 'normal', true,
        NULL, '{業界,sector}'),
    ('website', 'Webサイト', 'url', NULL, '{organization,group}', 'one', 'normal', true,
        NULL, '{url,homepage,ホームページ,hp}'),
    -- 関係（エッジ）
    ('closeness_rating', '親しさ（主観）', 'enum', '{1,2,3,4,5}', '{relationship}', 'one', 'normal', true,
        '1:顔見知り 2:知人 3:普通に話す 4:親しい 5:非常に親しい。自分が関わる関係にだけ付ける', '{親密度,親しさ,closeness,intimacy}'),
    ('how_we_met', '出会いのきっかけ', 'text', NULL, '{relationship}', 'one', 'personal', true,
        NULL, '{出会い,きっかけ,馴れ初め}'),
    ('shared_context', '共有している文脈', 'text', NULL, '{relationship}', 'many', 'personal', true,
        '2人の間で前提になっている経験・話題。短いやり取りが通じる理由', '{共通の話題,共通の経験,共有体験,context}'),
    ('inside_jokes', '内輪ネタ', 'text', NULL, '{relationship}', 'many', 'personal', true,
        NULL, '{内輪,inside_joke}'),
    ('introduced_by', '紹介者', 'entity_ref', NULL, '{relationship}', 'one', 'personal', true,
        'この関係を紹介した人', '{紹介,紹介元}'),
    ('calls_them', '呼び方（自分→相手）', 'text', NULL, '{relationship}', 'one', 'normal', true,
        'このワークスペースの主が相手をどう呼ぶか', '{呼び方,あだ名,nickname}'),
    ('called_by_them', '呼ばれ方（相手→自分）', 'text', NULL, '{relationship}', 'one', 'normal', true,
        '相手からどう呼ばれているか', '{呼ばれ方}'),
    ('support_notes', '応援の記録', 'text', NULL, '{relationship}', 'many', 'personal', true,
        'スパチャ・メンバーシップ・ファンアートなど。金額は書いてよいが決済情報は書かない', '{応援,スパチャ,superchat,membership,メンバーシップ}'),
    ('first_seen', '初めて会った日', 'date', NULL, '{relationship}', 'one', 'normal', true,
        '初見の配信日など', '{初見,初対面}'),
    -- 人格（AITuber 等）。人か信頼できる出典からしか書けない
    ('persona_type', '種別', 'enum', '{human,vtuber,aituber,other}', '{person}', 'one', 'normal', true,
        '実在の人か、VTuber・AITuber 等のキャラクターか', '{種別,タイプ}'),
    ('first_person', '一人称', 'text', NULL, '{person}', 'one', 'normal', true,
        NULL, '{一人称}'),
    ('speech_style', '口調', 'text', NULL, '{person}', 'many', 'normal', true,
        '語尾・敬語の使い方・話し方の特徴', '{話し方,語尾,口調}'),
    ('catchphrases', '口癖・決め台詞', 'text', NULL, '{person}', 'many', 'normal', true,
        NULL, '{口癖,決め台詞,catchphrase}'),
    ('backstory', '設定・背景', 'text', NULL, '{person}', 'many', 'normal', true,
        'キャラクターの設定。実在の人には使わない', '{設定,生い立ち,lore,背景}'),
    ('persona_values', '人格の価値観', 'text', NULL, '{person}', 'many', 'normal', true,
        'キャラクターの価値観・信条。実在の人の信条は記録しない（要配慮個人情報）', '{価値観}'),
    ('ng_topics', 'NG・避ける話題', 'text', NULL, '{person}', 'many', 'normal', true,
        NULL, '{ng,地雷,避ける話題,ng話題}')
),
ins AS (
    INSERT INTO field_definitions (key, label, value_type, enum_options, applies_to, cardinality,
                                   sensitivity, ai_shareable, description, status)
    SELECT key, label, value_type, enum_options, applies_to, cardinality,
           sensitivity, ai_shareable, description, 'approved'
      FROM seed
    RETURNING id, key
)
INSERT INTO field_aliases (alias, field_id)
SELECT DISTINCT normalize_label(a), ins.id
  FROM seed JOIN ins USING (key), unnest(seed.aliases) a
 WHERE normalize_label(a) <> seed.key;

-- key, label, directed, inverse_label, source_kinds, target_kinds, aliases
WITH seed (key, label, directed, inverse_label, source_kinds, target_kinds, aliases) AS (
    VALUES
    ('knows',            '知り合い',   false, NULL::text, '{person}'::text[], '{person}'::text[], '{知人,知り合い,acquaintance}'::text[]),
    ('friend',           '友人',       false, NULL, '{person}', '{person}', '{友達,友人,ともだち}'),
    ('family',           '家族',       false, NULL, '{person}', '{person}', '{家族,親族,relative}'),
    ('partner',          'パートナー', false, NULL, '{person}', '{person}', '{恋人,配偶者,spouse}'),
    ('colleague',        '同僚',       false, NULL, '{person}', '{person}', '{同僚,coworker}'),
    ('manager_of',       '上司',       true,  '部下', '{person}', '{person}', '{上司,boss,manages}'),
    ('mentor_of',        'メンター',   true,  'メンティー', '{person}', '{person}', '{メンター,師匠,mentor}'),
    ('business_partner', '取引先',     false, NULL, '{person,organization}', '{person,organization}', '{取引先,client,顧客}'),
    ('works_at',         '所属',       true,  '所属メンバー', '{person}', '{organization}', '{勤務先,所属,employer,works_for}'),
    ('member_of',        'メンバー',   true,  'メンバー一覧', '{person}', '{group}', '{参加,所属グループ,belongs_to}'),
    ('part_of',          '下部組織',   true,  '上部組織', '{organization,group}', '{organization,group}', '{子会社,部署,subsidiary}'),
    ('operator_of',      '運営・管理者', true, '担当タレント', '{person}', '{person}', '{管理者,運営,マネージャー,operator}'),
    ('collaborated_with','コラボ相手', false, NULL, '{person}', '{person}', '{コラボ,collab,共演}'),
    ('listener_of',      'リスナー',   true,  '配信者', '{person}', '{person}', '{リスナー,ファン,視聴者,fan,viewer}')
),
ins AS (
    INSERT INTO relation_types (key, label, directed, inverse_label, source_kinds, target_kinds, status)
    SELECT key, label, directed, inverse_label, source_kinds, target_kinds, 'approved' FROM seed
    RETURNING id, key
)
INSERT INTO relation_type_aliases (alias, relation_type_id)
SELECT DISTINCT normalize_label(a), ins.id
  FROM seed JOIN ins USING (key), unnest(seed.aliases) a
 WHERE normalize_label(a) <> seed.key;

-- 人格・管理者関係は、配信チャット等からの書き換えを防ぐ
UPDATE field_definitions SET requires_trusted_source = true
 WHERE key IN ('persona_type', 'first_person', 'speech_style', 'catchphrases', 'backstory', 'persona_values', 'ng_topics');
UPDATE relation_types SET requires_trusted_source = true WHERE key IN ('operator_of');
