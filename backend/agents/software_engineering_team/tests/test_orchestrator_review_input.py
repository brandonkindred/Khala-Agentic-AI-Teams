"""Coverage for orchestrator._run_code_review's files= passthrough.

Exercises the legacy orchestrator review helper directly (previously marked
``# pragma: no cover``): a ``files=`` mapping must reach the code-review agent
verbatim and suppress the legacy ``code=`` concatenation, while ``files=None``
forwards ``code_to_review``.
"""

from __future__ import annotations

from types import SimpleNamespace

from software_engineering_team import orchestrator


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        description="do the thing",
        requirements="reqs",
        user_story="",
        acceptance_criteria=["criterion"],
    )


class _CapturingAgent:
    def __init__(self) -> None:
        self.captured = None

    def run(self, review_input):  # noqa: D401 - test stub
        self.captured = review_input
        return SimpleNamespace(approved=True, issues=[])


def test_orchestrator_run_code_review_forwards_files_dict() -> None:
    agent = _CapturingAgent()
    files = {"app/main.py": "print('hi')\n", "app/util.py": "x = 1\n"}

    orchestrator._run_code_review(
        agents={"code_review": agent},
        code_to_review="### app/main.py ###\nignored",
        spec_content="spec",
        task=_task(),
        language="python",
        architecture=None,
        files=files,
    )

    assert agent.captured.files == files
    # files take precedence; the legacy blob is dropped (model normalizes to "").
    assert not agent.captured.code


def test_orchestrator_run_code_review_uses_code_when_no_files() -> None:
    agent = _CapturingAgent()

    orchestrator._run_code_review(
        agents={"code_review": agent},
        code_to_review="### a.py ###\nx = 1",
        spec_content="spec",
        task=_task(),
        language="python",
        architecture=None,
        files=None,
    )

    assert agent.captured.files is None
    assert agent.captured.code == "### a.py ###\nx = 1"
