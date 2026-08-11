"""Oversized-submission regression tests for the two code-review "tail passes".

Sub-issue of the effort that bounded the merged architecture/side-effect pass
(``merged_architecture_side_effect_pass.py``) and the false-positive verifier
(``false_positive_filter.py``) to a fixed per-call budget instead of one
unbounded whole-submission call. Earlier sub-issues added unit tests for the
batching mechanics themselves, but they force splitting by monkeypatching the
internal budget-computing functions to artificially tiny values on small (2-3
file / 4-5 finding) fixtures.

This module instead:

    - Builds one genuinely oversized fixture (25 changed files / 48 findings)
      shared by both tail passes.
    - Exercises the REAL budget-computation code path (no faked return
      values) so the assertions reflect production sizing, not a test-chosen
      number.
    - Asserts, against the actual configured budget read at runtime, that no
      single call exceeds it.
    - Compares a bounded (batched) run's output against a literal unbounded
      baseline run (an artificially huge budget forcing exactly one call per
      pass/group) to prove batching never changes *what* is found — only how
      many calls it takes.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from code_review_agent.false_positive_filter import (
    _verify_max_findings_per_group,
    filter_false_positives,
)
from code_review_agent.merged_architecture_side_effect_pass import (
    find_architecture_and_side_effect_issues,
)
from code_review_agent.models import CodeReviewInput, CodeReviewIssue
from tests.test_false_positive_filter import _SimulatesFileReadToolCall

from llm_service.clients.dummy import DummyLLMClient

_MERGED_PASS_ANCHOR = '"architecture_findings"/"side_effect_findings"'

# --------------------------------------------------------------------------- fixture

_OVERSIZED_FILE_COUNT = 25
# Individual file content chars stay well under any plausible tail-pass budget
# (~6,000+ chars for this fixture's manifest, measured empirically) so no
# single file is ever truncated -- only whole-file packing across batches is
# exercised, keeping the "raw content sum <= budget" assertion exact.
_OVERSIZED_FILE_CHARS = 800


def _oversized_changed_files(
    count: int = _OVERSIZED_FILE_COUNT, content_chars: int = _OVERSIZED_FILE_CHARS
) -> Dict[str, str]:
    """Build a large, realistic-looking changed-file set for an oversized submission."""
    files: Dict[str, str] = {}
    for i in range(count):
        path = f"pkg/module_{i:02d}.py"
        line = f"def handler_{i}(value):\n    return value + {i}\n\n"
        body = (line * (content_chars // len(line) + 1))[:content_chars]
        files[path] = body
    return files


def _oversized_findings(
    files: Dict[str, str], *, findings_per_file: int = 8, group_count: int = 6
) -> List[CodeReviewIssue]:
    """Build a large finding set spread across the first ``group_count`` files.

    Each finding's ``description`` embeds a globally unique id
    (``finding-<n>``) so a verdict stub can key its decision off finding
    identity rather than its position within whatever group/batch it lands
    in -- required for the bounded-vs-unbounded parity check below.
    """
    paths = list(files.keys())[:group_count]
    issues: List[CodeReviewIssue] = []
    global_id = 0
    for path in paths:
        for _ in range(findings_per_file):
            issues.append(
                CodeReviewIssue(
                    severity="high",
                    category="logic",
                    file_path=path,
                    line=1,
                    description=f"finding-{global_id}",
                    suggestion="investigate",
                )
            )
            global_id += 1
    return issues


def _merged_input(files: Dict[str, str]) -> CodeReviewInput:
    return CodeReviewInput(files=files, task_description="oversized submission review")


def _filter_input(files: Dict[str, str]) -> CodeReviewInput:
    return CodeReviewInput(files=files, task_description="oversized submission review")


# --------------------------------------------------------------------------- merged pass


def test_merged_pass_oversized_submission_stays_within_configured_budget(
    monkeypatch: Any,
) -> None:
    """A genuinely oversized changed-file set, run through the real (non-faked)
    budgeting path, must actually split into multiple calls, and no single
    call's inlined content may exceed the budget that path itself computed."""
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")

    import code_review_agent.submission_pass_runner as runner_mod

    real_compute = runner_mod.compute_code_review_merged_pass_budgets
    recorded_budgets: list = []

    def _recording_compute(*args: Any, **kwargs: Any) -> Any:
        budgets = real_compute(*args, **kwargs)
        recorded_budgets.append(budgets)
        return budgets

    monkeypatch.setattr(runner_mod, "compute_code_review_merged_pass_budgets", _recording_compute)

    files = _oversized_changed_files()
    prompts: List[str] = []

    class _RecordingStub(DummyLLMClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in prompt:
                prompts.append(prompt)
                return {
                    "architecture_findings": [],
                    "side_effect_findings": [
                        {
                            "severity": "medium",
                            "category": "side-effects",
                            "file_path": path,
                            "description": f"finding for {path}",
                            "suggestion": "n/a",
                            "pre_existing": False,
                        }
                        for path in files
                        if f"### {path} ###" in prompt
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    find_architecture_and_side_effect_issues(_RecordingStub(), _merged_input(files))

    assert len(prompts) > 1, "an oversized submission must actually split into multiple calls"
    assert recorded_budgets, "the real budget function must have been invoked"
    budget = recorded_budgets[-1].max_inline_code_chars
    assert budget > 0

    for prompt in prompts:
        # No file was ever truncated or dropped -- confirms the fixture's
        # per-file size assumption (well under budget) actually held, so the
        # raw-content-sum check below is exact, not an approximation.
        assert "are shown above" not in prompt
        assert "not shown above" not in prompt

        inlined_paths = [path for path in files if f"### {path} ###" in prompt]
        assert inlined_paths, "every call must inline at least one file"
        content_sum = sum(len(files[path]) for path in inlined_paths)
        assert content_sum <= budget, (
            f"call inlined {content_sum} chars of content, exceeding the "
            f"configured budget of {budget} chars"
        )


def test_merged_pass_oversized_submission_matches_unbounded_baseline(
    monkeypatch: Any,
) -> None:
    """Findings merged from a real, bounded (multi-batch) run must equal the
    findings from a literal unbounded baseline run (one call, everything
    inlined) over the same oversized submission -- batching must never change
    *what* is found, only how many calls it takes."""
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")
    files = _oversized_changed_files()

    def _make_stub(prompts: List[str]) -> DummyLLMClient:
        class _Stub(DummyLLMClient):
            def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
                if _MERGED_PASS_ANCHOR in prompt:
                    prompts.append(prompt)
                    return {
                        "architecture_findings": [],
                        "side_effect_findings": [
                            {
                                "severity": "medium",
                                "category": "side-effects",
                                "file_path": path,
                                "description": f"finding for {path}",
                                "suggestion": "n/a",
                                "pre_existing": False,
                            }
                            for path in files
                            if f"### {path} ###" in prompt
                        ],
                    }
                return {
                    "approved": True,
                    "issues": [],
                    "summary": "ok",
                    "spec_compliance_notes": "",
                }

        return _Stub()

    # Bounded run: the real, unmodified budgeting path.
    bounded_prompts: List[str] = []
    _arch_bounded, side_bounded = find_architecture_and_side_effect_issues(
        _make_stub(bounded_prompts), _merged_input(files)
    )
    assert len(bounded_prompts) > 1

    # Unbounded baseline: an artificially huge budget forces exactly one call.
    import code_review_agent.submission_pass_runner as runner_mod

    from software_engineering_team.shared.context_sizing import MergedPassBudgets

    monkeypatch.setattr(
        runner_mod,
        "compute_code_review_merged_pass_budgets",
        lambda *a, **k: MergedPassBudgets(
            max_architecture_chars=0,
            max_inline_code_chars=10_000_000,
            max_manifest_chars=100_000,
            reserved_response_tokens=4096,
        ),
    )
    baseline_prompts: List[str] = []
    _arch_baseline, side_baseline = find_architecture_and_side_effect_issues(
        _make_stub(baseline_prompts), _merged_input(files)
    )
    assert len(baseline_prompts) == 1

    assert {f.description for f in side_bounded} == {f.description for f in side_baseline}
    assert len(side_bounded) == len(files)


# --------------------------------------------------------------------------- false-positive filter

_FINDING_ID_RE = re.compile(r"description: finding-(\d+)")


def _keep_finding(global_id: int) -> bool:
    """Deterministic, identity-based verdict rule shared by both filter runs.

    Keyed on the finding's own global id rather than its position within
    whatever group/batch it lands in, so the merged result is independent of
    how findings are grouped -- required for the parity check below.
    """
    return global_id % 3 != 0


class _DeterministicVerdictStub(_SimulatesFileReadToolCall):
    """Returns a verdict per finding keyed to that finding's global id, so the
    merged outcome does not depend on group/batch boundaries."""

    def __init__(self) -> None:
        super().__init__()
        self.call_sizes: List[int] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        if "verdicts" not in prompt.lower():
            return super().complete_json(prompt, **kwargs)
        global_ids = [int(m) for m in _FINDING_ID_RE.findall(prompt)]
        self.call_sizes.append(len(global_ids))
        return {
            "verdicts": [
                {
                    "index": i,
                    "is_real_issue": _keep_finding(gid),
                    "confidence": "high",
                }
                for i, gid in enumerate(global_ids)
            ]
        }


def test_filter_oversized_submission_stays_within_configured_budget(monkeypatch: Any) -> None:
    """An oversized finding set, split under a real (small) per-group cap, must
    never send more findings in one call than ``_verify_max_findings_per_group()``
    actually allows."""
    monkeypatch.setenv("CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP", "5")
    files = _oversized_changed_files()
    issues = _oversized_findings(files)

    stub = _DeterministicVerdictStub()
    out = filter_false_positives(stub, _filter_input(files), issues)

    cap = _verify_max_findings_per_group()
    assert cap == 5
    assert len(stub.call_sizes) > len({i.file_path for i in issues}), (
        "an oversized per-file finding count must actually split into more "
        "calls than there are files"
    )
    assert all(size <= cap for size in stub.call_sizes)
    # Every third finding (id % 3 == 0) is dropped, the rest survive.
    kept_ids = {int(i.description.split("-")[1]) for i in out}
    assert kept_ids == {i for i in range(len(issues)) if _keep_finding(i)}


def test_filter_oversized_submission_matches_unbounded_baseline(monkeypatch: Any) -> None:
    """The filtered result from a real, bounded (many small groups) run must
    equal the filtered result from an unbounded baseline run (one call per
    file, no splitting) over the same oversized finding set."""
    files = _oversized_changed_files()
    issues = _oversized_findings(files)

    monkeypatch.setenv("CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP", "5")
    bounded_stub = _DeterministicVerdictStub()
    bounded = filter_false_positives(bounded_stub, _filter_input(files), issues)
    assert len(bounded_stub.call_sizes) > len({i.file_path for i in issues})

    monkeypatch.setenv("CODE_REVIEW_VERIFY_MAX_FINDINGS_PER_GROUP", "100000")
    baseline_stub = _DeterministicVerdictStub()
    baseline = filter_false_positives(baseline_stub, _filter_input(files), issues)
    assert len(baseline_stub.call_sizes) == len({i.file_path for i in issues})

    assert bounded == baseline
