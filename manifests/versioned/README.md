# versioned — component version-detection example

A tiny library whose sources live under a **version-named trailing directory**
(`lib/1.2.0/`). It exercises cidx's trailing-segment version detection
(`pathx.split_base_version`, regex `^v?[0-9]+([._-][0-9]+)*$`) and confirms
`import` records the **right information**: the version is stored on the
component, the component's `path` is the base *without* the version, and the
version is not duplicated into `directory.path`.

```
manifests/versioned/
└── lib/
    └── 1.2.0/            ← trailing segment is a version string
        ├── mathx.h
        ├── mathx.c
        └── compile_commands.json
```

## How version detection is reached

`import` derives the component root from `git_root(first_source)` and only
falls back to trailing-segment detection when there is no enclosing git repo.
Because `manifests/` lives inside the libclang-lab repo, a **plain** `import`
roots the component at the repo (name `libclang-lab`, version `(none)`) and the
version is *not* detected. To anchor at the versioned directory, register it
explicitly with `--no-git` first, then import:

```bash
# Use an isolated index so the standard ~/.cache/cidx/index.db is untouched.
export INDEXER_CACHE=/tmp/cidx_versioned

cidx add-source --no-git --name mathx --path manifests/versioned/lib/1.2.0
cidx import     --db                          manifests/versioned/lib/1.2.0
cidx index
cidx component show mathx
```

## Expected `component show mathx`

```
name           mathx
kind           repo
base path      <repo>/manifests/versioned/lib
version        1.2.0
effective root <repo>/manifests/versioned/lib/1.2.0
resolved root  <repo>/manifests/versioned/lib/1.2.0
```

## Expected stored rows

| table       | column    | value                                   |
|-------------|-----------|-----------------------------------------|
| `component` | `path`    | `<repo>/manifests/versioned/lib` (no version) |
| `component` | `version` | `1.2.0`                                 |
| `directory` | `path`    | `` (empty — file sits at the effective root) |
| `file`      | `name`    | `mathx.c` (basename only)               |

The absolute path is reconstructed on read as
`component.path / component.version / directory.path / file.name`. Python and
the C++ binary print `component show` byte-identically for this component.

To override or disable detection: `--version V` forces a version,
`--no-detect-version` keeps the trailing segment as a plain directory.
