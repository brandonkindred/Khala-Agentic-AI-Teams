# false_positive_filter Consolidated Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sweep all still-open #3612 sub-issue findings in `false_positive_filter.py` (path resolution, validation, invariants, tools, logging, dead params) in one PR without weakening the fail-safe removal policy.

**Architecture:** Surgical in-place edits to one implementation file and its unit test module. Align behavior with documented contracts (ambiguity before repo-reader fallback; DbC on helpers; tools never raise). Close already-fixed sub-issues (#2894, #2988, #3347) with no code change.

**Tech Stack:** Python 3.10+, pytest, existing `CodebaseIndex` / strands `@tool` patterns in the software_engineering_team code-review agent.

**Worktree:** `/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/issue-3612-false-positive-filter-cleanup` on branch `issue-3612-false-positive-filter-cleanup`.

**Spec:** `docs/superpowers/specs/2026-07-28-false-positive-filter-cleanup-design.md`

## Global Constraints

- Touch only `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` and `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` (plus plan/spec docs if needed).
- Fail-safe rule unchanged: drop a finding only on explicit high/medium-confidence false-positive verdict.
- Never reference GitHub issue numbers in code, comments, or commit messages; PR body uses `Closes #N`.
- Every new/changed public function keeps DbC docstring sections (`Preconditions` / `Postconditions` / `Invariants` as applicable).
- TDD: write failing test → run → implement → run → commit per task.
- Pytest command (from repo worktree):  
  `cd backend/agents/software_engineering_team && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest tests/test_false_positive_filter.py -q --tb=short`

## File Structure

| File | Responsibility |
|---|---|
| `code_review_agent/false_positive_filter.py` | Index, path resolution, tools, verdict coercion, verify/filter |
| `tests/test_false_positive_filter.py` | Unit + filter behavior coverage for the module above |

No new files.

---

### Task 1: Path normalization and ambiguity-before-reader

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` (`_resolve`, `resolve_path`, `_read`, related docstrings)
- Test: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py`

**Interfaces:**
- Consumes: existing `CodebaseIndex._resolve(key) -> Tuple[Optional[str], List[str]]`, `_reader_read`
- Produces: same signatures; new resolution precedence (ambiguous hits never consult reader)

- [ ] **Step 1: Write the failing tests**

Add near the existing `test_resolve_path_*` / reader tests:

```python
def test_resolve_preserves_hidden_file_basename() -> None:
    """Bare-name normalization must not strip the leading dot from ``.env``."""
    idx = CodebaseIndex(files={"config/.env": "SECRET=1\n"})
    assert idx.resolve_path(".env") == "config/.env"
    assert idx.resolve_path("./.env") == "config/.env"
    assert idx.read_file(".env") == "SECRET=1\n"


def test_ambiguous_submission_does_not_fall_through_to_reader() -> None:
    """Multiple submission suffix hits must not resolve via a same-basename repo file."""
    reader = _FakeReader({"helpers.py": "REPO"})
    idx = CodebaseIndex(
        files={"a/helpers.py": "A", "b/helpers.py": "B"},
        repo_reader=reader,  # type: ignore[arg-type]
    )
    assert idx.resolve_path("helpers.py") is None
    msg = idx.read_file("helpers.py")
    assert "ambiguous" in msg
    assert "REPO" not in msg
```

Also update `test_index_from_files_keeps_nonblank` only in Task 4 (whitespace); leave it alone here.

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd backend/agents/software_engineering_team && \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  tests/test_false_positive_filter.py::test_resolve_preserves_hidden_file_basename \
  tests/test_false_positive_filter.py::test_ambiguous_submission_does_not_fall_through_to_reader -v
```
Expected: FAIL — `.env` resolves wrong / reader content returned for ambiguous basename.

- [ ] **Step 3: Implement path fixes**

In `_resolve`, replace `normalized = key.lstrip("./")` with:

```python
normalized = key
while normalized.startswith("./"):
    normalized = normalized[2:]
if normalized.startswith("/"):
    normalized = normalized[1:]
```

In `resolve_path`:

```python
key = (path or "").strip()
resolved, hits = self._resolve(key)
if resolved is not None:
    return resolved
if len(hits) > 1:
    return None
if key and self._reader_read(key) is not None:
    return key
return None
```

In `_read`, after a failed submission resolve, check ambiguity **before** the reader:

```python
resolved, hits = self._resolve(key)
if resolved == self.EXISTING_CODEBASE_PATH:
    return self.existing_codebase, None
if resolved is not None:
    return self.files[resolved], None
if len(hits) > 1:
    return None, (
        f"Error: path '{path}' is ambiguous; it matches "
        f"{', '.join(sorted(hits))}. Use list_files() and read the exact path."
    )
reader_content = self._reader_read(key)
if reader_content is not None:
    return reader_content, None
if key == self.EXISTING_CODEBASE_PATH:
    return None, "Error: no existing-codebase excerpt available."
return None, f"Error: file not found: {path}. Use list_files() to see available paths."
```

Update `resolve_path` / `read_file` postcondition docs to state: ambiguous submission suffix hits are unresolved / error out before any repo-reader fallback.

- [ ] **Step 4: Run tests to verify they pass**

Run the two new tests plus:
```bash
... pytest tests/test_false_positive_filter.py::test_resolve_path_exact_suffix_and_misses \
  tests/test_false_positive_filter.py::test_read_file_ambiguous_suffix \
  tests/test_false_positive_filter.py::test_resolve_path_uses_reader \
  tests/test_false_positive_filter.py::test_index_read_file_falls_through_to_reader -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Fix false-positive path normalization and ambiguity precedence.

Preserve hidden basenames and stop ambiguous submission hits from
falling through to the repo reader.
EOF
)"
```

---

### Task 2: Strict `_coerce_verdict` index validation

**Files:**
- Modify: `false_positive_filter.py` (`_coerce_verdict`)
- Test: `test_false_positive_filter.py` (`test_coerce_verdict_variants`)

**Interfaces:**
- Consumes: raw verdict dicts from the model
- Produces: `Optional[Tuple[int, _Verdict]]` — `None` for non-int / bool / negative indices

- [ ] **Step 1: Extend the failing assertions in `test_coerce_verdict_variants`**

Append:

```python
    # bool / float / negative indices are rejected (not coerced)
    assert _coerce_verdict({"index": True, "is_real_issue": False, "confidence": "high"}) is None
    assert _coerce_verdict({"index": 1.9, "is_real_issue": False, "confidence": "high"}) is None
    assert _coerce_verdict({"index": -1, "is_real_issue": False, "confidence": "high"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

```bash
... pytest tests/test_false_positive_filter.py::test_coerce_verdict_variants -v
```
Expected: FAIL (bool/float currently accepted via `int()`).

- [ ] **Step 3: Implement validation**

Replace the `try: index = int(raw_index)` block with:

```python
raw_index = item.get("index")
if isinstance(raw_index, bool) or not isinstance(raw_index, int) or raw_index < 0:
    return None
index = raw_index
```

Update the `_coerce_verdict` postcondition bullet to mention non-negative int-only indices (bool/float rejected).

- [ ] **Step 4: Run test to verify it passes**

```bash
... pytest tests/test_false_positive_filter.py::test_coerce_verdict_variants \
  tests/test_false_positive_filter.py::test_parse_verdicts_filters_out_of_range_and_bad_shapes -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Reject non-integer and negative verdict indices.

Keep false-positive coercion fail-safe by ignoring malformed index values.
EOF
)"
```

---

### Task 3: Function-finder guards (tool, helpers, heuristic EOF)

**Files:**
- Modify: `false_positive_filter.py` (`_strip_numbered_prefixes`, `_find_python_function_at_line`, `_find_heuristic_function_at_line`, `find_function_at_line` inside `_build_tools`)
- Test: `test_false_positive_filter.py`

**Interfaces:**
- Consumes: `CodebaseIndex.resolve_path` / `read_file`
- Produces: tool still returns `str` and never raises; helpers raise on precondition violations

- [ ] **Step 1: Write the failing tests**

```python
def test_find_function_at_line_rejects_nonpositive_line() -> None:
    idx = CodebaseIndex(files={"app/main.py": "def f():\n    return 1\n"})
    _, _, _, find_fn = _build_tools(idx)
    assert "positive" in find_fn("app/main.py", 0).lower()
    assert "positive" in find_fn("app/main.py", -3).lower()


def test_strip_numbered_prefixes_rejects_bad_preconditions() -> None:
    with pytest.raises((TypeError, ValueError, AssertionError)):
        _strip_numbered_prefixes(None, 1)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError, AssertionError)):
        _strip_numbered_prefixes("x = 1\n", 0)


def test_find_heuristic_beyond_eof() -> None:
    from code_review_agent.false_positive_filter import _find_heuristic_function_at_line

    content = "function alpha() {\n  return 1;\n}\n"
    msg = _find_heuristic_function_at_line(content, 99, "app.ts")
    assert "beyond" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
... pytest tests/test_false_positive_filter.py::test_find_function_at_line_rejects_nonpositive_line \
  tests/test_false_positive_filter.py::test_strip_numbered_prefixes_rejects_bad_preconditions \
  tests/test_false_positive_filter.py::test_find_heuristic_beyond_eof -v
```
Expected: FAIL

- [ ] **Step 3: Implement guards**

At top of `_strip_numbered_prefixes`:

```python
if not isinstance(content, str):
    raise TypeError("content must be a string")
if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
    raise ValueError("line_number must be a positive integer")
```

Change its postcondition from “Never raises” to “Never raises when preconditions hold.”

At top of `_find_python_function_at_line` and `_find_heuristic_function_at_line`:

```python
if not isinstance(content, str) or not content:
    raise ValueError("content must be a non-empty string")
if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
    raise ValueError("line_number must be a positive integer")
```

In `_find_heuristic_function_at_line`, after `lines = content.splitlines()` (introduce that local):

```python
lines = content.splitlines()
if line_number > len(lines):
    return (
        f"Line {shown} is beyond the end of {path} "
        f"(file has {len(lines)} lines)."
    )
```

Then iterate `for i, line in enumerate(lines, start=1):` as today.

In `find_function_at_line`, before resolve/read:

```python
if not isinstance(line_number, int) or isinstance(line_number, bool) or line_number < 1:
    return f"Error: line_number must be a positive integer, got {line_number!r}."
```

Prefer `index.read_file_or_none(path)` (or resolve + `files`/reader) over treating `read_file` error strings as content when calling helpers — if `read_file_or_none` returns `None` after a successful `resolve_path`, return an error string. (Existing `Error:`-content test must keep passing.)

- [ ] **Step 4: Run related tests**

```bash
... pytest tests/test_false_positive_filter.py -k "find_function or strip_numbered or heuristic" -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Harden function-at-line helpers and tool line validation.

Enforce helper preconditions, reject bad tool line numbers, and stop
heuristic EOF guesses past the end of the file.
EOF
)"
```

---

### Task 4: `_Verdict` invariant, frozen index, whitespace-only files

**Files:**
- Modify: `false_positive_filter.py` (`CodebaseIndex`, `from_input`, `_Verdict`)
- Test: `test_false_positive_filter.py`

**Interfaces:**
- Consumes: `CodeReviewInput.files` / legacy code blocks
- Produces: frozen `CodebaseIndex`; `_Verdict` that raises on illegal FP+confidence

- [ ] **Step 1: Write / update failing tests**

Replace `test_index_from_files_keeps_nonblank` body and rename intent:

```python
def test_index_from_files_keeps_whitespace_only() -> None:
    """``from_input`` keeps whitespace-only files; only None/empty-string content is dropped."""
    idx = CodebaseIndex.from_input(
        _input(files={"a.py": "x = 1\n", "b.py": "   ", "c.py": "", "d.py": "\n"})
    )
    assert set(idx.files) == {"a.py", "b.py", "d.py"}


def test_verdict_invariant_rejects_low_confidence_false_positive() -> None:
    from code_review_agent.false_positive_filter import _Verdict

    with pytest.raises(ValueError):
        _Verdict(is_false_positive=True, confidence="low")
    with pytest.raises(ValueError):
        _Verdict(is_false_positive=True, confidence="")
    ok = _Verdict(is_false_positive=True, confidence="high")
    assert ok.is_false_positive is True


def test_codebase_index_is_frozen_and_isolates_files_dict() -> None:
    src = {"a.py": "x"}
    idx = CodebaseIndex(files=src)
    src["a.py"] = "mutated"
    assert idx.files["a.py"] == "x"
    with pytest.raises(Exception):
        idx.files = {}  # type: ignore[misc]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
... pytest tests/test_false_positive_filter.py::test_index_from_files_keeps_whitespace_only \
  tests/test_false_positive_filter.py::test_verdict_invariant_rejects_low_confidence_false_positive \
  tests/test_false_positive_filter.py::test_codebase_index_is_frozen_and_isolates_files_dict -v
```
Expected: FAIL (old whitespace test name removed / behaviors missing).

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class CodebaseIndex:
    ...
    def __post_init__(self) -> None:
        object.__setattr__(self, "files", dict(self.files))
```

In `from_input`:

```python
files = {
    path: content
    for path, content in input_data.files.items()
    if content is not None and content != ""
}
```

Legacy branch:

```python
if path and content != "":
    files[path] = content
```

(Do not use `content.strip()` as the keep/drop predicate.)

Update class / `from_input` docs: full excerpt (not “already capped”); files map includes whitespace-only bodies; empty string / missing content excluded.

```python
@dataclass
class _Verdict:
    ...
    def __post_init__(self) -> None:
        if self.is_false_positive and self.confidence not in ("high", "medium"):
            raise ValueError(
                "is_false_positive=True requires confidence 'high' or 'medium', "
                f"got confidence={self.confidence!r}"
            )
```

- [ ] **Step 4: Run index + filter suite slice**

```bash
... pytest tests/test_false_positive_filter.py -k "index or Verdict or coerce or filter_removes or reader" -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Enforce CodebaseIndex and verdict invariants.

Freeze the index, keep whitespace-only files, and validate false-positive
confidence at construction time.
EOF
)"
```

---

### Task 5: Remove dead `max_inline_chars` and `_CONTEXT_FIELD_CHARS`

**Files:**
- Modify: `false_positive_filter.py` (`_build_group_prompt`, `_verify_group`, `_verify_and_filter`, imports, constants)
- Test: `test_false_positive_filter.py` (prompt builder call + setup-exception injection)

**Interfaces:**
- Consumes: none of `compute_code_review_map_chunk_chars` in this module anymore
- Produces: `_build_group_prompt(index, file_path, issues, input_data) -> str`  
  `_verify_group(model, index, file_path, issues, input_data) -> Dict[int, _Verdict]`

- [ ] **Step 1: Update tests that encode the old API**

Change:

```python
prompt = _build_group_prompt(idx, "app/main.py", issues, _input(), max_inline_chars=10)
```

to:

```python
prompt = _build_group_prompt(idx, "app/main.py", issues, _input())
```

Rewrite `test_filter_keeps_on_setup_exception` so setup failure does not depend on context sizing. Example:

```python
def test_filter_keeps_on_setup_exception(monkeypatch) -> None:
    import code_review_agent.false_positive_filter as mod

    def _boom(*_a, **_k):
        raise RuntimeError("model resolve boom")

    monkeypatch.setattr(mod, "resolve_code_review_model", _boom)
    issues = [_issue()]
    out = filter_false_positives(DummyLLMClient(), _input(), issues)
    assert out == issues
```

- [ ] **Step 2: Run tests — expect TypeError / failure until signatures match**

```bash
... pytest tests/test_false_positive_filter.py::test_group_prompt_has_anchor_indices_and_full_file_body \
  tests/test_false_positive_filter.py::test_filter_keeps_on_setup_exception -v
```

- [ ] **Step 3: Remove dead parameter and constant**

- Delete `_CONTEXT_FIELD_CHARS` and its comment block.
- Remove `compute_code_review_map_chunk_chars` from the import and from `_verify_and_filter`.
- Drop `max_inline_chars` from `_build_group_prompt` / `_verify_group` signatures, docs, and `_ = max_inline_chars`.
- Update `_build_group_prompt` docstring: remove “intentionally unused” / call-site compatibility wording; keep the “full file body inlined” rationale.

- [ ] **Step 4: Run prompt + filter tests**

```bash
... pytest tests/test_false_positive_filter.py -k "group_prompt or filter_" -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Remove unused false-positive prompt sizing parameters.

Drop dead max_inline_chars plumbing and the unused context-field constant.
EOF
)"
```

---

### Task 6: Tool exception wrapping, log truncation, remaining doc fixes

**Files:**
- Modify: `false_positive_filter.py` (`_build_tools`, `_truncate_for_log`, drop INFO log, remaining “already capped” wording if any)
- Test: `test_false_positive_filter.py`

**Interfaces:**
- Produces: `_truncate_for_log(text: Optional[str], max_len: int = 400) -> str`  
  All four tools return `str` and never raise

- [ ] **Step 1: Write failing tests**

```python
def test_truncate_for_log_caps_length() -> None:
    from code_review_agent.false_positive_filter import _truncate_for_log

    assert _truncate_for_log("abc", 10) == "abc"
    assert _truncate_for_log(None, 10) == ""
    assert len(_truncate_for_log("x" * 500, 400)) == 403  # 400 + "..."
    assert _truncate_for_log("x" * 500, 400).endswith("...")


def test_build_tools_never_raise_on_index_errors(monkeypatch) -> None:
    idx = CodebaseIndex(files={"a.py": "x"})
    read_file, list_files, search_codebase, _find = _build_tools(idx)

    def _boom(*_a, **_k):
        raise RuntimeError("index boom")

    monkeypatch.setattr(idx, "read_file", _boom)
    monkeypatch.setattr(idx, "list_files", _boom)
    monkeypatch.setattr(idx, "search", _boom)
    assert read_file("a.py").startswith("Error")
    assert list_files().startswith("Error")
    assert search_codebase("x").startswith("Error")


def test_filter_drop_log_truncates_description(monkeypatch, caplog) -> None:
    import logging

    keep = _issue(description="real", line=5)
    drop = _issue(description="D" * 1000, line=1)
    stub = _VerdictStub(
        verdicts=[
            {"index": 0, "is_real_issue": True, "confidence": "high"},
            {
                "index": 1,
                "is_real_issue": False,
                "confidence": "high",
                "reasoning": "R" * 1000,
            },
        ]
    )
    with caplog.at_level(logging.INFO):
        out = filter_false_positives(stub, _input(), [keep, drop])
    assert out == [keep]
    joined = " ".join(r.message for r in caplog.records)
    assert "D" * 1000 not in joined
    assert "R" * 1000 not in joined
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
... pytest tests/test_false_positive_filter.py::test_truncate_for_log_caps_length \
  tests/test_false_positive_filter.py::test_build_tools_never_raise_on_index_errors \
  tests/test_false_positive_filter.py::test_filter_drop_log_truncates_description -v
```
Expected: FAIL

- [ ] **Step 3: Implement**

```python
def _truncate_for_log(text: Optional[str], max_len: int = 400) -> str:
    """Return ``text`` capped to ``max_len`` characters for log lines.

    Preconditions:
        - ``max_len`` >= 1.

    Postconditions:
        - Returns ``""`` when ``text`` is None or empty.
        - Returns ``text`` unchanged when ``len(text) <= max_len``.
        - Otherwise returns ``text[:max_len] + "..."``.
    """
    assert max_len >= 1
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
```

Wrap tools:

```python
@tool
def read_file(path: str) -> str:
    ...
    try:
        return index.read_file(path)
    except Exception as exc:
        return f"Error: could not read {path!r}: {type(exc).__name__}: {exc}"

@tool
def list_files() -> str:
    ...
    try:
        paths = index.list_files()
        return "\n".join(paths) if paths else "(no files available)"
    except Exception as exc:
        return f"Error: could not list files: {type(exc).__name__}: {exc}"

@tool
def search_codebase(query: str) -> str:
    ...
    try:
        matches = index.search(query)
        ...
    except Exception as exc:
        return f"Error: could not search for {query!r}: {type(exc).__name__}: {exc}"
```

Drop log:

```python
logger.info(
    "FalsePositiveFilter: dropping false positive [%s] %s:%s — %s (%s)",
    issue.severity,
    issue.file_path,
    issue.line if issue.line is not None else "-",
    _truncate_for_log(issue.description),
    _truncate_for_log(verdict.reasoning) or "no reasoning given",
)
```

Optionally truncate `exc` in the group-failure warning with `_truncate_for_log(str(exc))`.

Finish any remaining “already capped” / excerpt-truncation wording in this file’s docstrings.

- [ ] **Step 4: Run full module tests**

```bash
... pytest tests/test_false_positive_filter.py -q
```
Expected: all PASS (baseline was 70; expect more after new cases).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Make verifier tools fail soft and bound drop-log text.

Wrap index-backed tools so bad arguments become error strings, and
truncate oversized description/reasoning in INFO drop logs.
EOF
)"
```

---

### Task 7: Final verification and PR-ready closeout notes

**Files:**
- Verify only (optional: delete temporary design/plan docs before opening PR, matching repo habit)

- [ ] **Step 1: Run full false-positive filter suite**

```bash
cd backend/agents/software_engineering_team && \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  tests/test_false_positive_filter.py -q --tb=short
```
Expected: all PASS

- [ ] **Step 2: Spec coverage checklist (manual)**

Confirm each sub-issue is addressed or already fixed:

| Issue | Task / status |
|---|---|
| 2892 | Task 5 |
| 2894 | already fixed |
| 2981 | Task 1 |
| 2983 | Task 1 |
| 2985 | Task 2 |
| 2988 | already fixed |
| 3114 | Task 6 |
| 3227 | Task 2 |
| 3228 | Task 3 |
| 3233 | Tasks 4 + 6 |
| 3339 | Task 3 |
| 3344 | Task 4 |
| 3345 | Task 3 |
| 3346 | Task 3 |
| 3347 | already fixed |
| 3474 | Task 6 |
| 3478 | Task 4 |
| 3480 | Task 4 |
| 3481 | Task 4 |
| 3482 | Task 1 |
| 3483 | Task 1 |

- [ ] **Step 3: Lint touched files**

```bash
cd backend && \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff check \
  agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  agents/software_engineering_team/tests/test_false_positive_filter.py && \
  /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/ruff format \
  agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  agents/software_engineering_team/tests/test_false_positive_filter.py
```

- [ ] **Step 4: Stop for PR**

Do not open the PR until the user asks. When opening, PR body must `Closes #3612` and list sub-issue closes; note #2894/#2988/#3347 as already resolved on main.

---

## Self-review (plan vs spec)

| Spec requirement | Plan task |
|---|---|
| Path `./` normalization | Task 1 |
| Ambiguity before repo reader + docstring sync | Task 1 |
| `_coerce_verdict` int/sign checks | Task 2 |
| Tool `line_number` guard | Task 3 |
| Helper DbC + heuristic EOF | Task 3 |
| `_Verdict.__post_init__` | Task 4 |
| Frozen `CodebaseIndex` + shallow copy | Task 4 |
| Keep whitespace-only files | Task 4 |
| Remove `max_inline_chars` + `_CONTEXT_FIELD_CHARS` | Task 5 |
| Tool try/except wrappers | Task 6 |
| Log truncation | Task 6 |
| “already capped” doc fix | Tasks 4 + 6 |
| Already-fixed 2894/2988/3347 | Task 7 checklist (no code) |
| Tests only in `test_false_positive_filter.py` | All tasks |
| Fail-safe policy unchanged | Global constraints |

No placeholders left; signatures for `_build_group_prompt` / `_verify_group` / `_truncate_for_log` are consistent across tasks.
