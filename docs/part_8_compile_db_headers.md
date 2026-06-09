# Part 8 — Compilation Databases in Depth (and how to get flags for headers)

[← Part 7 — Capstone Project](part_7_capstone_project.md) | [Lab Index →](README.md)

> **Reference part, not a lesson.** [Part 4 §4.5](part_4_preprocessor_diagnostics.md)
> taught the *mechanics* — load `compile_commands.json`, strip the driver/`-c`/`-o`/source
> tokens, feed the rest to `parse()`. Part 8 goes deeper for `cidx` (Part 7): what
> the file really contains, how it's generated, and the problem §4.5 never had to
> face — **headers are not in it.** Snippets are copy-pasteable; there is no shipped
> script.

## Why a Compilation Database Exists

A real project's compiler flags are not guessable. `-I` paths, `-D` defines,
`-std`, `-isystem` for third-party headers, target `-arch` — they're decided by
the build system, per file, and they *change the AST* ([§4.3](part_4_preprocessor_diagnostics.md)).
Parse a TU with the wrong flags and you get a truncated or wrong AST, silently.

`compile_commands.json` is the build system's answer: a JSON array recording the
**exact** command used to compile **each translation unit**. It is the contract
between a build and every Clang-based tool — clangd, clang-tidy, and your `cidx`
all read the same file. The format is the
[Clang JSON Compilation Database spec](https://clang.llvm.org/docs/JSONCompilationDatabase.html).

---

## What's Actually in the File

One object per **compiled translation unit**. From the real librdkafka DB you
built in `test-repo/`:

```json
{
  "directory": "/…/librdkafka/build/src",
  "command":   "/usr/bin/cc -Drdkafka_EXPORTS -I/…/librdkafka/src -isystem /opt/homebrew/include -arch arm64 -fPIC -o CMakeFiles/rdkafka.dir/crc32c.c.o -c /…/librdkafka/src/crc32c.c",
  "file":      "/…/librdkafka/src/crc32c.c",
  "output":    "CMakeFiles/rdkafka.dir/crc32c.c.o"
}
```

| Field | Meaning | Why it matters |
|---|---|---|
| `directory` | CWD the command ran from | **Relative `-I.` / `-Ibuild` resolve against this**, not your CWD |
| `file` | the TU being compiled | `parse()` takes this separately — strip it from the flags |
| `command` *or* `arguments` | the invocation | **Two mutually-exclusive forms** (see below) |
| `output` | the `.o` path (optional) | ignore for parsing |

### `command` (string) vs `arguments` (array)

The spec allows **either** key, never both:

| Form | Shape | Producer | Splitting |
|---|---|---|---|
| `"command"` | one shell string | CMake (what librdkafka emitted), most tools | you must shell-split it (`shlex.split`) |
| `"arguments"` | pre-split list | Bear, `compiledb`, some Ninja paths | use as-is, **do not** re-split |

`CompilationDatabase.getCompileCommands(file)[i].arguments` normalizes both forms
to an iterator for you — but if you read the JSON yourself (often faster for an
indexer), handle both:

```python
import json, shlex
def raw_args(entry):
    if "arguments" in entry:
        return list(entry["arguments"])      # already split — never re-split
    return shlex.split(entry["command"])     # CMake-style string form
```

> librdkafka's DB uses the **`command`** form, has an **`output`** field, and zero
> `-std` (it's C, compiler default). Don't assume any field's presence — probe it.

---

## How the File Gets Generated

You rarely write it by hand. Know the generators so you can produce one for any
project `cidx` is pointed at:

| Build system | Command |
|---|---|
| **CMake** (used here) | `cmake -S <src> -B <build> -DCMAKE_EXPORT_COMPILE_COMMANDS=ON` → `build/compile_commands.json` |
| **Make / autotools / arbitrary** | `bear -- make` (intercepts exec calls) |
| **Make (pure-Python)** | `compiledb make` |
| **Meson** | writes it automatically into the build dir |
| **Ninja** | `ninja -t compdb > compile_commands.json` |
| **Bazel** | `bazel run @hedron_compile_commands//:refresh_all` |

Tools look for it in the **project root** (often a symlink to the build dir's
copy). For `cidx`, take the path explicitly via `--db` and don't guess.

> **Generated headers / sources must exist first.** CMake writes `command` entries
> that `-I` into `build/generated/…`. If you parse before configuring/building,
> those includes 404 and the AST truncates. Configure (and, for codegen-heavy
> projects, build) before indexing.

---

## Extracting the Flags You Keep

This is the §4.5 strip recipe, restated as the full rule set. Starting from
`raw_args(entry)`, **drop**:

| Token | Why |
|---|---|
| `argv[0]` (`cc`, `/usr/bin/cc`, `clang`, `c++`) | the driver, not a flag |
| `-c` | "compile only" — no meaning to a parser |
| `-o <out>` | output file — the flag **and** its argument |
| the `file` value | the source — `parse()` supplies it |
| `@response.rsp` | a response file — **expand it first** (read the file, splice its tokens in) |
| `--` | separator some `getCompileCommands` paths inject |

**Keep** everything semantic: `-I`, `-isystem`, `-iquote`, `-D`, `-U`, `-std=`,
`-include`, `-arch`, `-f…` feature flags, `-W…` (harmless), and target/sysroot
flags. Then **resolve relative include paths against `entry["directory"]`**,
because `parse()` does not run from there:

```python
import os
def strip_for_libclang(entry):
    args, out, srcs = [], None, {entry["file"], os.path.basename(entry["file"])}
    it = iter(raw_args(entry)[1:])            # drop argv[0]
    for tok in it:
        if tok == "-c" or tok == "--":            continue
        if tok == "-o": next(it, None);           continue   # drop flag + its arg
        if tok in srcs:                            continue   # the source filename
        # resolve relative -I / -isystem against the command's directory
        for flag in ("-I", "-isystem", "-iquote"):
            if tok == flag:                                   # space-separated form
                p = next(it, ""); args += [flag, _abs(p, entry["directory"])]; break
            if tok.startswith(flag) and len(tok) > len(flag): # glued form -I.
                args.append(flag + _abs(tok[len(flag):], entry["directory"])); break
        else:
            args.append(tok)
    return args

def _abs(p, base):
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))
```

Then — on **this** machine — append the macOS sysroot + builtin-header includes
that the pip libclang wheel lacks ([§1.2](part_1_foundations.md)):

```python
args = strip_for_libclang(entry) + clang_args()[1:]   # [1:] drops clang_args's leading -std
tu = parse(entry["file"], args=args)
```

`clang_args()` is additive here: the DB gives project flags, `clang_args()` gives
the toolchain flags the wheel is missing. You need both.

---

## The Header Problem (the real subject of this part)

Run the numbers on the librdkafka DB you built:

```
TUs listed in compile_commands.json : 98   (all .c / .cpp)
header files on disk (src, src-cpp)  : 105  (.h / .hpp)
headers listed in the DB             : 0
```

**A compilation database lists only translation units.** Headers are *compiled
into* TUs via `#include`; they are never a compile target, so they have **no
entry**. Yet for `cidx` you very much want to index `rdkafka.h` — it declares the
public API — and to precompile it as a PCH ([§6.3b](part_6_advanced_production.md))
you need *some* command line to parse it with. The DB gives you none.

Every Clang tool hits this. Here is how to solve it, weakest to strongest.

### Strategy 1 — Borrow a sibling TU's flags (clangd's default)

When clangd opens a header with no DB entry, it finds a TU in the **same
directory** (or the nearest one), takes that command, and **substitutes the
filename**. The reasoning: files compiled together in a target almost always share
`-I`/`-D`/`-std`. This is correct often enough to be the industry default.

```python
def flags_for_header(db_entries, header_path):
    hdr_dir = os.path.dirname(header_path)
    # 1) prefer a TU in the same directory
    same = [e for e in db_entries if os.path.dirname(e["file"]) == hdr_dir]
    pick = (same or _nearest_by_path(db_entries, header_path))[0]
    return strip_for_libclang(pick)          # reuse its -I/-D/-std, drop its source
```

### Strategy 2 — Borrow from a TU that *actually includes* the header

Stronger, because it guarantees the header is reachable with those flags. Build an
include map (you're walking every TU for `cidx` anyway): record, per TU, which
headers its `INCLUSION_DIRECTIVE`s / `tu.get_includes()` pulled in
([§4.4](part_4_preprocessor_diagnostics.md)). Then for a header, pick any TU that
includes it and reuse that command. `src/rdkafka_int.h` is included by many TUs;
any of their flag sets will resolve it.

```
TU walk (you do this regardless)  ──▶  header → {including TUs}
header with no DB entry  ──▶  pick an includer  ──▶  its stripped flags
```

### Strategy 3 — Parse the header *as a header* (for PCH)

To precompile a header (`cidx pch`), don't parse it as a `.c`. Use the
header-mode flags from §6.3b on top of a borrowed flag set:

```python
args = flags_for_header(db, "src/rdkafka.h") + clang_args()[1:] + ["-x", "c-header"]
hdr_tu = index.parse("src/rdkafka.h", args=args,
                     options=cx.TranslationUnit.PARSE_INCOMPLETE)
hdr_tu.save(".cidx/pch/rdkafka.pch")
```

`-x c-header` is what makes it a reusable PCH; `PARSE_INCOMPLETE` tolerates the
missing `main`/undefined symbols a header alone has.

### Strategy 4 — Project-wide fallback command

If a header matches no directory and no includer (rare — generated or
standalone), fall back to a single representative command: the most common flag
set across the DB, or a hand-specified `--default-flags`. Log when you use it, so
a wrong index entry is traceable, not silent.

### The resolver, as `cidx` should ship it

```
header_flags(header):
    if header has an includer TU      -> that TU's stripped flags     (Strategy 2)
    elif a TU shares its directory    -> that TU's stripped flags     (Strategy 1)
    else                              -> project default + a warning  (Strategy 4)
    always += clang_args()[1:]                                        (toolchain)
```

This is the single most important design decision in `cidx` that the §6.7 capstone
never had to make — and the reason a real indexer's output covers headers at all.

---

## Gotchas Checklist

| Gotcha | Symptom | Fix |
|---|---|---|
| Relative `-I` not resolved | header not found, AST truncated | join against `entry["directory"]` |
| `command` vs `arguments` confusion | flags split wrong or double-split | branch on which key exists |
| Headers expected in the DB | `getCompileCommands(header)` → empty | use the header resolver above |
| Generated includes missing | `-Ibuild/generated/...` 404s | configure/build the project first |
| Response files (`@file.rsp`) | half the flags vanish | expand the file before stripping |
| `clang_args()` omitted | `stddef.h not found` fatal (§1.2) | always append toolchain flags |
| Per-file flag variance assumed away | one TU's `-D` wrongly applied to another | resolve flags **per file**, never globally |

---

## Checkpoint

| Concept | What You Can Now Do |
|---|---|
| DB schema | Read `directory`/`file`/`command`\|`arguments`/`output`; handle both command forms |
| Generation | Produce a DB from CMake/Bear/Ninja/Meson/Bazel for any project |
| Flag extraction | Strip driver/`-c`/`-o`/source, resolve relative `-I`, append `clang_args()` |
| **Header flags** | Resolve flags for a file with **no DB entry** via includer/sibling/default — the key to indexing and precompiling headers |

With this, `cidx` can index *every* file in librdkafka — the 98 TUs the DB lists
**and** the 105 headers it doesn't.

---

[← Part 7 — Capstone Project](part_7_capstone_project.md) | [Lab Index →](README.md)
