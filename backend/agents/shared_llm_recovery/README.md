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

- **Stdlib-only**, import-safe, and never raises on malformed input (returns
  `None` / `{}` instead).
- `extract_json_object` returns the first *balanced* `{...}` that parses to a
  `dict`; `extract_task_assignment_from_content` additionally requires a non-empty
  `tasks` list.
- Depends on `backend/agents` being on `sys.path` (the `shared_*` convention).
