---
name: Standardize agent prompt logging
overview: Remove full/truncated agent prompt content from logs and replace with a single short, standardized one-line log that describes what is being done (agent, mode, task hint, prompt length). Optionally shorten LLM request logs in shared/llm.py for consistency.
todos: []
isProject: false
---

# Standardize agent prompt logging

## Current behavior

- **[prompt_utils.py](software_engineering_team/shared/prompt_utils.py)** `log_llm_prompt()`:
  - `mode=initial`: logs one line with agent, mode, task hint, length (no body) — already acceptable.
  - `mode=problem_solving`: logs the same metadata **plus** the prompt body (truncated to `max_chars`, e.g. 2000). That produces long, noisy lines (and in your run the logged message was 84k+ chars, so truncation may be applied but the line is still huge).
- **[llm.py](software_engineering_team/shared/llm.py)** logs `*** ACTIVE LLM MODEL (REQUEST) *** provider=..., model=..., base_url=...` on every `complete_json` call.

## Target behavior

- **No prompt body in logs** — never log the assembled prompt content (neither full nor truncated).
- **One standardized, short line** per LLM call that explains what is being done: agent, mode, task hint (truncated), and prompt length. Same format for both `initial` and `problem_solving`.
- **Optional**: Shorten the LLM-layer request log to a single succinct line so all LLM-related logs are consistent.

---

## 1. Change `log_llm_prompt` in [prompt_utils.py](software_engineering_team/shared/prompt_utils.py)

- **Single format for both modes** — e.g. one line only:
  - `"LLM call: agent=%s mode=%s task=%s prompt_len=%d"`
  - Example: `LLM call: agent=Backend mode=problem_solving task=Implement the core data models... prompt_len=43310`
- **Never include `prompt` (or any substring of it) in the log.** Remove all branches that pass `truncated` or `prompt_str` to `log.info(...)`.
- **Task hint**: keep truncation at 80 chars for the log line.
- **Simplify the function**: drop `max_chars` from the signature and from docstring (or keep the parameter for backward compatibility but ignore it). Callers can continue passing it; it will be unused.
- **Docstring**: update to state that only metadata is logged (agent, mode, task hint, prompt length); no prompt body is ever logged.

Result: one short INFO line per call, no prompt content.

---

## 2. Update tests that depend on prompt logging

- **[test_prompt_utils.py](software_engineering_team/tests/test_prompt_utils.py)**  
  - **test_log_llm_prompt_truncates_long_prompt**: today it expects the log message to contain the truncated prompt body and "truncated"/"total_length". Change to assert only metadata: e.g. "Problem-solving" (or the new wording), mode=problem_solving, and prompt_len=5000 (or equivalent). Remove any assertion that the prompt text appears in the log.
  - **test_log_llm_prompt_emits_info_record**: keep asserting INFO with agent, mode, length; adjust to the new message format if the wording changes (e.g. "LLM call" and "prompt_len").
  - **test_log_llm_prompt_initial_mode_omits_body**: still valid; optionally tighten to assert the log message does not contain a given body string.
  - **test_log_llm_prompt_handles_none_gracefully**: unchanged.
- **[test_backend_agent.py](software_engineering_team/tests/test_backend_agent.py)** (around line 152)  
  - **test_backend_agent_logs_llm_prompt**: currently asserts `"Backend LLM prompt"` and `"mode=initial"`. Update to the new standardized message (e.g. assert "Backend" and "mode=initial" and that a log line is emitted, or match the new "LLM call: agent=Backend ..." format).
- **[test_frontend_agent.py](software_engineering_team/tests/test_frontend_agent.py)** (around line 377)  
  - **test_frontend_agent_logs_llm_prompt**: same as backend — update assertions to the new format (e.g. "Frontend" and "mode=initial").

---

## 3. Optional: shorten LLM request log in [llm.py](software_engineering_team/shared/llm.py)

- In `OllamaLLMClient.complete_json` (around line 665), replace the current `logger.info("*** ACTIVE LLM MODEL (REQUEST) *** ...")` with a single succinct line, e.g.:
  - `"LLM request: provider=ollama model=%s base_url=%s"` (no asterisks, one line).
- Startup logs (lines 827, 832) can stay as-is or be aligned to the same style (e.g. "LLM config: ...") for consistency. This is optional and can be done in the same change or left for a follow-up.

---

## 4. Call sites (no code change required)

- [backend_agent/agent.py](software_engineering_team/backend_agent/agent.py) and [frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py) call `log_llm_prompt(logger, "Backend"|"Frontend", mode, task_hint, prompt, MAX_PROMPT_LOG_CHARS)`. Signature can remain; only the implementation of `log_llm_prompt` changes. You may remove the unused `MAX_PROMPT_LOG_CHARS` constant and pass a dummy or drop the argument if you remove `max_chars` from the function signature.

---

## Summary


| File                                                 | Action                                                                                                        |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `shared/prompt_utils.py`                             | Log only one short line (agent, mode, task, prompt_len); never log prompt body; single format for both modes. |
| `tests/test_prompt_utils.py`                         | Adjust tests for metadata-only logging (no body, no truncation marker in message).                            |
| `tests/test_backend_agent.py`                        | Update assertion to new log message format.                                                                   |
| `tests/test_frontend_agent.py`                       | Update assertion to new log message format.                                                                   |
| `shared/llm.py`                                      | (Optional) Shorten request log to one succinct line.                                                          |
| `backend_agent/agent.py` / `frontend_agent/agent.py` | Optional: stop passing `MAX_PROMPT_LOG_CHARS` if `max_chars` is removed.                                      |


No new dependencies; changes are limited to logging and tests.