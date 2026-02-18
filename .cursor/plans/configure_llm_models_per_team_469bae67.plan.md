---
name: Configure LLM models per team
overview: Set the blogging team to use deepseek-r1 and the software engineering team to use qwen2.5-coder, both via local Ollama. Blogging already uses deepseek-r1; software engineering needs its default changed from deepseek-r1 to qwen2.5-coder.
todos: []
isProject: false
---

# Configure LLM models per team (Ollama)

## Current state

**Blogging team:** Already uses `deepseek-r1` in all entry points (API, CLI scripts). No changes needed.

**Software engineering team:** Uses `deepseek-r1` as the default model. Needs to switch to `qwen2.5-coder`.

---

## Changes required (software engineering team only)

### 1. Update `get_llm_client()` default model

**File:** [software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)

**Location:** Line 536, inside `get_llm_client()`.

**Current:**

```python
model = os.environ.get(ENV_LLM_MODEL) or "deepseek-r1"
```

**Change to:**

```python
model = os.environ.get(ENV_LLM_MODEL) or "qwen2.5-coder"
```

This affects the orchestrator (run via API) when `SW_LLM_PROVIDER=ollama` and `SW_LLM_MODEL` is unset.

---

### 2. Update `run_team.py` hardcoded model

**File:** [software_engineering_team/agent_implementations/run_team.py](software_engineering_team/agent_implementations/run_team.py)

**Location:** Line 37.

**Current:**

```python
LLM = DummyLLMClient() if USE_DUMMY else OllamaLLMClient(model="deepseek-r1", timeout=1800.0)
```

**Change to:**

```python
LLM = DummyLLMClient() if USE_DUMMY else OllamaLLMClient(model="qwen2.5-coder", timeout=1800.0)
```

This affects the CLI script when `USE_DUMMY=False`.

---

### 3. Update README documentation

**File:** [software_engineering_team/README.md](software_engineering_team/README.md)

**Location:** LLM configuration table and example (around lines 59-75).

**Changes:**

- In the table: change default for `SW_LLM_MODEL` from `deepseek-r1` to `qwen2.5-coder`.
- In the example: change `export SW_LLM_MODEL=deepseek-r1` to `export SW_LLM_MODEL=qwen2.5-coder` (or remove the export so the default is used).

---

## Blogging team (no code changes)

The blogging team already uses `deepseek-r1` in:

- [api/main.py](api/main.py) (root): `OllamaLLMClient(model="deepseek-r1", timeout=1800.0)`
- [blogging/api/main.py](blogging/api/main.py): same
- [blogging/agent_implementations/blog_writing_process.py](blogging/agent_implementations/blog_writing_process.py): `model="deepseek-r1"`
- [blogging/agent_implementations/run_research_agent.py](blogging/agent_implementations/run_research_agent.py): `model="deepseek-r1"`
- [blogging/agent_implementations/run_review_agent_with_context.py](blogging/agent_implementations/run_review_agent_with_context.py): `model="deepseek-r1"`
- [blogging/agent_implementations/run_draft_agent.py](blogging/agent_implementations/run_draft_agent.py): `model="deepseek-r1"`
- [blogging/agent_implementations/run_copy_editor_agent.py](blogging/agent_implementations/run_copy_editor_agent.py): `model="deepseek-r1"`
- [blogging/agent_implementations/run_publication_agent.py](blogging/agent_implementations/run_publication_agent.py): `model="deepseek-r1"`

No changes are required for the blogging team.

---

## Summary


| Team                 | Model         | Action                                                       |
| -------------------- | ------------- | ------------------------------------------------------------ |
| Blogging             | deepseek-r1   | Already configured; no changes                               |
| Software engineering | qwen2.5-coder | Update default in `shared/llm.py`, `run_team.py`, and README |


**Prerequisite:** Ensure `qwen2.5-coder` is installed in Ollama (e.g. `ollama pull qwen2.5-coder`).