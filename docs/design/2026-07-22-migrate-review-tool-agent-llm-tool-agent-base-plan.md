# Migrate ReviewToolAgent onto LlmToolAgentBase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ReviewToolAgent` / `BaseReviewToolAgent` a thin specialization of `LlmToolAgentBase` that selects the Review recipe (resolve models, `run_strands_agent` invocation, lenient/text JSON parse, single-shot fallback in `review`) without changing subclass behavior.

**Architecture:** In-place rewrite in `tool_agent_base.py`: inherit `LlmToolAgentBase`, set Review recipe class attrs, delete duplicate `__init__` / `_agent_factory`, keep `_run_agent` as an `_invoke_llm` alias, and rewire only the LLM branch of `review()` onto shared helpers. Leave `problem_solve` and `_engine_review` control flow untouched.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `LlmToolAgentBase` / `llm_service.strands_model`.

**Spec:** `docs/design/2026-07-22-migrate-review-tool-agent-llm-tool-agent-base-design.md`

**Worktree:** `.worktrees/issue-2044-migrate-review-tool-agent` on branch `refactor/2044-migrate-review-tool-agent`

## Global Constraints

- Do not change `_engine_review()` / `review_via_engine` behavior or its lazy `code_review_agent` import.
- Do not migrate `PlanGeneratorToolAgent` or `JsonGeneratorToolAgent`.
- Do not edit `tool_agent_static.py` (not a Review intermediate).
- Prefer no changes to `llm_tool_agent_base.py`.
- Hybrid fallback: wire helpers in `review()`; keep `problem_solve` hand-rolled loop and log wording.
- Keep `_run_agent` as a public-ish alias so documentation / other intermediates keep calling it.
- Dynamic review summaries stay dynamic — ignore `FallbackPayload.summary` / recommendations from helpers.
- Before `_parse_llm_json`, set `self.parse_context` and `self.parse_on_fail_msg` to match today’s lenient logging.
- Resolver/invocation come from `llm_service.strands_model` via the base (SE `shared.strands_model` remains a shim; existing tests that monkeypatch the SE path still work because of `sys.modules` aliasing).
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: update class/module docstrings — `Preconditions:` / `Postconditions:` / `Invariants:` where contracts change.
- ≥90% line coverage on touched files; `make lint` and relevant pytest must pass from `backend/`.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/tool_agent_base.py` | Inherit, recipe attrs, `_run_agent` alias, `review` LLM-path wire |
| `backend/agents/software_engineering_team/tests/test_shared_tool_agent_base.py` | Inheritance/recipe contract tests; existing behavioral suite stays green |
| Intermediates / concrete agents | No edits |
| `shared/llm_tool_agent_base.py` | No edits |

---

### Task 1: Failing inheritance / recipe contract tests

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_shared_tool_agent_base.py`

**Interfaces:**
- Consumes: `BaseReviewToolAgent`, `ReviewToolAgent` (existing); expects future inheritance from `LlmToolAgentBase` and recipe class attrs
- Produces: failing tests that lock the migration contract

- [ ] **Step 1: Add imports and contract tests**

At the top of `test_shared_tool_agent_base.py`, ensure these imports exist (add if missing):

```python
from software_engineering_team.shared.llm_tool_agent_base import LlmToolAgentBase
from software_engineering_team.shared.tool_agent_base import (
    BaseReviewToolAgent,
    ReviewToolAgent,
    # ... keep existing imports
)
```

Append at the end of the file:

```python
# ---------------------------------------------------------------------------
# LlmToolAgentBase migration contract
# ---------------------------------------------------------------------------


def test_review_tool_agent_is_llm_tool_agent_base_subclass():
    assert issubclass(BaseReviewToolAgent, LlmToolAgentBase)
    assert issubclass(ReviewToolAgent, LlmToolAgentBase)
    assert ReviewToolAgent is BaseReviewToolAgent


def test_review_tool_agent_selects_review_recipe_attrs():
    assert BaseReviewToolAgent.resolve_models is True
    assert BaseReviewToolAgent.response_format == "text"
    assert BaseReviewToolAgent.use_run_strands_agent is True
    assert BaseReviewToolAgent.json_parse_strategy == "lenient"
    assert BaseReviewToolAgent.review_parse_mode == "text"
    assert BaseReviewToolAgent.uses_json_model is False
```

- [ ] **Step 2: Run tests — expect FAIL**

```bash
cd backend
../../backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_shared_tool_agent_base.py::test_review_tool_agent_is_llm_tool_agent_base_subclass \
  agents/software_engineering_team/tests/test_shared_tool_agent_base.py::test_review_tool_agent_selects_review_recipe_attrs \
  -v --no-cov
```

(Use the repo’s `backend/.venv/bin/python` if the worktree has no local venv.)

Expected: FAIL — `BaseReviewToolAgent` is not a subclass of `LlmToolAgentBase` and/or missing recipe attrs.

- [ ] **Step 3: Commit the failing tests**

```bash
git add backend/agents/software_engineering_team/tests/test_shared_tool_agent_base.py
git commit -m "$(cat <<'EOF'
Add failing contract tests for ReviewToolAgent LlmToolAgentBase migration.

EOF
)"
```

---

### Task 2: Inherit `LlmToolAgentBase` and drop duplicate init/factory

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/tool_agent_base.py`

**Interfaces:**
- Consumes: `LlmToolAgentBase` (`resolve_models`, `use_run_strands_agent`, `json_parse_strategy`, `_invoke_llm`, `_agent_factory`, `__init__`)
- Produces: `BaseReviewToolAgent(LlmToolAgentBase)` with recipe attrs; `_run_agent` alias; no local `__init__` / `_agent_factory`

- [ ] **Step 1: Update imports**

In `tool_agent_base.py`:

- Remove unused `import importlib` (only used by the local `_agent_factory`).
- Keep `json`, `logging`, typing imports needed by `lenient_json_object` and the class.
- Add:

```python
from software_engineering_team.shared.llm_tool_agent_base import LlmToolAgentBase
```

- [ ] **Step 2: Change the class declaration and recipe attrs**

Replace:

```python
class BaseReviewToolAgent:
```

with:

```python
class BaseReviewToolAgent(LlmToolAgentBase):
```

Immediately after the class docstring / near the top of class attrs (before or among prompts/parsing), add the Review recipe selectors. Keep existing `review_parse_mode = "text"` and `uses_json_model = False` (do not duplicate if already present — only add the missing ones):

```python
    # --- LlmToolAgentBase Review recipe ---------------------------------
    resolve_models: bool = True
    response_format: str = "text"
    use_run_strands_agent: bool = True
    json_parse_strategy: str = "lenient"
    # review_parse_mode / uses_json_model already declared below as "text" / False
```

Update the class docstring invariants to note inheritance from `LlmToolAgentBase` and that model resolution / invocation / Agent factory come from the shared base when `resolve_models` / `use_run_strands_agent` are set.

Update the module docstring bullet that says `__init__` resolves via an in-method SE import — replace with: resolution is opted in via `LlmToolAgentBase` (`resolve_models = True`), which lazy-imports `llm_service.strands_model.resolve_strands_model`.

- [ ] **Step 3: Delete local `__init__` and `_agent_factory`; alias `_run_agent`**

Delete the entire local:

```python
def __init__(self, llm=None) -> None:
    from software_engineering_team.shared.strands_model import resolve_strands_model
    ...
```

and:

```python
def _agent_factory(self):
    ...
```

Replace `_run_agent` with:

```python
def _run_agent(self, model, prompt: str) -> str:
    """Invoke the LLM via the shared base path (``run_strands_agent``).

    Kept as a named alias so intermediates (e.g. documentation tool agents)
    that call ``_run_agent`` keep working without edits.

    Preconditions:
        ``use_run_strands_agent`` is True on this class (Review recipe).

    Postconditions:
        Returns the stripped string from ``_invoke_llm``.
    """
    return self._invoke_llm(model, prompt)
```

Do **not** change `review()` or `problem_solve()` in this task.

- [ ] **Step 4: Run contract + constructor + existing shared suite**

```bash
cd backend
../../backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_shared_tool_agent_base.py \
  -v --no-cov
```

Expected: PASS (including new contract tests and `test_constructor_resolves_*`).

If `test_constructor_resolves_*` fails because the monkeypatch target no longer fires, patch both paths in those two tests only:

```python
monkeypatch.setattr("llm_service.strands_model.resolve_strands_model", _record)
monkeypatch.setattr(
    "software_engineering_team.shared.strands_model.resolve_strands_model", _record
)
```

Prefer leaving tests unchanged if the SE shim already makes the existing patch work.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/shared/tool_agent_base.py \
  backend/agents/software_engineering_team/tests/test_shared_tool_agent_base.py
git commit -m "$(cat <<'EOF'
Derive ReviewToolAgent from LlmToolAgentBase with Review recipe attrs.

EOF
)"
```

---

### Task 3: Wire `review()` LLM path onto shared helpers

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/tool_agent_base.py` (`review` method only)

**Interfaces:**
- Consumes: `_fallback_no_model`, `_call_with_single_fallback`, `_invoke_llm`, `_parse_llm_json` from `LlmToolAgentBase`
- Produces: identical `ToolAgentPhaseOutput` summaries/issues for no-model, call-error, text parse, and json parse paths

- [ ] **Step 1: Replace the LLM branch of `review()`**

Keep the build-runner and engine branches exactly as they are. Replace only the body after those guards with:

```python
    def review(self, inp) -> ToolAgentPhaseOutput:
        if self.build_runner is not None:
            return self._build_review(inp)
        if self.review_via_engine:
            return self._engine_review(inp)
        review_label = f"{self.name} review"
        if self._fallback_no_model(self._model) is not None:
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no LLM).")
        code_text = self._build_code_text(inp.current_files)
        if not code_text.strip():
            return ToolAgentPhaseOutput(summary=f"{review_label} skipped (no code).")
        prompt = self.review_prompt.format(
            task_description=inp.task_description or "N/A",
            code=code_text,
        )
        model = getattr(self, self.review_model_attr)
        status, result = self._call_with_single_fallback(
            lambda: self._invoke_llm(model, prompt),
            log_label=review_label,
        )
        if status == "error":
            return ToolAgentPhaseOutput(summary=f"{review_label} failed (LLM error).")
        raw = result
        self.parse_context = review_label
        self.parse_on_fail_msg = "reporting 0 issues."
        data = self._parse_llm_json(raw)
        issues: List[ReviewIssue] = []
        for item in (data or {}).get("issues") or []:
            if isinstance(item, dict):
                issues.append(
                    ReviewIssue(
                        source=self.issue_source,
                        severity=item.get("severity", "medium"),
                        description=item.get("description", ""),
                        file_path=item.get("file_path", ""),
                        recommendation=item.get("recommendation", ""),
                    )
                )
        return ToolAgentPhaseOutput(
            issues=issues,
            summary=f"{review_label}: {len(issues)} issue(s) found.",
        )
```

Notes:

- Do not call `lenient_json_object` directly from `review` anymore — `_parse_llm_json` selects lenient vs `_parse_review` via `json_parse_strategy` / `review_parse_mode`.
- `(data or {})` guards the extract strategy’s `None`; Review uses lenient, which returns `{}` on failure, but the guard is harmless.
- Leave `problem_solve` unchanged (still uses `_run_agent` + local try/except).

- [ ] **Step 2: Run shared tool-agent suite**

```bash
cd backend
../../backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_shared_tool_agent_base.py \
  -v --no-cov
```

Expected: PASS — especially `test_review_no_model`, `test_review_llm_exception`, `test_review_finds_issues`, `test_review_json_mode`, and all `test_engine_review_*`.

- [ ] **Step 3: Run representative v2 + llm base suites**

```bash
cd backend
../../backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_shared_tool_agent_base.py \
  agents/software_engineering_team/tests/test_v2_tool_agents_testing_ux.py \
  agents/software_engineering_team/tests/test_v2_tool_agents.py \
  agents/software_engineering_team/tests/test_v2_tool_agents_more.py \
  agents/software_engineering_team/tests/test_llm_tool_agent_base.py \
  -v --no-cov
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/shared/tool_agent_base.py
git commit -m "$(cat <<'EOF'
Wire ReviewToolAgent.review onto LlmToolAgentBase fallback and parse helpers.

EOF
)"
```

---

### Task 4: Lint, coverage, full verification

**Files:**
- Modify: only if coverage or lint requires a tiny test/docstring fix in the files already touched

**Interfaces:**
- Consumes: Tasks 1–3 deliverables
- Produces: green `make lint` + coverage ≥90% on `tool_agent_base.py` + green targeted/`make test` evidence

- [ ] **Step 1: Lint**

```bash
cd backend && make lint
```

Expected: ruff check + format clean for touched files. Fix any new issues in `tool_agent_base.py` / the test file only.

- [ ] **Step 2: Coverage on touched module**

```bash
cd backend
../../backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_shared_tool_agent_base.py \
  agents/software_engineering_team/tests/test_v2_tool_agents_testing_ux.py \
  agents/software_engineering_team/tests/test_v2_tool_agents.py \
  agents/software_engineering_team/tests/test_v2_tool_agents_more.py \
  --cov=software_engineering_team.shared.tool_agent_base \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: coverage ≥90% for `tool_agent_base.py`. If a new line on the `_run_agent` alias or parse-context path is uncovered, add a focused assertion in `test_shared_tool_agent_base.py` (e.g. call `_run_agent` directly once) — do not lower the threshold.

- [ ] **Step 3: Full backend test target**

```bash
cd backend && make test
```

Expected: PASS. If the full suite is too slow in this environment, run it and report; do not claim done without this step or an explicit user waiver.

- [ ] **Step 4: Mark design status implemented (optional one-liner)**

In `docs/design/2026-07-22-migrate-review-tool-agent-llm-tool-agent-base-design.md`, change `Status: approved` → `Status: implemented`.

- [ ] **Step 5: Final commit if Step 1/2/4 produced diffs**

```bash
git add backend/agents/software_engineering_team/shared/tool_agent_base.py \
  backend/agents/software_engineering_team/tests/test_shared_tool_agent_base.py \
  docs/design/2026-07-22-migrate-review-tool-agent-llm-tool-agent-base-design.md
git commit -m "$(cat <<'EOF'
Finish ReviewToolAgent migration verification and mark design implemented.

EOF
)"
```

(Only include paths that actually changed.)

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Inherit `LlmToolAgentBase` + Review recipe attrs | 1, 2 |
| Delete duplicate `__init__` / `_agent_factory` | 2 |
| `_run_agent` → `_invoke_llm` alias | 2 |
| `review` uses `_fallback_no_model` / `_call_with_single_fallback` / `_invoke_llm` / `_parse_llm_json` | 3 |
| Dynamic summaries preserved | 3 |
| `parse_context` / `parse_on_fail_msg` set before parse | 3 |
| `problem_solve` unchanged (no `_call_partial_tolerant`) | 3 (explicit non-edit) |
| `_engine_review` / `review_via_engine` unchanged | 3 (explicit non-edit) |
| Intermediates unmodified | Global + Task 2 alias |
| Existing suites pass; lint; ≥90% coverage | 4 |
| No Plan/Json / static migration | Global Constraints |

## Placeholder / consistency self-review

- No TBD/TODO steps; full code for `review()` and contract tests included.
- Recipe attr names match `LlmToolAgentBase` (`resolve_models`, `use_run_strands_agent`, `json_parse_strategy`, `review_parse_mode`).
- Python path assumes worktree + main-repo `backend/.venv`; adjust if a worktree venv is created later.
