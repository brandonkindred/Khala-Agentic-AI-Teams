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
from shared.dev_models.models import ArchitectureComponent, SystemArchitecture

# Unique anchor in this pass's user prompt (never the system prompt -- a
# DummyLLMClient subclass must branch on the user prompt only, matching the
# false-positive filter's established rationale for avoiding system-prompt
# scanning cross-contamination).
_ARCH_PASS_ANCHOR = '"findings" array as instructed'
_MERGED_PASS_ANCHOR = '"architecture_findings"/"side_effect_findings"'

_SIDE_EFFECT_PASS_ANCHOR = '"side-effects"/"documentation" findings array'


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
    prompt = _build_prompt(
        index,
        arch,
        max_inline_chars=100_000,
        max_architecture_chars=100_000,
        max_manifest_chars=100_000,
    )
    assert "All writes MUST go through the repository layer." in prompt
    assert "app/main.py" in prompt
    assert "def bar():" in prompt


def test_build_prompt_falls_back_to_overview_with_no_document() -> None:
    """With no ``architecture_document``, the overview is inlined instead."""
    arch = _arch(overview="Overview-only architecture.", architecture_document="")
    index = CodebaseIndex.from_input(_input(architecture=arch))
    prompt = _build_prompt(
        index,
        arch,
        max_inline_chars=100_000,
        max_architecture_chars=100_000,
        max_manifest_chars=100_000,
    )
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
    prompt = _build_prompt(
        index,
        arch,
        max_inline_chars=100_000,
        max_architecture_chars=100_000,
        max_manifest_chars=100_000,
    )
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
    prompt = _build_prompt(
        index,
        arch,
        max_inline_chars=100_000,
        max_architecture_chars=100_000,
        max_manifest_chars=100_000,
    )
    assert "auth-service" in prompt


def test_build_prompt_includes_architecture_document_in_full_when_budget_allows() -> None:
    """The architecture document is inlined in full when ``max_architecture_chars``
    is at least its size -- no tool exposes it, so a real truncation (tested
    separately) is the only way part of it goes missing."""
    arch = _arch(overview="", architecture_document="X" * 10_000)
    index = CodebaseIndex.from_input(_input(architecture=arch))
    prompt = _build_prompt(
        index,
        arch,
        max_inline_chars=100_000,
        max_architecture_chars=10_000,
        max_manifest_chars=100_000,
    )
    assert "X" * 10_000 in prompt
    assert "are shown above" not in prompt
    assert "was not available to this pass" not in prompt


def test_build_prompt_truncates_architecture_document_beyond_its_budget() -> None:
    """When the architecture document exceeds ``max_architecture_chars``, it is
    cut to that budget with a tool-unreachable omission note (unlike the
    changed-file content budget, no tool can recover the rest of the doc)."""
    arch = _arch(overview="", architecture_document="X" * 10_000)
    index = CodebaseIndex.from_input(_input(architecture=arch))
    prompt = _build_prompt(
        index,
        arch,
        max_inline_chars=100_000,
        max_architecture_chars=100,
        max_manifest_chars=100_000,
    )
    assert "X" * 100 in prompt
    assert "X" * 101 not in prompt
    assert "the remainder was omitted to fit the model context" in prompt


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
        index,
        arch,
        max_inline_chars=len(file_a_content),
        max_architecture_chars=100_000,
        max_manifest_chars=100_000,
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
    prompt = _build_prompt(
        index, arch, max_inline_chars=30, max_architecture_chars=100_000, max_manifest_chars=100_000
    )
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
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_architecture_and_redundancy_issues(
        _FindingsClient(),
        _input(files={"app/main.py": "def bar():\n    return 1\n"}, architecture=_arch()),
    )
    assert len(result) == 1
    assert result[0].line is None  # line 9999 doesn't exist in a 2-line file


# --------------------------------------------------------------------------- parsing


def test_coerce_finding_accepts_architecture_and_refactor_categories() -> None:
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


def test_coerce_finding_carries_through_pre_existing_tag() -> None:
    """The model's optional pre_existing tag (used by the PR-review whole-file
    path to route an architecture/refactor finding about a field, function, or
    class this submission did NOT add or modify to a human-review proposal
    instead of a blocking PR comment) survives conversion, tolerates string
    encodings, and defaults False when absent -- mirrors
    side_effect_impact_pass._coerce_finding's identical convention."""
    tagged_true = _coerce_finding(
        {"category": "architecture", "description": "d1", "pre_existing": True}
    )
    tagged_str = _coerce_finding(
        {"category": "architecture", "description": "d2", "pre_existing": "true"}
    )
    tagged_false_str = _coerce_finding(
        {"category": "refactor", "description": "d3", "pre_existing": "false"}
    )
    untagged = _coerce_finding({"category": "refactor", "description": "d4"})
    assert [f.pre_existing for f in (tagged_true, tagged_str, tagged_false_str, untagged)] == [
        True,
        True,
        False,
        False,
    ]


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


def test_runs_when_no_architecture_document_or_overview() -> None:
    """Blank overview/document must not skip the pass — architecture review
    can use established repository structure without a formal document when
    repository evidence (e.g. existing_codebase) is available."""
    arch = SystemArchitecture(overview="", architecture_document="")
    prompts: list = []

    class _EmptyFindings(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_architecture_and_redundancy_issues(
        _EmptyFindings(),
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 1\n"},
            task_description="wire up bar",
            architecture=arch,
            existing_codebase="existing/shared_helper.py\n",
        ),
    )
    assert result == []
    assert len(prompts) == 1
    assert "no formal" in prompts[0].lower() or "not provided" in prompts[0].lower()


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
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    find_architecture_and_redundancy_issues(_FindingsClient(), _input(architecture=arch))
    # The pass actually ran (the guard did not short-circuit before any LLM call).
    assert len(prompts) == 1
    assert "auth-service" in prompts[0]


def test_runs_when_no_architecture_at_all() -> None:
    prompts: list = []

    class _EmptyFindings(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    # No formal architecture document, but an existing-codebase excerpt gives
    # the pass repository evidence to derive established structure from.
    result = find_architecture_and_redundancy_issues(
        _EmptyFindings(),
        CodeReviewInput(
            files={"app/main.py": "def bar():\n    return 1\n"},
            task_description="wire up bar",
            architecture=None,
            existing_codebase="existing/shared_helper.py\n",
        ),
    )
    assert result == []
    assert len(prompts) == 1


def test_skips_when_no_architecture_evidence() -> None:
    """Without a document, repo_reader, or existing_codebase, tools only see
    the changed submission — do not ask the model to invent architecture rules."""

    class _FailIfAsked(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            assert _ARCH_PASS_ANCHOR not in prompt, "architecture pass should not run"
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_architecture_and_redundancy_issues(_FailIfAsked(), _input(architecture=None))
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
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

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
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_architecture_and_redundancy_issues(
        _FindingsClient(), _input(architecture=_arch())
    )
    assert len(result) == 1
    assert result[0].category == "architecture"
    assert result[0].description == "bypasses the repository layer"


def test_finds_and_returns_new_findings_tags_pre_existing() -> None:
    """End-to-end: a finding the model tags pre_existing=true (e.g. an
    architecture/refactor complaint about a field that already existed before
    this submission, in a file the submission also touched elsewhere) carries
    that tag all the way through to the returned CodeReviewIssue, so the
    PR-review whole-file path can route it to a human-review proposal instead
    of posting it as a blocking PR comment."""

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                return {
                    "findings": [
                        {
                            "severity": "medium",
                            "category": "refactor",
                            "file_path": "app/main.py",
                            "description": "duplicates an existing field untouched by this change",
                            "suggestion": "reuse the existing field",
                            "pre_existing": True,
                        }
                    ]
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_architecture_and_redundancy_issues(
        _FindingsClient(), _input(architecture=_arch())
    )
    assert len(result) == 1
    assert result[0].pre_existing is True


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
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

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


def test_finds_and_returns_new_findings_bounds_checks_when_full_content_supplied() -> None:
    """When ``full_content`` covers the pre-numbered submission, the citation
    IS bounds-checked against the real full body (not trusted as-is): the
    same out-of-range line that survives under bare ``pre_numbered=True`` is
    now nulled, proving the effective flag (``pre_numbered and not
    full_content_complete``), not the raw ``input_data.pre_numbered``, reaches
    ``_run_pass``/``_validate_findings`` here too -- matching the fix already
    applied to the side-effect and merged passes."""

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
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = find_architecture_and_redundancy_issues(
        _FindingsClient(),
        CodeReviewInput(
            files={"app/main.py": "4242: def bar():\n4243:     return 1\n"},
            task_description="wire up bar",
            architecture=_arch(),
            pre_numbered=True,
            full_content={"app/main.py": "def bar():\n    return 1\n"},
        ),
    )
    assert len(result) == 1
    assert result[0].line is None  # nulled: real file is 2 lines, 4242 is out of range


# --------------------------------------------------------------------------- batching / reactive recovery


def test_no_extra_batching_for_small_multi_file_submission_under_budget() -> None:
    """Several small files that together still fit the per-call budget must
    make exactly one LLM call, not one per file -- no behavior change for
    submissions under the budget."""
    prompts: list = []

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {
        "a.py": "def a():\n    return 1\n",
        "b.py": "def b():\n    return 2\n",
        "c.py": "def c():\n    return 3\n",
    }
    find_architecture_and_redundancy_issues(_Client(), _input(files=files, architecture=_arch()))
    assert len(prompts) == 1


def test_splits_into_multiple_batches_for_oversized_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the changed-file set's total estimated size exceeds one call's
    inline-code budget, the pass issues one independent LLM call per batch and
    concatenates each batch's findings into a single list."""
    import code_review_agent.submission_pass_runner as runner_mod

    from software_engineering_team.shared.context_sizing import MergedPassBudgets

    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: MergedPassBudgets(
            max_architecture_chars=100_000,
            max_inline_code_chars=200,
            max_manifest_chars=2_000,
            reserved_response_tokens=4096,
        ),
    )

    prompts: list = []

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                for path in ("a.py", "b.py", "c.py"):
                    if f"### {path} ###" in prompt:
                        return {
                            "findings": [
                                {
                                    "severity": "medium",
                                    "category": "architecture",
                                    "file_path": path,
                                    "description": f"finding for {path}",
                                    "suggestion": "n/a",
                                }
                            ]
                        }
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {
        "a.py": "x = 1\n" * 10,
        "b.py": "y = 2\n" * 10,
        "c.py": "z = 3\n" * 10,
    }
    result = find_architecture_and_redundancy_issues(
        _Client(), _input(files=files, architecture=_arch())
    )

    assert len(prompts) == 3
    assert {f.description for f in result} == {
        "finding for a.py",
        "finding for b.py",
        "finding for c.py",
    }
    # Every batch's manifest still lists all three changed files (whole-
    # submission awareness), even though its content section shows only one.
    for prompt in prompts:
        manifest_section = prompt.split("**Full content of the changed files", 1)[0]
        assert "a.py" in manifest_section
        assert "b.py" in manifest_section
        assert "c.py" in manifest_section
    assert any("batch 1 of 3" in p for p in prompts)
    assert any("batch 3 of 3" in p for p in prompts)


def test_one_batch_failure_does_not_discard_other_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed reply for one batch must not wipe out findings already
    collected from other, successful batches."""
    import code_review_agent.submission_pass_runner as runner_mod

    from software_engineering_team.shared.context_sizing import MergedPassBudgets

    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: MergedPassBudgets(
            max_architecture_chars=100_000,
            max_inline_code_chars=200,
            max_manifest_chars=2_000,
            reserved_response_tokens=4096,
        ),
    )

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                if "### a.py ###" in prompt:
                    return "not even a dict-shaped reply"  # type: ignore[return-value]
                if "### b.py ###" in prompt:
                    return {
                        "findings": [
                            {
                                "severity": "medium",
                                "category": "architecture",
                                "file_path": "b.py",
                                "description": "finding for b.py",
                                "suggestion": "n/a",
                            }
                        ]
                    }
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {
        "a.py": "x = 1\n" * 10,
        "b.py": "y = 2\n" * 10,
        "c.py": "z = 3\n" * 10,
    }
    result = find_architecture_and_redundancy_issues(
        _Client(), _input(files=files, architecture=_arch())
    )
    assert [f.description for f in result] == ["finding for b.py"]


def test_reactive_recovery_bisects_overflowing_batch_through_public_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass must now benefit from the shared runner's reactive bisect
    recovery: a combined batch call that overflows mid-turn is retried as two
    single-file calls, rather than simply skipped (the pre-runner behavior --
    this pass had no reactive recovery of its own)."""
    import code_review_agent.submission_pass_runner as runner_mod
    from strands.types.exceptions import ContextWindowOverflowException

    from software_engineering_team.shared.context_sizing import MergedPassBudgets

    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: MergedPassBudgets(
            max_architecture_chars=100_000,
            max_inline_code_chars=100_000,
            max_manifest_chars=2_000,
            reserved_response_tokens=4096,
        ),
    )

    call_count = {"n": 0}

    class _Client(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _ARCH_PASS_ANCHOR in prompt:
                call_count["n"] += 1
                if "### a.py ###" in prompt and "### b.py ###" in prompt:
                    raise ContextWindowOverflowException("combined batch too large")
                for path in ("a.py", "b.py"):
                    if f"### {path} ###" in prompt:
                        return {
                            "findings": [
                                {
                                    "severity": "medium",
                                    "category": "architecture",
                                    "file_path": path,
                                    "description": f"finding for {path}",
                                    "suggestion": "n/a",
                                }
                            ]
                        }
                return {"findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    files = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
    result = find_architecture_and_redundancy_issues(
        _Client(), _input(files=files, architecture=_arch())
    )

    assert {f.description for f in result} == {"finding for a.py", "finding for b.py"}
    # More than one call proves the overflowing combined attempt was actually
    # retried (bisected), not that the test happened to pass on the first try.
    assert call_count["n"] > 1


# --------------------------------------------------------------------------- coordinator integration


def test_coordinator_runs_pass_once_per_submission_not_per_chunk() -> None:
    """The merged pass runs once per submission; standalone tail passes are not invoked."""
    calls = {"merged_pass": 0, "arch_pass": 0, "side_effect_pass": 0, "chunk_review": 0}

    class _CountingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                calls["merged_pass"] += 1
                return {"architecture_findings": [], "side_effect_findings": []}
            if _ARCH_PASS_ANCHOR in prompt:
                calls["arch_pass"] += 1
                return {"findings": []}
            if _SIDE_EFFECT_PASS_ANCHOR in prompt:
                calls["side_effect_pass"] += 1
                return {"findings": []}
            calls["chunk_review"] += 1
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    # Two files -> at least the map phase runs more than once; the false-positive
    # filter is disabled by default (its own env toggle) so this isolates tail-call count.
    files = {"a.py": "def a():\n    return 1\n", "b.py": "def b():\n    return 2\n"}
    run_coordinator(_CountingClient(), CodeReviewInput(files=files, architecture=_arch()))

    assert calls["merged_pass"] == 1
    assert calls["arch_pass"] == 0
    assert calls["side_effect_pass"] == 0


def test_coordinator_merges_architecture_findings_into_final_output() -> None:
    """A finding from this pass reaches the coordinator's merged ``issues`` list,
    dedupes/sizes alongside chunk findings, and does not spuriously block approval
    (default severity keeps it out of the critical/high gate)."""

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                return {
                    "architecture_findings": [
                        {
                            "severity": "medium",
                            "category": "refactor",
                            "file_path": "app/main.py",
                            "description": "duplicates the existing HttpClient wrapper",
                            "suggestion": "reuse shared.http_client.HttpClient",
                        }
                    ],
                    "side_effect_findings": [],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = run_coordinator(
        _FindingsClient(),
        CodeReviewInput(files={"app/main.py": "def bar():\n    return 1\n"}, architecture=_arch()),
    )
    assert result.approved  # a medium refactor finding never blocks approval alone
    assert any(
        i.category == "refactor" and "HttpClient wrapper" in i.description for i in result.issues
    )


def test_coordinator_merges_side_effect_findings_into_final_output() -> None:
    """A side-effect finding emitted by the merged pass is split into
    the coordinator's final issues list."""

    class _FindingsClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                return {
                    "architecture_findings": [],
                    "side_effect_findings": [
                        {
                            "severity": "medium",
                            "category": "side-effects",
                            "file_path": "app/main.py",
                            "description": "mutates shared cache without notifying callers",
                            "suggestion": "emit a cache-invalidation event after mutation",
                            "pre_existing": False,
                        }
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = run_coordinator(
        _FindingsClient(),
        CodeReviewInput(files={"app/main.py": "def bar():\n    return 1\n"}, architecture=_arch()),
    )
    assert result.approved  # medium findings never block approval alone
    assert any(i.category == "side-effects" and "cache" in i.description for i in result.issues)


def test_coordinator_runs_pass_with_no_architecture() -> None:
    """No architecture on the input -> the merged additive pass still runs
    (document is optional); it must not be short-circuited before the LLM call.
    Uses ``files=`` so ``CodebaseIndex`` has readable submission files — the
    same shape production reviews use for this pass."""
    prompts: list = []

    class _CountingClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {"architecture_findings": [], "side_effect_findings": []}
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    result = run_coordinator(
        _CountingClient(),
        CodeReviewInput(files={"app/main.py": "def f():\n    return 1\n"}),
    )
    assert result.approved
    assert len(prompts) == 1


def test_run_pass_inlines_full_arch_doc_without_shrinking_code_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The architecture document's full length reaches the shared runner as
    ``extra_reserved_chars`` regardless of its size, so the runner's budgeting
    -- not this pass -- decides how much of it (and how much code) actually
    fits one call."""
    import code_review_agent.architecture_consistency_pass as pass_mod

    captured: Dict[str, Any] = {}
    original_run_submission_pass = pass_mod.run_submission_pass

    def _spy(llm, **kwargs):
        captured["extra_reserved_chars"] = kwargs["extra_reserved_chars"]
        captured["finding_array_count"] = kwargs["finding_array_count"]
        return original_run_submission_pass(llm, **kwargs)

    monkeypatch.setattr(pass_mod, "run_submission_pass", _spy)

    arch = _arch(overview="", architecture_document="X" * 100_000)

    class _EmptyClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    find_architecture_and_redundancy_issues(_EmptyClient(), _input(architecture=arch))

    assert captured["extra_reserved_chars"] == 100_000
    assert captured["finding_array_count"] == 1


def test_run_pass_wires_scoped_tools_into_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The architecture-consistency pass hands the shared runner the full
    shared tool set, including the scoped read_lines/read_function/
    find_references tools the module docstring previously omitted."""
    import code_review_agent.architecture_consistency_pass as pass_mod

    captured: Dict[str, Any] = {}
    original_run_submission_pass = pass_mod.run_submission_pass

    def _spy(llm, **kwargs):
        captured["tool_names"] = {t.tool_name for t in kwargs["tools"]}
        return original_run_submission_pass(llm, **kwargs)

    monkeypatch.setattr(pass_mod, "run_submission_pass", _spy)

    class _EmptyClient(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            return {"findings": []}

    find_architecture_and_redundancy_issues(_EmptyClient(), _input(architecture=_arch()))

    assert captured["tool_names"] == {
        "read_file",
        "read_lines",
        "read_function",
        "list_files",
        "search_codebase",
        "find_function_at_line",
        "find_references",
    }
