-- 既存の DB を今の seed.sql に追いつかせる変更。何度流しても同じ結果になるように書く。
-- hre-mcp --http が起動時に oauth.sql と一緒に毎回適用するので、手作業は要らない。

-- 運営・管理者は人だけでなく、組織（事務所・グループ）やグループも運営できる（2026-10-16）
UPDATE hre.relation_types SET target_kinds = '{person,organization,group}'
 WHERE key = 'operator_of' AND NOT (target_kinds @> '{organization,group}');
