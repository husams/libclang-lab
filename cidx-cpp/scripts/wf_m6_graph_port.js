export const meta = {
  name: 'cidx-m6-graph-cpp-port',
  description: 'Backport cidx graph command group (8 subcommands) to C++ — byte-identical Python parity, Architect→SrDev→Dev⇄QA loop',
  phases: [
    { title: 'Architect', detail: 'design C++ graph port + write ADR-007' },
    { title: 'Plan', detail: 'detailed build-ready implementation plan' },
    { title: 'Implement', detail: 'C++ Developer implements per plan; build; commit' },
    { title: 'QA', detail: 'ctest + independent byte-diff sweep vs Python; structured verdict' },
  ],
}

const CTX = `
REPO FACTS (cidx M6 — backport the \`graph\` command group to C++):
- Git root & repo root: /Users/husam/workspace/qemu-vms/libclang-lab. Branch \`feat/cidx-cpp-graph\` is ALREADY checked out — work on it; do NOT create another branch; do NOT open a PR or merge.
- GOAL: port the Python \`graph\` group (8 subcommands: callers, callees, refs, neighbors, walk, path, hierarchy, dispatch) to the C++ binary, with BYTE-IDENTICAL stdout (text AND --json) and identical exit codes vs Python for the same index + inputs. This is the read-side graph-query layer, currently Python-only (like \`ast\` was before M5).
- Python reference to mirror EXACTLY:
  * project/indexer/query.py  — the ~1697-line GraphQuery engine (read-side graph traversal + output formatting).
  * project/indexer/cli.py    — the graph CLI block: cmd_graph_callers/callees/refs/neighbors/walk/path/hierarchy/dispatch (~lines 1094-1697) + the graph argparse subparser tree.
- Run Python cidx (the ORACLE): \`.venv/bin/python -m indexer graph <sub> ...\` from the repo root.
- C++ project: cidx-cpp/. Binary: cidx-cpp/build/cidx. Build: \`cmake --build cidx-cpp/build -j8\`. Test: \`cd cidx-cpp/build && ctest -j8\` (baseline 21/21 green; M6 adds graph tests). Same wheel libclang 18.1.1 as Python.
- REUSE the M5 \`ast\` scaffolding (do NOT reinvent): cidx-cpp/src/cli/{json_out,format,commands,args}.{cpp,hpp}.
  * cli/json_out is a byte-replica of Python json.dumps(indent=2). json_out::Value::of is a constrained integral template — pass ints freely.
  * The --usr/--id/--name symbol selector + resolver pattern and cli/format helpers already exist (used by \`ast\`). Mirror them; reuse the resolver for ambiguous/missing-symbol behavior.
  * Structural template: cidx-cpp/docs/adr/ADR-006-cpp-ast-port.md.
- Dispatch switch where \`graph\` plugs in: cidx-cpp/src/cli/commands.cpp (run_command). src/graph/ exists but is EMPTY — it is the new home for the graph engine.
- Storage: cidx-cpp/src/storage/{records.hpp,storage.hpp,storage.cpp} currently has edge/edge_site WRITE accessors ONLY. Add NET-NEW read-only edge/edge_site traversal accessors that mirror query.py's SQL EXACTLY, including ORDER BY (stable output). \`graph\` reads index.db ONLY — it never parses source.
- argparse fidelity: mirror the Python tree exactly — NO prefix abbreviation, exit code 2 on arg errors, --usr/--id/--name/--kind selectors + per-subcommand flags (--depth/--kinds/--limit/etc). Capture verbatim help with: \`COLUMNS=80 .venv/bin/python -m indexer graph <sub> -h\`.
- HARD CONSTRAINTS: no product-version bump; no schema change (stays v13); NO Python changes; do NOT open a PR or merge. Stop at a green branch.
- DOGFOOD the cidx indexer to reason about the code (not only grep/Read). The standard index covers cidx-cpp. From repo root:
  * .venv/bin/python -m indexer graph callees --name run_command     (the dispatch switch where graph plugs in, commands.cpp)
  * .venv/bin/python -m indexer graph callers|callees|refs|neighbors|hierarchy|dispatch --name <Sym>
  * .venv/bin/python -m indexer ast dump|locals --name <fn> <file> -- <flags>
  * the cidx-graph skill (read-only graph API over index.db).
- Parity corpus: dual-index manifests/graphlab (rich graph corpus) + manifests/project, with BOTH tools on the SAME libclang (the harness pins CIDX_LIBCLANG_LIB), then diff every \`graph <sub>\` (text + --json + error/exit paths).
- Lesson from M5: ctest passing is NOT proof. The M5 ast port had a hollow green that hid error-path bugs. Real parity comes from hand byte-diffs on the SAME libclang + adversarial error-path review.
`

const QA_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['green', 'ctest_all_pass', 'ctest_summary', 'subcommands', 'blockers', 'notes'],
  properties: {
    green: { type: 'boolean', description: 'true ONLY if ctest is ALL-pass AND all 8 subcommands are byte-identical (text + json + error paths + exit codes)' },
    ctest_all_pass: { type: 'boolean' },
    ctest_summary: { type: 'string', description: 'e.g. "29/29 passed" plus any failing test names' },
    subcommands: {
      type: 'array',
      description: 'one entry per subcommand verified',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['name', 'text_identical', 'json_identical', 'error_paths_identical'],
        properties: {
          name: { type: 'string' },
          text_identical: { type: 'boolean' },
          json_identical: { type: 'boolean' },
          error_paths_identical: { type: 'boolean' },
          diff_detail: { type: 'string', description: 'empty if identical; else the exact diff summary' },
        },
      },
    },
    blockers: {
      type: 'array',
      description: 'one per byte-diff or failing test; empty iff green',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['summary', 'repro', 'expected', 'actual'],
        properties: {
          summary: { type: 'string' },
          repro: { type: 'string', description: 'exact command(s) to reproduce' },
          expected: { type: 'string', description: 'Python oracle output (relevant lines)' },
          actual: { type: 'string', description: 'C++ output (relevant lines)' },
          cpp_location: { type: 'string', description: 'file:line in cidx-cpp to fix, best guess' },
        },
      },
    },
    notes: { type: 'string' },
  },
}

// ---- Phase 1: Architect ----
phase('Architect')
const arch = await agent(
  `You are the ARCHITECT for cidx M6 (C++ graph port).
${CTX}
TASK:
1. Dogfood cidx + Read to fully understand: query.py's GraphQuery (each subcommand's traversal + output formatting), the cli.py graph block (argparse tree + cmd_graph_* handlers), the M5 ast scaffolding (json_out, cli/format, the --usr/--id/--name selector + resolver, commands.cpp dispatch), and the existing Storage edge WRITE accessors + the records.hpp / schema (v13) edge & edge_site tables.
2. Design the C++ port: module layout under src/graph/; the new read-only Storage accessors (one per query.py SQL query, each with its EXACT ORDER BY); how each of the 8 subcommands maps to (a) accessor calls, (b) traversal logic, (c) text + json formatting via json_out; how the argparse tree is mirrored; how it plugs into commands.cpp run_command. Enumerate every place byte-identical output is at risk (result ordering, path tie-breaks, dispatch target sets, stub/external-symbol formatting, empty results, ambiguous/missing symbol, --kinds/--depth/--limit, exit codes).
3. WRITE the ADR to cidx-cpp/docs/adr/ADR-007-cpp-graph-port.md, mirroring ADR-006's structure (context, decision, module layout, accessor list with SQL, per-subcommand mapping, parity strategy, risks, alternatives).
RETURN (your final message = the data the Senior Developer consumes): a concise but COMPLETE design summary — module layout, the full list of new Storage accessors with their SQL + ORDER BY, the 8 subcommand mappings, and the enumerated byte-parity risks. Cite exact file:line where useful.`,
  { agentType: 'architect', label: 'architect' },
)
log('Architect done; ADR-007 drafted')

// ---- Phase 2: Senior Developer ----
phase('Plan')
const plan = await agent(
  `You are the SENIOR DEVELOPER for cidx M6 (C++ graph port). The Architect's design + ADR-007:
<<ARCHITECT DESIGN>>
${arch}
<<END ARCHITECT DESIGN>>
${CTX}
TASK: produce a DETAILED, build-ready implementation plan the C++ Developer will follow verbatim. Dogfood cidx + Read query.py / cli.py for the specifics. Your plan MUST include:
- Ordered increments (e.g. 1: storage read accessors + unit tests; 2: arg-tree + dispatch wiring; 3..10: one subcommand at a time), each independently buildable & committable.
- Exact C++ signatures for every new Storage accessor and the SQL string it runs (copy query.py's SQL incl ORDER BY verbatim; note column order and any binding).
- For EACH of the 8 subcommands: the exact Python source to mirror (query.py method + cmd_graph_* in cli.py, with line refs), the C++ function signature, the traversal/formatting steps, and a GOLDEN EXAMPLE — a concrete command plus the expected first ~15 lines of TEXT output AND a --json snippet, captured by actually RUNNING the Python oracle against an index of manifests/graphlab.
- The argparse-mirror spec per subcommand (flags, metavars, help text) captured VERBATIM via \`COLUMNS=80 .venv/bin/python -m indexer graph <sub> -h\`.
- The parity_check.sh graph-block spec: exactly what to dual-index, which commands to diff (text + --json + error/exit), and the expected exit codes.
- The enumerated byte-parity hazards, each with its concrete mitigation.
RETURN: the full plan as your final message (this IS the spec handed to the developer). Be exhaustive and concrete; prefer real captured oracle output over prose.`,
  { agentType: 'senior-developer', label: 'senior-dev' },
)
log('Plan ready; entering Implement<->QA loop')

// ---- Phase 3/4: Developer <-> QA loop ----
const MAX_ITERS = 6
let qa = null
let devReport = null
let iters = 0

function devPrompt(iter, qaVerdict) {
  const base = `You are the C++ DEVELOPER for cidx M6 (C++ graph port).
${CTX}
THE IMPLEMENTATION PLAN (follow it):
<<PLAN>>
${plan}
<<END PLAN>>
`
  if (iter === 1) {
    return base + `
TASK (first implementation pass):
- Implement the full plan: the new read-only Storage edge/edge_site accessors (mirror query.py SQL incl ORDER BY exactly), the src/graph/ engine, all 8 subcommands (callers, callees, refs, neighbors, walk, path, hierarchy, dispatch), the argparse-mirror, and wire into commands.cpp run_command.
- REUSE json_out / cli/format / the --usr/--id/--name selector+resolver scaffolding; do NOT reinvent.
- Extend cidx-cpp/scripts/parity_check.sh with the graph block per the plan, and register/keep it under ctest.
- Add ctest suites for the new code (a storage-accessor test + a graph-command test mirroring the existing cli/ast test style); register them in cidx-cpp/tests/CMakeLists.txt.
- BUILD after each increment: \`cmake --build cidx-cpp/build -j8\`. As you go, self-check byte-identity on representative commands vs the Python oracle (\`.venv/bin/python -m indexer graph ...\` vs cidx-cpp/build/cidx graph ...) using \`diff <(...) <(...)\`.
- Commit incrementally on the current branch (feat/cidx-cpp-graph) with clear messages. Do NOT bump the version, change schema (stays v13), touch any Python, or open a PR/merge.
RETURN: a summary of what you implemented, which files changed, commit hashes, build status, ctest status, and any byte-diffs you could not yet resolve.`
  }
  return base + `
TASK (FIX pass, iteration ${iter}): QA found the previous pass NOT byte-identical and/or ctest not all-green. Fix EVERY blocker below, rebuild, re-self-check vs the Python oracle, and commit. Do NOT regress already-passing subcommands.
<<QA VERDICT (previous iteration)>>
${JSON.stringify(qaVerdict, null, 2)}
<<END QA VERDICT>>
For each blocker: reproduce with the Python oracle (the \`repro\` field), find the C++ root cause (use the \`cpp_location\` hint), fix it, rebuild, and verify the exact command now diffs byte-identical. Commit incrementally on feat/cidx-cpp-graph. Same hard constraints (no version bump, schema stays v13, no Python changes, no PR).
RETURN: per-blocker resolution (root cause + fix + the file:line changed + verification that the command now diffs clean), changed files, commit hashes, build + ctest status.`
}

function qaPrompt(iter) {
  return `You are QA for cidx M6 (C++ graph port). Independently verify — do NOT trust the developer's self-report; the M5 ast port had a hollow green. Iteration ${iter}.
${CTX}
THE SPEC (the 8 subcommands + their expected behavior + golden examples):
<<PLAN>>
${plan}
<<END PLAN>>
TASK:
1. Build: \`cmake --build cidx-cpp/build -j8\`. Then run the FULL suite: \`cd cidx-cpp/build && ctest -j8\`. Record pass/fail counts and any failing test names; the new graph parity_check block MUST pass.
2. INDEPENDENT byte-diff sweep — the real acceptance bar. Dual-index manifests/graphlab AND manifests/project with BOTH tools on the SAME libclang (CIDX_LIBCLANG_LIB pinned). For EACH of the 8 subcommands (callers, callees, refs, neighbors, walk, path, hierarchy, dispatch) diff C++ (cidx-cpp/build/cidx graph ...) vs the Python oracle (.venv/bin/python -m indexer graph ...) for:
   - default TEXT output, --json output, and error/edge paths: result ordering, dispatch target sets, path tie-breaks, stub/external-symbol formatting, empty results, --kinds / --depth / --limit, ambiguous --name, missing symbol, and EXIT CODES.
   Use \`diff <(cppcmd 2>&1; echo "exit=$?") <(pycmd 2>&1; echo "exit=$?")\` and report EXACT diffs with a best-guess C++ source location (file:line) for each mismatch.
3. Verify argparse help (\`COLUMNS=80 ... -h\`) for the group and each subcommand matches, and exit-2 on bad/abbreviated args.
PASS CRITERIA: green = true ONLY IF ctest is ALL-pass AND all 8 subcommands are byte-identical (text + json + error paths + exit codes). Any single diff or failing test ⇒ green=false with a precise blocker per issue.
RETURN the structured verdict (the provided schema). For every mismatch include a blocker with summary, exact repro command, expected (Python) vs actual (C++) output, and cpp_location.`
}

for (let i = 1; i <= MAX_ITERS; i++) {
  iters = i
  phase(`Implement (iter ${i})`)
  devReport = await agent(devPrompt(i, qa), { agentType: 'developer', label: `dev:iter${i}` })

  phase(`QA (iter ${i})`)
  qa = await agent(qaPrompt(i), { agentType: 'qa-engineer', schema: QA_SCHEMA, label: `qa:iter${i}` })

  if (!qa) {
    log(`QA iter ${i} returned null (agent died) — stopping loop`)
    break
  }
  log(`QA iter ${i}: green=${qa.green} ctest=${qa.ctest_summary} blockers=${qa.blockers ? qa.blockers.length : '?'}`)
  if (qa.green) break
}

return {
  green: qa ? qa.green : false,
  iterations: iters,
  ctest: qa ? qa.ctest_summary : 'unknown',
  open_blockers: qa && qa.blockers ? qa.blockers.map(b => b.summary) : [],
  subcommand_status: qa ? qa.subcommands : [],
  last_dev_report: devReport,
  qa_notes: qa ? qa.notes : '',
}
