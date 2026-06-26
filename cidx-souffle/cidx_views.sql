-- cidx_views.sql — expose cidx's integer edge tables to Soufflé, IN PLACE.
--
-- These are lightweight VIEWs created directly in index.db — NO copy, NO separate file.
-- Each returns the raw integer (src_id,dst_id); it touches only the `edge`/`entity_edge`
-- tables, never the (millions of rows) `symbol` table. Soufflé reads them by name.
-- Idempotent: safe to re-run. Output tables are reset here so a re-run is clean.
--
-- edge.kind:        1 calls 2 inherits 3 contains 4 specializes 5 instantiates
--                   6 overrides 7 uses 8 field_of 9 method_of 17 friend
-- entity_edge.kind: 1 generalizes 2 implements 3 specializes 4 composes 5 aggregates
--                   6 associates 7 creates 8 uses 9 destroys 10 befriends 11 instantiates

-- base graph edges
DROP VIEW IF EXISTS calls;     CREATE VIEW calls     AS SELECT src_id AS a, dst_id AS b FROM edge WHERE kind=1;
DROP VIEW IF EXISTS inherits;  CREATE VIEW inherits  AS SELECT src_id AS a, dst_id AS b FROM edge WHERE kind=2;
DROP VIEW IF EXISTS overrides; CREATE VIEW overrides AS SELECT src_id AS a, dst_id AS b FROM edge WHERE kind=6;
DROP VIEW IF EXISTS uses;      CREATE VIEW uses      AS SELECT src_id AS a, dst_id AS b FROM edge WHERE kind=7;
DROP VIEW IF EXISTS field_of;  CREATE VIEW field_of  AS SELECT src_id AS a, dst_id AS b FROM edge WHERE kind=8;
DROP VIEW IF EXISTS method_of; CREATE VIEW method_of AS SELECT src_id AS a, dst_id AS b FROM edge WHERE kind=9;

-- design-level entity edges
DROP VIEW IF EXISTS e_generalizes; CREATE VIEW e_generalizes AS SELECT src_id AS a, dst_id AS b FROM entity_edge WHERE kind=1;
DROP VIEW IF EXISTS e_implements;  CREATE VIEW e_implements  AS SELECT src_id AS a, dst_id AS b FROM entity_edge WHERE kind=2;
DROP VIEW IF EXISTS e_composes;    CREATE VIEW e_composes    AS SELECT src_id AS a, dst_id AS b FROM entity_edge WHERE kind=4;
DROP VIEW IF EXISTS e_aggregates;  CREATE VIEW e_aggregates  AS SELECT src_id AS a, dst_id AS b FROM entity_edge WHERE kind=5;
DROP VIEW IF EXISTS e_associates;  CREATE VIEW e_associates  AS SELECT src_id AS a, dst_id AS b FROM entity_edge WHERE kind=6;
DROP VIEW IF EXISTS e_creates;     CREATE VIEW e_creates     AS SELECT src_id AS a, dst_id AS b FROM entity_edge WHERE kind=7;
DROP VIEW IF EXISTS e_uses;        CREATE VIEW e_uses        AS SELECT src_id AS a, dst_id AS b FROM entity_edge WHERE kind=8;
DROP VIEW IF EXISTS e_destroys;    CREATE VIEW e_destroys    AS SELECT src_id AS a, dst_id AS b FROM entity_edge WHERE kind=9;

-- reset Soufflé output tables + the seed table (idempotent re-run; these are the ONLY
-- rows the reasoning layer adds to index.db — drop them anytime to remove all trace).
DROP TABLE IF EXISTS subtype;
DROP TABLE IF EXISTS edep;
DROP TABLE IF EXISTS reach;
DROP TABLE IF EXISTS seed;
CREATE TABLE seed(x INTEGER);
