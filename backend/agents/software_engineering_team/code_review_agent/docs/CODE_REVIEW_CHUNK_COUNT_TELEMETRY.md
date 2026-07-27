# Code review map-phase chunk-count telemetry (estimate)

Sub-issue of #2805 ("Adaptive fan-out width for the code review map phase
instead of a fixed concurrency ceiling"), see [`docs/ENV_VARS.md`](../../../../../docs/ENV_VARS.md)
(`CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES` / `CODE_REVIEW_EXECUTE_TIMEOUT_S`).
This note gathers a p50/p95/max chunk-count-per-review data point to inform
that ceiling design. No production code changes here — measurement only.

## Why an estimate, not measured production telemetry

Chunk count is **not currently captured as durable telemetry** anywhere in the
codebase. The only existing signal is two `logger.info` lines in
`coordinator.py` (`"CodeReviewCoordinator: %s blocks -> %s chunks"` and the
end-of-run `"... chunks=%s (sub-reviews=%s)"` line) — plain log output, never
written to a Postgres table, Prometheus counter, or OTel metric. There is no
test fixture or historical log archive with realistic PR sizes either. Per the
issue's own acceptance criteria, this is therefore an **estimate from a
representative sample**, not a measurement of live production traffic.

## Methodology

Ran the real, unmodified `build_review_chunks` (from
`code_review_agent/chunking.py`) against representative changesets, using the
practical per-chunk budget of **80,000 chars**
(`CODE_REVIEW_ABS_CHUNK_CHARS` — the absolute ceiling
`compute_code_review_map_chunk_chars()` clamps to for ordinary model context
sizes; see `shared/context_sizing.py`). Two sample groups:

1. **Real commit sample** — every commit in this repository's history (50
   commits), using the *full post-commit content of every file the commit
   touched* as `{path: content}` input. This matches the default GitHub
   PR-review path (`api/pr_review.py`, `files=head_files`), which submits
   whole changed-file content at the PR head, not diff hunks — so chunk count
   tracks total bytes of touched files, not lines-of-diff.
   - One commit (3,764 files / ~900K insertions, a one-time repository
     bootstrap import, not a PR a human would ever submit for review) was
     excluded as non-PR-shaped (>100 files changed) and reported separately as
     a worst-case data point rather than folded into the "typical PR"
     distribution.
2. **Synthetic large-PR sample** — individual commits here are mostly small,
   single-task diffs rather than squashed multi-file PRs, so real large PRs
   aren't well represented by any single commit. To approximate the "large
   PR, dozens of chunks" case `docs/ENV_VARS.md` describes qualitatively, the
   5/10/20 largest PR-shaped commits (by total touched-file bytes) were
   unioned into synthetic multi-file submissions and chunked the same way.

## Findings

**Real, PR-shaped commits (n=49, excludes the bootstrap outlier):**

| stat | value |
|---|---|
| min | 1 |
| p50 | 1 |
| p95 | ~5 |
| max | 6 |
| mean | 1.86 |

Most task-sized changes in this pipeline's normal operating range fit in a
single chunk; the largest observed PR-shaped commit (6 chunks, ~330K chars
across touched files) still fits comfortably under 10 chunks.

**Excluded non-PR-shaped outlier** (repo-bootstrap commit, 3,764 files, ~35.9M
chars touched): **540 chunks**. Not representative of a real PR, but useful as
a demonstration of how unbounded the map phase's chunk count is at the extreme
end — consistent with `docs/ENV_VARS.md`'s "no hard cap... latency scales with
PR size" statement.

**Synthetic large-PR approximations** (union of the largest real commits'
files):

| sample | touched-file chars | chunks |
|---|---|---|
| 5 largest commits unioned | ~1.39M | 24 |
| 10 largest commits unioned | ~2.10M | 36 |
| 20 largest commits unioned | ~2.58M | 44 |

This range (24-44 chunks) is a reasonable stand-in for the "large PR, dozens
of chunks" case the existing `CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES` doc entry
describes.

## Takeaways for the adaptive ceiling (next two sub-issues)

- The overwhelming majority of reviews are small (1 chunk, occasionally up to
  ~5-6) — a fixed low-width fan-out (today's `CODE_REVIEW_MAP_PARALLELISM=4` /
  `CODE_REVIEW_MAX_CONCURRENT_ACTIVITIES=8`) already covers this range with
  room to spare, so an adaptive ceiling costs these reviews nothing.
- A realistic "large PR" band is roughly **20-50 chunks**; an adaptive ceiling
  sized in that neighborhood (well below `LLM_MAX_CONCURRENCY`'s process-wide
  cap) would let large reviews fan out materially wider than today's fixed 8
  without over-provisioning small ones.
- The unbounded extreme (hundreds of chunks) is real but rare and
  non-representative of an ordinary PR; the adaptive ceiling should cap fan-out
  width rather than attempt to size for this tail, consistent with issue
  #2805's framing (a configurable ceiling, not an unbounded scale-up).
