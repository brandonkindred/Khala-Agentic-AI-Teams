---
name: speed_up_se_team_planning
overview: Diagnose why the software engineering team’s planning phase is slower and propose using the planning cache with a stable spec to reduce repeated planning work.
todos:
  - id: enable-cache
    content: Enable and verify SW_ENABLE_PLANNING_CACHE so that repeated runs on the same spec reuse the existing TaskAssignment and skip re-planning loops.
    status: completed
isProject: false
---

## Root cause summary

Based on the current `software_engineering_team` implementation, the **main reason planning is taking so long** is that the new, more sophisticated planning pipeline:

- Fan-outs into many more agents (Spec Intake, Project Planning, Tech Lead multi-agent pipeline, Architecture Expert, and multiple domain planning agents) before any coding starts.
- Runs in iterative alignment and conformance loops where each iteration can re-run the entire Tech Lead + Architecture planning stack.
- Uses very high default iteration caps for alignment, conformance, and reviews (e.g. `SW_MAX_ALIGNMENT_ITERATIONS`, `SW_MAX_CONFORMANCE_RETRIES`, and related `SW_MAX_*` vars) which can multiply LLM calls dramatically.

Concretely, one planning run now fans out through many agents and loops, so **re-running planning from scratch for the same spec and architecture** is a major contributor to slow runs. When you invoke the team multiple times on a stable spec or small spec edits, you pay this full cost each time unless you reuse prior results.

## Proposed improvement: planning cache with stable spec

- **Goal:** Avoid re-running the full multi-agent planning pipeline when the spec and high-level plan have not materially changed.
- **Mechanism:** Use the existing planning cache controlled by `SW_ENABLE_PLANNING_CACHE` so that:
  - The first successful planning run stores the `TaskAssignment`, requirement–task mapping, and summary under a cache key derived from spec, architecture, and project overview.
  - Subsequent runs on the same repo/spec (and compatible architecture/overview) **reuse the cached assignment** instead of recomputing Tech Lead + Architecture + alignment/conformance loops.
- **Workflow adjustment:** Treat the spec and project overview as **stable per branch**:
  - Make any major spec edits deliberately and then trigger a “cache reset” by changing the spec enough that the cache key changes (or by clearing cached entries when desired).
  - For small, non-structural edits (typos, wording clarifications) try to batch changes rather than repeatedly invoking the full team on slightly different specs.
- **Expected impact:** Planning time becomes dominated by:
  - The **initial** deep planning run on a given spec/branch, and
  - Much faster **subsequent** runs that load a cached plan, skipping most multi-agent planning work and loops.

## Implementation outline

- **1. Enable and verify the planning cache**
  - Set `SW_ENABLE_PLANNING_CACHE=1` in the environment where you run the team.
  - Run the team twice on the same repo/spec and confirm via logs that the second run:
    - Hits the planning cache.
    - Skips re-invoking the Tech Lead multi-agent pipeline and most alignment/conformance logic.
- **2. Establish a “stable spec per branch” practice**
  - Document a simple guideline in your project README or runbook:
    - When you branch for a feature, keep `initial_spec.md` mostly stable.
    - If you need a substantial spec change, make it once, then re-run the team to regenerate a new cached plan for that branch.
  - This keeps cache hit rates high and maximizes the benefit of the planning cache.