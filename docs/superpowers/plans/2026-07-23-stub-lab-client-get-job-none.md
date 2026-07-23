# StubLabClient get_job Missing-ID Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pin `_StubLabClient.get_job()` so an unknown job ID returns `None` (matching production), via two direct unit tests and no stub rewrite.

**Architecture:** The stub already uses a membership check before indexing `self.by_id`. Add two tests immediately after the `_StubLabClient` class in `test_strategy_lab_routes.py`. Do not change production code or the stub body.

**Tech Stack:** Python 3.10+, pytest, FastAPI test suite under `backend/agents/investment_team`.

## Global Constraints

- Leave `_StubLabClient.get_job` ternary unchanged.
- Touch only `backend/agents/investment_team/tests/test_strategy_lab_routes.py` for code.
- Never reference GitHub issue numbers in code, comments, docs, or commit messages.
- Run verification with `LLM_PROVIDER=dummy`.
- Design-by-Contract: document preconditions/postconditions on new test helpers only if any are introduced (prefer none).

## File map

| File | Role |
|---|---|
| `backend/agents/investment_team/tests/test_strategy_lab_routes.py` | Hosts `_StubLabClient`; add two unit tests after the class |
| `docs/superpowers/specs/2026-07-23-stub-lab-client-get-job-none-design.md` | Spec (already committed; do not edit unless requirements change) |

---

### Task 1: Direct unit tests for `_StubLabClient.get_job`

**Files:**
- Modify: `backend/agents/investment_team/tests/test_strategy_lab_routes.py` (insert after `_StubLabClient`, before the `# run_strategy_lab` section ~line 115)
- Test: same file

**Interfaces:**
- Consumes: `_StubLabClient.__init__(jobs: Optional[List[Dict[str, Any]]] = None)`, `_StubLabClient.get_job(jid: str) -> Optional[Dict[str, Any]]`
- Produces: `test_stub_lab_client_get_job_returns_none_for_unknown_id`, `test_stub_lab_client_get_job_returns_copy_for_known_id`

- [ ] **Step 1: Write the two tests (TDD — stub already correct, so they should pass on first run)**

Insert this block after the `_StubLabClient` class (after `delete_job`, before the `# run_strategy_lab` banner):

```python
# ---------------------------------------------------------------------------
# _StubLabClient.get_job contract
# ---------------------------------------------------------------------------


def test_stub_lab_client_get_job_returns_none_for_unknown_id() -> None:
    stub = _StubLabClient()
    assert stub.get_job("missing-id") is None


def test_stub_lab_client_get_job_returns_copy_for_known_id() -> None:
    job = {"job_id": "run-1", "status": "completed", "data": {"total_cycles": 2}}
    stub = _StubLabClient(jobs=[job])
    got = stub.get_job("run-1")
    assert got == job
    assert got is not job
    assert got is not stub.by_id["run-1"]
```

- [ ] **Step 2: Run the new tests**

Run from `backend/`:

```bash
LLM_PROVIDER=dummy python -m pytest agents/investment_team/tests/test_strategy_lab_routes.py::test_stub_lab_client_get_job_returns_none_for_unknown_id agents/investment_team/tests/test_strategy_lab_routes.py::test_stub_lab_client_get_job_returns_copy_for_known_id -v
```

Expected: both PASS.

If either FAILS with `KeyError`, restore/keep:

```python
def get_job(self, jid: str) -> Optional[Dict[str, Any]]:
    return dict(self.by_id[jid]) if jid in self.by_id else None
```

Do **not** rewrite to a longer form unless the ternary is broken.

- [ ] **Step 3: Run the full file suite**

```bash
LLM_PROVIDER=dummy python -m pytest agents/investment_team/tests/test_strategy_lab_routes.py -q
```

Expected: all tests PASS (no regressions for existing `_StubLabClient` consumers).

- [ ] **Step 4: Lint**

```bash
cd backend && make lint
```

Expected: ruff check + format clean for touched files.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/investment_team/tests/test_strategy_lab_routes.py
git commit -m "$(cat <<'EOF'
Pin StubLabClient get_job missing-ID contract with direct unit tests.

EOF
)"
```

---

## Spec coverage self-review

| Spec requirement | Task |
|---|---|
| Unknown ID → `None` | Task 1 Step 1 (`test_stub_lab_client_get_job_returns_none_for_unknown_id`) |
| Known ID → dict equal to seeded job / copy semantics | Task 1 Step 1 (`test_stub_lab_client_get_job_returns_copy_for_known_id`) |
| Leave stub ternary unchanged | Global Constraints + Task 1 Step 2 |
| Existing consumers unchanged | Task 1 Step 3 |
| `LLM_PROVIDER=dummy` + lint | Task 1 Steps 2–4 |
| Out of scope (production client, other stubs, if-rewrite) | Not tasked |

No placeholders. Single task — appropriate for Fibonacci complexity 1.
