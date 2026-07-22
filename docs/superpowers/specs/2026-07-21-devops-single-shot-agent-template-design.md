# DevOps single-shot agent template

**Date:** 2026-07-21  
**Status:** Approved for implementation planning

## Goal

Add a shared base class that captures the byte-identical scaffolding shared by the seven `devops_team` single-shot JSON agents, without migrating any of those agents yet. Consumer migrations are tracked as separate follow-up work.

## Motivation

These seven agent files hand-roll the same skeleton:

1. `assert llm_client is not None`
2. `self.llm = llm_client`
3. `resolve_strands_model(llm_client, agent_key="devops", get_strands_model_fn=get_strands_model)`
4. `run()` builds a context string, calls `complete_json_with_continuation(self._model, PROMPT + "\n\n---\n\n" + context, ...)`, and constructs an output model from the returned dict

Four of the seven add genuine special-case logic beyond name/prompt/output-model:

| Agent | Special case |
|---|---|
| `infra_patch_agent` | Early return before any LLM call when errors are not fixable; also filters empty patched artifacts |
| `infra_debug_agent` | Derives `fixable` from a loop over returned errors |
| `devsecops_review_agent` | `derive_approved()` distinguishing absent vs null `"approved"`; uses `temperature=0.0` |
| `doc_runbook_agent` | Builds a second, non-LLM `DevOpsCompletionPackage` from input; omits `temperature`/`think` kwargs |

## Decisions (locked)

| Decision | Choice |
|---|---|
| Shape | Base class with template-method overrides |
| Canonical LLM helper | `complete_json_with_continuation` |
| Monkeypatch strategy | Import helper into `_agent_template`; migrations patch `_agent_template.complete_json_with_continuation` |
| Special-case hooks | `pre_call` (optional early return) + `build_output` (all post-call logic) |
| Generics / ABC ceremony | No `Generic[InputT, OutputT]`; plain class with methods that raise `NotImplementedError` if not overridden |
| Scope this change | New module + unit tests only; zero edits to the seven agent files |

## Canonical helper decision record

`complete_json_with_continuation` is the canonical helper for `devops_team`'s single-shot JSON agents.

`run_structured_persona` (`shared/persona_agent_base.py`) remains the pattern for the four agents already using it (`security_agent`, `qa_agent`, `accessibility_agent`, `integration_team`). Switching devops onto `run_structured_persona` was considered and deferred: that helper centralizes dataclass construction via Strands `structured_output_model` and requires a `fallback_factory` per agent, and several devops outputs carry nested models (`DevOpsCompletionPackage`, `IaCExecutionError`, `ReviewFinding`) that would need verification before a switch. The devops standardization effort only asks to standardize on one helper, not to migrate away from `complete_json_with_continuation`.

This decision record must appear in the new module's docstring so future readers do not re-litigate the helper choice.

## Monkeypatchability

The base imports and invokes `complete_json_with_continuation` from `software_engineering_team.shared.llm` directly (no per-subclass-module lookup).

**Implication for migration work:** any test that monkeypatches `…devops_team.<agent>.agent.complete_json_with_continuation` must be retargeted to `software_engineering_team.devops_team._agent_template.complete_json_with_continuation` (the name bound by the template's direct import), or continue patching `shared.llm.Agent` via `_patch_fenced_response`.

This design documents that choice; it does not change existing tests.

## Architecture

### New file

`backend/agents/software_engineering_team/devops_team/_agent_template.py`

Not re-exported from `devops_team/__init__.py` in this change. Migrations import the base directly:

```python
from software_engineering_team.devops_team._agent_template import DevOpsSingleShotAgent
```

### Class: `DevOpsSingleShotAgent`

**Class attributes (subclass-overridable):**

| Attr | Default | Notes |
|---|---|---|
| `PROMPT` | `""` | Required non-empty at `run` time |
| `temperature` | `0.1` | `None` → omit kwarg (doc_runbook path; helper default `0.0` applies) |
| `think` | `True` | `None` → omit kwarg |

**`__init__(self, llm_client)`** — identical to today's agents:

```text
assert llm_client is not None, "llm_client is required"
self.llm = llm_client
self._model = resolve_strands_model(
    llm_client, agent_key="devops", get_strands_model_fn=get_strands_model
)
```

**Template methods:**

| Method | Required? | Contract |
|---|---|---|
| `build_context(self, input_data) -> str` | Yes | Build the context string appended after the prompt separator |
| `build_output(self, input_data, data: dict) -> Any` | Yes | Construct the agent output from the parsed JSON dict; owns all post-call special cases |
| `pre_call(self, input_data) -> Any \| None` | No (default `None`) | If non-`None`, return immediately without calling the LLM |

**`run(self, input_data)` flow:**

1. `early = self.pre_call(input_data)`; if `early is not None`, return `early`
2. Assert `PROMPT` is non-empty
3. `context = self.build_context(input_data)`
4. Build invocation kwargs: include `temperature` / `think` only when the class attr is not `None`
5. `data = complete_json_with_continuation(self._model, self.PROMPT + "\n\n---\n\n" + context, **kwargs)`
6. `return self.build_output(input_data, data)`

No new try/except: LLM and parse errors propagate from `complete_json_with_continuation` unchanged.

### How special cases map onto the contract

| Agent (future migration) | How it uses the base |
|---|---|
| `iac_agent`, `cicd_pipeline_agent`, `deployment_strategy_agent` | Pure `build_context` + `build_output`; default `temperature`/`think` |
| `infra_patch_agent` | `pre_call` returns early when not fixable; `build_output` filters empty patched artifacts |
| `infra_debug_agent` | `build_output` builds `IaCExecutionError` list and derives `fixable` |
| `devsecops_review_agent` | `temperature = 0.0`; `build_output` calls `derive_approved` with absent-vs-null `approved` handling |
| `doc_runbook_agent` | `temperature = None`, `think = None`; `build_output` builds `DevOpsCompletionPackage` from input fields plus LLM `files`/`summary` |

## Testing

**New file:** `backend/agents/software_engineering_team/tests/test_devops_agent_template.py`

Required cases:

1. **Pure boilerplate** — fake subclass with `PROMPT`, `build_context`, `build_output`; monkeypatch `complete_json_with_continuation` where the template imports it; assert prompt shape (`PROMPT + "\n\n---\n\n" + context`), default kwargs (`temperature=0.1`, `think=True`), and returned output.
2. **Pre-call hook** — `pre_call` returns a sentinel output; assert the LLM helper is never called and the sentinel is returned.
3. **Post-call hook** — `build_output` derives a field from `data`; assert the derived value appears on the result.

Additional coverage for the omit-kwargs path and the `llm_client is None` precondition:

4. `temperature=None` / `think=None` → those kwargs absent from the helper call
5. `llm_client is None` → assertion failure

**Coverage floor:** ≥90% line coverage on `_agent_template.py`.

**Unchanged:** existing `test_devops_team.py` / `test_devops_debug_patch.py` and all seven agent modules.

## Out of scope

- Migrating any of the seven agent files onto the base
- Changing `complete_json_with_continuation`'s signature or behavior
- Migrating agents outside `devops_team`
- Deprecating or removing `run_structured_persona`

## Acceptance criteria mapping

| Criterion | How this design satisfies it |
|---|---|
| Shared definition parameterized by name/prompt/output/context/hooks | Base class + `PROMPT` + template methods; public agent names stay on subclasses at migration time |
| Monkeypatch strategy stated | Documented: patch `_agent_template.complete_json_with_continuation` (or `shared.llm.Agent`) |
| Unit tests: boilerplate, pre-call, post-call | `test_devops_agent_template.py` cases 1–3 |
| No changes to the seven agents | Explicit out of scope |
| `make test` / `make lint`; 90% on new file | Implementation plan must verify |
