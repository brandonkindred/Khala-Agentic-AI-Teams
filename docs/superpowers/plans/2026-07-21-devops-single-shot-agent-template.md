# DevOps Single-Shot Agent Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `DevOpsSingleShotAgent` — a shared base class capturing the scaffolding used by the seven devops single-shot JSON agents — plus unit tests, without migrating any consumer agents.

**Architecture:** One new module under `devops_team/` owning `__init__` + `run` scaffolding; subclasses will later override `PROMPT`, `build_context`, `build_output`, and optionally `pre_call` / `temperature` / `think`. This change ships only the base and its tests.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `complete_json_with_continuation` / `resolve_strands_model` helpers.

**Spec:** `docs/superpowers/specs/2026-07-21-devops-single-shot-agent-template-design.md`

## Global Constraints

- Do not edit any of the seven existing devops agent files (`iac_agent`, `cicd_pipeline_agent`, `deployment_strategy_agent`, `doc_runbook_agent`, `infra_patch_agent`, `infra_debug_agent`, `devsecops_review_agent`).
- Do not change `complete_json_with_continuation`'s signature or behavior.
- Call `complete_json_with_continuation` via a direct import from `software_engineering_team.shared.llm` (no per-subclass-module lookup). Document that migrations must patch `_agent_template.complete_json_with_continuation` (module binding).
- Include the canonical-helper decision record (`complete_json_with_continuation` vs `run_structured_persona`) in the module docstring.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: every public method gets `Preconditions:` / `Postconditions:` (and `Invariants:` on the class) in its docstring.
- ≥90% line coverage on the new module; `make lint` and the new test file must pass from `backend/`.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/devops_team/_agent_template.py` | `DevOpsSingleShotAgent` base + decision-record docstring |
| `backend/agents/software_engineering_team/tests/test_devops_agent_template.py` | Unit tests: boilerplate, pre-call, post-call, omit-kwargs, None client |

---

### Task 1: Failing tests for the base class

**Files:**
- Create: `backend/agents/software_engineering_team/tests/test_devops_agent_template.py`

**Interfaces:**
- Consumes: (nothing yet — tests import `DevOpsSingleShotAgent` which does not exist)
- Produces: five failing tests that lock the public contract

- [ ] **Step 1: Create the test file**

Write `backend/agents/software_engineering_team/tests/test_devops_agent_template.py` with this exact content:

```python
"""Unit tests for devops_team._agent_template.DevOpsSingleShotAgent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import pytest

from software_engineering_team.tests.conftest import _strands_model_double


@dataclass
class _FakeOut:
    summary: str
    derived: bool = False


def test_boilerplate_calls_helper_with_prompt_context_and_defaults(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    captured: Dict[str, Any] = {}

    def fake_complete(model, prompt, **kwargs):
        captured["model"] = model
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return {"summary": "ok"}

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        fake_complete,
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "SYSTEM PROMPT"

        def build_context(self, input_data: str) -> str:
            return f"ctx={input_data}"

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary=data.get("summary", ""))

    model = _strands_model_double()
    agent = Agent(model)
    out = agent.run("task-1")

    assert out == _FakeOut(summary="ok")
    assert captured["model"] is agent._model
    assert captured["prompt"] == "SYSTEM PROMPT\n\n---\n\nctx=task-1"
    assert captured["kwargs"] == {"temperature": 0.1, "think": True}


def test_pre_call_short_circuits_without_llm(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    def boom(*_a, **_kw):
        raise AssertionError("complete_json_with_continuation must not be called")

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        boom,
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "UNUSED"

        def pre_call(self, input_data: str) -> Optional[_FakeOut]:
            if input_data == "skip":
                return _FakeOut(summary="early")
            return None

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary="should-not-reach")

    out = Agent(_strands_model_double()).run("skip")
    assert out == _FakeOut(summary="early")


def test_build_output_post_call_derives_field(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        lambda *_a, **_kw: {"errors": [{"error_type": "syntax"}], "summary": "dbg"},
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "DEBUG"

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            errors = data.get("errors") or []
            derived = bool(errors) and all(e.get("error_type") == "syntax" for e in errors)
            return _FakeOut(summary=data.get("summary", ""), derived=derived)

    out = Agent(_strands_model_double()).run("x")
    assert out == _FakeOut(summary="dbg", derived=True)


def test_none_temperature_and_think_omit_kwargs(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    captured: Dict[str, Any] = {}

    def fake_complete(model, prompt, **kwargs):
        captured["kwargs"] = kwargs
        return {"summary": "bare"}

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        fake_complete,
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "DOC"
        temperature = None
        think = None

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary=data.get("summary", ""))

    out = Agent(_strands_model_double()).run("notes")
    assert out.summary == "bare"
    assert captured["kwargs"] == {}


def test_none_llm_client_raises() -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    class Agent(DevOpsSingleShotAgent):
        PROMPT = "X"

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary="")

    with pytest.raises(AssertionError, match="llm_client is required"):
        Agent(None)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run tests to verify they fail on missing module**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_devops_agent_template.py -v
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError` for `software_engineering_team.devops_team._agent_template`.

- [ ] **Step 3: Commit the failing tests**

```bash
git add backend/agents/software_engineering_team/tests/test_devops_agent_template.py
git commit -m "$(cat <<'EOF'
Add failing tests for devops single-shot agent template.

Lock the boilerplate, pre-call, post-call, omit-kwargs, and None-client
contracts before the base class exists.
EOF
)"
```

---

### Task 2: Implement `DevOpsSingleShotAgent`

**Files:**
- Create: `backend/agents/software_engineering_team/devops_team/_agent_template.py`
- Test: `backend/agents/software_engineering_team/tests/test_devops_agent_template.py`

**Interfaces:**
- Consumes: `llm_service.LLMClient`, `llm_service.get_strands_model`, `llm_service.strands_model.resolve_strands_model`, `software_engineering_team.shared.llm.complete_json_with_continuation`
- Produces:
  - `class DevOpsSingleShotAgent` with class attrs `PROMPT: str = ""`, `temperature: float | None = 0.1`, `think: bool | None = True`
  - `__init__(self, llm_client) -> None`
  - `pre_call(self, input_data) -> Any | None` (default `None`)
  - `build_context(self, input_data) -> str` (raises `NotImplementedError`)
  - `build_output(self, input_data, data: dict) -> Any` (raises `NotImplementedError`)
  - `run(self, input_data) -> Any`

- [ ] **Step 1: Create the module**

Write `backend/agents/software_engineering_team/devops_team/_agent_template.py` with this exact content:

```python
"""Config-driven base for devops_team single-shot JSON agents.

Canonical helper decision
-------------------------
``complete_json_with_continuation`` is the canonical helper for
``devops_team``'s single-shot JSON agents.

``run_structured_persona`` (``shared/persona_agent_base.py``) remains the
pattern for the four agents already using it (``security_agent``,
``qa_agent``, ``accessibility_agent``, ``integration_team``). Switching
devops onto ``run_structured_persona`` was considered and deferred: that
helper centralizes dataclass construction via Strands
``structured_output_model`` and requires a ``fallback_factory`` per agent,
and several devops outputs carry nested models
(``DevOpsCompletionPackage``, ``IaCExecutionError``, ``ReviewFinding``)
that would need verification before a switch. The devops standardization
effort only asks to standardize on one helper, not to migrate away from
``complete_json_with_continuation``.

Monkeypatchability
------------------
This module imports and calls ``complete_json_with_continuation`` from
``software_engineering_team.shared.llm`` directly (no per-subclass-module
lookup). When a consumer agent is migrated onto this base, any test that
monkeypatches ``…devops_team.<agent>.agent.complete_json_with_continuation``
must retarget the patch to
``software_engineering_team.devops_team._agent_template.complete_json_with_continuation``
(or continue patching ``shared.llm.Agent``, which fence-recovery helpers
already do via ``_patch_fenced_response``).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from llm_service import LLMClient, get_strands_model
from llm_service.strands_model import resolve_strands_model
from software_engineering_team.shared.llm import complete_json_with_continuation


class DevOpsSingleShotAgent:
    """Shared scaffolding for devops single-shot JSON agents.

    Invariants:
        - Instance state is limited to ``llm`` and the resolved Strands
          ``_model``.
        - ``run`` is stateless across calls aside from that resolved model.
        - Subclasses set a non-empty ``PROMPT`` and override
          ``build_context`` / ``build_output``; ``pre_call`` may short-circuit.
    """

    PROMPT: str = ""
    temperature: Optional[float] = 0.1
    think: Optional[bool] = True

    def __init__(self, llm_client: LLMClient) -> None:
        """Resolve the devops-routed Strands model.

        Preconditions: ``llm_client`` is not ``None`` (an ``LLMClient`` or a
        Strands ``Model``).
        Postconditions: ``self.llm`` is the passed client; ``self._model`` is
        the resolved Strands model under ``agent_key="devops"``.
        """
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._model = resolve_strands_model(
            llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
        )

    def pre_call(self, input_data: Any) -> Any | None:
        """Optional early-return hook before any LLM call.

        Preconditions: ``input_data`` is whatever the subclass ``run`` accepts.
        Postconditions: returns ``None`` to continue, or a finished output to
        return from ``run`` without calling the LLM. Default: ``None``.
        """
        return None

    def build_context(self, input_data: Any) -> str:
        """Build the context string appended after the prompt separator.

        Preconditions: called only when ``pre_call`` returned ``None``.
        Postconditions: returns a string (possibly empty) concatenated into
        the LLM prompt. Subclasses must override.
        """
        raise NotImplementedError(f"{type(self).__name__}.build_context must be overridden")

    def build_output(self, input_data: Any, data: Dict[str, Any]) -> Any:
        """Construct the agent output from the parsed JSON dict.

        Preconditions: ``data`` is the dict returned by
        ``complete_json_with_continuation``.
        Postconditions: returns the subclass output object. Owns all post-call
        special cases (derived fields, secondary non-LLM objects). Subclasses
        must override.
        """
        raise NotImplementedError(f"{type(self).__name__}.build_output must be overridden")

    def run(self, input_data: Any) -> Any:
        """Run the single-shot LLM call and build the output.

        Preconditions:
            ``self.PROMPT`` is a non-empty string; ``build_context`` and
            ``build_output`` are overridden on the concrete subclass.
        Postconditions:
            If ``pre_call`` returns non-``None``, that value is returned and
            the LLM is not called. Otherwise returns
            ``build_output(input_data, data)`` where ``data`` comes from
            ``complete_json_with_continuation`` with prompt
            ``PROMPT + "\\n\\n---\\n\\n" + context``. ``temperature`` /
            ``think`` class attrs are passed as kwargs only when not ``None``.
            LLM/parse errors propagate unchanged.
        """
        early = self.pre_call(input_data)
        if early is not None:
            return early

        assert self.PROMPT, f"{type(self).__name__}.PROMPT must be a non-empty string"

        context = self.build_context(input_data)
        prompt = self.PROMPT + "\n\n---\n\n" + context

        kwargs: Dict[str, Any] = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.think is not None:
            kwargs["think"] = self.think

        data = complete_json_with_continuation(self._model, prompt, **kwargs)
        return self.build_output(input_data, data)
```

- [ ] **Step 2: Run the new tests**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_devops_agent_template.py -v
```

Expected: all five tests PASS.

- [ ] **Step 3: Commit the implementation**

```bash
git add backend/agents/software_engineering_team/devops_team/_agent_template.py
git commit -m "$(cat <<'EOF'
Add DevOpsSingleShotAgent base for single-shot JSON agents.

Captures the shared init/run scaffolding with pre_call and build_output
hooks so later migrations can drop duplicated agent skeletons.
EOF
)"
```

---

### Task 3: Lint and coverage verification

**Files:**
- Verify only (no intentional edits unless lint/coverage forces a fix):
  - `backend/agents/software_engineering_team/devops_team/_agent_template.py`
  - `backend/agents/software_engineering_team/tests/test_devops_agent_template.py`

**Interfaces:**
- Consumes: Task 1–2 deliverables
- Produces: confirmed lint-clean files and ≥90% line coverage on `_agent_template.py`

- [ ] **Step 1: Run ruff on the new files**

```bash
cd backend && python -m ruff check agents/software_engineering_team/devops_team/_agent_template.py agents/software_engineering_team/tests/test_devops_agent_template.py && python -m ruff format --check agents/software_engineering_team/devops_team/_agent_template.py agents/software_engineering_team/tests/test_devops_agent_template.py
```

Expected: exit 0. If format fails, run `python -m ruff format` on those two paths and re-check.

- [ ] **Step 2: Measure coverage on the new module**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_devops_agent_template.py -v \
  --cov=software_engineering_team.devops_team._agent_template \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: PASS with line coverage ≥90%. If `NotImplementedError` branches on `build_context` / `build_output` are reported missing, add this test to `test_devops_agent_template.py` and re-run:

```python
def test_unimplemented_template_methods_raise() -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    agent = DevOpsSingleShotAgent(_strands_model_double())
    with pytest.raises(NotImplementedError, match="build_context"):
        agent.build_context("x")
    with pytest.raises(NotImplementedError, match="build_output"):
        agent.build_output("x", {})
```

Also, if the empty-`PROMPT` assert is uncovered, add:

```python
def test_empty_prompt_raises(monkeypatch) -> None:
    from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent

    monkeypatch.setattr(
        "software_engineering_team.devops_team._agent_template.complete_json_with_continuation",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not call")),
    )

    class Agent(DevOpsSingleShotAgent):
        PROMPT = ""

        def build_context(self, input_data: str) -> str:
            return input_data

        def build_output(self, input_data: str, data: dict) -> _FakeOut:
            return _FakeOut(summary="")

    with pytest.raises(AssertionError, match="PROMPT must be a non-empty string"):
        Agent(_strands_model_double()).run("x")
```

- [ ] **Step 3: Confirm seven agent files are untouched**

```bash
git status --short backend/agents/software_engineering_team/devops_team/
```

Expected: only `_agent_template.py` (and possibly `__pycache__`) as new under `devops_team/`; no modifications to `iac_agent/`, `cicd_pipeline_agent/`, `deployment_strategy_agent/`, `doc_runbook_agent/`, `infra_patch_agent/`, `infra_debug_agent/`, or `devsecops_review_agent/`.

- [ ] **Step 4: Commit any coverage/lint fixes** (skip if working tree clean for those files)

```bash
git add backend/agents/software_engineering_team/devops_team/_agent_template.py \
  backend/agents/software_engineering_team/tests/test_devops_agent_template.py
git commit -m "$(cat <<'EOF'
Cover remaining DevOpsSingleShotAgent edge paths.

Reach the empty-PROMPT and NotImplementedError branches so the new
module clears the 90% coverage floor.
EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| New `_agent_template.py` with decision-record docstring | Task 2 |
| Monkeypatch strategy documented (call `shared.llm` directly) | Task 2 module docstring |
| `DevOpsSingleShotAgent` with assert / `self.llm` / `resolve_strands_model(..., agent_key="devops", ...)` | Task 2 |
| `PROMPT` + `temperature` / `think` (incl. `None` = omit) | Task 2 + Task 1 omit-kwargs test |
| `pre_call` / `build_context` / `build_output` / `run` flow | Task 2 |
| Tests: boilerplate, pre-call, post-call | Task 1 |
| Tests: omit-kwargs, None client | Task 1 |
| No edits to the seven agents | Task 3 Step 3 |
| Lint + ≥90% coverage | Task 3 |

## Self-review notes

- No TBD/TODO placeholders in steps; full source is inlined.
- Types/names consistent: `DevOpsSingleShotAgent`, `pre_call`, `build_context`, `build_output`, `PROMPT`, `temperature`, `think`.
- Spec's "parameterized by name" maps to subclass class names at migration time (out of scope here); public names are not a constructor parameter on the base.
