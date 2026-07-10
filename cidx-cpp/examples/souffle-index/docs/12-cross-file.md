# Cross-file dependencies: `09_cross_file.dl`

## What the script does

This experiment builds a file-level semantic dependency graph from cross-file
call edges. It reports each source-file/target-file pair and the number of
indexed caller/callee pairs contributing to that dependency.

## Explain the code

`symbol_file` projects annotated symbols and their defining files from
`symbol_fact`, excluding missing file names.

`file_call` joins `calls` to the caller and callee file mappings and excludes
same-file calls. `file_dependency` groups by source and target file and counts
the contributing semantic call pairs.

Only the aggregate relation is output. This avoids printing every cross-file
call on large applications. It is a semantic call-dependency graph, not a
textual `#include` graph.

## How to run it

```bash
./run.sh 09_cross_file.dl
```

Rank the strongest file dependencies:

```bash
./run.sh 09_cross_file.dl |
  sort -t $'\t' -k3,3nr |
  head -20
```

Profile the global aggregation:

```bash
/usr/bin/time -p ./run.sh \
  --jobs 1 \
  --profile /tmp/cross-file.json \
  09_cross_file.dl >/tmp/cross-file.out
```

This experiment is global but approximately linear/join-based over call edges;
its output is bounded by file pairs rather than call-edge count.
