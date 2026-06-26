-- cidx_views.sql — adapter VIEWs exposing the cidx graph as Soufflé input relations.
--
-- The Soufflé layer reads each relation via `.input rel(IO=sqlite,...)`, which does
-- `SELECT * FROM rel`. cidx stores edges as integer (src_id,dst_id,kind) triples, so
-- these views join through `symbol` and re-key every edge by the symbol's USR
-- (NOT NULL UNIQUE → no node merging). `symname` maps USR → a human-readable name.
--
-- Idempotent: safe to re-run. Run against a COPY of index.db (see run.sh), never the
-- canonical ~/.cache/cidx/index.db.

DROP VIEW IF EXISTS symname;
CREATE VIEW symname AS
  SELECT usr, COALESCE(qual_name, spelling, usr) AS name FROM symbol;

------------------------------------------------------------------- base graph edges
-- edge.kind:  1 calls  2 inherits  3 contains  4 specializes  5 instantiates
--             6 overrides  7 uses  8 field_of  9 method_of  17 friend
DROP VIEW IF EXISTS calls;
CREATE VIEW calls AS
  SELECT s.usr AS caller, d.usr AS callee
  FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=1;

DROP VIEW IF EXISTS inherits;
CREATE VIEW inherits AS
  SELECT s.usr AS derived, d.usr AS base
  FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=2;

DROP VIEW IF EXISTS overrides;
CREATE VIEW overrides AS
  SELECT s.usr AS method, d.usr AS base_method
  FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=6;

DROP VIEW IF EXISTS uses;
CREATE VIEW uses AS
  SELECT s.usr AS src, d.usr AS dst
  FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=7;

DROP VIEW IF EXISTS field_of;
CREATE VIEW field_of AS
  SELECT s.usr AS field, d.usr AS record
  FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=8;

DROP VIEW IF EXISTS method_of;
CREATE VIEW method_of AS
  SELECT s.usr AS method, d.usr AS cls
  FROM edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=9;

----------------------------------------------------- design-level entity_edge graph
-- entity_edge.kind: 1 generalizes 2 implements 3 specializes 4 composes 5 aggregates
--                   6 associates 7 creates 8 uses 9 destroys 10 befriends 11 instantiates
DROP VIEW IF EXISTS e_generalizes;
CREATE VIEW e_generalizes AS
  SELECT s.usr AS sub, d.usr AS super
  FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=1;

DROP VIEW IF EXISTS e_implements;
CREATE VIEW e_implements AS
  SELECT s.usr AS impl, d.usr AS iface
  FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=2;

DROP VIEW IF EXISTS e_composes;
CREATE VIEW e_composes AS
  SELECT s.usr AS owner, d.usr AS part
  FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=4;

DROP VIEW IF EXISTS e_aggregates;
CREATE VIEW e_aggregates AS
  SELECT s.usr AS owner, d.usr AS part
  FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=5;

DROP VIEW IF EXISTS e_associates;
CREATE VIEW e_associates AS
  SELECT s.usr AS src, d.usr AS dst
  FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=6;

DROP VIEW IF EXISTS e_creates;
CREATE VIEW e_creates AS
  SELECT s.usr AS src, d.usr AS dst
  FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=7;

DROP VIEW IF EXISTS e_uses;
CREATE VIEW e_uses AS
  SELECT s.usr AS src, d.usr AS dst
  FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=8;

DROP VIEW IF EXISTS e_destroys;
CREATE VIEW e_destroys AS
  SELECT s.usr AS src, d.usr AS dst
  FROM entity_edge e JOIN symbol s ON s.id=e.src_id JOIN symbol d ON d.id=e.dst_id WHERE e.kind=9;
