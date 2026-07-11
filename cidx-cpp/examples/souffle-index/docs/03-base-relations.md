# Base relations: `cidx_base.dl`

## What the script does

`cidx_base.dl` is the shared Datalog prelude included by all numbered examples.
It defines the `Sym` type, declares SQLite inputs, and provides reusable
transitive relations for class hierarchy, entity dependencies, seeded call
reachability, and forward/reverse call cones.

## Explain the code

The prelude has three input families:

- symbol/site facts: `symbol_fact`, `callable_fact`, `template_arg_fact`,
  `call_site_fact`;
- Layer-0 edges: calls, inheritance, overrides, instantiation, uses and
  ownership;
- Layer-1 edges: generalization, implementation, composition, aggregation,
  association, creation, usage and destruction.

`subtype` closes raw inheritance and design-level generalization/implementation
transitively. `edep` closes design dependencies transitively. `reach`, `cg_out`,
and `cg_in` demonstrate seeded fixpoint patterns.

`callable_fact` maps the collision-safe graph identity to a full callable
signature. Rules should continue joining graph edges by identity and add the
signature only to presentation/output relations.

Each `.input` uses `IO=sqlite, dbname="index.db"`; therefore the process must
run from the directory containing the database. Souffle interns symbol strings,
so readable names do not imply string comparisons at every internal join.

## How to run it

The prelude is not executed alone. Include it from another rule file:

```prolog
#include "cidx_base.dl"

.decl direct_user(source:Sym, target:Sym)
direct_user(s, t) :- uses(s, t).

.output direct_user(IO=stdout)
```

Run the rule through the shared runner so the views and include path are ready:

```bash
./run.sh my_experiment.dl
```

When adding a native edge kind, add a matching view in `cidx_views.sql` and a
matching `.decl`/`.input` pair in this prelude.
