"""Regression tests for the pre-merge build/lint quality gate.

Parent finding: the standalone ``/backend-code-v2/run`` and
``/frontend-code-v2/run`` endpoints merge to ``development`` unconditionally
(``merge_to_development`` defaults to ``True``, no Tech Lead re-review), and
after build/lint was removed from the code review phase, that path had no
build/lint gate before merging. The fix added
``shared.deliver_utils.run_pre_merge_quality_gate`` and wired it into
``deliver_inline_merge`` immediately before the merge call. Before that fix,
``deliver_inline_merge`` had no gate call at all and merged unconditionally
after commit -- these tests fail against that prior behavior (no gate call,
``merge_branch`` always invoked) and pass against the current code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

from shared.git.git_utils import DEVELOPMENT_BRANCH
from software_engineering_team.linting_tool_agent.models import LintToolInput
from software_engineering_team.shared.deliver_utils import (
    DeliverGitOps,
    deliver_inline_merge,
    run_pre_merge_quality_gate,
)


def _logger() -> logging.Logger:
    return logging.getLogger("test_deliver_utils_quality_gate")


def _passing_lint_agent() -> MagicMock:
    agent = MagicMock()
    agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=True), passed=True, linter_issues=[]
    )
    return agent


def _failing_lint_agent() -> MagicMock:
    agent = MagicMock()
    agent.run.return_value = MagicMock(
        execution_result=MagicMock(success=False), passed=False, linter_issues=["boom"]
    )
    return agent


class TestRunPreMergeQualityGate:
    """Direct unit tests for ``run_pre_merge_quality_gate``."""

    def test_no_verifier_or_linter_skips_both_checks(self, tmp_path: Path) -> None:
        ok, msg = run_pre_merge_quality_gate(repo_path=tmp_path, task_id="t1", logger=_logger())
        assert (ok, msg) == (True, "")

    def test_build_failure_fails_closed(self, tmp_path: Path) -> None:
        ok, msg = run_pre_merge_quality_gate(
            repo_path=tmp_path,
            task_id="t1",
            build_verifier=lambda repo_path, label, task_id: (False, "build broke"),
            build_verify_label="backend",
            logger=_logger(),
        )
        assert ok is False
        assert msg == "Build failed: build broke"

    def test_build_exception_fails_closed(self, tmp_path: Path) -> None:
        def _boom(repo_path, label, task_id):
            raise RuntimeError("verifier crashed")

        ok, msg = run_pre_merge_quality_gate(
            repo_path=tmp_path, task_id="t1", build_verifier=_boom, logger=_logger()
        )
        assert ok is False
        assert msg == "Build failed: verifier crashed"

    def test_build_success_passes(self, tmp_path: Path) -> None:
        ok, msg = run_pre_merge_quality_gate(
            repo_path=tmp_path,
            task_id="t1",
            build_verifier=lambda repo_path, label, task_id: (True, ""),
            logger=_logger(),
        )
        assert (ok, msg) == (True, "")

    def test_lint_failure_fails_closed(self, tmp_path: Path) -> None:
        ok, msg = run_pre_merge_quality_gate(
            repo_path=tmp_path,
            task_id="t1",
            linting_tool_agent=_failing_lint_agent(),
            lint_agent_type="backend",
            logger=_logger(),
        )
        assert ok is False
        assert msg == "Lint failed."

    def test_lint_success_passes(self, tmp_path: Path) -> None:
        ok, msg = run_pre_merge_quality_gate(
            repo_path=tmp_path,
            task_id="t1",
            linting_tool_agent=_passing_lint_agent(),
            lint_agent_type="frontend",
            logger=_logger(),
        )
        assert (ok, msg) == (True, "")

    def test_lint_tool_exception_fails_open(self, tmp_path: Path, caplog) -> None:
        agent = MagicMock()
        agent.run.side_effect = RuntimeError("lint infra crashed")

        with caplog.at_level("WARNING", logger="test_deliver_utils_quality_gate"):
            ok, msg = run_pre_merge_quality_gate(
                repo_path=tmp_path,
                task_id="t1",
                linting_tool_agent=agent,
                lint_agent_type="backend",
                logger=_logger(),
            )

        assert (ok, msg) == (True, "")
        assert "linting tool agent failed" in caplog.text

    def test_build_and_lint_failures_combine_with_semicolon(self, tmp_path: Path) -> None:
        ok, msg = run_pre_merge_quality_gate(
            repo_path=tmp_path,
            task_id="t1",
            build_verifier=lambda repo_path, label, task_id: (False, "build broke"),
            linting_tool_agent=_failing_lint_agent(),
            lint_agent_type="backend",
            logger=_logger(),
        )
        assert ok is False
        assert msg == "Build failed: build broke; Lint failed."

    def test_lint_agent_invoked_with_expected_input(self, tmp_path: Path) -> None:
        agent = _passing_lint_agent()

        run_pre_merge_quality_gate(
            repo_path=tmp_path,
            task_id="t42",
            linting_tool_agent=agent,
            lint_agent_type="frontend",
            logger=_logger(),
        )

        agent.run.assert_called_once()
        (call_arg,) = agent.run.call_args.args
        assert isinstance(call_arg, LintToolInput)
        assert call_arg.repo_path == str(tmp_path)
        assert call_arg.agent_type == "frontend"
        assert call_arg.task_id == "t42"


class _RecordingOps:
    """Wraps a literal ``DeliverGitOps`` to record call order/args for assertions."""

    def __init__(self, *, merge_result=(True, ""), commit_result=(True, "")) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._merge_result = merge_result
        self._commit_result = commit_result

    def _track(self, name, fn):
        def _wrapped(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return fn(*args, **kwargs)

        return _wrapped

    def as_deliver_git_ops(self) -> DeliverGitOps:
        return DeliverGitOps(
            abort_merge=self._track("abort_merge", lambda *a, **k: None),
            checkout_branch=self._track("checkout_branch", lambda *a, **k: (True, "")),
            commit_working_tree=self._track(
                "commit_working_tree", lambda *a, **k: self._commit_result
            ),
            create_feature_branch=self._track(
                "create_feature_branch", lambda *a, **k: (True, "feature/t1")
            ),
            delete_branch=self._track("delete_branch", lambda *a, **k: True),
            merge_branch=self._track("merge_branch", lambda *a, **k: self._merge_result),
            write_agent_output=self._track("write_agent_output", lambda *a, **k: (True, "")),
        )

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


class TestDeliverInlineMergeQualityGate:
    """Integration coverage: the gate call inside ``deliver_inline_merge``."""

    def test_gate_failure_blocks_merge_and_restores_development(self, tmp_path: Path) -> None:
        ops = _RecordingOps()

        result = deliver_inline_merge(
            task_id="t1",
            repo_path=tmp_path,
            deliver_files={"a.py": "x"},
            summary="impl",
            task_title="Title",
            commit_msg_template="[{scope}] {summary}",
            ops=ops.as_deliver_git_ops(),
            logger=_logger(),
            build_verifier=lambda repo_path, label, task_id: (False, "build broke"),
            build_verify_label="backend",
        )

        assert result.merged is False
        assert result.summary == "Pre-merge quality gate failed: Build failed: build broke"
        assert "merge_branch" not in ops.names()
        assert "commit_working_tree" not in ops.names()
        checkout_calls = [c for c in ops.calls if c[0] == "checkout_branch"]
        assert checkout_calls[-1][1] == (tmp_path, DEVELOPMENT_BRANCH)

    def test_gate_pass_sweeps_autofix_commit_before_merge(self, tmp_path: Path) -> None:
        """The gate must run against the final delivered file state -- i.e.
        after write_agent_output -- and its build/lint checks must themselves
        precede the autofix commit and merge. The verifier/linter calls are
        recorded into the *same* ordered trace as the git ops (not tracked
        separately) so an implementation that ran the gate before writing the
        delivered files, or reordered the gate relative to the autofix
        commit/merge, would fail this assertion instead of passing silently.
        """
        ops = _RecordingOps()

        def _build_verifier(repo_path, label, task_id):
            ops.calls.append(("build_verifier", (repo_path, label, task_id), {}))
            return True, ""

        class _TracedLintAgent:
            def run(self, inp):
                ops.calls.append(("linting_tool_agent.run", (inp,), {}))
                return MagicMock(
                    execution_result=MagicMock(success=True), passed=True, linter_issues=[]
                )

        result = deliver_inline_merge(
            task_id="t1",
            repo_path=tmp_path,
            deliver_files={"a.py": "x"},
            summary="impl",
            task_title="Title",
            commit_msg_template="[{scope}] {summary}",
            ops=ops.as_deliver_git_ops(),
            logger=_logger(),
            build_verifier=_build_verifier,
            linting_tool_agent=_TracedLintAgent(),
            lint_agent_type="backend",
        )

        assert result.merged is True
        names = ops.names()
        assert names.index("write_agent_output") < names.index("build_verifier")
        assert names.index("build_verifier") < names.index("linting_tool_agent.run")
        assert names.index("linting_tool_agent.run") < names.index("commit_working_tree")
        assert names.index("commit_working_tree") < names.index("merge_branch")
        autofix_calls = [
            c
            for c in ops.calls
            if c[0] == "commit_working_tree"
            and c[1][1] == "chore: pre-merge quality gate autofix"
        ]
        assert len(autofix_calls) == 1

    def test_autofix_commit_failure_blocks_merge_and_restores_development(
        self, tmp_path: Path
    ) -> None:
        """A genuine git failure while sweeping up the autofix commit (as opposed
        to the common "nothing to commit" no-op, which ``commit_working_tree``
        reports as success) must fail closed: skip the merge and restore
        ``development``, mirroring the gate-failure branch above.
        """
        ops = _RecordingOps(commit_result=(False, "git commit failed: disk full"))

        result = deliver_inline_merge(
            task_id="t1",
            repo_path=tmp_path,
            deliver_files={"a.py": "x"},
            summary="impl",
            task_title="Title",
            commit_msg_template="[{scope}] {summary}",
            ops=ops.as_deliver_git_ops(),
            logger=_logger(),
        )

        assert result.merged is False
        assert result.summary == "Autofix commit failed: git commit failed: disk full"
        assert "merge_branch" not in ops.names()
        assert "commit_working_tree" in ops.names()
        checkout_calls = [c for c in ops.calls if c[0] == "checkout_branch"]
        assert checkout_calls[-1][1] == (tmp_path, DEVELOPMENT_BRANCH)

    def test_no_verifier_or_linter_preserves_pre_fix_merge_behavior(self, tmp_path: Path) -> None:
        ops = _RecordingOps()

        result = deliver_inline_merge(
            task_id="t1",
            repo_path=tmp_path,
            deliver_files={"a.py": "x"},
            summary="impl",
            task_title="Title",
            commit_msg_template="[{scope}] {summary}",
            ops=ops.as_deliver_git_ops(),
            logger=_logger(),
        )

        assert result.merged is True
        assert "merge_branch" in ops.names()
