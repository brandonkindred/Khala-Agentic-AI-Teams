# BaseTeamLead Multi-Gate Phase Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a complementary `_run_phase_gates` hook on `BaseTeamLead` that runs multiple sequential early-exit gates within a single phase, covered by unit tests, with no consumer wiring yet.

**Architecture:** `_run_phase_gates(gates: Sequence[Callable[[], Optional[T]]]) -> Optional[T]` delegates to existing `_run_gated_phases`. Same `Optional[T]` failure contract; naming marks the call site as an intra-phase gate sequence (devops Phase 4 shape). No loop duplication. No devops migration in this plan.

**Tech Stack:** Python 3.10, pytest, Ruff (via `make lint`)

**Spec:** `docs/superpowers/specs/2026-07-22-base-team-lead-multi-gate-phase-extension-design.md`

## Global Constraints

- Failure contract is `Optional[T]`: `None` = success; non-`None` = failure payload to return.
- Method name is `_run_phase_gates`; gates are zero-arg `Callable[[], Optional[T]]`.
- Implementation must delegate to `_run_gated_phases` (no duplicated loop body).
- Empty sequence → `None`; exceptions from gates are not caught.
- No logging/status inside the helper.
- No changes to `devops_team/orchestrator.py`, code-v2 orchestrators, or coding_team.
- Do not change `_run_gated_phases` semantics or loop body.
- 90% coverage floor on touched files; `make test` and `make lint` must pass from `backend/`.
- Design-by-Contract: document Preconditions/Postconditions on `_run_phase_gates`.
- Never reference GitHub issue numbers in code, comments, docs, or commit messages.

## File Structure

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | `_run_phase_gates` implementation (delegate); module/class docstring mention |
| `backend/agents/software_engineering_team/tests/test_team_lead_base.py` | Unit tests for multi-gate success, early-exit, empty sequence, exception propagation |

---

### Task 1: Intra-phase gate hook (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_team_lead_base.py`
- Modify: `backend/agents/software_engineering_team/shared/team_lead_base.py`

**Interfaces:**
- Consumes: existing `BaseTeamLead._run_gated_phases` and `_make_lead()` test helper
- Produces: `BaseTeamLead._run_phase_gates(gates: Sequence[Callable[[], Optional[T]]]) -> Optional[T]`

- [ ] **Step 1: Write the failing tests**

Append after the existing `_run_gated_phases` tests in `backend/agents/software_engineering_team/tests/test_team_lead_base.py` (before the bounded-retry tests). Existing imports already include `pytest`; no new imports required:

```python
def test_run_phase_gates_all_succeed_returns_none():
    lead = _make_lead()
    calls: list[str] = []

    def gate_a():
        calls.append("a")
        return None

    def gate_b():
        calls.append("b")
        return None

    def gate_c():
        calls.append("c")
        return None

    result = lead._run_phase_gates([gate_a, gate_b, gate_c])
    assert result is None
    assert calls == ["a", "b", "c"]


def test_run_phase_gates_early_exit_skips_later_gates():
    lead = _make_lead()
    calls: list[str] = []
    failure = object()

    def gate_ok():
        calls.append("ok")
        return None

    def gate_fail():
        calls.append("fail")
        return failure

    def gate_never():
        calls.append("never")
        return None

    result = lead._run_phase_gates([gate_ok, gate_fail, gate_never])
    assert result is failure
    assert calls == ["ok", "fail"]


def test_run_phase_gates_empty_sequence_returns_none():
    lead = _make_lead()
    assert lead._run_phase_gates([]) is None


def test_run_phase_gates_propagates_gate_exceptions():
    lead = _make_lead()

    def boom():
        raise RuntimeError("gate exploded")

    with pytest.raises(RuntimeError, match="gate exploded"):
        lead._run_phase_gates([boom])
```

- [ ] **Step 2: Run tests to verify they fail**

From the worktree's `backend/` directory (reuse the main-repo venv if the worktree has none):

```bash
PY=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python
$PY -m pytest \
  agents/software_engineering_team/tests/test_team_lead_base.py::test_run_phase_gates_all_succeed_returns_none \
  agents/software_engineering_team/tests/test_team_lead_base.py::test_run_phase_gates_early_exit_skips_later_gates \
  agents/software_engineering_team/tests/test_team_lead_base.py::test_run_phase_gates_empty_sequence_returns_none \
  agents/software_engineering_team/tests/test_team_lead_base.py::test_run_phase_gates_propagates_gate_exceptions \
  -v
```

Expected: FAIL — `AttributeError` for missing `_run_phase_gates` on `BaseTeamLead`.

- [ ] **Step 3: Implement `_run_phase_gates`**

In `backend/agents/software_engineering_team/shared/team_lead_base.py`:

1. Update the module docstring clause that currently mentions `_run_gated_phases` / `_run_bounded_retry_loop` so it also names `_run_phase_gates` as the intra-phase gate hook. Keep it one short clause — for example:

```text
gate-based phase-sequencing helper via :meth:`BaseTeamLead._run_gated_phases`,
an intra-phase multi-gate hook via :meth:`BaseTeamLead._run_phase_gates`,
and a bounded retry/patch-loop via :meth:`BaseTeamLead._run_bounded_retry_loop`.
```

2. Update the `BaseTeamLead` class docstring similarly (the sentence that currently lists `_run_gated_phases` and `_run_bounded_retry_loop`) to also mention `_run_phase_gates`.

3. Add the method on `BaseTeamLead` immediately after `_run_gated_phases` (before `_run_bounded_retry_loop`):

```python
def _run_phase_gates(
    self,
    gates: Sequence[Callable[[], Optional[T]]],
) -> Optional[T]:
    """Run intra-phase gate callables; return the first failure payload.

    Preconditions: ``gates`` is a sequence (may be empty); each element is
      a zero-arg callable returning ``Optional[T]``.
    Postconditions: same as :meth:`_run_gated_phases` — first non-``None``
      wins; all-``None`` / empty → ``None``; exceptions propagate.
    """
    return self._run_gated_phases(gates)
```

Do **not** edit `devops_team/orchestrator.py` or any code-v2/coding_team consumer. Do **not** rewrite the `_run_gated_phases` loop.

- [ ] **Step 4: Run unit tests to verify they pass**

From the worktree's `backend/`:

```bash
PY=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python
$PY -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -v \
  --cov=software_engineering_team.shared.team_lead_base \
  --cov-report=term-missing
```

Expected: all tests PASS (existing + new); `team_lead_base.py` line coverage ≥ 90%.

- [ ] **Step 5: Lint and broader SE-team sanity check**

From the worktree's `backend/`:

```bash
make lint
PY=/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python
$PY -m pytest agents/software_engineering_team/tests/test_team_lead_base.py -q
```

Expected: Ruff clean; new + existing `test_team_lead_base` tests pass.

Full suite when ready:

```bash
make test
```

Confirm `git diff -- backend/agents/software_engineering_team/devops_team/orchestrator.py` is empty.

- [ ] **Step 6: Commit**

Only if the user has asked to commit (or this plan is being executed under an explicit commit instruction). Stage the Python files; force-add plan/spec docs only if they are part of this change and still untracked (`docs/superpowers/*` is gitignored):

```bash
git add \
  backend/agents/software_engineering_team/shared/team_lead_base.py \
  backend/agents/software_engineering_team/tests/test_team_lead_base.py
git commit -m "$(cat <<'EOF'
Add intra-phase multi-gate hook to BaseTeamLead.

Provides _run_phase_gates as a thin delegate over _run_gated_phases
for multi-gate phases, without wiring devops consumers yet.
EOF
)"
```

---

## Self-review

1. **Spec coverage:** Complementary `_run_phase_gates` API, delegate implementation, multi-gate success + early-exit tests, empty/exception coverage, no devops edits, lint/test/coverage — all covered by Task 1.
2. **Placeholders:** None.
3. **Type consistency:** `_run_phase_gates(gates: Sequence[Callable[[], Optional[T]]]) -> Optional[T]` matches the locked design and the test call sites; delegates to `_run_gated_phases(gates)`.
