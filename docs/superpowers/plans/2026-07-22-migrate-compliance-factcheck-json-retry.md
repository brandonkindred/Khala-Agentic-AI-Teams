# Migrate Compliance/Fact-Check Agents to Shared JSON-Retry Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-rolled 2-attempt JSON-retry loops in `blog_compliance_agent` and `blog_fact_check_agent` with `call_json_with_retry()`, preserving each agent's exception classification and fallback behavior.

**Architecture:** Direct call-site migration. Fold the always-on "Respond with valid JSON…" instruction into the initial helper `prompt`. Pass agent-specific `strict_json_suffix`. Compliance uses `on_exhausted` + `on_unexpected_error` (fail closed). Fact-check uses `on_exhausted` (fail closed on parse) and wraps unexpected errors in `FactCheckError` outside the helper. Accept helper retry-suffix order (`prompt + suffix`) as a non-semantic change.

**Tech Stack:** Python 3.10+, pytest, ruff, `agents.blogging.shared.call_json_with_retry`, strands `Agent`.

**Spec:** `docs/superpowers/specs/2026-07-22-migrate-compliance-factcheck-json-retry-design.md`

**Worktree:** `.worktrees/issue-2080-migrate-compliance-factcheck-json-retry` on branch `refactor/2080-migrate-compliance-factcheck-json-retry`

## Global Constraints

- Do not modify `backend/agents/blogging/shared/json_retry.py`.
- Do not migrate any other blogging agents.
- Preserve exception classification exactly: transient → re-raise; compliance unexpected → FAIL fallback; fact-check unexpected → `FactCheckError`.
- Attempt-1 prompts must stay identical to today (always-on JSON instruction in the initial `prompt`).
- Retry suffix *order* may differ from today; content of agent-specific suffixes must be preserved.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: keep/update `Preconditions:` / `Postconditions:` on `run()` where the control flow changes.
- ≥90% line coverage on touched agent modules; `make lint` clean from `backend/`.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/blogging/blog_compliance_agent/agent.py` | Replace local retry loop with helper; fail-closed hooks |
| `backend/agents/blogging/blog_fact_check_agent/agent.py` | Replace local retry loop with helper; remove `_MAX_JSON_RETRIES` |
| `backend/agents/blogging/tests/test_more_agents.py` | Existing compliance retry/fallback/transient coverage (unchanged unless broken) |
| `backend/agents/blogging/tests/test_more_coverage.py` | Existing fact-check retry/fallback/error coverage (unchanged unless broken) |
| `backend/agents/blogging/tests/test_compliance.py` | Smoke coverage (unchanged) |
| `backend/agents/blogging/tests/test_fact_check.py` | Smoke + transient coverage (unchanged) |

---

### Task 1: Migrate `BlogComplianceAgent` onto `call_json_with_retry`

**Files:**
- Modify: `backend/agents/blogging/blog_compliance_agent/agent.py`
- Test: `backend/agents/blogging/tests/test_more_agents.py` (existing; do not rewrite unless a real failure appears)
- Test: `backend/agents/blogging/tests/test_compliance.py` (existing)

**Interfaces:**
- Consumes: `call_json_with_retry(agent_factory, prompt, *, max_attempts=2, strict_json_suffix=..., on_exhausted=..., on_unexpected_error=..., logger=...) -> Dict[str, Any]`
- Produces: `BlogComplianceAgent.run(...)` still returns `ComplianceReport` with the same classification table as the spec

- [ ] **Step 1: Confirm baseline tests pass before editing**

Run from `backend/`:

```bash
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_compliance.py \
  agents/blogging/tests/test_more_agents.py \
  -q --tb=line
```

Expected: PASS (all collected tests green).

- [ ] **Step 2: Replace the compliance retry loop with the shared helper**

In `backend/agents/blogging/blog_compliance_agent/agent.py`:

1. Change imports — remove `LLMJsonParseError`, `LLMRateLimitError`, `LLMTemporaryError`, and `extract_json_from_response`. Add:

```python
from agents.blogging.shared.json_retry import call_json_with_retry
```

2. Keep `_JSON_RETRY_SUFFIX` as-is.

3. Add a module-level always-on instruction constant (same text the loop appends today):

```python
_ALWAYS_ON_JSON_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."
```

4. Replace the block from `agent = Agent(...)` through `if not data: ... return report` with:

```python
        def _agent_factory():
            return Agent(
                model=self._model,
                system_prompt="You are a brand compliance evaluator.",
            )

        prompt_for_helper = prompt + _ALWAYS_ON_JSON_INSTRUCTION

        def _fallback_dict(exc: Exception) -> Dict[str, Any]:
            return _fallback_compliance_report(exc).to_dict()

        data = call_json_with_retry(
            _agent_factory,
            prompt_for_helper,
            max_attempts=2,
            strict_json_suffix=_JSON_RETRY_SUFFIX,
            on_exhausted=_fallback_dict,
            on_unexpected_error=_fallback_dict,
            logger=logger,
        )
```

5. Leave the existing post-parse report construction + `write_artifact` path unchanged. It must also handle fallback dicts (status/violations/required_fixes/notes) — do **not** special-case fallback returns inside the helper call.

6. Update `run()`'s Postconditions docstring only if needed for accuracy (helper never returns `None`; transient errors still propagate unwrapped via the helper).

- [ ] **Step 3: Re-run compliance tests**

```bash
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_compliance.py \
  agents/blogging/tests/test_more_agents.py \
  -q --tb=short
```

Expected: PASS, including:
- `test_compliance_run_fallback_on_persistent_parse_failure`
- `test_compliance_run_with_exception_fallback`
- `test_compliance_run_transient_error_reraises`

- [ ] **Step 4: Commit**

```bash
git add backend/agents/blogging/blog_compliance_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate blog compliance agent onto shared call_json_with_retry.

EOF
)"
```

---

### Task 2: Migrate `BlogFactCheckAgent` onto `call_json_with_retry`

**Files:**
- Modify: `backend/agents/blogging/blog_fact_check_agent/agent.py`
- Test: `backend/agents/blogging/tests/test_fact_check.py` (existing)
- Test: `backend/agents/blogging/tests/test_more_coverage.py` (existing)

**Interfaces:**
- Consumes: same `call_json_with_retry` signature as Task 1
- Produces: `BlogFactCheckAgent.run(...)` still returns `FactCheckReport`; unexpected non-transient errors still raise `FactCheckError`

- [ ] **Step 1: Confirm baseline fact-check tests pass**

```bash
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_fact_check.py \
  agents/blogging/tests/test_more_coverage.py \
  -q --tb=line
```

Expected: PASS.

- [ ] **Step 2: Replace the fact-check retry loop with the shared helper**

In `backend/agents/blogging/blog_fact_check_agent/agent.py`:

1. Remove `_MAX_JSON_RETRIES = 2`.

2. Change imports — remove `LLMJsonParseError` and `extract_json_from_response`. Keep `LLMRateLimitError` and `LLMTemporaryError` for the outer re-raise guard. Add:

```python
from agents.blogging.shared.json_retry import call_json_with_retry
```

3. Add constants:

```python
_ALWAYS_ON_JSON_INSTRUCTION = "\n\nRespond with valid JSON only, no markdown fences."

_JSON_RETRY_SUFFIX = (
    "\n\nCRITICAL: Your previous response contained invalid JSON. "
    "Output ONLY a single valid JSON object. No code blocks or markdown in values."
)
```

4. Replace the block from `agent = Agent(...)` through `if data is None: ... return report` with:

```python
        def _agent_factory():
            return Agent(
                model=self._model,
                system_prompt=FACT_CHECK_PROMPT.split("{draft}")[0].strip(),
            )

        prompt_for_helper = prompt + _ALWAYS_ON_JSON_INSTRUCTION

        def _on_exhausted(_exc: Exception) -> Dict[str, Any]:
            return {
                "claims_status": "FAIL",
                "risk_status": "FAIL",
                "risk_flags": ["Could not parse fact-check result; re-run fact check."],
                "required_disclaimers": [],
                "notes": "Fallback report: JSON parse failed after 2 attempts.",
            }

        try:
            data = call_json_with_retry(
                _agent_factory,
                prompt_for_helper,
                max_attempts=2,
                strict_json_suffix=_JSON_RETRY_SUFFIX,
                on_exhausted=_on_exhausted,
                logger=logger,
            )
        except (LLMRateLimitError, LLMTemporaryError):
            raise
        except Exception as e:
            logger.exception("Fact-check failed: %s", e)
            raise FactCheckError(f"Fact-check failed: {e}", cause=e) from e
```

Note: the helper already logs unexpected errors at exception level before re-raising. The outer `logger.exception` preserves today's agent-boundary log line before wrapping in `FactCheckError`. Keep it.

5. Leave the existing status-normalization + `FactCheckReport(...)` + `write_artifact` path unchanged so exhausted-fallback dicts flow through the same construction path.

- [ ] **Step 3: Re-run fact-check tests**

```bash
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_fact_check.py \
  agents/blogging/tests/test_more_coverage.py \
  -q --tb=short
```

Expected: PASS, including:
- `test_fact_check_run_json_retry_then_fallback`
- `test_fact_check_run_llm_exception_raises`
- `test_fact_check_transient_error_reraises`

- [ ] **Step 4: Commit**

```bash
git add backend/agents/blogging/blog_fact_check_agent/agent.py
git commit -m "$(cat <<'EOF'
Migrate blog fact-check agent onto shared call_json_with_retry.

EOF
)"
```

---

### Task 3: Lint and coverage closeout

**Files:**
- Verify only: the two agent modules from Tasks 1–2

**Interfaces:**
- Consumes: Task 1 + Task 2 migrations
- Produces: lint-clean, coverage-gated confirmation

- [ ] **Step 1: Run ruff on touched files**

From `backend/`:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff check \
  agents/blogging/blog_compliance_agent/agent.py \
  agents/blogging/blog_fact_check_agent/agent.py
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff format --check \
  agents/blogging/blog_compliance_agent/agent.py \
  agents/blogging/blog_fact_check_agent/agent.py
```

Expected: clean (exit 0). Fix any issues in place.

- [ ] **Step 2: Run focused suite with coverage on touched modules**

```bash
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/blogging/tests/test_compliance.py \
  agents/blogging/tests/test_fact_check.py \
  agents/blogging/tests/test_more_agents.py \
  agents/blogging/tests/test_more_coverage.py \
  agents/blogging/tests/test_json_retry.py \
  --cov=agents.blogging.blog_compliance_agent.agent \
  --cov=agents.blogging.blog_fact_check_agent.agent \
  --cov-report=term-missing \
  -q
```

Expected: all tests PASS; line coverage ≥ 90% on both agent modules.

- [ ] **Step 3: Commit any lint/format-only fixes (skip if none)**

```bash
git add backend/agents/blogging/blog_compliance_agent/agent.py \
        backend/agents/blogging/blog_fact_check_agent/agent.py
git commit -m "$(cat <<'EOF'
Clean lint on compliance and fact-check JSON-retry migrations.

EOF
)"
```

Only create this commit if Step 1 produced file changes.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Both call sites use `call_json_with_retry` | Tasks 1–2 |
| Remove `_MAX_JSON_RETRIES`-style locals | Task 2 (`_MAX_JSON_RETRIES`); Task 1 never had one |
| Preserve exception classification | Tasks 1–2 hooks / outer catch |
| Existing tests pass | Steps 3 in Tasks 1–2; Task 3 |
| `make lint` + 90% coverage | Task 3 |
| No helper changes / no other agents | Global constraints |
| Accept retry suffix order change | Global constraints + Task steps (no exact-prompt asserts) |
