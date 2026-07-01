"""
Backend stack profile: language detection + the knobs that select backend
behavior in the shared code-v2 phase implementations.

``_detect_language`` lives here (rather than in ``planning.py``) so the profile
can reference it without importing the heavier phase module — ``planning.py``
re-exports it for callers and tests. See ``shared/stack_profile.py``.
"""

from __future__ import annotations

from pathlib import Path

from software_engineering_team.shared.models import Task
from software_engineering_team.shared.stack_profile import StackProfile

from ..prompts import JAVA_CONVENTIONS, PYTHON_CONVENTIONS


def _detect_language(repo_path: Path, task: Task) -> str:
    """Infer whether the project is Python or Java from the repo.

    Preconditions:
        ``repo_path`` is a ``Path`` (may or may not exist); ``task`` is a
        ``Task`` whose ``description``/``requirements`` may be ``None``.
    Postconditions:
        Returns ``"java"`` or ``"python"``; never raises. Repo signals take
        precedence over task-text heuristics; ``"python"`` is the default.
    """
    if repo_path.is_dir():
        if any(repo_path.rglob("pom.xml")) or any(repo_path.rglob("build.gradle")):
            return "java"
        if any(repo_path.rglob("requirements.txt")) or any(repo_path.rglob("pyproject.toml")):
            return "python"
        if any(repo_path.rglob("*.java")):
            return "java"
    desc = (task.description or "").lower() + " " + (task.requirements or "").lower()
    if "spring" in desc or "java" in desc or "maven" in desc or "gradle" in desc:
        return "java"
    return "python"


PROFILE = StackProfile(
    name="backend",
    default_language="python",
    planning_language_label="Language",
    planning_progress_label="language",
    conventions_by_language={"java": JAVA_CONVENTIONS, "_default": PYTHON_CONVENTIONS},
    has_language_conventions=True,
    detect_language=_detect_language,
)
