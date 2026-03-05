---
name: LLM Context Configuration Update
overview: Update KNOWN_MODEL_CONTEXT with correct max context sizes per model, replace qwen3.5:cloud with qwen3.5:397b-cloud in AGENT_DEFAULT_MODELS, and configure effective context as max minus the largest agent's token usage per model.
todos:
  - id: update-known-model-context
    content: Update KNOWN_MODEL_CONTEXT in shared/llm.py with correct max values and effective context (max - largest agent)
    status: completed
  - id: replace-qwen35-cloud
    content: Replace qwen3.5:cloud with qwen3.5:397b-cloud in AGENT_DEFAULT_MODELS for all 11 planning/documentation agents
    status: completed
  - id: update-readme
    content: Update README.md model table and SW_LLM_CONTEXT_SIZE description with new context values and qwen3.5 note
    status: completed
isProject: false
---

# LLM Context Configuration Update

## Summary of Changes

1. **Correct model context sizes** in `KNOWN_MODEL_CONTEXT`:
  - minimax-m2.5:cloud: 198K (198,000)
  - qwen3.5:cloud: 198K (198,000)
  - qwen3-coder-next:cloud: 256K (262,144)
  - qwen3.5:397b-cloud: 256K (262,144) - replaces qwen3.5:cloud
2. **Replace qwen3.5:cloud with qwen3.5:397b-cloud** in `AGENT_DEFAULT_MODELS` for all agents that currently use qwen3.5:cloud (api_contract, data_architecture, ui_ux, frontend_architecture, infrastructure, devops_planning, qa_test_strategy, security_planning, observability, acceptance_verifier, documentation).
3. **Configure effective context = max - largest agent tokens** per model. The stored value in `KNOWN_MODEL_CONTEXT` will be the effective context (max minus largest agent's prompt+response reservation).

## Largest Agent Analysis (from [context_sizing.py](software_engineering_team/shared/context_sizing.py))


| Model                  | Agents                                                              | Largest Agent              | Largest Reservation                  |
| ---------------------- | ------------------------------------------------------------------- | -------------------------- | ------------------------------------ |
| qwen3.5:cloud            | tech_lead, architecture, spec_intake, project_planning, integration | tech_lead (Task Generator) | 110K prompt + 8K response = **118K** |
| minimax-m2.5:cloud     | qa, security, accessibility                                         | any                        | ~12K prompt + 8K response = **20K**  |
| qwen3-coder-next:cloud | backend, frontend, code_review, repair, devops, dbc_comments        | backend/frontend           | 12K prompt + 8K response = **20K**   |
| qwen3.5:397b-cloud     | api_contract, data_architecture, ui_ux, etc.                        | documentation              | 12K prompt + 8K response = **20K**   |


## Effective Context Values


| Model                  | Max Context | Largest Agent | Configured (max - largest) |
| ---------------------- | ----------- | ------------- | -------------------------- |
| qwen3.5:cloud            | 198,000     | 118,000       | **80,000**                 |
| minimax-m2.5:cloud     | 198,000     | 20,000        | **178,000**                |
| qwen3-coder-next:cloud | 262,144     | 20,000        | **242,144**                |
| qwen3.5:397b-cloud     | 262,144     | 20,000        | **242,144**                |


**Important:** For qwen3.5:cloud, the tech_lead (Task Generator) reserves 110K tokens. With configured context 80K, `compute_task_generator_spec_chars` would compute `available = 80K - 110K - 8K` = negative, falling back to 512 tokens and 12K chars. This may severely limit tech_lead for large specs. If this causes issues, users can override with `SW_LLM_CONTEXT_SIZE=198000` for qwen3.5.

## Files to Modify

### 1. [software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)

**KNOWN_MODEL_CONTEXT** - Update with correct max values and effective context (max - largest agent):

```python
# Model max context (tokens). Effective context = max - largest agent reservation.
# 198K = 198000 (minimax, qwen3.5). 256K = 262144 (qwen models).
# Largest reservations: tech_lead 118K (qwen3.5), coding/review 20K (qwen3-coder, qwen3.5).
KNOWN_MODEL_CONTEXT: dict[str, int] = {
    "qwen3.5:397b": 262144,
    "qwen3.5:397b-cloud": 242144,   # 256K - 20K
    "qwen3.5:cloud": 242144,       # keep for overrides; 256K - 20K
    "qwen3-coder-next": 242144,
    "qwen3-coder-next:cloud": 242144,  # 256K - 20K
    "qwen3-coder:480b-cloud": 242144,
    "qwen3-coder:480b": 242144,
    "qwen3.5:cloud": 80_000,         # 198K - 118K (tech_lead)
    "minimax-m2.5:cloud": 178_000, # 198K - 20K
}
```

**AGENT_DEFAULT_MODELS** - Replace qwen3.5:cloud with qwen3.5:397b-cloud:

```python
"api_contract": "qwen3.5:397b-cloud",
"data_architecture": "qwen3.5:397b-cloud",
"ui_ux": "qwen3.5:397b-cloud",
"frontend_architecture": "qwen3.5:397b-cloud",
"infrastructure": "qwen3.5:397b-cloud",
"devops_planning": "qwen3.5:397b-cloud",
"qa_test_strategy": "qwen3.5:397b-cloud",
"security_planning": "qwen3.5:397b-cloud",
"observability": "qwen3.5:397b-cloud",
"acceptance_verifier": "qwen3.5:397b-cloud",
"documentation": "qwen3.5:397b-cloud",
```

### 2. [software_engineering_team/README.md](software_engineering_team/README.md)

Update the model table and SW_LLM_CONTEXT_SIZE description to reflect:

- minimax-m2.5:cloud, qwen3.5:cloud: 198K max
- qwen3-coder-next:cloud, qwen3.5:397b-cloud: 256K max
- Effective context = max minus largest agent reservation
- Note on qwen3.5 tech_lead: if planning fails on large specs, set `SW_LLM_CONTEXT_SIZE=198000`

## Alternative: Store Max, Subtract in context_sizing

If the "minus largest" approach breaks tech_lead, an alternative is to store the **true max** in KNOWN_MODEL_CONTEXT and have `context_sizing.py` apply the subtraction per-agent. That would require passing agent_key into the sizing functions. The current design uses a single context value per model. Implementing the user's explicit request first; we can revert if needed.