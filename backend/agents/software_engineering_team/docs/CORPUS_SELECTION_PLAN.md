# SE Review Gate Corpus Case Selection Plan

## Purpose

`CORPUS_CASE_FORMAT.md`, `GATE_FINDING_MATCHING_RULE.md`, and
`CORPUS_FALSE_POSITIVE_RESISTANCE.md` specify what a corpus case *is* and how
it gets scored. None of them decide what the 50+ cases in the corpus
(#7587) actually are — which defect classes, how many of each, how many are
sourced from this repository's real history versus invented, and what
proportion is backend (Python) versus frontend (TypeScript/Angular). This
document makes that selection and justifies it, so the case-authoring
stories that follow (#7661 for must-find cases, #7670 for
false-positive-resistance cases) fill a designed sample instead of
accumulating whatever was easiest to find.

The corpus is the ground truth every later quality claim in this
improvement set is measured against. A distribution accidentally weighted
toward defect classes the pipeline rarely meets would produce confident
numbers about nothing in particular — the risk this document is written to
avoid, per its own selection method: every count below is justified either
against a real, cited commit in this repository's history, or against the
gates' own documented behavior (`GATE_FINDING_INVENTORY.md` and the
code-review false-positive-verifier prompt), never against an assumed or
invented taxonomy.

**Out of scope for this document:** authoring any case file (`case.yaml`,
`labels.yaml`, `diff.patch`, or a `files/` tree) — that is #7661 and #7670's
job, against the table in §3 below. Conformance verification (#7678). Tuning
any gate's prompt or logic to perform better on these cases. No case
content and no production code accompany this document.

## 1. Total corpus size and authoring buckets

**Target: 60 cases total**, comfortably above the 50+ floor in #7587, split
into two buckets matching how the case-authoring issues divide the work:

- **34 must-find-primary cases (56.7%)** — for #7661, sourced overwhelmingly
  from real repository history.
- **26 false-positive-resistance-primary cases (43.3%)** — for #7670,
  clearing the 40% floor with a margin.

This split is a floor, not a ceiling. Per `CORPUS_FALSE_POSITIVE_RESISTANCE.md`,
a single case may carry both a `must_find` label and an adjacent
`must_not_find` decoy in the same fixture (the format's own `CASE-0003`
pattern). Several of the must-find cases planned in §3 are expected to pick
up an incidental decoy label this way, so the true fraction of *cases
carrying at least one `must_not_find` label* should land above 43.3% once
the corpus is authored.

## 2. Backend / frontend proportion: ~72% / 28%

**Target: 43 backend-primary cases, 17 frontend-primary cases** (of the 60
in §1). Four converging signals support this, from lightest to most direct:

1. **Raw size and activity.** Backend is roughly 85–90% of this repository
   by lines of code, file count, and commit count (Python under `backend/`
   versus TypeScript/Angular under `user-interface/src/`). A strictly
   proportional split would push frontend down to 10–16%.
2. **Confirmed real frontend supply is broader than the raw ratio suggests.**
   Verified, shipped-and-fixed frontend defects exist across `xss`
   (security), `resource-leak` (qa — two distinct leaks in one file),
   `standards`, `logic`, and `testing` (code_review), and `auth`
   (cross-stack) — seven of the 31 defect classes with real frontend
   evidence, found without deliberately over-searching frontend history.
3. **Frontend contributes categories with no backend analogue in this
   codebase**: stored XSS via an unescaped Angular `bypassSecurityTrustHtml()`
   call, RxJS/DOM-listener resource leaks, an Angular Material API misuse
   (a real `mat-checkbox`/`aria-label` accessibility bug), and accessibility
   generally. A strictly proportional 10–16% split would likely force
   several already-real classes into "invented" for no reason.
4. Directionally, of the 31 classes in §3, 23 have their strongest evidence
   on backend, 2 are frontend-primary, and 6 are genuinely cross-stack —
   consistent with, though not itself the basis for, a backend-heavy skew.

72/28 sits inside a 70–75%/25–30% range: less extreme than raw size alone
would justify, giving frontend more corpus share than its size warrants
because of points 2 and 3. Classes tagged `both` in §3 give case authors
flexibility in landing the exact 43/17 split; track it cumulatively across
the whole corpus, not per class.

## 3. Per-class defect distribution

31 rows — the full closed vocabulary defined in `CORPUS_CASE_FORMAT.md` §3.
**The MF (must-find) and FP (false-positive-resistance) columns are
label-count targets, not literal case counts** — §4 explains how they
reconcile to the ~60 cases in §1 via multi-gate and multi-class
consolidation, the same patterns `CORPUS_CASE_FORMAT.md`'s own worked
examples already demonstrate.

Real-history commits are cited by short SHA from
`brandonkindred/Khala-Agentic-AI-Teams`, verified via the GitHub commit API
against the live repository. **The local development clone is shallow** —
case authors must fetch these commits via the GitHub API or a full
unshallow clone/fetch, not local `git show`, which will not have them.

### Group A — code review (12 classes)

| Class | MF | FP | Sourcing | Stack |
|---|---|---|---|---|
| `naming` | 1 | 1 | **Invented.** Naming nits are fixed pre-merge inside the same squash-merged PR that introduced them; no isolated "renamed for clarity" survivor exists in `main`'s history. | backend |
| `structure` | 1 | 1 | **Invented.** No standalone real fix found for file/module misplacement. Base the case on this repository's own documented convention (this README: leaf agents "typically follow a three-file convention: `agent.py`/`models.py`/`prompts.py`") — e.g. a Pydantic model defined inline in `agent.py` instead of `models.py`. | backend |
| `logic` | 3 | 2 | **Real.** `0040820` (JSON `null` stringified to the literal `"None"` — multi-gate with qa `null-deref`), `766c6e5` (substring match wrongly excluded unrelated names — "Apple" matched "Appleton"), `3fd49f5` (`[attr.aria-label]` bound on `<mat-checkbox>`, which nulls the host attribute — a real functional bug from Material API misuse). | both |
| `spec-compliance` | 1 | 1 | **Invented.** Real commits found (`0dfb7fd1`, `7f320ef3`, `bdc39055`, `538e82f5`) are about *building* the spec-compliance-check pass itself, not an application defect it later caught. Telling: this gate blocks merge on review approval, so a spec violation reaching `main` and needing a later fix is close to the failure mode the gate exists to prevent. | backend |
| `standards` | 2 | 2 | **Real.** `c6169cd` (read `AGENT_CACHE_DIR`, never set, instead of the platform's documented `AGENT_CACHE` convention), `1308c1d` (native blocking `alert()` instead of the app's `MatSnackBar` convention). | both |
| `integration` | 1 | 2 | **Real** (marginal): `e325ad8` (`create_research_agent` ignored its own injected `llm_client` argument). FP weighted heavier — matches false-positive claim #1/#5 in §5. | backend |
| `testing` | 1 | 2 | **Real:** `0175839` (a "teardown success" spec's mock never returned an `Observable`, so the subscribe chain never ran; the assertion passed by coincidence). FP weighted heavier — matches false-positive claim #2 in §5, and is the parent issue's own "no tests for X" example territory. | frontend |
| `architecture` | 1 | 1 | **Real:** `ae3ccf70` (a test in team-agnostic platform infra `agent_sandbox_runtime` imported a specific team package directly, crossing a documented boundary). | backend |
| `refactor` | 1 | 3 | **Real:** `51810fd5` (two teams' `run_workflow` hand-inlined the same per-microtask review-gated execution logic; extracted to a shared base class). FP weighted heaviest in this group — this is the parent issue's own explicit false-positive-resistance example ("an intentionally unused parameter") via claim #4 in §5. | backend |
| `maintainability` | 1 | 2 | **Real:** `66bc52d5` (a `.get()` default already covered a fallback; the trailing `or default` was dead code that also risked masking valid falsy LLM output). Backup: `000ebdea` (deleted a dead V1 class). FP matches claim #4 in §5. | backend |
| `side-effects` | 1 | 1 | **Real:** `c8cbbb7` (a crash handler set some fields but forgot the dedicated `error` field — multi-gate with qa `inconsistent-state`). | backend |
| `documentation` | 1 | 1 | **Real:** `c0db42b` (a docstring documented a `top_n >= 1` precondition that was never actually validated — on-theme given this repository's own Design-by-Contract mandate). | backend |

### Group B — security (7 classes)

| Class | MF | FP | Sourcing | Stack |
|---|---|---|---|---|
| `injection` | 1 | 2 | **Real:** `27691e3` (a SQL table name was f-string-interpolated with no identifier validation — parameterized queries protect values, never identifiers). | backend |
| `xss` | 1 | 1 | **Real:** `7e11ddc` (an SVG string was built via interpolation of LLM-seeded content and rendered via `bypassSecurityTrustHtml()` unescaped — stored XSS). | frontend |
| `csrf` | 1 | 1 | **Invented.** Zero commits and zero defense code (`SessionMiddleware`, `set_cookie`) found anywhere in history — this application's auth model is bearer-token/API-key/webhook-signature based (confirmed by `f1f605b`'s HMAC pattern), not cookie-session based, so classic CSRF has structurally limited applicability. | backend |
| `auth` | 2 | 1 | **Real:** `f1f605b` (an inbound Slack webhook signature check was skipped entirely when unconfigured — fail-open, cross-stack fix), `c5a9017` (an unsanitized path join enabled unauthorized file reads — multi-gate with qa `unvalidated-input`). | both |
| `crypto` | 1 | 1 | **Invented.** Real Fernet-based encryption (`325f2368`, `INTEGRATION_ENCRYPTION_KEY`) is used correctly today; no weak-cipher or hardcoded-key defect was found. Model the case on that real code path (e.g. a hardcoded key or a silent fallback to a weaker cipher). | backend |
| `insecure-deserialization` | 1 | 1 | **Invented.** A full grep of `backend/` for `pickle.loads?`/`pickle.dump` and `yaml.load`/`yaml.unsafe_load` found zero production hits (one unrelated test file). No unsafe deserialization exists in this codebase to have had a defect. | backend |
| `ssrf` | 1 | 1 | **Invented.** No SSRF-guard code or defect found. Model on real user-configured outbound URLs that do exist (`38704a47`'s TradingView MCP server URL, Slack webhook URLs, configurable Ollama/RunPod base URLs). | backend |

### Group C — QA bug patterns (8 classes)

| Class | MF | FP | Sourcing | Stack |
|---|---|---|---|---|
| `off-by-one` | 1 | 1 | **Real:** `70f16f7` (story-elicitation progress `35 + idx` could grow past the next phase's fixed value of 40). | backend |
| `race-condition` | 3 | 1 | **Real** (5 confirmed, 3 targeted): `cb8aded` (TOCTOU between a cancellation check and the terminal status write), `f5eb3e9` (a lock was dropped between session enumeration and write-back), `56c2fcd` (a non-atomic read-then-delete). Backup: `0523b9c`, `ce865d8`. | backend |
| `resource-leak` | 2 | 1 | **Real:** `caed749` + `9ac88a3` — same file, two distinct leaks (untracked DOM click listeners; twelve RxJS subscriptions with no `ngOnDestroy`/`takeUntil`) — a genuine paired, systemic example. | frontend |
| `null-deref` | 3 | 2 | **Real** (6 confirmed, 3 targeted — the strongest real supply of any class): `0040820` (multi-gate with code_review `logic`), `c0e71da` (`float(None)` crashed OHLC-bar processing), `fd3b9a0` (the same stringify-null-as-`"None"` bug in a sibling file — a systemic pattern worth showing twice). Backup: `91c511f`, `d2a19a6`, `eed1887`. | backend |
| `integer-overflow` | 1 | 1 | **Invented.** Python's arbitrary-precision integers make classic overflow rare server-side; no real fix commit was found. A realistic instance is JS numeric-precision loss/truncation at an external boundary (frontend) or truncation against a fixed-width database or external-API field (backend). | both |
| `unvalidated-input` | 3 | 2 | **Real** (4 confirmed, 3 targeted): `c5a9017` (multi-gate with security `auth`), `3a31cee` (an unsanitized HTTP-controlled `agent_id` enabled a `../../etc/passwd`-style path escape into the encrypted credential store), `0436308` (one store method skipped the `safe_path_component` check every sibling method uses). Backup: `deb488d`. | backend |
| `missing-error-handling` | 2 | 2 | **Real:** `4f1b84e` (a bare `except Exception: return None` was indistinguishable from not-found), `87a02c2` (a catch-all was mislabeled as an unrelated "Temporal worker unavailable"). FP weighted heavier — matches false-positive claim #3 in §5. | backend |
| `inconsistent-state` | 1 | 1 | **Real:** `c8cbbb7` (multi-gate with code_review `side-effects` — the same missed-`error`-field bug). | backend |

### Group D — QA `fix_build`-mode root causes (4 classes)

| Class | MF | FP | Sourcing | Stack |
|---|---|---|---|---|
| `missing-import` | 1 | 1 | **Invented** — see §4.1 for the structural reason shared by all of Group D. | backend |
| `wrong-path` | 1 | 1 | **Invented** — see §4.1. The FP case can directly reuse false-positive claim #6 in §5: a relative import that is this codebase's established convention, flagged as unresolved. | backend |
| `type-error` | 1 | 1 | **Invented** — see §4.1. Informed, though not sourced, by real commit `78651ff` (an SSE event typed as a `{type: ...; [key: string]: unknown}` catch-all instead of a discriminated union) — a related typing-looseness defect, not itself a build-breaking one. | both |
| `syntax-error` | 1 | 1 | **Invented** — see §4.1. | backend |

**Column totals: 43 must-find labels + 42 must-not-find labels = 85 labels**
(by group: A = 15 MF / 19 FP, B = 8 / 8, C = 16 / 11, D = 4 / 4).

## 4. Reconciling 85 labels to ~60 cases

### 4.1 Why every Group D class is invented

Two compounding, structural reasons — not a research gap:

- This repository's CI gates lint, build, and coverage *before* merge (per
  `CLAUDE.md`). A build-breaking defect reaching `main` — a prerequisite for
  a genuine "shipped and later fixed" real example — is itself close to the
  failure mode the pipeline is designed to prevent. Real examples, if they
  exist, are the edge case, not the norm.
- Every commit examined in this repository's history has the shape of a
  GitHub default squash-merge (bulleted subjects, `Co-authored-by: Claude`),
  meaning the actual mid-PR "fix the import I broke two commits ago"
  commits — which happen constantly during development — are squashed away
  before ever reaching `main`'s history. They are real and common but
  structurally invisible to a commit-history search of the merged branch.
  Weak corroborating evidence that the class is real even though no clean
  instance survived: `7038e66f`, a large lint-cleanup squash commit that
  lists "Add missing imports: ..." as one of over 20 unrelated bullets.

Since `fix_build` mode is a real, exercised QA code path, author these four
classes as clean invented `case.yaml`/`files/` fixtures shaped like a
plausible build failure — there is no need for a `diff.patch` against a
real commit.

### 4.2 Consolidation patterns

Both patterns below are already demonstrated by `CORPUS_CASE_FORMAT.md`'s
own worked examples — nothing new is introduced here.

- **Multi-gate, same commit** (the format's `CASE-0002` pattern: one diff,
  one label per gate). Three confirmed instances: `0040820` (qa
  `null-deref` + code_review `logic`), `c8cbbb7` (qa `inconsistent-state` +
  code_review `side-effects`), `c5a9017` (qa `unvalidated-input` + security
  `auth`). This alone turns 6 must-find label-targets into 3 case
  directories; the remaining ~37 must-find labels are predominantly
  single-gate, giving an estimated 37–40 unique must-find diffs — consistent
  with the 34-case bucket target in §1, with headroom via the backup SHAs
  listed in §3.
- **Multi-class, same decoy fixture** (the format's `CASE-0004` pattern: one
  diff, several file-wide `must_not_find` labels across classes). A single
  pure, behavior-preserving refactor fixture is simultaneously a plausible
  false-positive trigger for `naming`, `logic`, `refactor`, and
  `maintainability` at once — literally `CASE-0004`'s own shape. One or two
  such fixtures (backend and/or frontend) can satisfy most of Group A's
  false-positive-resistance label-targets. Applying this, and the
  `CASE-0003` bounded-decoy pattern elsewhere, gives an estimated 20–24
  unique false-positive-resistance diffs against 42 FP-target labels.

Net: **~57–64 unique case directories**, centered on the 60 proposed in §1
— comfortably above the 50+ floor regardless of exactly where authoring
lands within that range. Case authors should report the actual count as a
number, not force it to exactly 60; this is a target, not a quota.

## 5. False-positive-resistance weighting rationale

The single strongest evidence source for *which* classes deserve the
heaviest false-positive-resistance weight is the code-review gate's own
documented self-knowledge of what it hallucinates:
`code_review_agent/prompts.py:41-48` (`FALSE_POSITIVE_VERIFY_BODY`) lists
six claim shapes its false-positive verifier is specifically primed to
catch:

1. "X is undefined / never defined / not imported / not registered" (when X
   is defined elsewhere) — weighted into `integration`, `logic`.
2. "no tests for X / missing test coverage" (when a test actually exists) —
   weighted into `testing`.
3. "missing error handling / validation / null check" (when it is handled
   by a caller, wrapper, decorator, or base class) — weighted into
   `missing-error-handling`.
4. "duplicate / unused / dead code" (when the other usage is elsewhere) —
   weighted into `refactor` and `maintainability`. This is also the parent
   issue's own explicit false-positive-resistance example ("an
   intentionally unused parameter").
5. "file/module Y must be created / does not exist" (when Y already exists
   but was not touched by this diff) — weighted into Group D's
   `missing-import`/`wrong-path`, and `integration`.
6. "this relative import is unclear / unresolved / should be absolute"
   (when it is this codebase's established convention) — weighted into
   `standards`, and Group D's `wrong-path`.

`refactor` (3 false-positive labels), and `integration`/`testing`/
`maintainability`/`missing-error-handling` (2 each), carry the heaviest
false-positive weight in §3 for exactly this reason: it is the gate's own
institutional false-positive knowledge, not a guess about what might trip
it.

## 6. Real vs. invented sourcing summary

By **class count**: 19 of the 31 classes (61%) are real-sourced; 12 of 31
(39%) are invented — `naming`, `structure`, `spec-compliance`, `csrf`,
`crypto`, `insecure-deserialization`, `ssrf`, `integer-overflow`, and all
four of Group D (`missing-import`, `wrong-path`, `type-error`,
`syntax-error`).

By **label count** — a better proxy for corpus volume, since real-sourced
classes are weighted toward higher counts in §3 — 61 of 85 labels (72%)
trace to a real commit, and 24 of 85 (28%) are invented. This satisfies the
parent issue's "mostly from real repository history" instruction more
strongly than the class-count split alone suggests.

Every invented class in §3 carries a one-line, evidence-backed reason: a
grep result showing the pattern does not exist in this codebase, a
structural argument about squash-merging or pre-merge gating, or both —
never "no example was found" stated alone.

## 7. What this corpus deliberately does not cover

**Inherited from the corpus format specification — not re-litigated here:**
`gate` is a closed 3-value enum (`code_review`, `qa`, `security`). Design-by-
Contract review is out of scope: `DbcCommentsAgent` only inserts
documentation and precondition comments and never emits a finding shape.
The lint/build gate (`LintIssue`), the devops team's `change_review_agent`/
`ReviewFinding` (a translated shape of the same code-review engine, not a
new taxonomy), the Tech Lead's own simple approve/reject merge gate, and the
frontend tool-agents with no dedicated gate of their own (accessibility, UI
design, and similar) are all out of scope. Code review's `general` category
is excluded from the vocabulary as a non-specific overflow bucket.

**New limits this distribution design itself implies:**

- **Single-instance classes cannot test sub-variety.** Fifteen of the 31
  classes get only one must-find label-target (several the same for
  false-positive-resistance). The corpus can confirm a gate catches *a*
  `csrf`/`crypto`/`ssrf`/`syntax-error` (and similar) instance, not that it
  generalizes — one miss swings that class's measured recall from 100% to
  0%, and the corpus has no statistical power to distinguish "this class is
  genuinely weak" from "this one case was unlucky."
- **Multi-gate and multi-class consolidation (§4.2) means the label count
  overstates independent test coverage.** A single miscalibrated decoy
  fixture can move several rows of §3 at once.
- **Invented cases are a structurally weaker evidence grade than real
  ones** — mirroring `CORPUS_CASE_FORMAT.md`'s own grading of its Group C
  (QA prompt-citation) as weaker evidence than Groups A/B (validated
  enums). 28% of labels here test a gate against a plausible-but-never-
  actually-observed pattern, not its track record against a defect this
  pipeline is known to have produced.
- **The distribution reflects one codebase's history, not general
  software-defect frequency.** "`csrf` and `ssrf` are thin here" is a claim
  about this pipeline's shipped-and-fixed history — this application's
  bearer-token auth model, its lack of pickle/unsafe-YAML usage — not a
  claim that these vulnerability classes are rare in software generally.
- **QA's defect-class check is skipped entirely by the matching rule**
  (`GATE_FINDING_MATCHING_RULE.md` §3.3 — `BugReport` has no category
  field). However the Group C/D per-class counts above are balanced, the
  runner can only ever verify QA found *something* at the right location,
  never that it correctly identified the pattern. Those counts are a
  location-recall stratification tool for authoring variety, not a
  class-discrimination dimension the matching rule can actually score.
- **Severity calibration is not scored.** The matching rule never compares
  `severity` — only gate, location, and defect class. This corpus cannot
  tell whether a gate over- or under-rates a correctly found defect's
  severity.
- **Real cases are pinned to their origin commit's diff, not to a moving
  `HEAD`.** As this repository evolves past the cited commits, case authors
  must fix each real case's fixture to the state as of its cited commit,
  not attempt to keep it synchronized with the live repository.
- **Multi-defect interaction effects are not sampled.** Whether a gate
  correctly reports two unrelated real defects in the same file without
  merging, dropping, or misattributing one is not a dimension this
  distribution deliberately targets.
