# false_positive_filter.py consolidated cleanup

**Date:** 2026-07-28  
**Issue:** #3612 (parent); sub-issues 2892, 2894, 2981, 2983, 2985, 2988, 3114, 3227, 3228, 3233, 3339, 3344, 3345, 3346, 3347, 3474, 3478, 3480, 3481, 3482, 3483  
**Status:** Approved design  
**Approach:** Surgical in-place changes to `false_positive_filter.py` + `test_false_positive_filter.py` only

## Goal

Sweep automated-code-review findings against the false-positive verifier in one PR: tighten path resolution, input validation, dataclass invariants, tool fail-safety, logging bounds, and docstring accuracy — without changing the module’s fail-safe removal policy.

## Non-goals

- No new modules or extraction of `CodebaseIndex` into a separate package.
- No changes to architecture-consistency / side-effect passes, coordinator wiring, or ENV docs (unless a one-line docstring in this file requires it).
- No change to the fail-safe rule: findings are removed only on explicit high/medium-confidence false-positive verdicts.

## Already fixed on main (close without code)

| Issue | Why already done |
|---|---|
| #2894 | Removals track original list indices via `removed_indices` / `group_orig_indices`, not `id(issue)`. |
| #2988, #3347 | `find_function_at_line` resolves via `resolve_path` then reads content; it no longer treats an `Error:`-prefixed body as a read failure. `read_file_or_none` exists for internal callers. |

## Change map

| Cluster | Sub-issues | Change |
|---|---|---|
| Path resolution | 2981, 2983, 3482, 3483 | Targeted `./` / `/` prefix stripping; ambiguous submission hits unresolved before repo-reader fallback; docstring sync |
| Validation | 2985, 3227, 3228, 3339, 3345, 3346 | Strict index/line checks; heuristic EOF guard; DbC asserts in helpers (tool layer still never raises) |
| Invariants / mutability | 3344, 3478, 3480, 3481 | `_Verdict.__post_init__`; freeze `CodebaseIndex`; keep whitespace-only files |
| Tools / logging / dead API | 2892, 3114, 3233, 3474 | Drop `max_inline_chars`; truncate drop logs; wrap tools; fix “already capped” docs |

## Path resolution

### Normalization (#2981)

Replace `key.lstrip("./")` with targeted prefix stripping:

1. While the key starts with `./`, strip those two characters.
2. If the key starts with `/`, strip that single character once.

Preserve leading dots that are part of the basename (`.env`, `./.env` → `.env`).

### Ambiguity before repo fallback (#2983, #3483, #3482)

Shared precedence for `resolve_path` and `_read`:

1. Exact match, existing-codebase pseudo-path, or unique suffix match → use it.
2. If `_resolve` returned multiple suffix hits → treat as unresolved (`None` / ambiguity error). Do **not** consult `repo_reader`.
3. Only when there are zero submission hits → fall through to `repo_reader`.
4. Otherwise absent.

Update `read_file` / `resolve_path` docstrings so they describe this precedence (ambiguous submission paths error before any repo content is returned).

## Validation / Design by Contract

### `_coerce_verdict` (#2985, #3227)

Accept only non-negative `int` indices. Reject `bool`, `float`, negatives, and non-ints. Malformed → `None` (fail-safe: keep finding). Do not call `int()` on floats (no silent truncation).

### Tool boundary (#3228)

At the start of `find_function_at_line`, require `isinstance(line_number, int) and line_number >= 1`; otherwise return an `Error: ...` string. Tools still never raise into the agent loop.

### Helpers (#3345, #3346)

`_strip_numbered_prefixes` and `_find_python_function_at_line` enforce documented preconditions (`content` type / non-empty as documented; `line_number >= 1`) via raise/assert. Update `_strip_numbered_prefixes`’s “never raises” postcondition to: never raises **when preconditions hold**. The tool’s outer `try/except` remains the safety net for model-supplied junk.

Also enforce the same line/content preconditions on `_find_heuristic_function_at_line` where its docstring already claims them (keeps the three helpers consistent).

### Heuristic EOF (#3339)

In `_find_heuristic_function_at_line`, after splitting lines: if `line_number > len(lines)`, return an explicit beyond-EOF message instead of attributing the last column-0 construct.

## Invariants / mutability

### `_Verdict` (#3344, #3478)

Add `__post_init__`: if `is_false_positive` is True, `confidence` must be `"high"` or `"medium"`, else raise `ValueError`. Coercion paths that keep findings continue to set `is_false_positive=False` for low/blank/unrecognized confidence.

### `CodebaseIndex` (#3480)

Use `@dataclass(frozen=True)` and shallow-copy `files` in `__post_init__` so the instance is isolated from the caller’s dict and matches the “read-only / thread-shareable” claim.

### Whitespace-only files (#3481)

In `from_input`, keep entries whose content is not `None` and not `""`. Do **not** drop whitespace-only bodies (e.g. `"\n"` empty `__init__.py`). Update class / `from_input` docs accordingly. Legacy `### path ###` parsing should use the same non-blank rule (exclude only empty string after optional path checks, not `.strip()` emptiness) for consistency.

## Tools / logging / dead params

### Remove unused `max_inline_chars` (#2892)

Remove the parameter from `_build_group_prompt`, `_verify_group`, and the `_verify_and_filter` call chain. Drop the unused `compute_code_review_map_chunk_chars` import/call in this module. Update the test call site that still passes `max_inline_chars=...`.

### Tool “never raises” (#3474)

Wrap `read_file`, `list_files`, and `search_codebase` in `try/except Exception`, returning `Error: ...` strings — matching `find_function_at_line`.

### Log truncation (#3114)

Add `_truncate_for_log(text, max_len=400)`. Use it for `issue.description` and `verdict.reasoning` on the drop INFO log. Apply the same helper to oversized exception text on group-failure WARNING logs when useful.

### Doc drift (#3233)

Remove “already capped” wording for `existing_codebase`; state it is the full excerpt. Scan this file for other stale cap/truncation claims about that excerpt and fix them. Remove the unused `_CONTEXT_FIELD_CHARS` constant (defined but never applied; `_build_group_prompt` intentionally leaves task/acceptance fields uncapped).

## Testing

Extend `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` only. Baseline before changes: 70 passed.

| Area | Cases |
|---|---|
| Path | `.env` / `./.env` normalization; ambiguous submission + repo hit stays unresolved; unique miss still uses reader |
| Coercion | Reject `True` / `1.9` / `-1` indices; keep valid non-negative ints |
| Tool | Bad `line_number` → error string; wrappers swallow unexpected raises from index methods |
| Heuristic | Line past EOF → beyond-EOF message |
| `_Verdict` | Invalid FP+confidence raises; valid high/medium FP ok |
| Frozen index | Post-construct mutation of `.files` fails or is isolated from caller dict |
| Whitespace | Whitespace-only file kept in index |
| Logging | Drop log uses truncated description/reasoning (length capped) |
| Dead param | Prompt builder call no longer takes `max_inline_chars` |

Existing suite must remain green.

## Error handling (unchanged policy)

- Ambiguous / invalid / unreadable paths → keep finding or return tool error string.
- Verifier / LLM / setup failures → keep affected findings.
- Confident false-positive verdict → remove finding and log (truncated) reason.

## Closeout

One PR from branch `issue-3612-false-positive-filter-cleanup` closes #3612 and all listed sub-issues. Already-fixed sub-issues are called out in the PR body as resolved by prior work on `main`.
