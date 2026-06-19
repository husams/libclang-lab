# versioned — component version-detection examples

Two tiny libraries whose sources live under a **version-named trailing
directory**. They exercise cidx's trailing-segment version detection
(`pathx.split_base_version`, regex `^v?[0-9]+([._-][0-9]+)*$`) and confirm
`import` records the **right information**: the version is stored on the
component, the component's `path` is the base *without* the version, and the
version is not duplicated into `directory.path`.

- **`lib/1.2.0/`** — a plain C library (numeric version `1.2.0`).
- **`cpplib/v1.4.0/`** — a real C++ library (namespace, virtual base + override,
  template) with a `v`-prefixed version, exercising C++ symbol/graph extraction
  on a versioned directory.

```
manifests/versioned/
├── lib/
│   └── 1.2.0/            ← trailing segment is a version string (C)
│       ├── mathx.h
│       ├── mathx.c
│       └── compile_commands.json
└── cpplib/
    └── v1.4.0/           ← "v"-prefixed version (C++)
        ├── widget.hpp
        ├── widget.cpp
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

## C++ example: `cpplib/v1.4.0`

```bash
export INDEXER_CACHE=/tmp/cidx_cpplib
cidx add-source --no-git --name cpplib --path manifests/versioned/cpplib/v1.4.0
cidx import     --db                          manifests/versioned/cpplib/v1.4.0
cidx index
cidx component show cpplib          # version -> v1.4.0
cidx graph hierarchy --name ui::Widget --first   # subclass: ui::Button
```

Verified identical between the Python and C++ binaries (each building its own
index from the same libclang): `component show`, `list symbols` (13 symbols:
`ui::Widget`/`ui::Button` with the inheritance edge, methods, ctors, destructor,
members, the `ui::clamp` template), and `graph hierarchy` (text + `--json`).
The stored rows show `component.version = v1.4.0`, empty `directory.path`, and
basenames in `file` — same shape as the C example.
