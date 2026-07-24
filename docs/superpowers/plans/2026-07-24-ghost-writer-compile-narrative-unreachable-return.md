# Remove unreachable `_compile_narrative` return Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the unreachable trailing `return None` after the narrator retry loop in `GhostWriterElicitationAgent._compile_narrative`.

**Architecture:** Inside `for attempt in range(2)`, every path returns or continues: success/`strip or None` returns in the `try`; attempt 0 exceptions `continue`; attempt 1 exceptions `return None`. The statement after the loop is dead and can be removed with no behavior change. Existing compile-narrative unit tests are the regression net.

**Tech Stack:** Python 3.10, pytest, existing ghost-writer tests in `test_ghost_writer_and_more.py`

## Global Constraints

- Touch only `backend/agents/blogging/ghost_writer_agent/agent.py` for production code.
- Delete exactly the trailing post-loop `return None`; do not rewrite the retry loop.
- Do not change docstrings, DbC, sleep duration, logging, or exception handling.
- Do not add new tests; rely on existing `compile_narrative` tests.
- Do not scan or edit other blogging modules.
- Work in the existing worktree at `.worktrees/fix-2369-unreachable-return-dead-code` on branch `fix/2369-unreachable-return-dead-code`.

---

## File map

| File | Responsibility |
|------|----------------|
| `backend/agents/blogging/ghost_writer_agent/agent.py` | Host of `_compile_narrative`; delete the unreachable post-loop `return None` |
| `backend/agents/blogging/tests/test_ghost_writer_and_more.py` | Existing regression tests for empty content, happy path, and double failure — read-only |

---

### Task 1: Delete the unreachable post-loop return

**Files:**
- Modify: `backend/agents/blogging/ghost_writer_agent/agent.py` (end of `_compile_narrative`)
- Test: `backend/agents/blogging/tests/test_ghost_writer_and_more.py` (run only; no edits)

**Interfaces:**
- Consumes: existing `_compile_narrative(self, gap, conversation, story_context=None) -> Optional[str]` retry loop
- Produces: same method signature and runtime behavior; no post-loop statement

- [ ] **Step 1: Confirm the dead line is still present**

From the worktree root:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2369-unreachable-return-dead-code
rg -n -A 15 'agent = Agent\(model=self\._model, system_prompt=_NARRATOR_SYSTEM\)' \
  backend/agents/blogging/ghost_writer_agent/agent.py
```

Expected: the snippet ends with:

```python
        agent = Agent(model=self._model, system_prompt=_NARRATOR_SYSTEM)
        for attempt in range(2):
            try:
                result = agent(prompt)
                return str(result).strip() or None
            except Exception as e:  # pragma: no cover - LLM-failure retry/exit branch in narrator; covered by integration tests with a flaky model.
                if attempt == 0:
                    logger.warning("Ghost writer narrator error, retrying: %s", e)
                    time.sleep(2.0)
                    continue
                logger.warning("Ghost writer narrator error after retry: %s", e)
                return None
        return None
```

- [ ] **Step 2: Delete the trailing `return None`**

In `backend/agents/blogging/ghost_writer_agent/agent.py`, change the end of `_compile_narrative` so the method closes immediately after the loop’s final in-body `return None`. After the edit, the block must be exactly:

```python
        agent = Agent(model=self._model, system_prompt=_NARRATOR_SYSTEM)
        for attempt in range(2):
            try:
                result = agent(prompt)
                return str(result).strip() or None
            except Exception as e:  # pragma: no cover - LLM-failure retry/exit branch in narrator; covered by integration tests with a flaky model.
                if attempt == 0:
                    logger.warning("Ghost writer narrator error, retrying: %s", e)
                    time.sleep(2.0)
                    continue
                logger.warning("Ghost writer narrator error after retry: %s", e)
                return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
```

Do not alter the `pragma: no cover` comment, the sleep call, log messages, or the Helpers section divider.

- [ ] **Step 3: Confirm the dead line is gone**

```bash
rg -n -A 14 'agent = Agent\(model=self\._model, system_prompt=_NARRATOR_SYSTEM\)' \
  backend/agents/blogging/ghost_writer_agent/agent.py
```

Expected: the loop ends with `return None` inside the `except`, then the Helpers divider — no indented `return None` between them.

Also assert with Python that the method source has no trailing post-loop return:

```bash
python - <<'PY'
from pathlib import Path
import ast, textwrap
src = Path("backend/agents/blogging/ghost_writer_agent/agent.py").read_text()
mod = ast.parse(src)
cls = next(n for n in mod.body if isinstance(n, ast.ClassDef) and n.name == "GhostWriterElicitationAgent")
fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_compile_narrative")
last = fn.body[-1]
assert isinstance(last, ast.For), type(last)
assert isinstance(last.body[-1], ast.Try)
print("ok: _compile_narrative ends with the retry for-loop")
PY
```

Expected: `ok: _compile_narrative ends with the retry for-loop`

- [ ] **Step 4: Run existing compile-narrative tests**

From `backend/` in the worktree:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2369-unreachable-return-dead-code/backend
pytest agents/blogging/tests/test_ghost_writer_and_more.py -k compile_narrative -q
```

Expected: PASS for at least:

- `test_ghost_compile_narrative_empty_user_content`
- `test_ghost_compile_narrative_happy_path_with_context`
- `test_ghost_compile_narrative_handles_errors`

- [ ] **Step 5: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-2369-unreachable-return-dead-code
git add backend/agents/blogging/ghost_writer_agent/agent.py
git commit -m "$(cat <<'EOF'
Remove unreachable return after ghost-writer narrator retry loop.

EOF
)"
```

---

## Spec coverage self-review

| Spec requirement | Task |
|------------------|------|
| Delete trailing `return None` only | Task 1 Steps 2–3 |
| Scope limited to `_compile_narrative` | Global Constraints + File map |
| Retry loop structure unchanged | Task 1 Step 2 exact block |
| No docstring / DbC / logging / sleep changes | Task 1 Step 2 |
| No new tests; existing compile-narrative tests | Task 1 Step 4 |
| Broader blogging scan out of scope | Global Constraints |
| No runtime behavior change | Task 1 Steps 2 + 4 |

Placeholder scan: none. Type consistency: N/A (deletion only).
