-- cidx_views.sql — expose cidx's edges to Soufflé as NAME-keyed relations, IN PLACE.
--
-- Lightweight VIEWs in index.db (NO copy, NO separate file). Edges are re-keyed by an
-- ANNOTATED qualified name so distinct symbols never merge and rules read in real names:
--   calls("app::scale(int)", "app::scale(double)")     -- overloads kept distinct
--   e_uses("cont::Wrapper<int>", "...")                -- template instances kept distinct
--
-- The key is built in ONE place (symdisp): qual_name + the signature/template-arg suffix
-- from libclang's display_name (e.g. app::scale + "(int)"; cont::Wrapper + "<int>"). Where
-- that STILL collides across distinct symbols — const/ref-qualified overloads (not shown in
-- display_name), methods of different template instances, or same-named functions in
-- different TUs — a " #<id>" tiebreaker is appended to the colliding names ONLY, so the
-- graph stays sound (no two distinct symbols share a node). Soufflé interns the strings to
-- integers, so reasoning stays fast.
--
-- edge.kind:        1 calls 2 inherits 3 contains 4 specializes 5 instantiates
--                   6 overrides 7 uses 8 field_of 9 method_of 17 friend
-- entity_edge.kind: 1 generalizes 2 implements 3 specializes 4 composes 5 aggregates
--                   6 associates 7 creates 8 uses 9 destroys 10 befriends 11 instantiates

-- symbol id -> annotated, collision-free display name
DROP VIEW IF EXISTS symdisp;
CREATE VIEW symdisp AS
SELECT id,
       CASE WHEN cnt > 1 THEN b || ' #' || id ELSE b END AS name
FROM (
  SELECT id, b, count(*) OVER (PARTITION BY b) AS cnt
  FROM (
    SELECT id,
      CASE
        WHEN display_name IS NOT NULL AND display_name <> '' AND instr(display_name, spelling) = 1
          THEN COALESCE(qual_name, spelling) || substr(display_name, length(spelling) + 1)
        ELSE COALESCE(qual_name, spelling, usr)
      END AS b
    FROM symbol
  )
);

-- base graph edges (annotated src name, annotated dst name)
DROP VIEW IF EXISTS calls;     CREATE VIEW calls     AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=1;
DROP VIEW IF EXISTS inherits;  CREATE VIEW inherits  AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=2;
DROP VIEW IF EXISTS overrides; CREATE VIEW overrides AS SELECT ss.name AS a, ds.name AS b FROM edge e JOIN symdisp ss ON ss.id=e.src_id JOIN symdisp ds ON ds.id=e.dst_id WHERE e.kind=6;
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

-- reset Soufflé output tables + the seed table (idempotent re-run; these + the views are
-- the ONLY things the reasoning layer adds to index.db — drop them anytime to remove all
-- trace). Soufflé writes these as TABLES, so a plain DROP TABLE resets cleanly.
DROP TABLE IF EXISTS subtype;
DROP TABLE IF EXISTS edep;
DROP TABLE IF EXISTS reach;
DROP TABLE IF EXISTS seed;
CREATE TABLE seed(x TEXT);
