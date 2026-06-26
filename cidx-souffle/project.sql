-- project.sql — fast projection: read the (possibly huge) cidx index READ-ONLY and
-- copy ONLY the integer edge tables into a small sidecar (build/graph.db). No 4 GiB copy,
-- no 12M-row symbol load into Soufflé. Run as:
--   sqlite3 build/graph.db < project.sql   (with index path passed via .param / ATTACH below)
--
-- The caller ATTACHes the real index read-only as `src` before sourcing this file:
--   ATTACH 'file:/abs/path/index.db?immutable=1' AS src;
--
-- edge.kind:        1 calls 2 inherits 3 contains 4 specializes 5 instantiates
--                   6 overrides 7 uses 8 field_of 9 method_of 17 friend
-- entity_edge.kind: 1 generalizes 2 implements 3 specializes 4 composes 5 aggregates
--                   6 associates 7 creates 8 uses 9 destroys 10 befriends 11 instantiates

PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;

-- base graph edges (integer src_id,dst_id) — tiny vs the full index
CREATE TABLE calls     AS SELECT src_id AS a, dst_id AS b FROM src.edge WHERE kind=1;
CREATE TABLE inherits  AS SELECT src_id AS a, dst_id AS b FROM src.edge WHERE kind=2;
CREATE TABLE overrides AS SELECT src_id AS a, dst_id AS b FROM src.edge WHERE kind=6;
CREATE TABLE uses      AS SELECT src_id AS a, dst_id AS b FROM src.edge WHERE kind=7;
CREATE TABLE field_of  AS SELECT src_id AS a, dst_id AS b FROM src.edge WHERE kind=8;
CREATE TABLE method_of AS SELECT src_id AS a, dst_id AS b FROM src.edge WHERE kind=9;

-- design-level entity edges
CREATE TABLE e_generalizes AS SELECT src_id AS a, dst_id AS b FROM src.entity_edge WHERE kind=1;
CREATE TABLE e_implements  AS SELECT src_id AS a, dst_id AS b FROM src.entity_edge WHERE kind=2;
CREATE TABLE e_composes    AS SELECT src_id AS a, dst_id AS b FROM src.entity_edge WHERE kind=4;
CREATE TABLE e_aggregates  AS SELECT src_id AS a, dst_id AS b FROM src.entity_edge WHERE kind=5;
CREATE TABLE e_associates  AS SELECT src_id AS a, dst_id AS b FROM src.entity_edge WHERE kind=6;
CREATE TABLE e_creates     AS SELECT src_id AS a, dst_id AS b FROM src.entity_edge WHERE kind=7;
CREATE TABLE e_uses        AS SELECT src_id AS a, dst_id AS b FROM src.entity_edge WHERE kind=8;
CREATE TABLE e_destroys    AS SELECT src_id AS a, dst_id AS b FROM src.entity_edge WHERE kind=9;

-- NO symname projection: 12M-row name map never enters Soufflé. Names are joined back
-- onto the (small) RESULT rows only, read-only from src, after Soufflé runs (see run.sh).
