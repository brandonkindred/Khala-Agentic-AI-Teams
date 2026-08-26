"""Shared test doubles for coding-team orchestrator tests.

Extracted from ``test_coding_team_orchestrator.py`` so
``test_coding_team_orchestrator_concurrency.py`` (and any future orchestrator
test module) can reuse the same Tech-Lead grooming fallback, fake worktree
manager, and git patch helper instead of duplicating them, matching the
extraction pattern already used by ``_review_fallback_test_doubles.py`` and
``_v2_config_fixtures.py``.

Not a test module itself -- its ``_``-prefixed name prevents pytest from
collecting it (same convention as those two modules).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

GIT_UTILS = "shared.git.git_utils"


class DefaultGroomTaskMixin:
    """Ungroomed-default ``run_groom_task`` for Tech-Lead stubs that don't care about grooming.

    Mirrors the real ``run_groom_task``'s own default/fallback shape, so mixing this in changes no
    existing assertion for a stub that doesn't otherwise override it.
    """

    def run_groom_task(
        self,
        task_id: str,
        task_title: str,
        task_description: str,
        task_dependencies: List[str],
        plan_context: str,
    ) -> Dict[str, Any]:
        return {
            "acceptance_criteria": [],
            "out_of_scope": "",
            "description_enriched": task_description,
            "priority": "medium",
            "subtasks": [],
            "task_dependencies": task_dependencies,
        }


class FakeWorktreeManager:
    """Test double for coding_team.worktree_manager.WorktreeManager.

    Stub-worker tests never touch git, so a real WorktreeManager would pay for
    (and, with no git patch applied, actually attempt) real `git worktree add`
    calls against a plain tmp_path with no `.git`. This double gives each
    agent_id a distinct child directory under the swarm's own tmp_path
    instead -- no git, no filesystem writes outside tmp_path's own
    pytest-managed cleanup. Real worktree mechanics are covered by
    test_coding_team_worktree_manager.py against WorktreeManager itself.
    """

    def __init__(self, repo_path: Path, agent_ids: List[str]) -> None:
        self._paths = {aid: Path(repo_path) / f"_wt_{aid}" for aid in agent_ids}
        self.prepare_calls = 0
        self.cleanup_calls = 0

    def prepare(self) -> None:
        self.prepare_calls += 1
        for path in self._paths.values():
            path.mkdir(parents=True, exist_ok=True)

    def path_for(self, agent_id: str) -> Path:
        return self._paths[agent_id]

    def cleanup(self) -> None:
        self.cleanup_calls += 1


def patch_git(
    monkeypatch: pytest.MonkeyPatch, diff: str = "", merge: tuple[bool, str] = (True, "ok")
) -> None:
    """Patch ``shared.git.git_utils.branch_diff``/``merge_branch`` for the rest of the test.

    Preconditions:
        - ``merge`` is a ``(success, message)`` pair, matching ``merge_branch``'s own
          return shape.
    Postconditions:
        - For the remainder of the test (``monkeypatch`` un-does this at teardown),
          ``branch_diff(...)`` unconditionally returns ``diff`` and ``merge_branch(...)``
          unconditionally returns ``merge``, regardless of the arguments either is called
          with — no real git process is invoked.
    """
    monkeypatch.setattr(f"{GIT_UTILS}.branch_diff", lambda *a, **k: diff)
    monkeypatch.setattr(f"{GIT_UTILS}.merge_branch", lambda *a, **k: merge)
