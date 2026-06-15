# cidx index schema (v7) — for custom `g.sql()` queries

The helpers cover the common cases. Drop to `g.sql("SELECT …")` (read-only) for
anything else. This is the layout you're querying.

## Tables

```
component(id, name, path, kind)              one repo ('repo') or external lib ('external')
directory(id, component_id, path)            path relative to component.path ('' = root)
file(id, directory_id, name, mtime, md5,
     compile_options, driver, indexed,       compile_options = JSON list of parse args
     indexed_at)                             driver = compile-command argv[0]
symbol(id, usr, spelling, qual_name,
       display_name, kind, type_info,
       file_id, line, col,                   definition site (NULL until a body is seen)
       decl_file_id, decl_line, decl_col,    declaration site (e.g. the prototype)
       is_definition, is_pure, linkage,
       access, parent_usr, resolved)         resolved=0 + spelling='' => minted stub
edge(id, src_id, dst_id, kind, count,        kind -> edge_kind.id; UNIQUE(src,dst,kind)
     base_access, is_virtual, vtable_slot)   base_access/is_virtual: inherits edges
edge_site(edge_id, file_id, line, col,       one row per concrete occurrence
          conditional, args_sig)             conditional=1 => inside #if / template
edge_kind(id, name)                          1..9, see below
template_param(owner_id, position,           generic parameters of a template
               param_kind, name, default_txt)  param_kind 1=type 2=non-type 3=tmpl 4=pack
template_arg(owner_id, position, arg_kind,   arguments at an instantiation
             ref_id, literal)                ref_id->symbol for type args, else literal
meta(key, value)                             schema_version, graph_resolved_at
```

## Edge kinds (`edge.kind` → `edge_kind.name`)

| id | name | src → dst | direction meaning |
|----|------|-----------|-------------------|
| 1 | `calls` | caller → callee | function/method call |
| 2 | `inherits` | derived → base | class inheritance (`base_access`, `is_virtual`) |
| 3 | `contains` | scope → child | namespace→member, record→nested type |
| 4 | `specializes` | specialization → primary template | |
| 5 | `instantiates` | use site → template | template instantiation |
| 6 | `overrides` | overriding method → overridden (base) method | **dynamic dispatch** |
| 7 | `uses` | user → used symbol | type/variable use (non-call) |
| 8 | `field_of` | field → owning record | data member membership |
| 9 | `method_of` | method → owning record | method membership |

Direction matters for traversal: e.g. to find a base class's subclasses, follow
`inherits` **inbound** (`direction="in"`); to find a base method's overriders,
follow `overrides` **inbound** — which is exactly what `dispatch_targets` does.

## Reconstructing a file path
A symbol/edge stores `file_id`, not a path. Join up:
```sql
SELECT c.path || '/' || (CASE WHEN d.path != '' THEN d.path || '/' ELSE '' END) || f.name
FROM file f JOIN directory d ON d.id=f.directory_id JOIN component c ON c.id=d.component_id
WHERE f.id = ?
```
(The `Graph` already does this and caches it — prefer `Sym.file` / `Site.file`.)

## Useful indexes (queries that stay fast)
`symbol(spelling, qual_name, file_id, parent_usr, kind)`, `edge(src_id, kind)`,
`edge(dst_id, kind)`. Filter on these columns; a `count`/recursive query over
`edge` using `src_id`/`dst_id` + `kind` is index-backed.

## Recursive traversal in pure SQL (alternative to `walk`)
```sql
WITH RECURSIVE reach(id, depth) AS (
    SELECT :start, 0
    UNION
    SELECT e.dst_id, r.depth+1 FROM edge e JOIN reach r ON e.src_id = r.id
    WHERE e.kind = 1 AND r.depth < 4
)
SELECT DISTINCT s.qual_name FROM reach r JOIN symbol s ON s.id = r.id;
```
Prefer `g.walk()` / `g.reaches()` — they bound results and rebuild paths for you.
