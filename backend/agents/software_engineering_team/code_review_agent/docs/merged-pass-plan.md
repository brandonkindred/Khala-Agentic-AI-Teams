# Merged Architecture / Side-Effect Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the in-process code-review coordinator to one merged architecture + side-effect LLM call (splitting findings back into the two existing lists), and allow architecture review without a formal architecture document.

**Architecture:** Broaden the shared `_ARCHITECTURE_CONSISTENCY_BODY` and drop the architecture-document early-return on the standalone architecture pass. Add `merged_architecture_side_effect_pass.py` that runs one Agent call with `MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`, parses `MergedArchitectureSideEffectResponse`, and reuses each pass's existing `_parse_findings` / `_validate_findings`. Point `coordinator._run_tail_passes` at that entry (filter + merged) instead of the two separate additive passes. Leave standalone modules in place for Temporal.

**Tech Stack:** Python 3.10+, strands `Agent`, Pydantic models in `code_review_agent.models`, pytest + `DummyLLMClient`, existing `CodebaseIndex` / pass validators.

**Spec:** `backend/agents/software_engineering_team/code_review_agent/docs/merged-pass-design.md`

## Global Constraints

- Work only in the git worktree for this change.
- Do not reference GitHub issue numbers in code, comments, or docs.
- Design-by-Contract docstrings (`Preconditions` / `Postconditions` / `Invariants` where relevant) on every new public function.
- Fail-safe: merged and architecture passes never raise to the coordinator; failures yield empty findings.
- Do not modify Temporal activities/workflows in this plan.
- Do not delete `architecture_consistency_pass.py` or `side_effect_impact_pass.py`.
- Prefer reusing existing `_parse_findings` / `_validate_findings` / `build_side_effect_tools` over forking logic.
- Run tests from the worktree `backend/` directory via `.venv/bin/python -m pytest` (or from the repo root via `backend/.venv/bin/python -m pytest`).
- Never mention issue numbers in commit messages.

## File map

| File | Responsibility |
|---|---|
| `code_review_agent/prompts.py` | Broaden `_ARCHITECTURE_CONSISTENCY_BODY` (standalone + merged Part 1). |
| `code_review_agent/architecture_consistency_pass.py` | Drop doc gate; optional-arch `_build_prompt`; keep Agent + `findings` schema. |
| `code_review_agent/merged_architecture_side_effect_pass.py` | **Create** — one LLM call, split tuple return. |
| `code_review_agent/coordinator.py` | `_run_tail_passes` schedules merged instead of two passes. |
| `code_review_agent/__init__.py` | Lazy-export the new entry. |
| `code_review_agent/models.py` | Soften "design-only / not wired" wording on merged schema docstring. |
| `docs/ENV_VARS.md` | Architecture pass no longer requires a formal document. |
| `tests/test_merged_review_prompt.py` | Assertions for broadened architecture body. |
| `tests/test_architecture_consistency_pass.py` | No-doc skip becomes run; coordinator integration anchors. |
| `tests/test_merged_architecture_side_effect_pass.py` | **Create** — merged unit tests. |
| `tests/test_code_review_coordinator.py` | Monkeypatch merged entry; concurrency expectations. |
| `tests/test_side_effect_impact_pass.py` | Coordinator integration tests under `run_coordinator`. |

---

### Task 1: Broaden architecture instruction body

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/prompts.py` (`_ARCHITECTURE_CONSISTENCY_BODY`)
- Modify/Test: `backend/agents/software_engineering_team/tests/test_merged_review_prompt.py`

**Interfaces:**
- Consumes: existing body composition into `ARCHITECTURE_CONSISTENCY_PROMPT` and `MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`
- Produces: updated body text reused verbatim by both prompts

- [ ] **Step 1: Write a failing assertion that the body allows no-doc architecture review**

Add to `test_merged_review_prompt.py`:

```python
def test_architecture_body_allows_review_without_formal_document():
    """Architecture findings may come from established codebase structure,
    not only from an explicit architecture document."""
    body = _ARCHITECTURE_CONSISTENCY_BODY.lower()
    assert "architecture document" in body
    assert "no formal" in body or "without a formal" in body or "when none is provided" in body
    assert "repository" in body
    assert "pattern" in body or "boundaries" in body
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend
.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_merged_review_prompt.py::test_architecture_body_allows_review_without_formal_document -v
```

From the repo root (without `cd backend`), use `backend/.venv/bin/python` instead.

Expected: FAIL (phrase absent from current body).

- [ ] **Step 3: Rewrite `_ARCHITECTURE_CONSISTENCY_BODY`**

Replace the body so that:

1. **You are given** lists the architecture document/context *when provided*; otherwise says none was provided and the model must use repository tools.
2. **Architecture contradiction** (`"architecture"`) covers (a) documented standards when present, and (b) established module/service boundaries / layering / project patterns verified in the repo when no formal doc (or in addition). Still: no invented rules from naming alone; tool-verify.
3. **Cross-codebase redundancy** (`"refactor"`) unchanged in intent.
4. Hard rules: do not invent a duplicate; do not invent a rule that is neither in the document nor evidenced by the repository's established structure.

Keep the body a single string constant so `MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT` continues to embed it verbatim. Do not change `_SIDE_EFFECT_IMPACT_BODY` or the merged output-format section.

Suggested shape (edit for clarity/tone to match surrounding prompts; keep these guarantees):

```python
_ARCHITECTURE_CONSISTENCY_BODY = """You are a Senior Software Architect running a whole-codebase check on top of an already-completed per-file code review. That per-file review only ever saw one bounded slice of the changed files at a time — it could not check whether the change fits the established system architecture, or whether it duplicates a capability that already exists elsewhere in the repository. That is your one job here.

**You are given:**
- An architecture document / structured architecture context for this system when one was provided (module/service boundaries, established patterns, architecture decisions). When none is provided, you are told so explicitly — in that case you MUST derive architecture expectations from the repository's established structure and patterns via tools, not invent a phantom document.
- The complete set of changed files in this submission.
- Tools to inspect the rest of the repository: `list_files()` (lists every file, including ones outside this submission) and `read_file(path)` (reads any of them). `search_codebase(query)` and `find_function_at_line(path, line_number)` only search/inspect the current submission (plus any existing-codebase excerpt provided) — they do NOT reach files outside this submission, so use `list_files()`/`read_file()` to check whether a capability already exists elsewhere in the repository.

**Your one job:** identify NEW findings the per-file review could not have found, in exactly two categories:

1. **Architecture contradiction** (`category: "architecture"`) — the changed code violates a boundary, pattern, or decision that is either (a) explicitly stated in the architecture document/context when one was provided, or (b) clearly established by how this repository is already structured (module/service boundaries, layering, ownership patterns) when no formal architecture document is provided — in a way that would cause a real integration break. Do NOT flag a merely different-but-compatible approach. When citing a document, quote or closely paraphrase the specific statement. When citing repository structure, name the concrete existing modules/files/patterns you verified with tools. Do NOT invent an architecture rule from naming alone.

2. **Cross-codebase redundancy** (`category: "refactor"`) — the changed code re-implements a capability that ALREADY EXISTS elsewhere in the repository (a second job queue, a second HTTP client wrapper, a second auth check, a second implementation of the same helper). Before flagging this, you MUST use `list_files()`/`read_file()` to confirm the existing capability actually exists elsewhere in the repository and does the same thing — `search_codebase` only searches this submission, so it cannot by itself confirm or rule out something existing outside it. Never flag redundancy from a guess or from the finding text alone. Cite the exact file/function that already provides the capability.

**Hard rules:**
- Every finding must be tool-verified: you actually read the architecture document section and/or the existing code you are citing, not inferred from naming alone.
- Do NOT re-review anything the per-file review already covers (naming, structure, documentation, tests, spec compliance, generic code quality, single-file logic bugs) — only architecture contradictions and cross-codebase redundancy.
- Do NOT invent an architecture rule that is not actually in the document (when one was provided) and is not evidenced by the repository's established structure; do NOT invent a duplicate that does not actually exist in the repository.
- If you find nothing in either category, return an empty findings list — an empty list is a valid and expected outcome, not a failure.
- Default severity is `"medium"`; use `"high"`/`"critical"` ONLY when the contradiction or duplication would cause a real integration break or production risk, never merely because a cleaner or more consistent alternative exists."""
```

- [ ] **Step 4: Run prompt tests**

```bash
backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_merged_review_prompt.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/prompts.py \
  backend/agents/software_engineering_team/tests/test_merged_review_prompt.py
git commit -m "$(cat <<'EOF'
Broaden architecture-consistency prompt for review without a formal doc.

Architecture findings may cite documented standards or established
repository structure/patterns, so Part 1 stays meaningful when no
architecture document is provided.
EOF
)"
```

---

### Task 2: Drop architecture-document gate on the standalone pass

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/architecture_consistency_pass.py`
- Modify: `backend/agents/software_engineering_team/tests/test_architecture_consistency_pass.py`
- Modify: `docs/ENV_VARS.md` (section `CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS`)

**Interfaces:**
- Consumes: broadened prompt body from Task 1
- Produces: `find_architecture_and_redundancy_issues` runs without architecture on the input; `_build_prompt(index, architecture: Optional[SystemArchitecture], max_inline_chars)` handles `None` / empty arch

- [ ] **Step 1: Rewrite the failing no-doc tests**

Replace `test_returns_empty_when_no_architecture_document_or_overview` and `test_returns_empty_when_no_architecture_at_all`:

```python
def test_runs_when_no_architecture_document_or_overview() -> None:
    """Blank overview/document must not skip the pass — architecture review
    can use established repository structure without a formal document."""
    arch = SystemArchitecture(overview="", architecture_document="")
    prompts: list = []

    class _EmptyFindings(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_architecture_and_redundancy_issues(
        _EmptyFindings(), _input(architecture=arch)
    )
    assert result == []
    assert len(prompts) == 1
    assert "no formal" in prompts[0].lower() or "not provided" in prompts[0].lower()


def test_runs_when_no_architecture_at_all() -> None:
    prompts: list = []

    class _EmptyFindings(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_architecture_and_redundancy_issues(
        _EmptyFindings(), _input(architecture=None)
    )
    assert result == []
    assert len(prompts) == 1
```

- [ ] **Step 2: Run rewritten tests — expect FAIL**

```bash
backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_architecture_consistency_pass.py::test_runs_when_no_architecture_document_or_overview \
  agents/software_engineering_team/tests/test_architecture_consistency_pass.py::test_runs_when_no_architecture_at_all \
  -v
```

Expected: FAIL (`len(prompts) == 0` from early return).

- [ ] **Step 3: Implement gate removal + optional-arch prompt**

In `find_architecture_and_redundancy_issues`, delete the early-return that requires architecture document/overview/components/decisions. Pass `input_data.architecture` (possibly `None`) into `_run_pass`.

Change `_build_prompt` to take `architecture: Optional[SystemArchitecture]`. When arch text is empty/`None`, emit an explicit "no formal architecture document was provided" section; when present, keep full inline of document + rendered context. Still inline changed files + tool guidance.

Update public docstring postconditions. Update `docs/ENV_VARS.md` so `CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS` no longer implies a formal document is required.

- [ ] **Step 4: Run architecture pass suite**

```bash
backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_architecture_consistency_pass.py -v
```

Expected: PASS (except coordinator-integration cases that Task 4 will retarget if they still fail solely due to merged wiring — if coordinator still calls the standalone pass, update `test_coordinator_skips_pass_with_no_architecture` now to expect the arch anchor IS present).

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/architecture_consistency_pass.py \
  backend/agents/software_engineering_team/tests/test_architecture_consistency_pass.py \
  docs/ENV_VARS.md
git commit -m "$(cat <<'EOF'
Allow architecture-consistency pass without a formal architecture document.

Drop the document/overview early-return and teach the user prompt to state
when no document was provided so the pass can use repository structure.
EOF
)"
```

---

### Task 3: Merged pass module (TDD)

**Files:**
- Create: `backend/agents/software_engineering_team/code_review_agent/merged_architecture_side_effect_pass.py`
- Create: `backend/agents/software_engineering_team/tests/test_merged_architecture_side_effect_pass.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/__init__.py`
- Modify: `backend/agents/software_engineering_team/code_review_agent/models.py` (merged schema docstring)

**Interfaces:**
- Consumes: `MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`, `MergedArchitectureSideEffectResponse`, `side_effect_impact_pass.build_side_effect_tools` (public wrapper over `_build_side_effect_tools`), `architecture_consistency_pass._parse_findings` + `_validate_findings`, `side_effect_impact_pass._parse_findings` + `_validate_findings`, `resolve_code_review_model`, `compute_code_review_map_chunk_chars`, `CodebaseIndex`, `env_flag_enabled`
- Produces:

```python
def find_architecture_and_side_effect_issues(
    llm: LLMClient,
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader] = None,
    index: Optional[CodebaseIndex] = None,
) -> Tuple[List[CodeReviewIssue], List[CodeReviewIssue]]:
```

Returns `(architecture_findings, side_effect_findings)`.

- [ ] **Step 1: Write failing unit tests**

Create `test_merged_architecture_side_effect_pass.py` using DummyLLMClient + user-prompt anchor:

```python
_MERGED_PASS_ANCHOR = '"architecture_findings"/"side_effect_findings"'
```

Required cases:

- both env flags off -> `([], [])`, no LLM call
- non-`CODE_REVIEW` profile -> skip
- no readable files -> skip
- happy path: two-key JSON splits into correctly categorized lists
- LLM raise / gibberish / missing required key -> `([], [])`
- no architecture document still runs; user prompt mentions no formal document / not provided

- [ ] **Step 2: Run tests — expect FAIL**

```bash
backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_merged_architecture_side_effect_pass.py -v
```

Expected: FAIL (module missing).

- [ ] **Step 3: Implement the module**

Key behaviors:

- Eligibility: `CODE_REVIEW` profile, readable files, at least one of `CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS` / `CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS` enabled. No architecture-doc requirement. Do not skip solely for `pre_numbered`.
- Tools: `side_effect_impact_pass.build_side_effect_tools(index)` (or `import side_effect_impact_pass as side_pass` then `side_pass.build_side_effect_tools(index)`).
- System prompt: `MERGED_ARCHITECTURE_SIDE_EFFECT_PROMPT`.
- Parse: `json.loads` then `MergedArchitectureSideEffectResponse.model_validate`.
- Convert each half via the corresponding pass's `_parse_findings({"findings": [f.model_dump() for f in ...]})` then `_validate_findings(...)`.
- If only one env flag is on, still make the merged call but return `[]` for the disabled half.
- Fail-safe outer try/except -> `([], [])`.
- User prompt return line must contain `_MERGED_PASS_ANCHOR`.

Lazy-export `find_architecture_and_side_effect_issues` from `__init__.py`. Update `MergedArchitectureSideEffectResponse` docstring to note in-process consumption.

- [ ] **Step 4: Run merged unit tests — expect PASS**

```bash
backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_merged_architecture_side_effect_pass.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/merged_architecture_side_effect_pass.py \
  backend/agents/software_engineering_team/tests/test_merged_architecture_side_effect_pass.py \
  backend/agents/software_engineering_team/code_review_agent/__init__.py \
  backend/agents/software_engineering_team/code_review_agent/models.py
git commit -m "$(cat <<'EOF'
Add merged architecture/side-effect pass for a single LLM call.

New module runs both additive whole-submission checks once, validates via
the existing per-pass helpers, and returns the two finding lists separately.
EOF
)"
```

---

### Task 4: Wire coordinator `_run_tail_passes`

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/coordinator.py`
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_coordinator.py`
- Modify: coordinator-integration tests in `test_architecture_consistency_pass.py` and `test_side_effect_impact_pass.py`

**Interfaces:**
- Consumes: `find_architecture_and_side_effect_issues`
- Produces: `_run_tail_passes` schedules one `merged` call; unpacks `(architecture_findings, side_effect_findings)`; merge order unchanged

- [ ] **Step 1: Retarget coordinator monkeypatches (tests first)**

Replace dual monkeypatches with:

```python
def _merged(llm, input_data, repo_reader=None, index=None):
    return [arch_issue], []  # or ([], [side_effect_issue]) / record("merged")

monkeypatch.setattr(coord, "find_architecture_and_side_effect_issues", _merged)
```

Update concurrent arrivals to `["filter", "merged"]` and barrier parties from 3 to 2.

- [ ] **Step 2: Run retargeted tests — expect FAIL until wiring**

```bash
backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_coordinator.py -k "tail_pass or architecture_finding or side_effect" -v
```

- [ ] **Step 3: Implement coordinator wiring**

Import `find_architecture_and_side_effect_issues`. In `_run_tail_passes`:

```python
calls: List[Tuple[str, Callable[[], object]]] = []
if not input_data.skip_false_positive_filter:
    calls.append(("filter", lambda: filter_false_positives(...)))
calls.append(
    (
        "merged",
        lambda: find_architecture_and_side_effect_issues(
            llm, input_data, repo_reader=repo_reader, index=shared_index
        ),
    )
)
# sequential or parallel_map as today
verified = results.get("filter", genuine_issues)
architecture_findings, side_effect_findings = results["merged"]
```

Update `_run_tail_passes` and module docstrings for the merged additive pass.

- [ ] **Step 4: Fix DummyLLMClient `run_coordinator` integration tests**

Tests that branch on `_ARCH_PASS_ANCHOR` / `_SIDE_EFFECT_PASS_ANCHOR` under `run_coordinator` must use `_MERGED_PASS_ANCHOR` and return:

```python
{"architecture_findings": [...], "side_effect_findings": [...]}
```

Once-per-submission counters must count the merged call once.

- [ ] **Step 5: Run focused suites — expect PASS**

```bash
backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_coordinator.py \
  agents/software_engineering_team/tests/test_architecture_consistency_pass.py \
  agents/software_engineering_team/tests/test_side_effect_impact_pass.py \
  agents/software_engineering_team/tests/test_merged_architecture_side_effect_pass.py \
  agents/software_engineering_team/tests/test_merged_review_prompt.py \
  -v
```

- [ ] **Step 6: Commit**

```bash
git add backend/agents/software_engineering_team/code_review_agent/coordinator.py \
  backend/agents/software_engineering_team/tests/test_code_review_coordinator.py \
  backend/agents/software_engineering_team/tests/test_architecture_consistency_pass.py \
  backend/agents/software_engineering_team/tests/test_side_effect_impact_pass.py
git commit -m "$(cat <<'EOF'
Wire in-process coordinator to the merged architecture/side-effect pass.

Replace the two separate additive tail-pass calls with one merged call and
keep downstream merge order and finding categories unchanged.
EOF
)"
```

---

### Task 5: Final verification

- [ ] **Step 1: Broader code-review test surface**

```bash
cd backend
.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_code_review_coordinator.py \
  agents/software_engineering_team/tests/test_architecture_consistency_pass.py \
  agents/software_engineering_team/tests/test_side_effect_impact_pass.py \
  agents/software_engineering_team/tests/test_merged_architecture_side_effect_pass.py \
  agents/software_engineering_team/tests/test_merged_review_prompt.py \
  agents/software_engineering_team/tests/test_code_review_agent.py \
  agents/software_engineering_team/tests/test_code_review_cache.py \
  agents/software_engineering_team/tests/test_code_review_synthesis.py \
  agents/software_engineering_team/tests/test_false_positive_filter.py \
  -q
```

Expected: all PASS.

- [ ] **Step 2: Lint touched Python files**

```bash
.venv/bin/ruff check \
  agents/software_engineering_team/code_review_agent/merged_architecture_side_effect_pass.py \
  agents/software_engineering_team/code_review_agent/architecture_consistency_pass.py \
  agents/software_engineering_team/code_review_agent/side_effect_impact_pass.py \
  agents/software_engineering_team/code_review_agent/coordinator.py \
  agents/software_engineering_team/code_review_agent/prompts.py \
  agents/software_engineering_team/code_review_agent/__init__.py \
  agents/software_engineering_team/code_review_agent/models.py \
  agents/software_engineering_team/tests/test_merged_architecture_side_effect_pass.py \
  agents/software_engineering_team/tests/test_code_review_coordinator.py \
  agents/software_engineering_team/tests/test_architecture_consistency_pass.py \
  agents/software_engineering_team/tests/test_side_effect_impact_pass.py
.venv/bin/ruff format --check \
  agents/software_engineering_team/code_review_agent/merged_architecture_side_effect_pass.py \
  agents/software_engineering_team/code_review_agent/architecture_consistency_pass.py \
  agents/software_engineering_team/code_review_agent/side_effect_impact_pass.py \
  agents/software_engineering_team/code_review_agent/coordinator.py \
  agents/software_engineering_team/code_review_agent/prompts.py \
  agents/software_engineering_team/code_review_agent/__init__.py \
  agents/software_engineering_team/code_review_agent/models.py \
  agents/software_engineering_team/tests/test_merged_architecture_side_effect_pass.py \
  agents/software_engineering_team/tests/test_code_review_coordinator.py \
  agents/software_engineering_team/tests/test_architecture_consistency_pass.py \
  agents/software_engineering_team/tests/test_side_effect_impact_pass.py
```

- [ ] **Step 3: Commit any leftover lint/doc fixes only if needed**

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Broaden architecture body for no-doc review | Task 1 |
| Drop architecture-document early-return | Task 2 |
| New merged module + split response | Task 3 |
| Coordinator invokes merged; one fewer LLM call | Task 4 |
| Downstream finding categories unchanged | Task 3 validators + Task 4 merge order |
| Tests for coordinator / architecture / merged | Tasks 2-5 |
| Temporal out of scope | Global Constraints |
| Decision B (merged whenever either flag on) | Task 3 eligibility |

## Type consistency

- Entry name: `find_architecture_and_side_effect_issues` everywhere
- Return type: `Tuple[List[CodeReviewIssue], List[CodeReviewIssue]]`
- Env vars: `CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS`, `CODE_REVIEW_SIDE_EFFECT_IMPACT_PASS`
- Merged user-prompt anchor: `"architecture_findings"/"side_effect_findings"`

---

## Status note (post-completion)

This plan's Global Constraint against modifying Temporal activities/workflows applied only to this plan's own scope (the in-process coordinator). Temporal parity for the same merge order — a merged-pass activity plus a sequential combine → re-verify reorder, gated behind `_REORDERED_TAIL_PASSES_PATCH` for old-history replay compatibility — has since been completed as separate work; see `code_review_agent/temporal/workflows.py`'s module docstring for the durable-mode pipeline and `tests/test_code_review_temporal.py` for its replay/determinism coverage. Nothing in this document's tasks above changed as a result.
