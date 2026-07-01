# CXQ V2 Demo

CXQ V2 is a declarative code-graph query language for the cidx index.
V2 adds `path` (ordered route) and `rank` (top-N by metric) on top of
the V1 declarative core.

## V2 Grammar (concise)

```
# V1 core
match <kind> <var> [, <kind> <var>]* [where <conditions>] select <projections>

kind        ::= function | method | class | struct | interface | record | ...
conditions  ::= pred [and|or pred]* | not pred
pred        ::= var.attr op value              # attribute predicate
              | lhs calls+  rhs               # transitive call closure
              | lhs calls*  rhs               # reflexive-transitive
              | lhs inherits+ rhs             # transitive inheritance
              | lhs implements+ rhs           # same (alias)
op          ::= = | != | ~ | in
projection  ::= var | var.attr | count(var)

# V2: path
show path from "A" to "B" via calls|inherits

# V2: rank (prefix or postfix form)
rank var in match ... select var by count(callers+|callers|callees+|callees var) [desc] [limit N]
match ... select var rank var by count(...) [desc] [limit N]
```

## Shared Query Set (A–F)

### A. Attribute match — functions whose name contains "rank"

```
match function f where f.name ~ "rank" select f
```

Output: `chain::top_rank`

### B. Relation/join — classes that inherit from geo::Shape

```
match class c where c inherits+ "geo::Shape" select c
```

Output: `geo::Circle`, `geo::Rectangle`

### C. Closure — everything reachable from main via calls+

```
match function f where "main" calls+ f select f
```

Output: 30 functions reachable (add, mid, twice, leaf_a, leaf_b, ...)

### D. Hierarchy closure — all descendants of geo::Shape

```
match class c where c inherits+ "geo::Shape" select c
```

Output: `geo::Circle`, `geo::Rectangle`

### E. Path — call ROUTE from main to app::normalize

```
show path from "main" to "app::normalize" via calls
```

Output:
```
main -> app::run_pipeline -> app::stage_process -> app::transform -> app::normalize
(4 hops)
```

### F. Rank — top 10 functions by blast radius (transitive-caller count)

```
rank f in match function f select f by count(callers+ f) desc limit 10
```

Output:
```
score=4  app::normalize
score=4  std::__1::move
score=3  leaf_a
score=3  leaf_b
score=3  recurse
score=3  app::transform
score=3  std::__1::operator+
score=3  org::project::net::connect
score=2  mid
score=2  multiply
```

## Ergonomics Assessment

**What reads cleanly:**
- `match ... where ... select` is SQL-adjacent and immediately legible to any developer.
- Closure predicates (`"main" calls+ f`) read as "root reaches var" — very natural.
- `show path from A to B via calls` is fully self-documenting.
- `rank f by count(callers+ f) desc limit 10` maps directly to "top-N by blast radius".

**`path` operator feel:**
- Natural. The `via calls|inherits` qualifier keeps it typed to one edge flavour.
- The `A -> B -> C` output form is immediately readable.
- Limitation: returns only the shortest path; `show all paths` / `top N paths` would be
  a useful extension.

**`rank` operator feel:**
- Clean in prefix form: `rank f in match ... by count(callers+ f)` reads like a
  SQL wrapper.
- The postfix form (`match ... select f rank f by ...`) avoids nesting but looks
  slightly non-standard without parentheses around the inner query.
- The metric vocabulary (`callers+` = blast radius, `callees+` = fan-out) is meaningful
  to call-graph practitioners.

**Awkwardness:**
- The rank variable must match the match binding by name — implicit coupling that's easy
  to get wrong (`rank f in match function g` would fail silently).
- `count(callers+ f)` repeats `f`; since `rank f` already names it, `by callers+` alone
  would be less redundant.
- No multi-metric ranking (e.g. `by callers+ * callees+`).
- `path` does not support composed edge types (`calls or inherits`).
