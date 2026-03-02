# Agent Recovery Failures - Post-Mortem Log

This file documents failures where all recovery strategies (continuation and decomposition) were exhausted.

---

## Failure: 2026-03-02 09:24:59 - SystemDesign

### Task Description

Planning V2 tool agent - SystemDesign

### What Went Wrong

- **Continuation attempts**: 0/5 cycles exhausted
- **Decomposition depth**: 0/20 levels reached
- **Final error**: `Could not parse structured JSON from LLM response. Model returned invalid or non-JSON output. Response preview: ''...`

### Original Prompt (truncated)

```
You are a System Design expert. Review these planning artifacts for design coherence:

Artifacts:
---
--- plan/updated_spec.md ---
# Technical Spec: Todo Application (OAuth Auth)

## 1. Goal and non-goals

### Goal

Build a web-based todo app where users can:

* View a list of tasks
* Create new tasks
* Toggle task status between complete and not complete
* Store tasks persistently in a backend
* Login using OAuth (e.g., Google, GitHub, or other providers)
* Each user's tasks are isolated from o...
```

### Partial Responses

**Response 1/1** (0 chars):
```

```


### Suggested Fixes

- **Review token limits**: Check `SW_LLM_MAX_TOKENS` and model context size.
- **Reduce prompt complexity**: Simplify the original prompt or provide more focused instructions.
- **Check for infinite loops**: Ensure the LLM isn't generating repetitive content that never terminates naturally.

---

