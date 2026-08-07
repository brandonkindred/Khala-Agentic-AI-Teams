# Product-Analysis API Routes Temporal Dispatch Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the two redundant `is_temporal_enabled`-patched “even when disabled” tests from `test_api_product_analysis_routes.py` so the file reflects Temporal-only dispatch.

**Architecture:** Single-file surgical delete. Autouse already stubs `start_standalone_workflow`; the “even when disabled” cases only prove a gate patch does nothing — delete them and keep the remaining tests, including 503-on-dispatch-failure coverage. Leave `test_api_more_routes.py` untouched (already compliant).

**Tech Stack:** Python 3.10, pytest, FastAPI `TestClient`.

**Spec:** `docs/superpowers/specs/2026-08-07-api-routes-temporal-dispatch-tests-design.md`

**Worktree:** `.worktrees/4005-mock-temporal-dispatch-api-routes` on branch `feature/4005-mock-temporal-dispatch-api-routes`

## Global Constraints

- Touch only `backend/agents/software_engineering_team/tests/test_api_product_analysis_routes.py`.
- Do not edit `test_api_more_routes.py`.
- Do not rename remaining tests.
- Do not change production code.
- Do not edit sibling test files.
- Do not reference GitHub issue numbers in code, comments, commit messages, or docs (PR body may use `Closes #N`).
- Prefer exact, minimal diffs.

## File Structure

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/tests/test_api_product_analysis_routes.py` | Product-analysis API endpoint unit tests |

No new files. `test_api_more_routes.py` is explicitly out of scope for edits.

---

### Task 1: Delete redundant even_when_disabled product-analysis tests

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_api_product_analysis_routes.py`
- Test: same file

**Interfaces:**
- Consumes: existing remaining tests / autouse fixtures (no API changes)
- Produces: file with no `is_temporal_enabled` or `even_when_disabled` references; remaining tests pass including 503 coverage

- [ ] **Step 1: Delete the two redundant tests**

Remove these entire functions (and blank lines after them so spacing stays clean between neighboring tests):

1. `test_run_product_analysis_dispatches_to_temporal_even_when_disabled`
2. `test_start_from_spec_dispatches_to_temporal_even_when_disabled`

Exact bodies to delete (current `main`):

```python
def test_run_product_analysis_dispatches_to_temporal_even_when_disabled(
    client, tmp_path: Path, monkeypatch
):
    """No thread fallback: start_standalone_workflow is called regardless of is_temporal_enabled()."""
    import software_engineering_team.temporal.start_workflow as start_workflow
    from software_engineering_team.temporal.constants import STANDALONE_TYPE_PRODUCT_ANALYSIS

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("software_engineering_team.temporal.client.is_temporal_enabled", lambda: False)
    dispatched: dict = {}
    monkeypatch.setattr(
        start_workflow,
        "start_standalone_workflow",
        lambda standalone_type, job_id, repo_path, **kw: dispatched.update(
            standalone_type=standalone_type, job_id=job_id
        ),
    )

    resp = client.post(
        "/product-analysis/run",
        json={"repo_path": str(repo), "spec_content": "# Spec"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "running"
    assert dispatched["standalone_type"] == STANDALONE_TYPE_PRODUCT_ANALYSIS
    assert dispatched["job_id"] == resp.json()["job_id"]
```

```python
def test_start_from_spec_dispatches_to_temporal_even_when_disabled(
    monkeypatch, tmp_path: Path, client
):
    """No thread fallback: start_standalone_workflow is called regardless of is_temporal_enabled()."""
    import software_engineering_team.temporal.start_workflow as start_workflow
    from software_engineering_team.temporal.constants import STANDALONE_TYPE_PRODUCT_ANALYSIS

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("software_engineering_team.temporal.client.is_temporal_enabled", lambda: False)
    dispatched: dict = {}
    monkeypatch.setattr(
        start_workflow,
        "start_standalone_workflow",
        lambda standalone_type, job_id, repo_path, **kw: dispatched.update(
            standalone_type=standalone_type, job_id=job_id
        ),
    )

    resp = client.post(
        "/product-analysis/start-from-spec",
        json={"project_name": "myproj2", "spec_content": "# Spec\nFeature"},
    )

    assert resp.status_code == 200
    assert dispatched["standalone_type"] == STANDALONE_TYPE_PRODUCT_ANALYSIS
    assert dispatched["job_id"] == resp.json()["job_id"]
```

Leave unchanged:
- Autouse `_stub_background_workflow`
- `test_run_product_analysis_accepts_provided_spec_content` and other happy/validation tests
- `test_start_from_spec_keeps_project_dir_on_dispatch_failure` (503 coverage)
- Module docstring

- [ ] **Step 2: Verify greps and tests pass**

From `backend/`:

```bash
rg -n 'is_temporal_enabled|even_when_disabled' \
  agents/software_engineering_team/tests/test_api_product_analysis_routes.py
```

Expected: no hits.

```bash
rg -n 'test_start_from_spec_keeps_project_dir_on_dispatch_failure|assert resp.status_code == 503' \
  agents/software_engineering_team/tests/test_api_product_analysis_routes.py
```

Expected: both present (503 coverage preserved).

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_api_product_analysis_routes.py \
  -v
```

Expected: all remaining tests pass.

- [ ] **Step 3: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_api_product_analysis_routes.py
git commit -m "$(cat <<'EOF'
Drop redundant is_temporal_enabled patches from product-analysis route tests.

EOF
)"
```

---

## Self-Review

1. **Spec coverage:** Delete both even_when_disabled tests → Task 1. Grep + pytest + 503 preserved → Task 1 Step 2. more_routes untouched → Global Constraints.
2. **Placeholder scan:** No TBD; exact deletion targets and commands included.
3. **Type consistency:** N/A (test deletion only).
