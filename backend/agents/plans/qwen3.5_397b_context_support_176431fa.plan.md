---
name: qwen3.5 397b context support
overview: Add qwen3.5:397b and variants to the known model context table so the software engineering team uses the correct 256K context window when Ollama's /api/show does not return context info (common for remote/cloud models).
todos: []
isProject: false
---

# qwen3.5:397b context window support

## Problem

The qwen3.5:397b model (and qwen3.5:cloud) does not expose `num_ctx` or `context_length` in Ollama's `/api/show` response when used as a remote model. The current resolution order in `[shared/llm.py](software_engineering_team/shared/llm.py)` is:

1. `SW_LLM_CONTEXT_SIZE` env var
2. `KNOWN_MODEL_CONTEXT` table
3. Ollama `/api/show` (parse `num_ctx` or `context_length`)
4. **Fallback: 16384 tokens**

When step 3 fails or returns nothing, the fallback of 16K tokens is used. For a 397B model with a 256K context window, this severely underutilizes the model: context sizing in `[shared/context_sizing.py](software_engineering_team/shared/context_sizing.py)` would truncate code, specs, and architecture to fit 16K, wasting the model's capabilities.

## Model specs (from Ollama)

- **qwen3.5:cloud** – 256K context, text
- **qwen3.5:397b-cloud** – 256K context, text + image
- **qwen3.5:397b** – same 397B model; tag may vary by deployment

256K = 262144 tokens.

## Solution

Add qwen3.5 variants to `KNOWN_MODEL_CONTEXT` in `[software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)` with context size 262144. The known-model path is checked *before* the `/api/show` call, so remote models that do not expose context will still get the correct size.

## Changes

### 1. Update `KNOWN_MODEL_CONTEXT` in `[software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)`

Add entries for qwen3.5 models (lines 33–37):

```python
KNOWN_MODEL_CONTEXT: dict[str, int] = {
    "qwen3.5:397b": 262144,
    "qwen3.5:397b-cloud": 262144,
    "qwen3.5:cloud": 262144,
    "qwen3-coder-next": 262144,
    "qwen3-coder:480b-cloud": 262144,
    "qwen3-coder:480b": 262144,
}
```

### 2. Update README (optional)

In `[software_engineering_team/README.md](software_engineering_team/README.md)`, extend the `SW_LLM_CONTEXT_SIZE` description to mention qwen3.5:397b:

```
For qwen3-coder-next, qwen3.5:397b: 262144
```

## No other changes needed

- **Thinking / vision / tools**: The software engineering team uses text completion only (no images, no tool calls). The model's thinking and vision capabilities do not require code changes.
- **Embedding length**: Not used by the SE team.
- `**max_tokens**`: Already derived from `_fetch_model_num_ctx()`, so it will use 262144 when the known-model path is hit.

## User workaround (if model tag differs)

If the exact model tag (e.g. `qwen3.5:397b-something`) is not in the table, users can set:

```bash
export SW_LLM_CONTEXT_SIZE=262144
```

This takes precedence over both the known-model table and `/api/show`.