export const meta = {
  name: 'cidx-entity-edge',
  description:
    'cidx Layer-1 entity_edge (UML/ER) materialization — Design/Plan + adversarial Gate (Phase A), then gated Implement (PR1 Layer-0 extraction ‖ v17 schema → PR2 roll-up) + Review (Phase B/C). Orchestrates only; all git is the human.',
  phases: [
    { title: 'Design', detail: 'Architect finalizes ADR-008 ‖ Senior Dev authors the implementation plan' },
    { title: 'Gate', detail: 'Senior Dev + QA adversarially review BOTH artifacts; loop design→gate until both APPROVE' },
    { title: 'Implement-PR1', detail: '(Phase B, gated) Layer-0 extraction ast.py+ast.cpp ‖ v17 schema/storage scaffolding — worktree-isolated' },
    { title: 'Implement-PR2', detail: '(Phase B, gated) resolve-style entity_edge roll-up (depends on PR1)' },
    { title: 'Review', detail: '(Phase C, gated) QA full pytest+ctest+parity+graphlab; Senior Dev code-review' },
  ],
}

// ──────────────────────────────────────────────────────────────────────────
// Run parameterization. THIS invocation stops after the Phase-A gate.
//   args.stopAfter : 'A' (default — design+gate only) | 'B' | 'C' (full run)
// Phase B/C run ONLY when the human re-invokes with stopAfter:'B'/'C'
// (resumeFromRunId replays the cached Phase-A gate, then continues live).
// ──────────────────────────────────────────────────────────────────────────
// Normalize args — the harness may deliver it as an object OR a JSON string.
// (Observed: a fresh run received args as a stringified JSON, so args.stopAfter
// was undefined and STOP_AFTER silently fell back to 'A'.) Parse defensively.
let ARGS = args
if (typeof ARGS === 'string') {
  try { ARGS = JSON.parse(ARGS) } catch (e) { ARGS = {} }
}
if (!ARGS || typeof ARGS !== 'object') ARGS = {}
const STOP_AFTER = ARGS.stopAfter || 'A'
const MAX_GATE_ITERS = 3

// Artifact paths (the ONLY files Phase-A agents may write).
const ADR_PATH = 'cidx-cpp/docs/adr/ADR-008-entity-edge.md'
const ARCHLOG_PATH = 'cidx-cpp/docs/adr/architect-log-entity-edge.md'
const PLAN_PATH = 'cidx-cpp/docs/DESIGN_entity_edge_plan.md'

// ──────────────────────────────────────────────────────────────────────────
// Shared, pre-verified context. Every fact here was confirmed against the live
// tree in the orchestrating session — agents must NOT re-derive these.
// ──────────────────────────────────────────────────────────────────────────
const CTX = `
PROJECT: cidx — a libclang symbol-indexer + call-graph tool with TWO ports kept at byte-identical parity:
  * Python (the ORACLE, feature-complete): project/indexer/ (cli.py, storage.py, clang/ast.py, query.py, model.py)
  * C++ (mirror): cidx-cpp/src/ (cli/{args,commands}.{hpp,cpp}, storage/storage.{hpp,cpp}, clangx/ast.cpp)
  Repo root / git root: /Users/husam/workspace/qemu-vms/libclang-lab (run python cidx from repo root).

THE WORK (Layer-1 entity_edge): materialize a design-altitude UML/ER entity graph over the existing
Layer-0 symbol graph. GOAL = build the MATERIALIZED PRIMITIVES (a new entity_edge table + a resolve-style
roll-up that writes it). The query engine that reads them is EXPLICITLY OUT OF SCOPE (separate, later).

══ LOCKED CONTRACT — do NOT re-open, re-litigate, or "improve" any of these ══
- Goal = materialized Layer-1 primitives only. Query engine is later/separate.
- Concepts D-1..D-4 LOCKED; relations R-1..R-4 LOCKED. R-1 retired: an Entity IS a record/enum SYMBOL
  (filter symbol.kind ∈ {class,struct,union,enum}); NO separate entity table; entity_edge endpoints
  reference symbol(id).
- NEW table 'entity_edge', ALL-INTEGER / ZERO TEXT. The ONLY strings in the feature = the 11 rows of the
  'entity_edge_kind' lookup table.
- 11 kinds: 1 generalizes, 2 realizes, 3 specializes, 4 composes, 5 aggregates, 6 associates, 7 creates,
  8 uses, 9 destroys, 10 nests, 11 befriends.
- Columns: src_id, dst_id (record/enum symbol ids), kind (FK→entity_edge_kind), count,
  via_member_id (carrying field/method — ALSO the role, name is one join away), multiplicity (int enum
  1=one,2=0..1,3=0..*,4=N), access (int 0=pub,1=prot,2=priv), is_virtual (0/1 virtual base),
  create_form (int enum 1=ctor_call,2=return,3=value,4=temp,5=heap,6=factory,7=copy,8=move — creates/destroys only),
  partial (0/1 ⊤-soundness flag). UNIQUE(src_id,dst_id,kind,via_member_id). idx (src,kind)+(dst,kind).
- realizes XOR generalizes: a Layer-0 inherits(A,B) emits EXACTLY ONE — realizes(2) iff Interface(B)
  (record whose methods are ALL pure-virtual AND has NO data fields), else generalizes(1). Never both.
- creates/destroys = the RICH version (full create_form 1-8). NO degraded half-feature.
- Roll-up = cidx-resolve-style, DB-ONLY, NO reparse, GLOBAL phase, wired into resolve_pass().
  Re-materialize = DELETE FROM entity_edge + re-run (idempotent full rebuild each resolve).
- Schema TARGET = v17 (additive). v16 is already consumed (symbol-kind-as-int).

══ VERIFIED TREE FACTS (confirmed live in the orchestrating session — treat as ground truth) ══
- CURRENT product version = "0.16.0" in BOTH ports: Python project/indexer/cli.py:68 (VERSION) and
  C++ cidx-cpp/src/cli/args.hpp:27 (kVersion). (A 'cidx migrate' subcommand consumed 0.16.0 — git HEAD.)
  ⇒ THE ADR's version baseline is STALE: the prior draft says current=0.15.0 and PR1=0.15.0→0.16.0,
  PR2=0.16.0→0.17.0. CORRECT it: PR1 (Layer-0 extraction, NO schema) = 0.16.0 → 0.17.0 (MINOR);
  PR2 (v17 entity_edge) = 0.17.0 → 0.18.0 (MINOR), schema v16→v17. Bump rule: cidx-version-bump-rule —
  both ports byte-identical, agent picks level by semver (these are additive ⇒ MINOR).
- CURRENT schema version = 16 in BOTH ports: Python storage.py:35 (SCHEMA_VERSION=16) + the _SCHEMA
  literal (storage.py:68); C++ storage.hpp:30 (kSchemaVersion=16) + storage.cpp:221. kSchema MUST stay
  BYTE-IDENTICAL to Python's rendered _SCHEMA (parity gate).
- Roll-up hook: Python Storage.resolve_pass() = storage.py:1777; add_edge = storage.py:1599;
  replace_diagnostics = storage.py:1152; symbol_kind seed table storage.py:158; edge_kind seed
  storage.py:169 (SEED-ONLY, no-FK since v0.15.0 — new kinds need ZERO schema change). C++ resolve_pass
  ≈ storage.cpp:1950 (verify exact line — the ADR cites it).
- Layer-0 extraction site: Python project/indexer/clang/ast.py — _classify_value_source = ast.py:665,
  _body_descent = ast.py:1031, _emit_type_use = ast.py:497, _emit_overloaded_calls = ast.py:842.
  C++ mirror cidx-cpp/src/clangx/ast.cpp — classify_value_source = ast.cpp:829, body_descent = ast.cpp:1682,
  emit_type_use = ast.cpp:178, emit_overloaded_calls = ast.cpp:1132; CXXNewExpr(134) handled ast.cpp:890.
- OQ-1 (RESOLVED, the reason PR1 exists): _classify_value_source COLLAPSES 'new B' (CXX_NEW_EXPR, ast.py:731)
  and a ctor CALL_EXPR (ast.py:717-725) to the SAME src_kind "construct" — so the construction FORM is LOST
  TODAY. There is NO CXX_DELETE_EXPR / CXX_CONSTRUCT_EXPR / CXX_TEMPORARY_OBJECT_EXPR handler ⇒ delete/destroy
  is invisible and the factory route (make_unique<B> callee in <memory> system header, <B> arg not recovered)
  is dead. PR1 must persist the FORM as DISTINCT Layer-0 edge_kind seed ids (NOT a call_arg.create_form
  column — a default-ctor 'B b;' has no call_arg row, so the column has nowhere to attach). PR2 maps those
  Layer-0 form edges → entity_edge.create_form 1-8. By-value-return (form 2) is NOT a Layer-0 edge (no ctor
  cursor under RVO) — it stays DERIVED in PR2 from the return type via the type-classification kernel.
- Type-classification kernel: ONE shared classify_referent(type) helper drives has-a
  (composes/aggregates/associates) + factory-create + by-value-return; unwraps unique/shared/weak/raw-ptr/
  ref/container, recovers referent B via template_arg.ref_id (ADR-004) or canonical-spelling→symbol join —
  NEVER by string-parsing USRs. Pure-DB roll-up, NO reparse; it WRITES the on-disk table ⇒ MUST be at
  byte-identical Py↔C++ parity (NOT model.py-exempt).
- Acceptance fixture GAP (the ADR flags it): creates/destroys roll a construction site up to the ENCLOSING
  method's owner record — but graphlab's current new/delete sites live in FREE functions (no owning record
  ⇒ no entity_edge src). A method-scoped fixture (a class method that heap-allocates AND deletes another
  entity) does NOT yet exist in manifests/graphlab and is REQUIRED before PR2 lock.
- graphlab corpus = manifests/graphlab/ (C++17): shapes/creatures(multi-inherit)/chain(4-level)/containers
  (templates+specializations)/pipeline(deep calls, new/delete in FREE fns)/nested(namespaces)/cache+UseCache
  (member templates)/devirt3/instantiations/main + its own compile_commands listing. memory graphlab-test-project.

══ EXISTING ARTIFACTS (a prior session already drafted these — REVISE, do NOT clobber) ══
- ${ADR_PATH} — ADR-008, Status: accepted. Strong and largely correct; its ONLY known defects are the
  STALE version baseline (see above) and it must stay consistent with this CTX. Read it, fix the version
  numbers + any drift, keep the good content.
- ${ARCHLOG_PATH} — the architect log. Update its "Verified anchors" (versions now 0.16.0, not 0.15.0).
- ${PLAN_PATH} — the implementation plan. DOES NOT EXIST YET. The Senior Developer creates it.

══ HARD GUARDRAILS (a prior run violated these — enforce ABSOLUTELY) ══
- NO agent may run ANY git operation: no commit / branch / checkout / switch / merge / rebase / push /
  stash / 'gh pr' / tag. The workflow ORCHESTRATES ONLY. ALL git, commit, and merge decisions are the
  human's, done by hand AFTER review. (Phase B worktree isolation is the runtime's job, not yours.)
- Phase-A agents write ONLY these files: the Architect → ${ADR_PATH} and ${ARCHLOG_PATH}; the Senior
  Developer → ${PLAN_PATH}. NOTHING else. No code edits, no scratch/log files, no other docs.
- Do NOT create branches or worktrees yourself. Do NOT touch another session's branches/worktrees or any
  unrelated working-tree changes (a concurrent session may be active on main). NEVER rewrite history.
- You MAY freely READ any file, run read-only shells (grep/ls/cat/sed -n/python -m indexer ... read-only),
  and dogfood the cidx indexer to ground yourself. Read-only only.
`

// ──────────────────────────────────────────────────────────────────────────
// Gate verdict schema (machine-readable so the script can branch).
// ──────────────────────────────────────────────────────────────────────────
const GATE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['verdict', 'blockers', 'notes'],
  properties: {
    verdict: {
      type: 'string',
      enum: ['APPROVE', 'REQUEST_CHANGES'],
      description: 'APPROVE only if BOTH artifacts are implementation-ready with NO blocker/major issue',
    },
    blockers: {
      type: 'array',
      description: 'one entry per issue; empty iff verdict=APPROVE',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['severity', 'target', 'issue', 'fix'],
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          target: { type: 'string', enum: ['adr', 'plan', 'both'], description: 'which artifact must change' },
          issue: { type: 'string', description: 'the concrete defect, with a file/section anchor' },
          fix: { type: 'string', description: 'the specific change required to clear it' },
        },
      },
    },
    fixtures_verified: {
      type: 'boolean',
      description: 'QA only: did you actually ls/read manifests/graphlab and confirm which acceptance fixtures exist vs are missing (esp. the method-scoped new/delete fixture)?',
    },
    fixtures_detail: {
      type: 'string',
      description: 'QA only: per-acceptance-row, which graphlab file backs it and whether it exists today',
    },
    notes: { type: 'string' },
  },
}

const approved = (v) => v && v.verdict === 'APPROVE'
const blockersFor = (verdicts, target) =>
  verdicts
    .filter(Boolean)
    .flatMap((v) => v.blockers || [])
    .filter((b) => b.severity !== 'minor' && (b.target === target || b.target === 'both'))

// ══════════════════════════════════════════════════════════════════════════
// PHASE A — DESIGN (parallel) then adversarial GATE (loop until both APPROVE)
// ══════════════════════════════════════════════════════════════════════════
async function runPhaseADesignGate() {
phase('Design')
log('Phase A: Architect finalizes ADR-008 ‖ Senior Dev authors the implementation plan')

const [archSummary, planSummary] = await parallel([
  // ── Architect: finalize ADR-008 + architect-log to the LOCKED contract ──
  () =>
    agent(
      `You are the ARCHITECT for cidx Layer-1 entity_edge.
${CTX}

TASK — FINALIZE (revise in place, do NOT rewrite from scratch) ${ADR_PATH} and ${ARCHLOG_PATH}:
1. READ the existing ${ADR_PATH} and ${ARCHLOG_PATH} in full. They are already Status: accepted and largely
   correct — your job is to make them EXACTLY consistent with the LOCKED contract + VERIFIED TREE FACTS above.
2. FIX the stale version baseline everywhere it appears: current product is 0.16.0 (NOT 0.15.0). PR1 (Layer-0
   extraction, no schema) = 0.16.0 → 0.17.0; PR2 (v17 entity_edge) = 0.17.0 → 0.18.0 (schema v16→v17). Update
   the ADR's §7 "Versioning + two-PR build order", the "Hard constraints" version note, and the architect-log
   "Verified anchors" (0.16.0, not 0.15.0; confirm SCHEMA_VERSION=16, kSchemaVersion=16). Re-verify the exact
   C++ resolve_pass() line in storage.cpp (the ADR cites ~1950 — confirm or correct by grepping).
3. Confirm the ADR fully and unambiguously records the LOCKED decisions: roll-up placement (global phase on
   resolve_pass, DB-only, no reparse, index does NOT write entity_edge); re-materialize (DELETE+full rebuild,
   idempotent); int-enum expansion with zero schema change; the shared classify_referent kernel (parity-bound,
   NOT model.py-exempt); partial=1 soundness; realizes XOR generalizes via Interface(B); template-instance
   collapse onto primary; the PR1 create_form-as-distinct-edge-kind decision (NOT a call_arg column, because a
   default-ctor 'B b;' has no call_arg row) and the Layer-0→Layer-1 form mapping (value→3,temp→4,heap→5,
   factory→6,copy→7,move→8,ctor-call→1; by-value-return→2 derived in PR2; destroy→kind 9).
4. Keep/strengthen the "Consequences" acceptance-fixture caveat: graphlab's new/delete are in FREE functions
   (no owning record ⇒ no entity_edge src), so a NEW method-scoped new/delete graphlab fixture is required
   before PR2 lock. Make this explicit so the plan/QA own it.
5. Do NOT relitigate any LOCKED decision. Do NOT expand scope to the query engine.

WRITE ONLY ${ADR_PATH} and ${ARCHLOG_PATH}. No code, no other files, NO git.
RETURN (your final message = data the gate + the human consume): a concise changelog of what you changed in
the ADR/log (esp. the version-baseline fixes + any anchor corrections), and a 6-8 bullet summary of the final
architecture (roll-up placement, kernel, soundness, realizes-xor-generalizes, expansion, build order/versions).`,
      { agentType: 'architect', label: 'architect', phase: 'Design' },
    ),

  // ── Senior Developer: author the implementation plan (new file) ──
  () =>
    agent(
      `You are the SENIOR DEVELOPER for cidx Layer-1 entity_edge.
${CTX}

TASK — AUTHOR a build-ready implementation plan at ${PLAN_PATH} (it does NOT exist yet). Read the existing
${ADR_PATH} (the accepted architecture) as your design source, plus the VERIFIED TREE FACTS above. Dogfood
cidx + Read the real code anchors so every reference is concrete. The plan MUST contain:

A. PR BREAKDOWN — two PRs, each independently buildable + reviewable:
   - PR1 (Layer-0 extraction, NO schema change, product 0.16.0→0.17.0): add CXX_NEW_EXPR / CXX_DELETE_EXPR /
     CXX_CONSTRUCT_EXPR / CXX_TEMPORARY_OBJECT_EXPR handling + factory template-arg recovery (make_unique<B>/
     make_shared<B> → recover B) + a by-value-return flag, in BOTH ast.py and ast.cpp. Persist the construction/
     destruction FORM as DISTINCT new edge_kind SEED ids (seed-only, no schema bump) — NOT a call_arg column.
     Specify the exact new edge_kind ids + names you propose (construct-value/temp/heap/copy/move, factory-
     construct, destroy) and where they seed (storage.py:169 edge_kind / C++ mirror).
   - PR2 (v17 entity_edge, schema v16→v17, product 0.17.0→0.18.0): the entity_edge + entity_edge_kind schema
     (byte-identical Py _SCHEMA ↔ C++ kSchema), the seed, the new global materialize_entity_edges() phase wired
     into resolve_pass() (Py storage.py:1777 / C++ storage.cpp resolve_pass), the shared classify_referent
     kernel, all 11 kinds incl. rich creates/destroys (map Layer-0 form edges→create_form 1-8), realizes XOR
     generalizes, template-instance collapse, partial=1 rules, readers, and model.py typed accessors
     (Python-only / parity-exempt). PR2 depends on PR1.

B. PARITY CHECKLIST — a paired table: for every change, the Python file:line AND the matching C++ file:line,
   so a reviewer can confirm byte/behavior parity. Cover: schema string (storage.py:68 _SCHEMA ↔ storage.cpp
   kSchema), SCHEMA_VERSION 16→17 (storage.py:35 ↔ storage.hpp:30), version 0.16.0→0.17.0→0.18.0 (cli.py:68 ↔
   args.hpp:27), the ast.py↔ast.cpp extraction handlers (anchors in CTX), and materialize_entity_edges() in
   both storage layers. State explicitly that model.py is Python-only/exempt and classify_referent is NOT
   exempt (it writes the table).

C. TEST MATRIX — pytest + ctest cases per PR (extraction unit tests for each new expr kind; schema/migration
   test for v17; roll-up tests asserting each of the 11 kinds; partial=1 assertions; realizes-vs-generalizes
   discrimination). Note that parity_check.sh must GROW to exercise entity_edge (today it does not).

D. GRAPHLAB ACCEPTANCE ROWS — a concrete table: for each entity_edge kind, the graphlab file + the exact
   expected entity_edge row(s) (src/dst entity, via_member, kind, create_form, multiplicity, partial). CRUCIAL:
   identify which rows are covered by EXISTING fixtures vs which need a NEW/extended fixture — in particular the
   method-scoped new/delete fixture for creates(form=5)/destroys (graphlab's current new/delete are in free
   functions ⇒ no record src). Specify the new fixture's shape (a class whose method heap-allocates and deletes
   another entity) and that compile_commands.json must be updated for it (per CLAUDE.md the agent may add the
   entry). Do NOT write the fixture now — just specify it.

WRITE ONLY ${PLAN_PATH}. No code, no fixture, no other files, NO git.
RETURN (your final message = data the gate consumes): a summary of the plan — the PR1/PR2 increments, the
parity-checklist coverage, the test matrix headline, and the list of graphlab acceptance rows flagged as
"needs new fixture".`,
      { agentType: 'senior-developer', label: 'senior-dev', phase: 'Design' },
    ),
])

log('Design artifacts drafted; entering adversarial gate loop')

// ── GATE LOOP: Senior Dev + QA review BOTH artifacts; loop design→gate ──
let gateIter = 0
let srVerdict = null
let qaVerdict = null
let gatePassed = false

for (let i = 1; i <= MAX_GATE_ITERS; i++) {
  gateIter = i
  phase(`Gate (iter ${i})`)

  const reviewBlock = `
THE TWO ARTIFACTS UNDER REVIEW (read them from disk — do not trust these summaries alone):
  * ${ADR_PATH}  (Architect's finalized ADR-008)
  * ${PLAN_PATH} (Senior Developer's implementation plan)
ARCHITECT CHANGE SUMMARY:
${archSummary || '(architect agent returned no summary — treat as a blocker; read the ADR from disk)'}
PLAN SUMMARY:
${planSummary || '(senior-dev agent returned no summary — treat as a blocker; read the plan from disk)'}
`

  ;[srVerdict, qaVerdict] = await parallel([
    // ── Senior Developer review: implementability + parity + version/schema correctness ──
    () =>
      agent(
        `You are the SENIOR DEVELOPER acting as REVIEWER (iteration ${i}). Be adversarial — your job is to find
why this is NOT yet build-ready, not to rubber-stamp it.
${CTX}
${reviewBlock}

REVIEW BOTH artifacts and return a schema'd verdict. Check, concretely:
- VERSION/SCHEMA correctness: does the ADR use the CORRECT baseline (current 0.16.0; PR1→0.17.0; PR2→0.18.0,
  schema v16→v17)? Any lingering 0.15.0/v15→v16 is a BLOCKER (target=adr). Are both ports' anchors right?
- PARITY completeness: does the plan pair EVERY change with Python file:line AND C++ file:line? Is the
  classify_referent kernel correctly marked parity-BOUND (not model.py-exempt)? Is model.py correctly exempt?
- LOCKED-contract fidelity: all-integer/zero-TEXT schema; 11 kinds; UNIQUE(src,dst,kind,via_member); realizes
  XOR generalizes; create_form-as-distinct-edge-kind in PR1 (NOT call_arg column); global resolve-phase roll-up;
  DELETE+rebuild idempotency; partial=1 soundness; template collapse. Flag any drift or re-litigation.
- IMPLEMENTABILITY: are the PR1/PR2 increments concrete enough to code from? Any missing step, hand-wave, or
  under-specified mapping (esp. Layer-0 form edges → create_form 1-8)?
APPROVE only if there are NO blocker/major issues on either artifact. Otherwise REQUEST_CHANGES with one blocker
per issue, each tagged target=adr|plan|both, with a concrete fix. Set notes for anything minor.`,
        { agentType: 'senior-developer', schema: GATE_SCHEMA, label: `gate-srdev:iter${i}`, phase: `Gate (iter ${i})` },
      ),

    // ── QA review: test matrix soundness + ACTUALLY verify graphlab fixtures exist ──
    () =>
      agent(
        `You are QA acting as GATEKEEPER (iteration ${i}). Independently verify — do NOT trust the authors.
${CTX}
${reviewBlock}

TASK and return a schema'd verdict:
1. Review the plan's TEST MATRIX + GRAPHLAB ACCEPTANCE ROWS for soundness and coverage: is every one of the 11
   entity_edge kinds backed by at least one acceptance row? Are partial=1 cases and realizes-vs-generalizes
   discrimination tested? Is the parity_check.sh growth called out?
2. ACTUALLY VERIFY FIXTURES — this is mandatory: ls/read manifests/graphlab/ and confirm, per acceptance row,
   which graphlab file backs it and whether that fixture EXISTS today. In particular CONFIRM the claim that the
   method-scoped new/delete fixture (a class method that heap-allocates AND deletes another entity) is MISSING —
   graphlab's current new/delete sites are in free functions. Report findings in fixtures_detail and set
   fixtures_verified=true once you have actually inspected the directory.
3. A missing-but-UNACKNOWLEDGED fixture is a BLOCKER (target=plan). A missing fixture that the plan EXPLICITLY
   specifies as "needs new fixture" (shape given, compile_commands update noted) is acceptable — that is the
   correct state for a plan (the fixture is built in Phase B, not now).
APPROVE only if the test matrix is sound AND every acceptance row's fixture status is correctly accounted for
(exists, or specified-as-to-build). Otherwise REQUEST_CHANGES with one blocker per gap (target tagged), each
with a concrete fix.`,
        { agentType: 'qa-engineer', schema: GATE_SCHEMA, label: `gate-qa:iter${i}`, phase: `Gate (iter ${i})` },
      ),
  ])

  const srOk = approved(srVerdict)
  const qaOk = approved(qaVerdict)
  log(
    `Gate iter ${i}: srdev=${srVerdict ? srVerdict.verdict : 'NULL'} qa=${qaVerdict ? qaVerdict.verdict : 'NULL'} ` +
      `fixtures_verified=${qaVerdict ? qaVerdict.fixtures_verified : '?'}`,
  )

  if (srOk && qaOk) {
    gatePassed = true
    log(`Gate PASSED on iteration ${i} — both artifacts APPROVED`)
    break
  }
  if (i === MAX_GATE_ITERS) {
    log(`Gate did NOT converge in ${MAX_GATE_ITERS} iterations — stopping for human review`)
    break
  }

  // ── Revise only the artifact(s) with outstanding blockers, in parallel ──
  const verdicts = [srVerdict, qaVerdict]
  const adrBlk = blockersFor(verdicts, 'adr')
  const planBlk = blockersFor(verdicts, 'plan')
  log(`Revising: adr-blockers=${adrBlk.length} plan-blockers=${planBlk.length}`)

  const revisions = []
  if (adrBlk.length) {
    revisions.push(() =>
      agent(
        `You are the ARCHITECT. The gate found blockers in ${ADR_PATH}. Read it, fix EVERY blocker below, and
update ${ARCHLOG_PATH} accordingly. Do NOT regress already-correct content; do NOT relitigate LOCKED decisions.
${CTX}
BLOCKERS (target adr/both):
${JSON.stringify(adrBlk, null, 2)}
WRITE ONLY ${ADR_PATH} and ${ARCHLOG_PATH}. NO git, no code.
RETURN: per-blocker, what you changed (with the section anchor).`,
        { agentType: 'architect', label: `architect-fix:iter${i}`, phase: `Gate (iter ${i})` },
      ),
    )
  }
  if (planBlk.length) {
    revisions.push(() =>
      agent(
        `You are the SENIOR DEVELOPER. The gate found blockers in ${PLAN_PATH}. Read it (and the current
${ADR_PATH}), fix EVERY blocker below. Do NOT regress already-correct content.
${CTX}
BLOCKERS (target plan/both):
${JSON.stringify(planBlk, null, 2)}
WRITE ONLY ${PLAN_PATH}. NO git, no code, no fixture.
RETURN: per-blocker, what you changed (with the section anchor).`,
        { agentType: 'senior-developer', label: `plan-fix:iter${i}`, phase: `Gate (iter ${i})` },
      ),
    )
  }
  if (revisions.length) await parallel(revisions)
}

const phaseAResult = {
  phase: 'A (design + gate)',
  gate_passed: gatePassed,
  gate_iterations: gateIter,
  srdev_verdict: srVerdict ? srVerdict.verdict : 'NULL',
  qa_verdict: qaVerdict ? qaVerdict.verdict : 'NULL',
  qa_fixtures_verified: qaVerdict ? !!qaVerdict.fixtures_verified : false,
  qa_fixtures_detail: qaVerdict ? qaVerdict.fixtures_detail || '' : '',
  open_blockers: [srVerdict, qaVerdict]
    .filter(Boolean)
    .flatMap((v) => v.blockers || [])
    .map((b) => `[${b.severity}/${b.target}] ${b.issue}`),
  artifacts: { adr: ADR_PATH, architect_log: ARCHLOG_PATH, plan: PLAN_PATH },
  notes: {
    srdev: srVerdict ? srVerdict.notes : '',
    qa: qaVerdict ? qaVerdict.notes : '',
  },
}
  return { phaseAResult, gatePassed }
}

// ── Dispatch Phase A: run it live, OR skip when already APPROVED on disk ──
// args.assumePhaseA=true short-circuits the live design+gate (the ADR + plan were
// already gated APPROVE/APPROVE by run wf_66359163-87c, 2 iters, 0 blockers) so a
// fresh stopAfter:'B'/'C' run jumps straight to implementation without re-running
// Phase A. (Resume does NOT propagate new args, so Phase B/C requires a fresh run.)
let phaseAResult
let gatePassed
if (ARGS.assumePhaseA) {
  gatePassed = true
  phaseAResult = {
    phase: 'A (skipped — ADR/plan already APPROVED on disk by run wf_66359163-87c, gate 2 iters, 0 blockers)',
    gate_passed: true,
    gate_iterations: 2,
    srdev_verdict: 'APPROVE',
    qa_verdict: 'APPROVE',
    qa_fixtures_verified: true,
    artifacts: { adr: ADR_PATH, architect_log: ARCHLOG_PATH, plan: PLAN_PATH },
    open_blockers: [],
    notes: { skipped: 'assumePhaseA — using on-disk approved artifacts; Phase A not re-run' },
  }
  log('assumePhaseA=true — skipping live Phase A; using on-disk APPROVED ADR + plan')
} else {
  const r = await runPhaseADesignGate()
  phaseAResult = r.phaseAResult
  gatePassed = r.gatePassed
}

// Stop here when stopAfter=A. Phase B/C run on stopAfter:'B'/'C'.
if (STOP_AFTER === 'A') {
  log('STOP_AFTER=A — Phase A complete. Pausing for human go-ahead before Phase B (implementation).')
  return phaseAResult
}
if (!gatePassed) {
  log('Gate did not pass — refusing to start Phase B. Returning Phase A result for human review.')
  return phaseAResult
}

// ══════════════════════════════════════════════════════════════════════════
// PHASE B — IMPLEMENT (gated; runs only on stopAfter:'B'/'C' AND gate passed)
//   PR1 extraction ‖ v17 schema scaffolding are independent → parallel worktrees.
//   PR2 roll-up depends on PR1 → sequenced after. Each impl stage → QA loop.
//   Agents do NOT commit/merge — they leave green worktrees for the human.
// ══════════════════════════════════════════════════════════════════════════
const IMPL_QA_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['green', 'pytest_summary', 'ctest_summary', 'parity_ok', 'blockers', 'notes'],
  properties: {
    green: { type: 'boolean', description: 'true ONLY if pytest+ctest all pass AND Py↔C++ parity holds for this stage' },
    pytest_summary: { type: 'string' },
    ctest_summary: { type: 'string' },
    parity_ok: { type: 'boolean' },
    blockers: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['summary', 'repro', 'location'],
        properties: {
          summary: { type: 'string' },
          repro: { type: 'string' },
          location: { type: 'string', description: 'file:line to fix' },
        },
      },
    },
    notes: { type: 'string' },
  },
}

const IMPL_GUARD = `
IMPLEMENT-PHASE GUARDRAILS: you are in an ISOLATED git worktree provided by the runtime. Make code edits +
build + run tests there. Do NOT commit, branch, merge, rebase, push, or open a PR — leave the worktree GREEN
and report; the human integrates. Bump versions per the plan (PR1 0.16.0→0.17.0; PR2 0.17.0→0.18.0) in BOTH
ports byte-identical. Keep Python↔C++ byte/behavior parity on every change (model.py is the only exception).
After ANY server/MCP-relevant code change, note in your report that the user must restart the MCP server.`

// ── Stage 1: PR1 extraction ‖ schema scaffolding (independent) → QA each ──
phase('Implement-PR1')
log('Phase B Stage 1: PR1 Layer-0 extraction ‖ v17 schema/storage scaffolding (parallel worktrees)')

const stage1 = await parallel([
  // PR1a: Layer-0 extraction, ast.py + ast.cpp, byte-identical parity
  async () => {
    let qa = null
    let report = null
    for (let k = 1; k <= 4; k++) {
      report = await agent(
        `You are a DEVELOPER implementing PR1 (Layer-0 extraction) for cidx entity_edge — iteration ${k}.
${CTX}
${IMPL_GUARD}
PLAN: read ${PLAN_PATH} §PR1 and the accepted ${ADR_PATH}. Implement in BOTH project/indexer/clang/ast.py and
cidx-cpp/src/clangx/ast.cpp (keep them byte-identical in behavior): CXX_NEW_EXPR/CXX_DELETE_EXPR/
CXX_CONSTRUCT_EXPR/CXX_TEMPORARY_OBJECT_EXPR handling + factory template-arg recovery + by-value-return flag;
persist construction/destruction FORM as DISTINCT new edge_kind SEED ids (seed-only, no schema bump). Add the
extraction unit tests from the plan's test matrix. Build + run pytest and ctest. NO git.
${k > 1 ? `PRIOR QA BLOCKERS to fix:\n${JSON.stringify(qa && qa.blockers, null, 2)}` : ''}
RETURN: files changed, what you implemented, build/pytest/ctest status, any unresolved parity diff.`,
        { agentType: 'developer', label: `pr1-dev:iter${k}`, phase: 'Implement-PR1', isolation: 'worktree' },
      )
      qa = await agent(
        `You are QA for PR1 (Layer-0 extraction) — iteration ${k}. Independently verify in the worktree; do not
trust the developer's self-report.
${CTX}
Verify against ${PLAN_PATH} §PR1: run pytest + ctest (record counts/failures); confirm each new edge_kind is
seeded + emitted; confirm ast.py and ast.cpp are byte-identical in behavior (hand-diff the new handlers on the
same libclang 18.1.1); confirm no schema bump in PR1. green=true ONLY if all pass + parity holds.
RETURN the schema'd verdict; one blocker per failure with a file:line location.`,
        { agentType: 'qa-engineer', schema: IMPL_QA_SCHEMA, label: `pr1-qa:iter${k}`, phase: 'Implement-PR1', isolation: 'worktree' },
      )
      if (qa && qa.green) break
    }
    return { pr: 'PR1-extraction', qa, report }
  },

  // PR1b: v17 schema + entity_edge/entity_edge_kind scaffolding (no roll-up yet)
  async () => {
    let qa = null
    let report = null
    for (let k = 1; k <= 4; k++) {
      report = await agent(
        `You are a DEVELOPER implementing the v17 SCHEMA SCAFFOLDING for cidx entity_edge — iteration ${k}.
${CTX}
${IMPL_GUARD}
PLAN: read ${PLAN_PATH} §PR2 (schema portion) and ${ADR_PATH}. Add the entity_edge + entity_edge_kind tables to
BOTH the Python _SCHEMA (storage.py:68) and C++ kSchema (storage.cpp) BYTE-IDENTICALLY, bump SCHEMA_VERSION
16→17 (storage.py:35 + storage.hpp:30 + the meta insert), seed the 11 entity_edge_kind rows, and add the v17
migration (additive). Do NOT implement the roll-up yet (that is PR2 Stage 2). Add the schema/migration tests.
Build + pytest + ctest. NO git.
${k > 1 ? `PRIOR QA BLOCKERS to fix:\n${JSON.stringify(qa && qa.blockers, null, 2)}` : ''}
RETURN: files changed, schema diff, migration approach, build/pytest/ctest status, byte-identity check of
_SCHEMA vs kSchema.`,
        { agentType: 'developer', label: `schema-dev:iter${k}`, phase: 'Implement-PR1', isolation: 'worktree' },
      )
      qa = await agent(
        `You are QA for the v17 schema scaffolding — iteration ${k}. Verify independently in the worktree.
${CTX}
Confirm: SCHEMA_VERSION=17 in both ports; Python rendered _SCHEMA is BYTE-IDENTICAL to C++ kSchema (diff them);
entity_edge + entity_edge_kind match the LOCKED schema exactly (all-integer, UNIQUE+indexes, 11 seed rows);
the migration is additive + idempotent; pytest + ctest pass. green=true ONLY if all hold.
RETURN the schema'd verdict; one blocker per failure with file:line.`,
        { agentType: 'qa-engineer', schema: IMPL_QA_SCHEMA, label: `schema-qa:iter${k}`, phase: 'Implement-PR1', isolation: 'worktree' },
      )
      if (qa && qa.green) break
    }
    return { pr: 'PR2-schema-scaffold', qa, report }
  },
])

log(
  `Stage 1 done: ${stage1
    .filter(Boolean)
    .map((s) => `${s.pr} green=${s.qa ? s.qa.green : '?'}`)
    .join(' | ')}`,
)

if (STOP_AFTER === 'B') {
  log('STOP_AFTER=B — stopping after Stage 1 (PR1 extraction + schema scaffolding). Pausing for human.')
  return { phaseA: phaseAResult, phaseB_stage1: stage1 }
}

// ── Stage 2: PR2 roll-up (depends on PR1 + schema) → QA loop ──
phase('Implement-PR2')
log('Phase B Stage 2: PR2 materialize_entity_edges() roll-up (depends on PR1 + schema)')

let pr2qa = null
let pr2report = null
for (let k = 1; k <= 5; k++) {
  pr2report = await agent(
    `You are a DEVELOPER implementing PR2 (the entity_edge roll-up) for cidx — iteration ${k}.
${CTX}
${IMPL_GUARD}
NOTE: PR1 (Layer-0 form edges) and the v17 schema scaffolding are assumed integrated (Stage 1). PLAN: read
${PLAN_PATH} §PR2 and ${ADR_PATH}. Implement the new GLOBAL phase materialize_entity_edges() wired into
resolve_pass() (Py storage.py:1777 / C++ storage.cpp resolve_pass), byte-identical across ports: the shared
classify_referent kernel; all 11 entity_edge kinds incl. rich creates/destroys (map Layer-0 form edges →
create_form 1-8; by-value-return→2 derived from return type); realizes XOR generalizes via Interface(B);
template-instance collapse onto primary; partial=1 on every ⊤-incomplete derivation; DELETE+rebuild
idempotency. Add the readers + model.py typed accessors (Python-only/exempt). BUILD the new method-scoped
new/delete graphlab fixture the plan specifies (class method that heap-allocates AND deletes another entity)
and update manifests/compile_commands.json for it (allowed per CLAUDE.md). Add the roll-up + acceptance tests;
grow parity_check.sh to cover entity_edge. Bump product 0.17.0→0.18.0 both ports. Build + pytest + ctest. NO git.
${k > 1 ? `PRIOR QA BLOCKERS to fix:\n${JSON.stringify(pr2qa && pr2qa.blockers, null, 2)}` : ''}
RETURN: files changed, the roll-up design as built, build/pytest/ctest status, and a dump of the entity_edge
rows produced on graphlab.`,
    { agentType: 'developer', label: `pr2-dev:iter${k}`, phase: 'Implement-PR2', isolation: 'worktree' },
  )
  pr2qa = await agent(
    `You are QA for PR2 (entity_edge roll-up) — iteration ${k}. Verify independently in the worktree.
${CTX}
Against ${PLAN_PATH} §PR2 acceptance rows: reindex + resolve manifests/graphlab, then verify the materialized
entity_edge rows match the expected acceptance table (each of the 11 kinds; create_form values; multiplicity;
partial flags; realizes-vs-generalizes correct). Confirm the new method-scoped new/delete fixture exists and
yields creates(form=5)+destroys rows with a record src. Run pytest + ctest + parity_check (must now cover
entity_edge); hand-diff materialize_entity_edges() Py↔C++. Confirm product version 0.18.0 both ports.
green=true ONLY if all pass + parity holds + acceptance rows match.
RETURN the schema'd verdict; one blocker per mismatch with file:line + repro.`,
    { agentType: 'qa-engineer', schema: IMPL_QA_SCHEMA, label: `pr2-qa:iter${k}`, phase: 'Implement-PR2', isolation: 'worktree' },
  )
  if (pr2qa && pr2qa.green) break
}
log(`Stage 2 done: PR2 green=${pr2qa ? pr2qa.green : '?'}`)

if (STOP_AFTER === 'B') {
  return { phaseA: phaseAResult, phaseB_stage1: stage1, phaseB_pr2: { qa: pr2qa, report: pr2report } }
}

// ══════════════════════════════════════════════════════════════════════════
// PHASE C — REVIEW / GATEKEEP (final, gated)
// ══════════════════════════════════════════════════════════════════════════
phase('Review')
log('Phase C: final QA (full pytest+ctest+parity+graphlab) ‖ Senior Dev code-review')

const [finalQa, finalReview] = await parallel([
  () =>
    agent(
      `You are QA for the FINAL gatekeeping of cidx entity_edge (PR1+PR2 integrated in the worktree).
${CTX}
Run the FULL suites: pytest (all), ctest (all incl parity_check), and validate entity_edge on manifests/graphlab
— eyeball the rows AND produce a Mermaid class-diagram dump from the entity_edge rows for a human sanity check.
Confirm Py↔C++ parity end-to-end (hand-diff materialize_entity_edges + schema). green=true ONLY if everything
passes and the graphlab entity graph is correct + sound (partial flags where ⊤).
RETURN the schema'd verdict with any residual blockers (file:line + repro).`,
      { agentType: 'qa-engineer', schema: IMPL_QA_SCHEMA, label: 'final-qa', phase: 'Review', isolation: 'worktree' },
    ),
  () =>
    agent(
      `You are the SENIOR DEVELOPER doing the final CODE REVIEW of cidx entity_edge (PR1+PR2) in the worktree.
${CTX}
Review the diff for: LOCKED-contract fidelity, Py↔C++ parity (esp. classify_referent + materialize_entity_edges
+ schema byte-identity), soundness (partial=1 discipline), test adequacy, and version/schema correctness
(0.18.0, schema v17). Nothing is "green" by self-report — cite concrete file:line concerns.
RETURN a concise review: approve/blockers, each with file:line + rationale.`,
      { agentType: 'senior-developer', label: 'final-review', phase: 'Review' },
    ),
])

return {
  phaseA: phaseAResult,
  phaseB_stage1: stage1,
  phaseB_pr2: { green: pr2qa ? pr2qa.green : false },
  phaseC: { qa_green: finalQa ? finalQa.green : false, qa: finalQa, review: finalReview },
  reminder: 'Worktrees are left for the human to review + integrate; the workflow performed NO git operations.',
}
