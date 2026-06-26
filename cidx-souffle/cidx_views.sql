-- cidx_views.sql — expose cidx's edges to Soufflé as NAME-keyed relations, IN PLACE.
--
-- Lightweight VIEWs created directly in index.db (NO copy, NO separate file). Each edge is
-- re-keyed by the symbol's qualified name so rules read in real names, e.g.
--   calls("rd_kafka_produceva", "rd_kafka_msg_new").
-- COALESCE(qual_name, spelling, usr) guarantees a non-null, stable key (usr is unique).
-- Soufflé interns these strings to integers internally, so reasoning stays fast; only the
-- view's symbol join (to resolve names) costs more than a raw-id view.
-- Idempotent: safe to re-run. Output tables are reset here so a re-run is clean.
--
-- edge.kind:        1 calls 2 inherits 3 contains 4 specializes 5 instantiates
--                   6 overrides 7 uses 8 field_of 9 method_of 17 friend
-- entity_edge.kind: 1 generalizes 2 implements 3 specializes 4 composes 5 aggregates
--                   6 associates 7 creates 8 uses 9 destroys 10 befriends 11 instantiates

-- base graph edges (src name, dst name)
DROP VIEW IF EXISTS calls;     CREATE VIEW calls     AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=1;
DROP VIEW IF EXISTS inherits;  CREATE VIEW inherits  AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=2;
DROP VIEW IF EXISTS overrides; CREATE VIEW overrides AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=6;
DROP VIEW IF EXISTS uses;      CREATE VIEW uses      AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=7;
DROP VIEW IF EXISTS field_of;  CREATE VIEW field_of  AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=8;
DROP VIEW IF EXISTS method_of; CREATE VIEW method_of AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=9;

-- design-level entity edges
DROP VIEW IF EXISTS e_generalizes; CREATE VIEW e_generalizes AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=1;
DROP VIEW IF EXISTS e_implements;  CREATE VIEW e_implements  AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=2;
DROP VIEW IF EXISTS e_composes;    CREATE VIEW e_composes    AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=4;
DROP VIEW IF EXISTS e_aggregates;  CREATE VIEW e_aggregates  AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=5;
DROP VIEW IF EXISTS e_associates;  CREATE VIEW e_associates  AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=6;
DROP VIEW IF EXISTS e_creates;     CREATE VIEW e_creates     AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=7;
DROP VIEW IF EXISTS e_uses;        CREATE VIEW e_uses        AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=8;
DROP VIEW IF EXISTS e_destroys;    CREATE VIEW e_destroys    AS SELECT COALESCE(s.qual_name,s.spelling,s.usr) AS a, COALESCE(d.qual_name,d.spelling,d.usr) AS b FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=9;

-- reset Soufflé output tables + the seed table (idempotent re-run; these are the ONLY
-- rows the reasoning layer adds to index.db — drop them anytime to remove all trace).
-- Drop both view and table forms so a re-run is clean regardless of prior state.
DROP VIEW  IF EXISTS subtype; DROP TABLE IF EXISTS subtype;
DROP VIEW  IF EXISTS edep;    DROP TABLE IF EXISTS edep;
DROP VIEW  IF EXISTS reach;   DROP TABLE IF EXISTS reach;
DROP VIEW  IF EXISTS seed;    DROP TABLE IF EXISTS seed;
CREATE TABLE seed(x TEXT);
