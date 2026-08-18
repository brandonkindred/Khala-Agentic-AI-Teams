"""Oversized-submission regression tests for the two code-review "tail passes".

Covers the merged architecture/side-effect pass (now one unbounded whole-
submission call) and the false-positive verifier (still finding-group batched).

    - Builds one genuinely oversized fixture (25 changed files / 48 findings)
      shared by both tail passes.
    - Asserts the merged pass inlines the full set in a single call.
    - Keeps FPF group-budget coverage for the verifier path.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List

import pytest
from code_review_agent.false_positive_filter import (
    _verify_max_findings_per_group,
    filter_false_positives,
)
from code_review_agent.merged_architecture_side_effect_pass import (
    find_architecture_and_side_effect_issues,
)
from code_review_agent.models import CodeReviewInput, CodeReviewIssue
from tests.submission_pass_two_call_client import (
    SubmissionPassTwoCallClient,
    wire_run_agent_via_reasoning_for_test_clients,
)
from tests.test_false_positive_filter import _SimulatesFileReadToolCall


@pytest.fixture(autouse=True)
def _wire_submission_pass_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the submission-pass runner's ``run_agent_via_reasoning`` through the
    two-call test stub for every test in this module.

    File-scoped (a plain module-level fixture, not a ``pytest_plugins``
    registration): a fixture defined directly in a test module only applies to
    that module's own tests, so this cannot leak into sibling test files under
    pytest-xdist the way a ``pytest_plugins`` registration would (each xdist
    worker collects the whole test tree, so a session-wide plugin's autouse
    fixtures would otherwise apply to every test the worker runs).
    """
    import code_review_agent.submission_pass_runner as runner_mod

    wire_run_agent_via_reasoning_for_test_clients(monkeypatch, runner_mod)


_MERGED_PASS_ANCHOR = "Merged submission pass:"

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


def test_merged_pass_oversized_submission_is_one_unbounded_call(
    monkeypatch: Any,
) -> None:
    """Without token packing, an oversized changed-file set is one call that
    inlines every file."""
    monkeypatch.setenv("CODE_REVIEW_ARCHITECTURE_CONSISTENCY_PASS", "false")

    files = _oversized_changed_files()
    prompts: List[str] = []

    class _RecordingStub(SubmissionPassTwoCallClient):
        def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
            if _MERGED_PASS_ANCHOR in self.latest_reasoning_prompt():
                prompts.append(self.latest_reasoning_prompt())
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
                        if f"### {path} ###" in self.latest_reasoning_prompt()
                    ],
                }
            return {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}

    _arch, side = find_architecture_and_side_effect_issues(
        _RecordingStub(), _merged_input(files)
    )

    assert len(prompts) == 1
    prompt = prompts[0]
    assert "are shown above" not in prompt
    assert "not shown above" not in prompt
    for path in files:
        assert f"### {path} ###" in prompt
    assert len(side) == len(files)


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
        self._reasoning_prompt_local = threading.local()

    def stash_reasoning_prompt(self, prompt: str) -> None:
        """Record the FPF reasoning user prompt for the in-flight verify call."""
        self._reasoning_prompt_local.prompt = prompt

    def _take_reasoning_prompt(self) -> str:
        prompt = getattr(self._reasoning_prompt_local, "prompt", "")
        self._reasoning_prompt_local.prompt = ""
        return prompt

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:  # type: ignore[override]
        if "verdicts" not in prompt.lower():
            return super().complete_json(prompt, **kwargs)
        # Finding descriptions live on the reasoning user prompt (call 1), not
        # the format-pass prompt (call 2).
        source = self._take_reasoning_prompt() or prompt
        global_ids = [int(m) for m in _FINDING_ID_RE.findall(source)]
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


@pytest.fixture(autouse=True)
def _stash_fpf_reasoning_prompt_on_stub(monkeypatch: Any) -> None:
    """Bind each format pass to the reasoning prompt from the same verify call."""
    import code_review_agent.false_positive_filter as fpf_mod
    import code_review_agent.via_reasoning as vr_mod

    real_run = vr_mod.run_agent_via_reasoning

    def _run_with_stash(**kwargs: Any) -> Any:
        client = vr_mod._extract_llm_client(kwargs["model"])
        if client is not None and hasattr(client, "stash_reasoning_prompt"):
            client.stash_reasoning_prompt(kwargs["reasoning_prompt"])
        return real_run(**kwargs)

    monkeypatch.setattr(vr_mod, "run_agent_via_reasoning", _run_with_stash)
    monkeypatch.setattr(fpf_mod, "run_agent_via_reasoning", _run_with_stash)


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
