---
name: Resolve run log warnings
overview: Fix all recurring warnings from orchestrator/agent runs (JSON parse failure, empty completion, request body too large, write failed after code review) with exclusions, caps, extraction improvements, and tests.
todos:
  - id: 09f6f171-a597-48a5-9486-847c634ff954
    content: ""
    status: pending
  - id: 40790b69-8bc5-4930-83c2-33c2a407fd92
    content: ""
    status: pending
  - id: b7fbb557-8ebb-4c6e-adf4-4a01986b9e9e
    content: ""
    status: pending
  - id: 1c0e8a3f-7218-43bd-ae7a-6167ba86905c
    content: ""
    status: pending
  - id: ff8dd2f4-0ad4-4f2f-a133-83be26fd562b
    content: ""
    status: pending
  - id: 250d1eea-9a64-4212-8229-05f7c978faf7
    content: ""
    status: pending
  - id: 8ca4b42b-609e-4650-81b9-7eb808a8c7e5
    content: ""
    status: pending
  - id: 6cdd92c6-1d00-414d-a5c9-ebcfaa9094c0
    content: ""
    status: pending
  - id: 6147b05e-2c02-4c35-98f9-8ddc0b2af051
    content: ""
    status: pending
  - id: 372e8652-685c-420d-bb8c-b8211cc39cc1
    content: ""
    status: pending
  - id: 29caa634-1ef4-4e03-9372-d16a9a281c85
    content: ""
    status: pending
  - id: 4b0e7971-18e7-42ff-8278-bd0bc4cb0942
    content: ""
    status: pending
  - id: c04ab76a-e643-4de2-b941-7d8f50011f31
    content: ""
    status: pending
  - id: c6751161-635d-44f8-991a-959c70ceb55a
    content: ""
    status: pending
  - id: cac9cbdf-bc00-4e56-9ff8-68d894121132
    content: ""
    status: pending
  - id: 3d3e0b10-79b8-4887-81ee-7781cb697216
    content: ""
    status: pending
isProject: false
---

# Plan: Resolve All Run Log Warnings

## Todos

- **§4.1a** – In `frontend_agent/agent.py` `_read_repo_code`, skip any path where `node_modules`, `dist`, or `.angular` is in `f.parts` (in addition to `.git`).
- **§4.1b** – In `orchestrator.py` `_read_repo_code`, when extensions include frontend types (`.ts`, `.tsx`, `.html`, `.scss`), apply the same exclusions: `node_modules`, `dist`, `.angular` in path parts.
- **§4.2a** – Define `MAX_CODE_REVIEW_CHARS = 150_000` in a shared place (e.g. `shared/` module or `code_review_agent/models.py`) and document it.
- **§4.2b** – In `frontend_agent/agent.py`, when building `CodeReviewInput` for `_run_code_review`, truncate `code` to `MAX_CODE_REVIEW_CHARS` using `_truncate_for_context` (or equivalent) and pass truncated string.
- **§4.2c** – In `backend_agent/agent.py`, when building `CodeReviewInput` for `_run_code_review`, cap `code` to `MAX_CODE_REVIEW_CHARS` before passing.
- **§4.2d** – In `orchestrator.py`, wherever `CodeReviewInput` is built with `code_to_review`, truncate to `MAX_CODE_REVIEW_CHARS` before calling the code review agent.
- **§4.2e** – Optionally in `code_review_agent/agent.py` `run()`, if `len(input_data.code or "") > MAX_CODE_REVIEW_CHARS`, truncate and log that code was truncated for review (defense in depth).
- **§4.3** – Confirm backend and orchestrator use the same cap and (for frontend paths) the same directory exclusions; add a short comment in orchestrator `_read_repo_code` or call site documenting frontend exclusions.
- **§4.4a** – Add unit test: frontend `_read_repo_code` with a temp dir containing `node_modules/foo/bar.ts` and `src/app/app.ts`; assert result does not contain content from `node_modules` and length is bounded.
- **§4.4b** – Add test that code review input is truncated (e.g. pass a 300k-char string and assert prompt or input to LLM is capped at `MAX_CODE_REVIEW_CHARS`).
- **§1.1a** – In `shared/llm.py` `_extract_json`, add fallback: find all markdown code blocks (e.g. `json ...`  or `...`), try parsing each block as JSON; use first that yields a non-empty dict with at least one expected key (`files`, `summary`, `code`, `overview`, etc.).
- **§1.1b** – In `_extract_json`, when no JSON object is found, optionally call `extract_files_from_content(text)` from `shared.llm_response_utils`; if it returns a non-empty dict, return `{"files": extracted}` (and minimal other keys if needed) instead of `{"content": text.strip()}`.
- **§1.2** – In `complete_json`, tighten system message (e.g. add: "If you use a code block, put only the JSON object inside it with no surrounding text.") and add a one-line comment that models ignoring this may need a different model or pre-processing.
- **§1.3** – Add unit tests in `tests/test_llm.py` for `_extract_json`: (a) valid JSON inside markdown fence, (b) text + JSON on same line, (c) truncated JSON; assert no raw wrapper when a valid object can be recovered.
- **§2.1** – After §1 is in place, optionally downgrade architecture agent log from INFO to DEBUG for "LLM response unparseable or missing structure, building synthetic architecture" (or leave as INFO and skip).
- **§2.2** – Optional: add second LLM call in architecture agent when first response is unparseable (shorter prompt, parse result); document decision in code or plan.
- **§3.1** – In `backend_agent/agent.py`, when `data.get("content")` is present and `raw_files` is still empty after `extract_files_from_content`, add heuristic extraction: split by "File:", "path:", or lines that look like paths (e.g. ending in `.py`); build one or two file entries; log "Backend: using heuristic file extraction from raw content" when used.
- **§3.2** – In `frontend_agent/agent.py`, same heuristic extraction when content fallback returns empty (paths ending in `.ts`/`.html`/`.scss`); log "Frontend: using heuristic file extraction from raw content" when used.
- **§3.3** – Implement self-contained problem-solving prompt per [self-contained_problem-solving_prompt_66722550.plan.md](.cursor/plans/self-contained_problem-solving_prompt_66722550.plan.md): in `shared/prompt_utils.py` change instruction to "issue details in this section"; in backend agent build single problem-solving block with inlined QA/security/code review issue text and do not duplicate issue blocks later in prompt.
- **§3.4** – In frontend agent, apply same self-contained problem-solving block pattern (inline accessibility + code review issues into the block, no duplicate issue sections).
- **§3.5** – Backend code review context for repo-setup/initial-commit tasks: when building `code` for code review, include `.gitignore`, `README.md`, `CONTRIBUTORS.md` (e.g. by extending `_read_repo_code` with optional extra extensions or a separate read of these files when task type is repo-setup), or add a small helper that appends these file contents when present and task description suggests repo setup.
- **§3.6a** – Add test: backend/frontend with `data = {"content": "```python\\n# app/main.py\\nprint(1)\\n```"}` yields at least one extracted file.
- **§3.6b** – Add test: `data = {"content": "no code blocks"}` triggers empty_completion path and does not crash (no unhandled exception).
- **§7** – Run full pipeline (or relevant subset) and verify: no "request body too large"; fewer JSON parse warnings; no infinite backend git-setup loop; success criteria in §7 met.

---

## Overview

This plan identifies every source of the warnings seen in the orchestrator/agent run logs and defines concrete changes so they do not recur. The warnings fall into four categories: (1) LLM JSON parse failure and downstream empty completion, (2) architecture fallback, (3) code review "request body too large", and (4) backend workflow "Write failed after code review fix" / code review loop.

---

## 1. Warning: "Could not parse structured JSON from LLM response; returning raw content wrapper"

### Source

- **File:** [software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py)
- **Location:** `OllamaLLMClient._extract_json()` (around lines 614–659). When all JSON extraction attempts fail (primary parse, repair, object extraction, noise stripping), the code logs this warning and returns `{"content": text.strip()}` so callers do not crash.

### Root cause

The LLM (e.g. Ollama with `qwen3-coder:480b-cloud`) sometimes returns:

- Explanatory text around JSON
- Markdown code fences without valid JSON inside
- Truncated or malformed JSON
- Non-JSON (e.g. Mermaid) in contexts that expect JSON

So the structured fields expected by agents (`files`, `summary`, `code`, etc.) are missing; callers only see `content`.

### Resolution (prevent recurrence)

1. **Strengthen JSON extraction in `shared/llm.py**`
  - In `_extract_json`, add one more fallback: if the response contains markdown code blocks (e.g. `json ...` ), try parsing each block and use the first that yields a non-empty dict with at least one expected key (e.g. `files`, `summary`, `code`, `overview`).
  - Optionally try `extract_files_from_content` (from `shared.llm_response_utils`) on `text` when no JSON object is found, and if it returns files, build a minimal `{"files": extracted}` dict and return that instead of the raw wrapper (so backend/frontend get usable output without a second LLM call).
  - Keep the final fallback to `{"content": text.strip()}` and the existing warning so operators still see parse failures, but reduce how often we hit it.
2. **Tighten system prompt for JSON-only output**
  - In `complete_json`, the system message already says "Respond with a single valid JSON object only, no explanatory text, no Markdown, no code fences." Consider adding: "If you use a code block, put only the JSON object inside it with no surrounding text."
  - Document in code that models that frequently ignore this may need a different model or a post-processor that strips common wrappers before `_extract_json`.
3. **Tests**
  - Add unit tests in `software_engineering_team/tests/test_llm.py` for `_extract_json` with: (a) valid JSON in markdown fence, (b) text + JSON object on same line, (c) truncated JSON; assert no raw wrapper when a valid object can be recovered.

---

## 2. Warning: "Architecture Expert: LLM response unparseable or missing structure, building synthetic architecture from requirements"

### Source

- **File:** [software_engineering_team/architecture_agent/agent.py](software_engineering_team/architecture_agent/agent.py)
- **Location:** After `self.llm.complete_json(...)` (around lines 154–162). When `data` has no `overview` or is the raw wrapper (e.g. only `content`), the agent logs this and calls `_build_synthetic_architecture_data(reqs)`.

### Root cause

Same as §1: the LLM often returns non-JSON or a wrapper. The architecture agent correctly treats that as a parse failure and builds synthetic architecture so the pipeline can continue.

### Resolution (prevent recurrence)

1. **Reduce how often we hit this**
  - Improving `_extract_json` (see §1) will reduce the number of times the architecture agent receives only `content`, so fewer fallbacks and fewer log lines.
  - No need to remove the fallback or the log; it is correct behavior. Optionally downgrade the log from `INFO` to `DEBUG` once §1 is in place and parse success rate is high, so runs are less noisy.
2. **Optional: second LLM call**
  - Current design avoids a second call by building synthetic data. If product prefers a real architecture when the first response is unparseable, add an optional retry with a shorter prompt ("Return only a JSON object with keys: overview, components, architecture_document, diagrams, decisions, summary") and use that if parseable; otherwise keep synthetic. Document the choice.

---

## 3. Warnings: "Backend/Frontend: produced no files and no code (failure_class=empty_completion); re-prompting once" and "Write failed after code review fix: No files to write"

### Sources

- **Backend:** [software_engineering_team/backend_agent/agent.py](software_engineering_team/backend_agent/agent.py)  
  - Empty completion: around lines 1621–1627 (guard when `total_chars == 0` after parsing LLM response).  
  - Write failed: around lines 736–742 (when `write_agent_output` fails because `result.files` is empty after a code-review fix iteration).
- **Frontend:** [software_engineering_team/frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py)  
  - Empty completion: around lines 340–346 (same guard).

### Root cause

When the LLM response is the raw content wrapper (`{"content": "..."}`), backend and frontend:

1. Read `files` from `data` → empty.
2. Use the content fallback: `extract_files_from_content(str(data["content"]))`. If the content has no parseable code blocks or JSON `files`, this returns nothing.
3. So `validated_files` and `code` stay empty → `total_chars == 0` → empty_completion warning and retry. After retry, if the model again returns non-JSON or non-parseable content, we still have no files → write fails with "No files to write".

### Resolution (prevent recurrence)

1. **Improve LLM JSON extraction (see §1)**
  - So we get structured `files` (or a minimal `{"files": extracted}` from content) more often.
2. **Stronger content fallback in backend and frontend**
  - In both agents, when `data.get("content")` is present and `raw_files` is empty, after calling `extract_files_from_content`:
    - If `extracted` is still empty, try splitting `content` by common section headers (e.g. "File:", "path:") and heuristics (e.g. lines that look like paths ending in `.py`/`.ts`) and build one or two files so the agent has something to write instead of failing with zero files every time.
  - Log a single warning when using this heuristic fallback (e.g. "Backend/Frontend: using heuristic file extraction from raw content").
3. **Problem-solving prompt and self-contained issues**
  - Implement the [self-contained problem-solving prompt](.cursor/plans/self-contained_problem-solving_prompt_66722550.plan.md): put full issue text (code review, QA, security, accessibility) inside the problem-solving block so the model has clear, inlined instructions. This can improve the chance that the model returns valid JSON with a `files` key when fixing code review issues.
4. **Backend git-setup vs code review**
  - Code review reports "Missing .gitignore, README.md" because backend `_read_repo_code(repo_path)` only includes `.py`/`.java`, so the reviewer never sees those files. Options:
    - When building code for code review, optionally include a small set of repo metadata files (e.g. `.gitignore`, `README.md`, `CONTRIBUTORS.md`) for repo-setup or “initial commit” tasks, or
    - Have the backend agent’s initial step (or command_runner) ensure .gitignore and README are committed and then pass a note to the reviewer that "repo metadata files are present" so the reviewer can approve without needing them in the `code` string.
  - Prefer the first option so the reviewer can actually see and judge those files.
5. **Tests**
  - Add tests: (a) backend/frontend with `data = {"content": "```python\\n# app/main.py\\nprint(1)\\n```"}` → expect at least one file extracted; (b) with `data = {"content": "no code blocks"}` → expect empty_completion path and no crash.

---

## 4. Warning: "400 Bad Request: http: request body too large" (Code Review)

### Source

- **Call path:** Frontend workflow calls `_run_code_review(..., code=code_on_branch, ...)` with `code_on_branch = _read_repo_code(repo_path, [".ts", ".tsx", ".html", ".scss"])`.
- **File:** [software_engineering_team/frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py)  
  - `_read_repo_code` (lines 54–69) and its use for code review (e.g. lines 579–583, 638–656).
- **Code review agent:** [software_engineering_team/code_review_agent/agent.py](software_engineering_team/code_review_agent/agent.py) builds a prompt that includes `input_data.code` in full (line 82). No truncation is applied to `code` before sending to the LLM.
- **Log evidence:** "CodeReview: reviewing 22249012 chars of typescript code" → ~22 MB sent to Ollama → server returns 400.

### Root cause

`_read_repo_code` in the frontend agent uses `repo_path.rglob("*")` and only skips `.git`. It does **not** exclude `node_modules` or `dist`. So all `.ts`, `.tsx`, `.html`, `.scss` under `node_modules` (and any other such trees) are included, producing tens of millions of characters and exceeding the HTTP request body limit.

### Resolution (prevent recurrence)

1. **Exclude large/irrelevant directories in `_read_repo_code` (frontend)**
  - In [software_engineering_team/frontend_agent/agent.py](software_engineering_team/frontend_agent/agent.py), inside `_read_repo_code`, skip any path that has `node_modules`, `dist`, or `.angular` in `f.parts` (in addition to `.git`). This keeps only application source and avoids sending dependency code to the code review (and QA/accessibility/security) agents.
  - Apply the same exclusion in any other caller that reads frontend repo code for review (e.g. orchestrator’s `_read_repo_code` when used for frontend paths).
2. **Cap code size sent to the code review agent**
  - Wherever `CodeReviewInput` is built (frontend agent, backend agent, orchestrator), cap the `code` string to a safe maximum (e.g. 150_000–200_000 characters) before passing to the agent. Use the existing `_truncate_for_context` pattern and append "... [truncated for code review, N more chars]".
  - Define a constant e.g. `MAX_CODE_REVIEW_CHARS = 150_000` in a shared place (e.g. `shared` or in the code_review_agent) and use it in all callers so that even large application trees cannot exceed the server’s body limit.
  - Optionally, in the code review agent’s `run()`, if `len(input_data.code or "") > MAX_CODE_REVIEW_CHARS`, truncate and log that code was truncated for review.
3. **Backend and orchestrator**
  - Backend `_read_repo_code` only uses `.py`/`.java` by default and typically stays small; still apply the same `MAX_CODE_REVIEW_CHARS` cap when building `CodeReviewInput` so backend cannot ever exceed the limit.
  - Orchestrator: if it ever builds `CodeReviewInput` with `_read_repo_code(frontend_dir, ...)`, ensure (a) frontend dir uses the same exclusions (node_modules, dist, .angular), and (b) truncate to `MAX_CODE_REVIEW_CHARS`.
4. **Tests**
  - Unit test: a repo path that contains a fake `node_modules/foo/bar.ts` (or a tree with many files) is passed to `_read_repo_code`; assert that the returned string does not contain content from under `node_modules` and that length is below a reasonable bound when many app files exist.
  - Test that `CodeReviewInput(code=very_long_string)` is truncated to `MAX_CODE_REVIEW_CHARS` before the prompt is built (or that the agent truncates and does not send > limit).

---

## 5. Other log lines (informational)

- **"Build verification failed for task X: failure_class=ng_build_error"**  
  - Expected when the frontend does not compile. Reducing JSON parse failures and ensuring problem-solving receives inlined issue text (§3) and fixing code review body size (§4) will allow the agent to receive useful feedback and fix builds. No separate “warning fix” beyond the above.
- **"Code review REJECTED (N issues)"**  
  - Expected when the reviewer finds issues. The backend git-setup loop persists because fixes produce no files (§3); addressing §1 and §3 should break that loop.
- **"LLM 5xx error, retrying in Xs"**  
  - From [software_engineering_team/shared/llm.py](software_engineering_team/shared/llm.py) retry logic; operational, not a bug. No change required for “warnings” other than ensuring retries are logged at an appropriate level if desired.

---

## 6. Implementation order

1. **§4 – Code review body size**
  Exclude `node_modules`/`dist`/`.angular` in frontend `_read_repo_code` and add `MAX_CODE_REVIEW_CHARS` truncation everywhere `CodeReviewInput` is built. This stops 400 "request body too large" immediately.
2. **§1 – JSON extraction**
  Improve `_extract_json` (and optional use of `extract_files_from_content` when no JSON found). This reduces raw wrappers and thus empty completions and architecture fallbacks.
3. **§3 – Empty completion / Write failed**
  Stronger content fallback (heuristic extraction), self-contained problem-solving block, and optional inclusion of .gitignore/README in backend code review context for repo-setup tasks.
4. **§2 – Architecture**
  Optional: downgrade log level or add optional second LLM call; otherwise leave as-is once §1 is done.
5. **Tests**
  Add tests for §1, §3, and §4 as described above.

---

## 7. Success criteria

- No "request body too large" from the code review agent when running normal frontend workflows.
- Few or no "Could not parse structured JSON from LLM response" logs when the model returns JSON in code blocks or with minor noise (improved extraction).
- No repeated "produced no files and no code" / "Write failed after code review fix: No files to write" for the same task when the LLM returns fix instructions in content (better extraction or heuristic fallback + self-contained problem-solving).
- Backend git-setup task can pass code review without infinite loop (either by including .gitignore/README in review context or by agent producing them in the first place).

