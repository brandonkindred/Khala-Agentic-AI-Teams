---
name: Fix log warnings and exceptions
overview: "Fix the root causes of all warnings and exceptions seen in the software engineering team run: (1) ValidationError when LLM returns non-list/dict for PlanningNode fields; (2) noisy/inappropriate log levels for by-design behaviors; (3) graph cycles causing topological order warnings; (4) optional reduction of JSON-parse-failure log noise where callers have fallbacks."
todos: []
isProject: false
---

# Fix Warnings and Exceptions in Software Engineering Team Logs

## Issues identified from the terminal logs (lines 7–739)


| Log / exception                                                                                 | Root cause                                                                                                                                                                                                                                                               |
| ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **ValidationError: PlanningNode `inputs` value is not a valid list** (lines 722–739)            | LLM returned a node with `inputs` (or `outputs` / `acceptance_criteria`) as a non-list (e.g. string, null, or single value). Code passes `n.get("inputs", [])` etc. directly to Pydantic; `.get("inputs", [])` can still be a string if the LLM sends `"inputs": "api"`. |
| **Could not parse structured JSON from LLM response; returning raw content wrapper** (multiple) | LLM sometimes returns non-JSON or malformed JSON. [shared/llm.py](software_engineering_team/shared/llm.py) returns `{"content": text}` and logs at WARNING. Architecture and Quality Gate agents already handle this with fallbacks; the warning is noisy.               |
| **Tasks and architecture not aligned (iteration N/5)**                                          | By design: orchestrator runs up to 5 alignment iterations. Logged at WARNING although this is expected behavior.                                                                                                                                                         |
| **Spec conformance failed (N issues); re-running planning with feedback**                       | By design: orchestrator retries with feedback. Logged at WARNING.                                                                                                                                                                                                        |
| **PlanningGraph: node X not in topological order (cycle or disconnected)**                      | BLOCKS edges form a cycle or a node is disconnected. [planning_graph.py](software_engineering_team/planning_team/planning_graph.py) `_topological_order` appends such nodes at the end and logs a warning.                                                               |
| **LLM connection error, retrying... Connection refused**                                        | Transient (Ollama unreachable). Retry logic already exists; no bug.                                                                                                                                                                                                      |
| **404 model not found / 400 max_tokens** (earlier in log)                                       | Already addressed in codebase (default model and max_tokens cap).                                                                                                                                                                                                        |


---

## 1. Fix ValidationError: normalize list/dict fields when building PlanningNode from LLM output

**Root cause:** In [frontend_planning_agent/agent.py](software_engineering_team/planning_team/frontend_planning_agent/agent.py) and [backend_planning_agent/agent.py](software_engineering_team/planning_team/backend_planning_agent/agent.py), `PlanningNode(...)` is called with `inputs=n.get("inputs", [])`, `outputs=n.get("outputs", [])`, `acceptance_criteria=n.get("acceptance_criteria", [])`. If the LLM returns e.g. `"inputs": "api"` or `"acceptance_criteria": "one item"`, Pydantic raises `ValidationError: value is not a valid list`.

**Approach:** Add a small shared helper that coerces LLM node fields to the types required by `PlanningNode`:

- **List fields** (`inputs`, `outputs`, `acceptance_criteria`, `quality_gates`): if already a list of strings, use it; if a single string, wrap in `[s]`; if `None`/missing, use `[]`; if another iterable, convert to list (filter or cast elements to str); otherwise `[]`.
- **Dict field** (`metadata`): if already a dict, use it; otherwise `{}`.

**Where to add:**

- Option A: Helper in [planning_team/planning_graph.py](software_engineering_team/planning_team/planning_graph.py), e.g. `_ensure_str_list(val: Any) -> List[str]` and `_ensure_dict(val: Any) -> Dict[str, Any]`, and use them in a single place that builds a `PlanningNode` from a raw dict (e.g. `planning_node_from_llm_node(n: Dict) -> PlanningNode`). Then have each agent call that instead of constructing `PlanningNode` manually.
- Option B: Keep parsing in each agent but in each `_parse_graph_from_llm_output` normalize before building `PlanningNode`: e.g. `inputs=_ensure_str_list(n.get("inputs"))`, etc.

Recommendation: **Option B** with helpers in `planning_graph.py` (e.g. `ensure_str_list`, `ensure_dict`) to avoid touching every agent’s node-building logic in a single central API. Each agent that builds `PlanningNode` from LLM dicts should use these helpers for every list/dict field passed to `PlanningNode`.

**Agents to update (same pattern: normalize before passing to PlanningNode):**

- [frontend_planning_agent/agent.py](software_engineering_team/planning_team/frontend_planning_agent/agent.py) (lines 39–49): `inputs`, `outputs`, `acceptance_criteria`; `metadata` already from `dict(n.get("metadata") or {})` — ensure it’s always a dict (e.g. if LLM returns a string, use `{}`).
- [backend_planning_agent/agent.py](software_engineering_team/planning_team/backend_planning_agent/agent.py) (lines 39–49): same.
- [data_planning_agent/agent.py](software_engineering_team/planning_team/data_planning_agent/agent.py) (line 37): `acceptance_criteria` only (no inputs/outputs).
- [test_planning_agent/agent.py](software_engineering_team/planning_team/test_planning_agent/agent.py) (lines 41–47): `acceptance_criteria`, `metadata`.
- [documentation_planning_agent/agent.py](software_engineering_team/planning_team/documentation_planning_agent/agent.py) (lines 30–38): `acceptance_criteria`, `metadata`.
- [performance_planning_agent/agent.py](software_engineering_team/planning_team/performance_planning_agent/agent.py) (lines 54–61): `acceptance_criteria` (no metadata passed; optional normalization for consistency).

**Helper behavior (concise):**

- `ensure_str_list(val)` → return `[]` if val is None; if isinstance(list), return `[str(x) for x in val]`; if isinstance(str), return `[val]`; if iterable, same as list; else `[]`.
- `ensure_dict(val)` → return `{}` if val is None or not isinstance(dict); else return a copy or the dict.

This prevents all `PlanningNode` validation errors from malformed LLM list/dict fields.

---

## 2. Downgrade by-design orchestrator messages to INFO

**Files:** [orchestrator.py](software_engineering_team/orchestrator.py)

- **Tasks and architecture not aligned** (around line 1537): Change `logger.warning(...)` to `logger.info(...)` so expected alignment iterations are not reported as warnings.
- **Spec conformance failed; re-running planning with feedback** (around line 1618): Change `logger.warning(...)` to `logger.info(...)` for the same reason.

---

## 3. PlanningGraph topological order (cycle or disconnected) warning

**Current behavior:** In [planning_team/planning_graph.py](software_engineering_team/planning_team/planning_graph.py), `_topological_order` uses Kahn’s algorithm. Nodes that are never reached (cycles or disconnected) are appended at the end and a warning is logged per node.

**Options:**

- **A. Cycle breaking when merging/building graph:** Before or after merging planning graphs, detect cycles in BLOCKS edges (e.g. reuse or add a small function in [planning_team/validation.py](software_engineering_team/planning_team/validation.py)) and remove one edge per cycle to make the graph acyclic. Then topological order will include all nodes without the warning.
- **B. Downgrade log level:** Change the “not in topological order” log to INFO or DEBUG so it doesn’t look like a failure, and document that order for those nodes is best-effort.
- **C. Prompt/guidance:** Tighten planning prompts so the LLM is instructed not to produce circular BLOCKS dependencies (optional addition to A or B).

Recommendation: Implement **A** (cycle detection + break one edge per cycle when computing order or when merging) and optionally **B** (downgrade to INFO) so runs complete with a valid order and fewer scary logs.

---

## 4. JSON parse failure warning in shared/llm.py (optional)

**Current:** When the LLM response is not parseable as JSON, [shared/llm.py](software_engineering_team/shared/llm.py) returns `{"content": text}` and logs a WARNING. Callers like Architecture and Quality Gate already handle the missing structure.

**Options:**

- **A. Add an optional parameter** to `complete_json`, e.g. `log_parse_failure_at_debug: bool = False`. When True, log the “Could not parse structured JSON…” at DEBUG instead of WARNING. Callers that have a fallback could pass True (requires threading the flag through from orchestrator/agents that use it).
- **B. Leave as-is** and only rely on (1) to reduce real failures; the warning stays as a signal that the model sometimes returns non-JSON.

Recommendation: **B** unless you explicitly want less noise, in which case **A** is a small, localized change.

---

## 5. Summary of file-level changes


| File                                                                                                                                                                                 | Change                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [planning_team/planning_graph.py](software_engineering_team/planning_team/planning_graph.py)                                                                                         | Add `ensure_str_list(val)` and `ensure_dict(val)`; use them or expose for use in parsers. Optionally: cycle-breaking in topological order and/or downgrade “not in topological order” to INFO. |
| [planning_team/frontend_planning_agent/agent.py](software_engineering_team/planning_team/frontend_planning_agent/agent.py)                                                           | When building `PlanningNode` from `n`, use helpers for `inputs`, `outputs`, `acceptance_criteria`, `metadata`.                                                                                 |
| [planning_team/backend_planning_agent/agent.py](software_engineering_team/planning_team/backend_planning_agent/agent.py)                                                             | Same as frontend.                                                                                                                                                                              |
| [planning_team/data_planning_agent/agent.py](software_engineering_team/planning_team/data_planning_agent/agent.py)                                                                   | Use helper for `acceptance_criteria`.                                                                                                                                                          |
| [planning_team/test_planning_agent/agent.py](software_engineering_team/planning_team/test_planning_agent/agent.py)                                                                   | Use helpers for `acceptance_criteria`, `metadata`.                                                                                                                                             |
| [planning_team/documentation_planning_agent/agent.py](software_engineering_team/planning_team/documentation_planning_agent/agent.py)                                                 | Use helpers for `acceptance_criteria`, `metadata`.                                                                                                                                             |
| [planning_team/performance_planning_agent/agent.py](software_engineering_team/planning_team/performance_planning_agent/agent.py)                                                     | Use helper for `acceptance_criteria`.                                                                                                                                                          |
| [orchestrator.py](software_engineering_team/orchestrator.py)                                                                                                                         | Change the two alignment/conformance “warning” logs to INFO.                                                                                                                                   |
| [planning_team/validation.py](software_engineering_team/planning_team/validation.py) or [planning_team/planning_graph.py](software_engineering_team/planning_team/planning_graph.py) | Add cycle detection and break one edge per cycle before/during topological sort (if implementing 3A).                                                                                          |


---

## 6. Testing

- Add or extend a test that builds a `PlanningNode` (or a graph) from a dict with `inputs`/`outputs`/`acceptance_criteria` as string, null, or single value, and assert no ValidationError and correct list/dict values.
- Run the software engineering team pipeline on a representative spec and confirm: no PlanningNode ValidationError, alignment/conformance logs at INFO, and (if implemented) no or fewer topological-order warnings when cycles are broken.

