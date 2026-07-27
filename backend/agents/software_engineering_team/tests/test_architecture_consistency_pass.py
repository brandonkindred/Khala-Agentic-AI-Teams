"""Tests for the architecture-consistency / cross-codebase-redundancy pass.

This pass is purely additive (it can only ADD findings on top of what the map
phase and false-positive filter already produced) and fail-safe (any setup or
LLM failure yields no additional findings, never an exception). Style mirrors
``test_false_positive_filter.py``: the LLM seam is exercised with
``DummyLLMClient`` subclasses that pattern-match on the user prompt (never the
system prompt -- see that file's rationale) so one scripted client can serve
both the chunk-review call and this pass's call in an end-to-end
``run_coordinator`` run.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest
from code_review_agent.architecture_consistency_pass import (
    _ARCH_DOC_ABS_CHARS,
    _build_prompt,
    _coerce_finding,
    _is_changed_file,
    _parse_findings,
    _validate_finding_line,
    _validate_findings,
    find_architecture_and_redundancy_issues,
)
from code_review_agent.coordinator import run_coordinator
from code_review_agent.false_positive_filter import CodebaseIndex
from code_review_agent.models import CodeReviewInput, CodeReviewIssue

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.shared.models import ArchitectureComponent, SystemArchitecture

# Unique anchor in this pass's user prompt (never the system prompt -- a
# DummyLLMClient subclass must branch on the user prompt only, matching the
# false-positive filter's established rationale for avoiding system-prompt
# scanning cross-contamination).
_ARCH_PASS_ANCHOR = '"findings" array as instructed'


def _arch(
    *,
    overview: str = "Layered service architecture.",
    architecture_document: str = "",
) -> SystemArchitecture:
    return SystemArchitecture(overview=overview, architecture_document=architecture_document)


def _input(
    files: Optional[Dict[str, str]] = None, architecture: Optional[SystemArchitecture] = None
) -> CodeReviewInput:
    return CodeReviewInput(
        files=files if files is not None else {"app/main.py": "def bar():\n    return 1\n"},
        task_description="wire up bar",
        architecture=architecture,
    )


# --------------------------------------------------------------------------- helpers


def test_build_prompt_includes_architecture_document_and_changed_files() -> None:
    """The prompt inlines the architecture document, the file manifest, and each
    changed file's content."""
    arch = _arch(architecture_document="# Arch\nAll writes MUST go through the repository layer.")
    index = CodebaseIndex.from_input(_input(architecture=arch))
    prompt = _build_prompt(index, arch, max_inline_chars=100_000, max_arch_doc_chars=100_000)
    assert "All writes MUST go through the repository layer." in prompt
    assert "app/main.py" in prompt
    assert "def bar():" in prompt


def test_build_prompt_falls_back_to_overview_with_no_document() -> None:
    """With no ``architecture_document``, the overview is inlined instead."""
    arch = _arch(overview="Overview-only architecture.", architecture_document="")
    index = CodebaseIndex.from_input(_input(architecture=arch))
    prompt = _build_prompt(index, arch, max_inline_chars=100_000, max_arch_doc_chars=100_000)
    assert "Overview-only architecture." in prompt


def test_build_prompt_includes_components_and_decisions_alongside_document() -> None:
    """Regression test: components/decisions must reach this once-per-submission
    pass's prompt even when a full architecture_document is also present --
    previously the prompt only ever inlined architecture_document-or-overview,
    so an explicit component boundary or ADR was invisible to the one place
    that can verify it against the whole repository."""
    arch = SystemArchitecture(
        overview="Layered service architecture.",
        architecture_document="# Arch\nGeneral overview doc.",
        components=[
            ArchitectureComponent(
                name="billing-service", type="backend", description="Owns all billing writes."
            )
        ],
        decisions=[
            {"title": "ADR-003", "decision": "All billing writes go through billing-service."}
        ],
    )
    index = CodebaseIndex.from_input(_input(architecture=arch))
    prompt = _build_prompt(index, arch, max_inline_chars=100_000, max_arch_doc_chars=100_000)
    assert "General overview doc." in prompt
    assert "billing-service" in prompt
    assert "Owns all billing writes." in prompt
    assert "ADR-003" in prompt
    assert "All billing writes go through billing-service." in prompt


def test_build_prompt_includes_components_and_decisions_with_no_document() -> None:
    """Components/decisions reach the prompt even with no architecture_document
    and no overview (the early-return guard must not skip the pass either --
    see test_returns_non_empty_when_only_components_or_decisions_present)."""
    arch = SystemArchitecture(
        overview="",
        components=[ArchitectureComponent(name="auth-service", type="backend")],
    )
    index = CodebaseIndex.from_input(_input(architecture=arch))
    prompt = _build_prompt(index, arch, max_inline_chars=100_000, max_arch_doc_chars=100_000)
    assert "auth-service" in prompt


def test_build_prompt_honors_architecture_document_budget() -> None:
    arch = _arch(architecture_document="X" * 10_000)
    index = CodebaseIndex.from_input(_input(architecture=arch))
    prompt = _build_prompt(index, arch, max_inline_chars=100_000, max_arch_doc_chars=100)
    assert "X" * 100 in prompt
    assert "X" * 101 not in prompt
    assert "Only the first 100 characters of the architecture document are shown" in prompt


def test_build_prompt_omits_files_beyond_inline_budget() -> None:
    """Changed files beyond the inline budget are named as tool-reachable, not dropped."""
    arch = _arch()
    file_a_content = "x" * 50
    files = {"a.py": file_a_content, "b.py": "y" * 50}
    index = CodebaseIndex.from_input(_input(files=files, architecture=arch))
    # Budget computed from the first file's own size (not an independent magic
    # number that happens to match it) so the second file is fully omitted
    # rather than partially truncated -- see test_build_prompt_notes_mid_file_truncation
    # for that other branch.
    prompt = _build_prompt(
        index, arch, max_inline_chars=len(file_a_content), max_arch_doc_chars=1000
    )
    assert file_a_content in prompt  # inlined in full (fits the budget exactly)
    assert "more changed file(s) not shown above" in prompt
    assert "list_files()" in prompt


def test_build_prompt_notes_mid_file_truncation() -> None:
    """A file whose content is cut off mid-way by the shared inline budget gets
    its own truncation notice, not just wholly-omitted files."""
    arch = _arch()
    files = {"a.py": "x" * 100}
    index = CodebaseIndex.from_input(_input(files=files, architecture=arch))
    prompt = _build_prompt(index, arch, max_inline_chars=30, max_arch_doc_chars=1000)
    assert "Only the first 30 characters of `a.py` are shown above" in prompt


# --------------------------------------------------------------------------- line bounds


def test_validate_finding_line_keeps_in_range_line() -> None:
    index = CodebaseIndex.from_input(_input(files={"a.py": "one\ntwo\nthree\n"}))
    assert _validate_finding_line(index, "a.py", 2) == 2


def test_validate_finding_line_drops_out_of_range_line() -> None:
    index = CodebaseIndex.from_input(_input(files={"a.py": "one\ntwo\nthree\n"}))
    assert _validate_finding_line(index, "a.py", 9999) is None


def test_validate_finding_line_drops_when_file_unresolved() -> None:
    index = CodebaseIndex.from_input(_input(files={"a.py": "one\ntwo\n"}))
    assert _validate_finding_line(index, "does/not/exist.py", 1) is None


def test_validate_finding_line_passes_through_none() -> None:
    index = CodebaseIndex.from_input(_input(files={"a.py": "one\n"}))
    assert _validate_finding_line(index, "a.py", None) is None


def test_validate_finding_line_survives_file_content_starting_with_error() -> None:
    """A real file whose content starts with "Error:" must not be treated as an unreadable file."""
    index = CodebaseIndex.from_input(_input(files={"a.py": "Error: not a real failure\ntwo\n"}))
    assert _validate_finding_line(index, "a.py", 2) == 2
    assert _validate_finding_line(index, "a.py", 9999) is None


def test_validate_finding_line_drops_any_line_for_empty_file() -> None:
    """An empty file has zero real lines, so no citation -- not even line 1 --
    can be bounded against its actual content."""
    index = CodebaseIndex.from_input(_input(files={"a.py": ""}))
    assert _validate_finding_line(index, "a.py", 1) is None


def test_validate_finding_line_trusts_pre_numbered_citation_as_is() -> None:
    """PR hunk review mode shows only a few hunk lines, each prefixed with its
    ORIGINAL absolute line number as text (e.g. "4242: ..."); the hunk's
    physical line count bears no relation to that cited number, so it must be
    trusted as-is rather than bounds-checked against the physical count."""
    index = CodebaseIndex.from_input(
        CodeReviewInput(
            files={"a.py": "4242: one\n4243: two\n"},
            task_description="t",
            pre_numbered=True,
        )
    )
    assert _validate_finding_line(index, "a.py", 4242, pre_numbered=True) == 4242
    # None still passes through unchanged.
    assert _validate_finding_line(index, "a.py", None, pre_numbered=True) is None


def test_validate_findings_nulls_only_out_of_range_lines() -> None:
    index = CodebaseIndex.from_input(_input(files={"a.py": "one\ntwo\nthree\n"}))
    in_range = CodeReviewIssue(category="architecture", description="d1", file_path="a.py", line=2)
    out_of_range = CodeReviewIssue(
        category="refactor", description="d2", file_path="a.py", line=9999
    )
    validated = _validate_findings(index, [in_range, out_of_range])
    assert validated[0].line == 2
    assert validated[1].line is None
    # The rest of the finding is untouched -- only the hallucinated line is dropped.
    assert validated[1].description == "d2"


def test_validate_findings_trusts_pre_numbered_citations() -> None:
    """The same "physical line count bears no relation to the cited absolute
    number" exemption applies through the full ``_validate_findings`` path,
    not just the standalone line-check helper."""
    index = CodebaseIndex.from_input(
        CodeReviewInput(
            files={"a.py": "4242: one\n4243: two\n"},
            task_description="t",
            pre_numbered=True,
        )
    )
    finding = CodeReviewIssue(
        category="architecture", description="d1", file_path="a.py", line=4242
    )
    validated = _validate_findings(index, [finding], pre_numbered=True)
    assert validated[0].line == 4242


class _FakeReader:
    """A minimal duck-typed RepoReader over an in-memory {path: content} map."""

    def __init__(self, files: Dict[str, str]):
        self._files = files

    def list_files(self):
        return list(self._files)

    def read_file(self, path: str):
        return self._files.get((path or "").strip())


def test_is_changed_file_true_only_for_submission_files() -> None:
    index = CodebaseIndex(
        files={"app/main.py": "code"},
        repo_reader=_FakeReader({"app/existing_helper.py": "helper code"}),
    )
    assert _is_changed_file(index, "app/main.py") is True
    # A file reachable only via repo_reader (already exists, not part of this
    # change) is NOT a changed file, even though read_file/resolve_path can see it.
    assert _is_changed_file(index, "app/existing_helper.py") is False
    assert _is_changed_file(index, "") is False
    assert _is_changed_file(index, CodebaseIndex.EXISTING_CODEBASE_PATH) is False


def test_is_changed_file_resolves_a_basename_alias_of_a_changed_file() -> None:
    """A unique basename/suffix alias of a changed file (e.g. ``main.py`` for
    ``app/main.py``) must resolve as changed, matching
    ``CodebaseIndex.resolve_path``'s own alias support -- otherwise a valid
    finding anchored by alias is wrongly treated as outside the diff."""
    index = CodebaseIndex(files={"app/main.py": "code"})
    assert _is_changed_file(index, "main.py") is True


def test_validate_findings_normalizes_a_changed_file_alias_to_its_real_key() -> None:
    """A finding anchored by a basename/suffix alias of a changed file is kept
    AND its ``file_path`` is normalized to the submission's real key, so PR
    comment placement is exact rather than merely "not blanked"."""
    index = CodebaseIndex(files={"app/main.py": "def bar():\n    return 1\n"})
    finding = CodeReviewIssue(
        category="architecture", description="d1", file_path="main.py", line=1
    )
    validated = _validate_findings(index, [finding])
    assert validated[0].file_path == "app/main.py"
    assert validated[0].line == 1


def test_validate_findings_blanks_file_path_anchored_outside_the_diff() -> None:
    """Regression test: a cross-codebase-redundancy finding that cites the
    EXISTING file it found the duplicate in (rather than the changed file that
    should be fixed) cannot become a useful PR comment -- that file is not part
    of the diff. The finding is kept, but degraded to a submission-wide one."""
    index = CodebaseIndex(
        files={"app/new_queue.py": "code"},
        repo_reader=_FakeReader({"app/existing_queue.py": "class Queue: ...\n"}),
    )
    outside_diff = CodeReviewIssue(
        category="refactor",
        description="duplicates app/existing_queue.py's Queue",
        file_path="app/existing_queue.py",
        line=1,
    )
    inside_diff = CodeReviewIssue(
        category="architecture", description="d1", file_path="app/new_queue.py", line=1
    )
    validated = _validate_findings(index, [outside_diff, inside_diff])
    assert validated[0].file_path == ""
    assert validated[0].line is None
    assert validated[0].description == "duplicates app/existing_queue.py's Queue"  # kept
    # A finding already anchored inside the diff is untouched by this check.
    assert validated[1].file_path == "app/new_queue.py"
    assert validated[1].line == 1


def test_finds_and_returns_new_findings_drops_hallucinated_line() -> None:
    """End-to-end: a finding citing a line beyond the real file's length has its
    line anchor nulled rather than trusted verbatim."""

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                return {
                    "findings": [
                        {
                            "severity": "high",
                            "category": "architecture",
                            "file_path": "app/main.py",
                            "line": 9999,
                            "description": "bypasses the repository layer",
                            "suggestion": "use the repository",
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok"}

    result = find_architecture_and_redundancy_issues(
        _FindingsClient(),
        _input(files={"app/main.py": "def bar():\n    return 1\n"}, architecture=_arch()),
    )
    assert len(result) == 1
    assert result[0].line is None  # line 9999 doesn't exist in a 2-line file


# --------------------------------------------------------------------------- parsing


def test_coerce_finding_accepts_architecture_and_refactor_categories() -> None:
    """``_coerce_finding`` accepts both ``architecture`` and ``refactor`` categories."""
    arch_finding = _coerce_finding(
        {
            "severity": "high",
            "category": "architecture",
            "file_path": "app/main.py",
            "description": "bypasses the repository layer",
            "suggestion": "use the repository",
        }
    )
    assert arch_finding is not None
    assert arch_finding.category == "architecture"
    assert arch_finding.severity == "high"

    refactor_finding = _coerce_finding(
        {
            "category": "refactor",
            "description": "duplicates existing HttpClient wrapper",
        }
    )
    assert refactor_finding is not None
    assert refactor_finding.category == "refactor"
    assert refactor_finding.severity == "medium"  # default when unrecognized/absent


@pytest.mark.parametrize(
    "item",
    [
        "not-a-dict",
        {"category": "logic", "description": "wrong category for this pass"},
        {"category": "architecture", "description": ""},
        {"category": "", "description": "no category at all"},
        {
            "category": "architecture",
            "description": "matches the established pattern",
            "suggestion": "No changes needed.",
        },
        {
            "category": "refactor",
            "description": "no real duplicate found",
            "suggestion": "no action required",
        },
    ],
)
def test_coerce_finding_rejects_invalid_items(item: object) -> None:
    assert _coerce_finding(item) is None


def test_coerce_finding_coerces_line_and_unknown_severity() -> None:
    finding = _coerce_finding(
        {
            "severity": "not-a-real-severity",
            "category": "architecture",
            "description": "d",
            "line": "42",
        }
    )
    assert finding is not None
    assert finding.severity == "medium"
    assert finding.line == 42


def test_parse_findings_handles_off_contract_replies() -> None:
    assert _parse_findings("not-a-dict") == []
    assert _parse_findings({}) == []
    assert _parse_findings({"findings": "not-a-list"}) == []
    assert _parse_findings({"findings": []}) == []
    parsed = _parse_findings(
        {
            "findings": [
                {"category": "architecture", "description": "real"},
                {"category": "bogus", "description": "dropped"},
                "not-a-dict-either",
            ]
        }
    )
    assert len(parsed) == 1
    assert parsed[0].description == "real"


# --------------------------------------------------------------------------- gating / fail-safe


def test_returns_empty_when_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")
    result = find_architecture_and_redundancy_issues(DummyLLMClient(), _input(architecture=_arch()))
    assert result == []


def test_returns_empty_when_no_architecture_document_or_overview() -> None:
    arch = SystemArchitecture(overview="", architecture_document="")
    result = find_architecture_and_redundancy_issues(DummyLLMClient(), _input(architecture=arch))
    assert result == []


def test_runs_when_only_components_present_with_no_overview_or_document() -> None:
    """Regression test: the early-return guard must not skip the pass just
    because overview/architecture_document are both blank -- components alone
    (a normalized SystemArchitecture field) are enough to check against."""
    arch = SystemArchitecture(
        overview="",
        architecture_document="",
        components=[ArchitectureComponent(name="auth-service", type="backend")],
    )
    prompts: list = []

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok"}

    find_architecture_and_redundancy_issues(_FindingsClient(), _input(architecture=arch))
    # The pass actually ran (the guard did not short-circuit before any LLM call).
    assert len(prompts) == 1
    assert "auth-service" in prompts[0]


def test_returns_empty_when_no_architecture_at_all() -> None:
    result = find_architecture_and_redundancy_issues(DummyLLMClient(), _input(architecture=None))
    assert result == []


def test_returns_empty_when_submission_has_no_readable_files() -> None:
    result = find_architecture_and_redundancy_issues(
        DummyLLMClient(), _input(files={"empty.py": "   "}, architecture=_arch())
    )
    assert result == []


def test_fails_safe_on_llm_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any LLM/setup failure is swallowed -- this pass must never break the review."""

    class _Raiser(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("boom")

    result = find_architecture_and_redundancy_issues(_Raiser(), _input(architecture=_arch()))
    assert result == []


def test_fails_safe_on_unparsable_reply() -> None:
    class _Gibberish(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return "not even a dict-shaped reply"  # type: ignore[return-value]

    result = find_architecture_and_redundancy_issues(_Gibberish(), _input(architecture=_arch()))
    assert result == []


def test_returns_empty_for_non_code_review_profile() -> None:
    """Only the default CODE_REVIEW profile runs this pass. The other profiles
    (ACCEPTANCE, SPEC_CONFORMANCE, ...) expect every issue to be attributable to
    a specific criterion/requirement, which an architecture/refactor finding
    never is -- e.g. AcceptanceVerifierAgent treats any unattributed issue as an
    unmet criterion, so this pass could otherwise spuriously fail acceptance
    verification even when every criterion is satisfied."""
    from code_review_agent.models import CodeReviewInput
    from code_review_agent.profiles import ReviewProfile

    class _FailIfAskedClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _ARCH_PASS_ANCHOR not in prompt, "architecture pass should not run"
            return {"approved": True, "issues": [], "summary": "ok"}

    result = find_architecture_and_redundancy_issues(
        _FailIfAskedClient(),
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 1\n"},
            task_description="wire up bar",
            architecture=_arch(),
            profile=ReviewProfile.ACCEPTANCE,
        ),
    )
    assert result == []


# --------------------------------------------------------------------------- happy path


def test_finds_and_returns_new_findings() -> None:
    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                return {
                    "findings": [
                        {
                            "severity": "high",
                            "category": "architecture",
                            "file_path": "app/main.py",
                            "description": "bypasses the repository layer",
                            "suggestion": "use the repository",
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok"}

    result = find_architecture_and_redundancy_issues(
        _FindingsClient(), _input(architecture=_arch())
    )
    assert len(result) == 1
    assert result[0].category == "architecture"
    assert result[0].description == "bypasses the repository layer"


def test_finds_and_returns_new_findings_with_pre_numbered_input() -> None:
    """End-to-end: a finding citing a line far past the shown hunk's physical
    length survives under ``pre_numbered=True`` (PR hunk review mode), proving
    the flag reaches ``_run_pass``/``_validate_findings`` and is not lost
    along the way."""
    from code_review_agent.models import CodeReviewInput

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                return {
                    "findings": [
                        {
                            "severity": "high",
                            "category": "architecture",
                            "file_path": "app/main.py",
                            "line": 4242,
                            "description": "bypasses the repository layer",
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok"}

    result = find_architecture_and_redundancy_issues(
        _FindingsClient(),
        CodeReviewInput(
            files={"app/main.py": "4242: def bar():\n4243:     return 1\n"},
            task_description="wire up bar",
            architecture=_arch(),
            pre_numbered=True,
        ),
    )
    assert len(result) == 1
    assert result[0].line == 4242  # not nulled by the 2-physical-line hunk's length


# --------------------------------------------------------------------------- coordinator integration


def test_coordinator_runs_pass_once_per_submission_not_per_chunk() -> None:
    """The pass fires exactly once per submission, regardless of chunk count."""
    calls = {"arch_pass": 0, "chunk_review": 0}

    class _CountingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                calls["arch_pass"] += 1
                return {"findings": []}
            calls["chunk_review"] += 1
            return {"approved": True, "issues": [], "summary": "ok"}

    # Two files -> at least the map phase runs more than once; the false-positive
    # filter is disabled by default (its own env toggle) so this isolates the count.
    files = {"a.py": "def a():\n    return 1\n", "b.py": "def b():\n    return 2\n"}
    run_coordinator(_CountingClient(), CodeReviewInput(files=files, architecture=_arch()))

    assert calls["arch_pass"] == 1


def test_coordinator_merges_architecture_findings_into_final_output() -> None:
    """A finding from this pass reaches the coordinator's merged ``issues`` list,
    dedupes/sizes alongside chunk findings, and does not spuriously block approval
    (default severity keeps it out of the critical/high gate)."""

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                return {
                    "findings": [
                        {
                            "severity": "medium",
                            "category": "refactor",
                            "file_path": "app/main.py",
                            "description": "duplicates the existing HttpClient wrapper",
                            "suggestion": "reuse shared.http_client.HttpClient",
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok"}

    result = run_coordinator(
        _FindingsClient(),
        CodeReviewInput(files={"app/main.py": "def bar():\n    return 1\n"}, architecture=_arch()),
    )
    assert result.approved  # a medium refactor finding never blocks approval alone
    assert any(
        i.category == "refactor" and "HttpClient wrapper" in i.description for i in result.issues
    )


def test_coordinator_skips_pass_with_no_architecture() -> None:
    """No architecture on the input -> the pass contributes nothing (no extra
    LLM call beyond the ordinary map-phase chunk review)."""

    class _FailIfAskedClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _ARCH_PASS_ANCHOR not in prompt, "architecture pass should not run"
            return {"approved": True, "issues": [], "summary": "ok"}

    result = run_coordinator(_FailIfAskedClient(), CodeReviewInput(code="def f():\n    return 1\n"))
    assert result.approved


def test_run_pass_shrinks_code_budget_by_arch_doc_reserve(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inline code budget must shrink by however much
    ``CODE_REVIEW_ARCH_DOC_CHARS`` exceeds what
    ``compute_code_review_map_chunk_chars`` already reserved for an
    architecture excerpt, so a generous document budget cannot silently push
    the combined prompt past the model's context-derived ceiling."""
    import code_review_agent.architecture_consistency_pass as pass_mod

    captured: Dict[str, Any] = {}
    original_build_prompt = pass_mod._build_prompt

    def _spy(index, architecture, max_inline_chars, max_arch_doc_chars):
        captured["max_inline_chars"] = max_inline_chars
        captured["max_arch_doc_chars"] = max_arch_doc_chars
        return original_build_prompt(index, architecture, max_inline_chars, max_arch_doc_chars)

    monkeypatch.setattr(pass_mod, "_build_prompt", _spy)
    monkeypatch.setattr(pass_mod, "compute_code_review_map_chunk_chars", lambda llm: 20_000)
    monkeypatch.setattr(pass_mod, "compute_code_review_arch_overview_chars", lambda llm: 1_000)
    monkeypatch.setenv("CODE_REVIEW_ARCH_DOC_CHARS", "9000")

    class _EmptyClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    find_architecture_and_redundancy_issues(_EmptyClient(), _input(architecture=_arch()))

    assert captured["max_arch_doc_chars"] == 9000
    # 20_000 - max(9_000 - 1_000, 0) == 12_000
    assert captured["max_inline_chars"] == 12_000


def test_run_pass_clamps_arch_doc_when_it_would_still_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``CODE_REVIEW_ARCH_DOC_CHARS`` larger than the model's total
    context-derived budget must not be inlined in full -- on a smaller-context
    model even a code budget of zero would not save the combined prompt from
    overflow, so the document itself is clamped to at most half of the total
    available budget."""
    import code_review_agent.architecture_consistency_pass as pass_mod

    captured: Dict[str, Any] = {}
    original_build_prompt = pass_mod._build_prompt

    def _spy(index, architecture, max_inline_chars, max_arch_doc_chars):
        captured["max_inline_chars"] = max_inline_chars
        captured["max_arch_doc_chars"] = max_arch_doc_chars
        return original_build_prompt(index, architecture, max_inline_chars, max_arch_doc_chars)

    monkeypatch.setattr(pass_mod, "_build_prompt", _spy)
    monkeypatch.setattr(pass_mod, "compute_code_review_map_chunk_chars", lambda llm: 6_000)
    monkeypatch.setattr(pass_mod, "compute_code_review_arch_overview_chars", lambda llm: 2_000)
    monkeypatch.setenv("CODE_REVIEW_ARCH_DOC_CHARS", "50000")  # far past the 8_000 total

    class _EmptyClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    find_architecture_and_redundancy_issues(_EmptyClient(), _input(architecture=_arch()))

    # available_total = 6_000 + 2_000 == 8_000; clamped to half instead of the full 50_000.
    assert captured["max_arch_doc_chars"] == 4_000
    assert captured["max_inline_chars"] == 4_000


def test_arch_doc_abs_chars_default_is_generous() -> None:
    """Sanity check the default cap: generous relative to the per-chunk overview
    excerpt cap, since this pass pays its cost once per submission, not per chunk.

    Compared against the actual per-chunk cap constant (not an independent magic
    number) so this test states its real intent and does not silently drift out
    of sync if either constant is retuned.
    """
    from software_engineering_team.shared.context_sizing import (
        CODE_REVIEW_ARCH_OVERVIEW_ABS_CHARS,
    )

    assert _ARCH_DOC_ABS_CHARS >= CODE_REVIEW_ARCH_OVERVIEW_ABS_CHARS * 5
