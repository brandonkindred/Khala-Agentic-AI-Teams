---
name: standardize-agent-prompt-logging
overview: Remove full/truncated agent prompt content from logs and replace with a single short, standardized one-line log that describes what is being done (agent, mode, task hint, prompt length). Optionally shorten LLM request logs in shared/llm.py for consistency.
todos:
  - id: prompt-utils-single-format
    content: In prompt_utils.log_llm_prompt, use one log format for both initial and problem_solving (e.g. "LLM call: agent=%s mode=%s task=%s prompt_len=%d"); never pass prompt body to log.info.
    status: pending
  - id: prompt-utils-remove-body-branches
    content: Remove all branches in log_llm_prompt that include truncated or prompt_str in the log message; keep only metadata (agent, mode, hint, total_len).
    status: pending
  - id: prompt-utils-docstring
    content: Update log_llm_prompt docstring to state that only metadata is logged (agent, mode, task hint, prompt length) and that no prompt body is ever logged.
    status: pending
  - id: prompt-utils-max-chars
    content: Either remove max_chars parameter from log_llm_prompt and update callers, or keep parameter for backward compatibility but stop using it in the implementation.
    status: pending
  - id: prompt-utils-default-constant
    content: Remove or repurpose DEFAULT_MAX_PROMPT_LOG_CHARS in prompt_utils.py if max_chars is no longer used; update module docstring if needed.
    status: pending
  - id: test-prompt-utils-truncates
    content: In test_prompt_utils.py, rewrite test_log_llm_prompt_truncates_long_prompt to assert only metadata (mode=problem_solving, prompt_len or length) and that no prompt body appears in the log message.
    status: pending
  - id: test-prompt-utils-emits-info
    content: In test_prompt_utils.py, update test_log_llm_prompt_emits_info_record to assert the new message format (e.g. "LLM call", agent, mode, prompt_len) if wording changed.
    status: pending
  - id: test-prompt-utils-initial-omits-body
    content: In test_prompt_utils.py, keep test_log_llm_prompt_initial_mode_omits_body; optionally tighten to assert the log message does not contain a given body string.
    status: pending
  - id: test-prompt-utils-handles-none
    content: In test_prompt_utils.py, ensure test_log_llm_prompt_handles_none_gracefully still passes with the new implementation (no prompt body logged).
    status: pending
  - id: test-backend-logs-llm-prompt
    content: In test_backend_agent.py, update test_backend_agent_logs_llm_prompt to assert the new standardized log format (e.g. "Backend", "mode=initial", and that one INFO line is emitted).
    status: pending
  - id: test-frontend-logs-llm-prompt
    content: In test_frontend_agent.py, update test_frontend_agent_logs_llm_prompt to assert the new standardized log format (e.g. "Frontend", "mode=initial", and that one INFO line is emitted).
    status: pending
  - id: backend-agent-call-site
    content: In backend_agent/agent.py, remove MAX_PROMPT_LOG_CHARS and simplify log_llm_prompt call to omit max_chars if the parameter was removed; otherwise leave call as-is.
    status: pending
  - id: frontend-agent-call-site
    content: In frontend_agent/agent.py, remove MAX_PROMPT_LOG_CHARS and simplify log_llm_prompt call to omit max_chars if the parameter was removed; otherwise leave call as-is.
    status: pending
  - id: llm-request-log-shorten
    content: In shared/llm.py OllamaLLMClient.complete_json, replace the current ACTIVE LLM MODEL (REQUEST) log with a single succinct line (e.g. "LLM request: provider=ollama model=%s base_url=%s").
    status: pending
  - id: llm-startup-logs-optional
    content: Optionally align startup logs in get_llm_client (lines ~827, 832) to the same succinct style (e.g. "LLM config: ...") for consistency.
    status: pending
  - id: run-tests-prompt-utils
    content: Run tests for prompt_utils (tests/test_prompt_utils.py) and fix any remaining failures.
    status: pending
  - id: run-tests-backend-frontend
    content: Run tests for backend and frontend agents (test_backend_agent.py, test_frontend_agent.py) and fix any logging-related assertion failures.
    status: pending
  - id: docs-readme-logging
    content: Update software_engineering_team/README.md or any observability docs that reference "mode=problem_solving" or prompt body logging to describe the new metadata-only log format.
    status: pending
isProject: false
---

# Standardize agent prompt logging

## Current behavior

- **[prompt_utils.py](software_engineering_team/shared/prompt_utils.py)** `log_llm_prompt()`:
  - `mode=initial`: logs one line with agent, mode, task hint, length (no body) — already acceptable.
  - `mode=problem_solving`: logs the same metadata **plus** the prompt body (truncated to `max_chars`, e.g. 2000). That produces long, noisy lines.
- **[llm.py](software_engineering_team/shared/llm.py)** logs `*** ACTIVE LLM MODEL (REQUEST) *** provider=..., model=..., base_url=...` on every `complete_json` call.

## Target behavior

- **No prompt body in logs** — never log the assembled prompt content (neither full nor truncated).
- **One standardized, short line** per LLM call: agent, mode, task hint (truncated), prompt length. Same format for both `initial` and `problem_solving`.
- **Optional**: Shorten the LLM-layer request log to a single succinct line.

## 1. Change `log_llm_prompt` in prompt_utils.py

- Single format for both modes, e.g. `"LLM call: agent=%s mode=%s task=%s prompt_len=%d"`.
- Never include prompt (or any substring) in the log.
- Task hint: keep truncation at 80 chars.
- Simplify: drop or ignore `max_chars`; update docstring.

## 2. Update tests

- **test_prompt_utils.py**: Adjust tests for metadata-only logging (no body, no truncation marker in message).
- **test_backend_agent.py** / **test_frontend_agent.py**: Update assertions to the new log message format.

## 3. Optional: shorten LLM request log in llm.py

- Replace `*** ACTIVE LLM MODEL (REQUEST) ***` with one succinct line, e.g. `"LLM request: provider=ollama model=%s base_url=%s"`.
- Startup logs can stay as-is or be aligned to same style.

## 4. Call sites

- backend_agent/agent.py and frontend_agent/agent.py: simplify `log_llm_prompt` call if `max_chars` is removed; optionally remove `MAX_PROMPT_LOG_CHARS` constant.

## Summary

| File | Action |
|------|--------|
| shared/prompt_utils.py | Log only one short line (agent, mode, task, prompt_len); never log prompt body; single format for both modes. |
| tests/test_prompt_utils.py | Adjust tests for metadata-only logging. |
| tests/test_backend_agent.py | Update assertion to new log format. |
| tests/test_frontend_agent.py | Update assertion to new log format. |
| shared/llm.py | (Optional) Shorten request log to one succinct line. |
| backend_agent/agent.py, frontend_agent/agent.py | Optional: stop passing MAX_PROMPT_LOG_CHARS if max_chars removed. |
