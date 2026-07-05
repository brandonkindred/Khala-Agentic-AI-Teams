# shared_llm_recovery

Neutral, team-agnostic **recovery parsers** for imperfect LLM output.

Models sometimes ignore a structured-output request and return prose-wrapped
JSON, `<think>` blocks, markdown-fenced code, or a `{"content": "..."}` wrapper.
These helpers salvage a usable object/files dict instead of failing. Shared so any
team gets the same resilience.

## Layout

| Module | Was | Responsibility |
|---|---|---|
| `recovery` | `software_engineering_team/shared/llm_response_utils.py` | `extract_json_object`, `extract_task_assignment_from_content`, `extract_files_from_content`, `heuristic_extract_files_from_content`, `extract_single_python_block`. |

## Usage

```python
import json
from shared_llm_recovery import extract_json_object

try:
    data = json.loads(raw)
except json.JSONDecodeError:
    data = extract_json_object(raw)  # brace-scan salvage; None if nothing parses
```

## Contracts & conventions

- Import-safe and never raises on malformed input (returns `None` / `{}`
  instead) — including `RecursionError` from pathologically deep nesting.
- Both public object extractors share one salvage engine (`_salvage_object`):
  a **string-aware, linear-time** balanced-object scan (braces inside JSON
  string values don't corrupt the scan), strict `json.loads` first, then
  tolerant repair via the `json-repair` dependency (trailing commas,
  max-tokens-truncated output).
- **Selection rule:** strict-parsed outranks repaired, non-empty outranks
  empty, ties break toward the **last** candidate in document order — models
  routinely echo a format example before the final object, so the trailing
  object is authoritative. `extract_task_assignment_from_content` additionally
  requires a non-empty `tasks` list.
- **Repair gates:** two independent knobs control the tolerant `json-repair` legs.
  `extract_json_object(..., repair=False)` disables ALL repair, so only strictly
  valid JSON is recovered and anything malformed/truncated yields `None` (the
  strategy-lab spec agents use this so a bad payload re-prompts the model).
  `extract_json_object(..., repair_truncated=False)` (with `repair=True`) keeps
  repairing complete-but-broken objects (trailing commas, unescaped inner quotes)
  but lets a genuinely truncated reply yield `None`, so the caller recovers the
  tail via continuation instead of accepting a fabricated close (the Ollama client
  uses this — the "don't fabricate a truncated tail" policy lives in the engine,
  which knows the real payload boundaries, not in a caller-side heuristic).
- Depends on `backend/agents` being on `sys.path` (the `shared_*` convention).
