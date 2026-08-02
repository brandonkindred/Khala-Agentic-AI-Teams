# Snapshot comparison: two-call vs merged architecture/side-effect pass

**Date:** 2026-08-02
**Scope:** Validation harness only, gating rollout of the already-merged
consolidation (`merged_architecture_side_effect_pass.py`, wired into both the
in-process coordinator and the Temporal code review workflow). This does not
change either pass's instructions or the coordinator/Temporal wiring — see
`merged-pass-design.md`, which explicitly deferred this validation ("snapshot
comparison of findings" is listed under its own "Out of scope").

## What this compares

`code_review_agent/snapshot_comparison.py` runs a fixed corpus of real past
submissions through both:

- **Old (pre-consolidation) path** — `find_architecture_and_redundancy_issues`
  then `find_side_effect_impact_issues`: two independent LLM calls, the exact
  pair `coordinator._run_tail_passes` scheduled before the merge landed.
- **New (post-consolidation) path** — `find_architecture_and_side_effect_issues`:
  one LLM call, split back into the same two finding lists.

For each submission, the architecture findings from both paths are diffed
against each other, and the side-effect findings from both paths are diffed
against each other (never architecture vs. side-effect — that would produce
meaningless cross-category "matches").

## Corpus

Six entries (five real merged changes from this repository's own history; the
largest is used twice, with and without an architecture document attached)
selected to span finding density and the with/without-architecture-doc axis
the design doc's own risk note calls out:

| Label | Commit | Files | Why |
|---|---|---|---|
| clean-baseline (docstring-only) | `9036ef9` | 1 | Zero findings from either path is the expected, desired outcome — a false-positive check. |
| side-effect (small cross-file threading) | `83299e4` | 1 | Small, clear caller-impact case: a new parameter threaded through one call site. |
| architecture/refactor (dead-code removal) | `c8b4098` | 1 | Exercises the architecture pass's `category: "refactor"` (cross-codebase redundancy) path. |
| architecture + side-effect (Temporal wiring, no doc) | `569b78e` | 3 | Cross-cutting change (workflow + activities), moderate blast radius, no formal architecture document attached. |
| large multi-file feature, **with** architecture doc | `358873b` | 9 | Highest finding-density case in the corpus; architecture doc attached (`docs/ARCHITECTURE.md`). |
| large multi-file feature, **without** architecture doc | `358873b` | 9 | Same change, no document attached — isolates the architecture-doc-optional broadening (introduced by the same commit) from the call-count change being validated here. |

Full commit SHAs are in `CORPUS` in `snapshot_comparison.py`; each was located
by finding the squash-merge commit whose title matches the change described
(`git log --oneline | grep <keyword>`), then confirmed with `git show --stat`.

Test-only and documentation-only files are excluded from each entry's
`changed_files` (see `CORPUS` in `snapshot_comparison.py`) so the corpus
reflects production-code review targets, matching how these passes are
actually used.

Each entry is checked out at run time via `git worktree add --detach <tmp>
<commit_sha>` against the caller's own repo checkout (`shared.git.git_utils`,
the same primitive `WorktreeManager` uses elsewhere in this codebase) — not
embedded as static fixture content — so `CodeReviewInput.repo_root` points at
a real, complete checkout of the repository at that commit, giving both
passes genuine off-diff read access (caller search, duplicate-capability
search) instead of isolated snippets.

## Methodology

- **Matching heuristic** (`diff_findings` / `_finding_similarity`): a finding
  from the old path is paired with its best-scoring unused finding from the
  new path when both share the same `category`, do not cite different
  non-blank `file_path`s, and score ≥ 0.45 on a `difflib.SequenceMatcher`
  ratio over `description` (halved when both cite a `line` more than 5 apart).
  This is a **heuristic for shrinking the human-review surface**, not an
  automated regression verdict — two independent LLM calls never reproduce a
  finding's exact wording, so an exact-string diff would misreport nearly
  every real match as one lost + one added finding.
- **Repeats** (`--repeats N`): each path is run N times per submission and
  findings pooled before diffing, because `resolve_code_review_model` exposes
  no temperature control to this harness — a single before/after pair per
  submission cannot distinguish a genuine prompt-structure regression from
  ordinary sampling variance. `N=1` (the default) is a plumbing smoke test,
  not a statistically meaningful comparison; a real run should use `N ≥ 3`.
- **What counts as a candidate regression:** a `lost` entry (old path found
  it, new path didn't) on any submission is the signal this whole exercise
  exists to catch, and an `added` entry (new path found it, old path didn't)
  is a candidate new false positive — treat every entry in both lists as
  worth reviewing. The old path's `find_architecture_and_redundancy_issues`
  is the CURRENT standalone module, which already carries the
  architecture-doc-optional broadening (it is not a pre-broadening
  snapshot) — so that broadening is applied identically on both sides of
  every comparison, old and new. It is therefore **not** a valid reason to
  discount an `added` architecture finding on a without-doc entry: both
  paths are equally free to produce doc-optional architecture findings, so
  an asymmetry between them reflects the call-structure change (one call vs
  two competing for shared attention/budget) — exactly the kind of
  regression this harness exists to catch, not an artifact to filter out.
  The with-doc/without-doc split still matters for a different reason: it
  lets a reviewer see whether that doc-optional reasoning holds up
  consistently under both call structures, not as a pre-filter on findings.
  Every `matched` pair is also included in the report (not just a count) —
  spot-check a sample of these too, since the similarity heuristic can
  mismatch two genuinely different findings that merely share a category,
  file, and enough wording overlap, silently hiding a real lost+added pair.

## How to run this for real

This repository's LLM resolution has no legacy single-provider fallback (see
root `CLAUDE.md`): a real comparison run needs `POSTGRES_HOST` (+ `_PORT`/
`_USER`/`_PASSWORD`/`_DB`) set and at least one entry in the Postgres-backed
LLM provider list (`/llm-config`, see `docs/ENV_VARS.md`).

```bash
cd backend
PYTHONPATH="$(pwd):$(pwd)/agents/software_engineering_team:$(pwd)/agents" \
  .venv/bin/python -m code_review_agent.snapshot_comparison \
  --repo-root /path/to/a/full/clone/of/this/repo \
  --repeats 3 \
  --output snapshot_comparison_report.json
```

`code_review_agent` is only importable as a bare top-level package with that
exact `PYTHONPATH` (mirroring how `agents/software_engineering_team/conftest.py`
sets up `sys.path` for pytest — this module is not part of that test session,
so it needs the same three directories supplied explicitly; a bare
`cd backend && python -m code_review_agent.snapshot_comparison` fails
immediately with `ModuleNotFoundError: No module named 'code_review_agent'`).

`--repo-root` must be a clone that has the six corpus commits reachable
(a shallow clone may need `git fetch --deepen=<n>` or `--unshallow` first).
The command prints a one-screen summary and writes the full per-submission
diff (matched/lost/added, with full finding detail) to `--output`.

A **smoke-test mode** (`--dummy`) is available and safe to run in any
environment, including one with no LLM provider configured — it substitutes
`DummyLLMClient` for the real model, proving the worktree checkout and both
call paths execute end-to-end without exceptions. **It produces no meaningful
regression signal**: a scripted stub answers identically regardless of call
count, so every submission will show as either all-matched or all-empty. Use
it only to verify the harness itself is wired correctly, never to answer the
go/no-go question below.

## Go/No-Go recommendation: **PENDING**

This session has no configured LLM provider (no `POSTGRES_HOST`, no
provider-list entry — see the environment note in `CLAUDE.md`), so the real
two-call-vs-merged comparison described above has **not been executed**. What
*has* been done in this session:

- The harness (`snapshot_comparison.py`) is implemented and unit-tested
  (`tests/test_snapshot_comparison.py`) — matching/diffing logic and the
  `compare_submission` wiring (real local git worktree + both call paths) are
  covered offline.
- `--dummy` smoke-test plumbing is available to verify the harness runs
  end-to-end in any environment.
- The corpus above is selected and checked into `snapshot_comparison.py`.

What remains before this validation can be marked complete:

1. Run the command above (with `--repeats 3` or higher) in an environment
   with a configured LLM provider.
2. Review the `lost` entries in the output report for both categories across
   all six submissions — any non-empty `lost` list is a candidate regression
   and must be individually assessed (is the old-path finding still valid?
   did the merged prompt genuinely drop it, or word it differently enough to
   evade the similarity match?).
3. Review every `added` entry — each is a candidate new false positive (see
   the Methodology section above for why without-doc architecture findings
   must not be discounted by default).
4. Spot-check a sample of `matched` pairs for both categories to confirm the
   similarity heuristic paired genuinely equivalent findings, not two
   different findings that merely share a category/file and overlapping
   wording.
5. Record the outcome and a go/no-go recommendation in this section.
