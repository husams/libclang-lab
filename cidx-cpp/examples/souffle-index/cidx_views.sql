-- cidx_views.sql — expose cidx's edges to Soufflé as NAME-keyed relations, IN PLACE.
--
-- Lightweight VIEWs in index.db (NO copy, NO separate file). Edges are re-keyed by an
-- ANNOTATED qualified name so distinct symbols never merge and rules read in real names:
--   calls("app::scale(int)", "app::scale(double)")     -- overloads kept distinct
--   e_uses("cont::Wrapper<int>", "...")                -- template instances kept distinct
--
-- The key is built in ONE place (symdisp), in three layers so every name is both readable
-- and unique:
--   1. qual_name + the signature/template-arg suffix from libclang's display_name
--      (app::scale + "(int)";  cont::Wrapper + "<int>").
--   2. for a member of a template INSTANCE, splice the owner's instance args, so methods of
--      different instances stay distinct AND readable: cont::Wrapper<bool>::label().
--   3. if a name STILL collides (const/ref-qualified overloads not shown in display_name,
--      same-named functions in different TUs), append " @file:line"; and for the rare
--      genuinely-indistinguishable rows, a final " [n]" ordinal.
-- Layers 2-3 apply to the colliding names ONLY, so no two distinct symbols ever share a
-- node (sound) and the common case stays clean. Soufflé interns the strings to integers,
-- so reasoning stays fast.
--
-- edge.kind:        1 calls 2 inherits 3 contains 4 specializes 5 instantiates
--                   6 overrides 7 uses 8 field_of 9 method_of 17 friend
-- entity_edge.kind: 1 generalizes 2 implements 3 specializes 4 composes 5 aggregates
--                   6 associates 7 creates 8 uses 9 destroys 10 befriends 11 instantiates

-- symbol id -> annotated, collision-free display name (see header for the 3 layers)
DROP VIEW IF EXISTS symdisp;
CREATE VIEW symdisp AS
WITH ann AS (   -- layer 1: base = qual_name + signature/template-arg suffix from display_name
  SELECT s.id, s.usr, s.spelling, s.qual_name, s.parent_usr, s.decl_path, s.decl_line,
         s.line, s.file_id,
    CASE WHEN s.display_name IS NOT NULL AND s.display_name <> '' AND instr(s.display_name, s.spelling) = 1
      THEN substr(s.display_name, length(s.spelling) + 1) ELSE '' END AS sig,
    CASE WHEN s.display_name IS NOT NULL AND s.display_name <> '' AND instr(s.display_name, s.spelling) = 1
      THEN COALESCE(s.qual_name, s.spelling, s.usr) || substr(s.display_name, length(s.spelling) + 1)
      ELSE COALESCE(s.qual_name, s.spelling, s.usr) END AS base
  FROM symbol s
),
based AS (      -- layer 2: splice the owner template-instance args for members of an instance
  SELECT a.id, a.decl_line, a.line, a.decl_path, a.file_id,
    CASE WHEN p.usr IS NOT NULL AND p.base <> p.qual_name AND instr(a.qual_name, p.qual_name) = 1
      THEN p.base || substr(a.qual_name, length(p.qual_name) + 1) || a.sig
      ELSE a.base END AS nm
  FROM ann a LEFT JOIN ann p ON p.usr = a.parent_usr
),
loc AS (        -- decl location, for the layer-3 tiebreaker (basename(decl_path) else file.name)
  SELECT b.id, b.nm,
    COALESCE(NULLIF(replace(b.decl_path, rtrim(b.decl_path, replace(b.decl_path, '/', '')), ''), ''),
             f.name, '?') || ':' || COALESCE(b.decl_line, b.line, 0) AS loc
  FROM based b LEFT JOIN file f ON f.id = b.file_id
)
SELECT id,                                  -- layer 3: @file:line, then [n], only if still colliding
  CASE WHEN cnt_nm = 1  THEN nm
       WHEN cnt_loc = 1 THEN nm || ' @' || loc
       ELSE nm || ' @' || loc || ' [' || rn || ']' END AS name
FROM (
  SELECT id, nm, loc,
    count(*)     OVER (PARTITION BY nm)      AS cnt_nm,
    count(*)     OVER (PARTITION BY nm, loc) AS cnt_loc,
    row_number() OVER (PARTITION BY nm, loc ORDER BY id) AS rn
  FROM loc
);

-- Symbol/file facts used by the standalone examples.  Every projected column is
-- NULL-free because Souffle's SQLite reader expects a concrete value in every cell.
DROP VIEW IF EXISTS symbol_fact;
CREATE VIEW symbol_fact AS
SELECT s.id, d.name,
       COALESCE(k.name, 'unknown') AS kind,
       COALESCE(s.type_info, '') AS type_info,
       COALESCE(f.name, COALESCE(s.decl_path, '')) AS file,
       COALESCE(s.line, s.decl_line, 0) AS line,
       s.is_definition, s.is_pure, s.is_static,
       s.is_instantiation, s.is_named_instance,
       COALESCE(s.linkage, '') AS linkage,
       COALESCE(s.access, '') AS access
FROM symbol s
JOIN symdisp d ON d.id=s.id
LEFT JOIN symbol_kind k ON k.id=s.kind
LEFT JOIN file f ON f.id=COALESCE(s.file_id, s.decl_file_id);

DROP VIEW IF EXISTS template_arg_fact;
CREATE VIEW template_arg_fact AS
SELECT o.name AS owner, a.position, a.arg_kind,
       COALESCE(r.name, '') AS referenced,
       COALESCE(a.literal, '') AS literal
FROM template_arg a
JOIN symdisp o ON o.id=a.owner_id
LEFT JOIN symdisp r ON r.id=a.ref_id;

DROP VIEW IF EXISTS call_site_fact;
CREATE VIEW call_site_fact AS
SELECT s.name AS caller, d.name AS callee, COALESCE(f.name, '') AS file,
       COALESCE(es.line, 0) AS line, COALESCE(es.col, 0) AS col,
       es.conditional, e.count
FROM edge e
JOIN symdisp s ON s.id=e.src_id
JOIN symdisp d ON d.id=e.dst_id
LEFT JOIN edge_site es ON es.edge_id=e.id
LEFT JOIN file f ON f.id=es.file_id
WHERE e.kind=1;

-- base graph edges (annotated src name, annotated dst name)
DROP VIEW IF EXISTS calls;     CREATE VIEW calls     AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=1;
DROP VIEW IF EXISTS inherits;  CREATE VIEW inherits  AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=2;
DROP VIEW IF EXISTS overrides; CREATE VIEW overrides AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=6;
DROP VIEW IF EXISTS instantiates; CREATE VIEW instantiates AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=5;
DROP VIEW IF EXISTS uses;      CREATE VIEW uses      AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=7;
DROP VIEW IF EXISTS field_of;  CREATE VIEW field_of  AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=8;
DROP VIEW IF EXISTS method_of; CREATE VIEW method_of AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=9;

-- design-level entity edges
DROP VIEW IF EXISTS e_generalizes; CREATE VIEW e_generalizes AS SELECT ss.name AS a, ds.name AS b FROM entity_edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=1;
DROP VIEW IF EXISTS e_implements;  CREATE VIEW e_implements  AS SELECT ss.name AS a, ds.name AS b FROM entity_edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=2;
DROP VIEW IF EXISTS e_composes;    CREATE VIEW e_composes    AS SELECT ss.name AS a, ds.name AS b FROM entity_edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=4;
DROP VIEW IF EXISTS e_aggregates;  CREATE VIEW e_aggregates  AS SELECT ss.name AS a, ds.name AS b FROM entity_edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=5;
DROP VIEW IF EXISTS e_associates;  CREATE VIEW e_associates  AS SELECT ss.name AS a, ds.name AS b FROM entity_edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=6;
DROP VIEW IF EXISTS e_creates;     CREATE VIEW e_creates     AS SELECT ss.name AS a, ds.name AS b FROM entity_edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=7;
DROP VIEW IF EXISTS e_uses;        CREATE VIEW e_uses        AS SELECT ss.name AS a, ds.name AS b FROM entity_edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=8;
DROP VIEW IF EXISTS e_destroys;    CREATE VIEW e_destroys    AS SELECT ss.name AS a, ds.name AS b FROM entity_edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=9;

-- NOTE: resetting the Soufflé output tables (subtype/edep/reach/cg_out/cg_in) + the `seed`
-- table is done by the runner (run.sh / query.sh), not here, because a prior session may
-- have left some as VIEWs and `DROP TABLE IF EXISTS` errors on a view (and vice-versa); the
-- runner issues a type-agnostic `DROP <type> IF EXISTS` read from sqlite_master. This file
-- is the pure INPUT adapter (the views above) — `.read` it to (re)create the edge views.
