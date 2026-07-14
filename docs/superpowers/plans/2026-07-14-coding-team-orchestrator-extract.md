# Coding Team Orchestrator Extract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract repo-context and progress/config helpers from `coding_team/orchestrator.py` into focused modules with no behavior change and no orchestrator re-exports.

**Architecture:** Two new modules (`repo_context.py`, `progress_config.py`); orchestrator and swarm mixins import them; tests retarget imports.

**Tech Stack:** Python 3.10, pytest, existing coding-team package patterns.

## Global Constraints

- Pure structural move — preserve names, DbC docstrings, and behavior.
- Do not re-export moved symbols from `orchestrator.py`.
- Do not merge with `shared/repo_context_cache.py`.
- Do not reference GitHub issue numbers in code/comments/docs (PR body only later).

---

### Task 1: Create `progress_config.py`

**Files:**
- Create: `backend/agents/software_engineering_team/coding_team/progress_config.py`
- Modify: (none yet)

- [ ] Move concurrency/cap parsers, `_NoopBridge`, progress helpers from orchestrator into this module (same bodies).

### Task 2: Create `repo_context.py`

**Files:**
- Create: `backend/agents/software_engineering_team/coding_team/repo_context.py`

- [ ] Move filters, enumerate/render/join, `_read_repo_context`, `_RepoContextCache` into this module.

### Task 3: Wire callers

**Files:**
- Modify: `orchestrator.py`, `swarm_implementation.py`, `swarm_review.py`

- [ ] Import from new modules; delete moved bodies from orchestrator.
- [ ] Late-bind mixin lookups for concurrency/cap via `progress_config`.

### Task 4: Retarget tests + verify

**Files:**
- Modify: `tests/test_coding_team_orchestrator.py` (and split into focused test modules if practical)

- [ ] Point unit tests at `repo_context` / `progress_config`.
- [ ] Run targeted pytest; fix failures.
