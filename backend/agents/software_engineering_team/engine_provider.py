"""CodeEngineProvider: coding_team's inversion of its dependency on SE's engines.

coding_team orchestrates the coding pipeline but does not own the concrete engines
that write code and gate quality — the frontend/backend code-v2 team leads, the
build/lint/review tools, and the PR code-review agent all live in
``software_engineering_team``. To keep coding_team importable and testable without
importing SE, coding_team depends on the ``CodeEngineProvider`` *interface* defined
here and receives a concrete implementation by injection:

    - the software-engineering team passes one when it drives
      ``run_coding_team_orchestrator`` (in-process), and
    - the standalone coding-team service installs one at startup via
      ``set_engine_provider`` (its out-of-package composition root).

Result: the ``coding_team`` package imports nothing from
``software_engineering_team`` and the two packages form an acyclic graph.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class CodeEngineProvider(Protocol):
    """The implementation-engine capabilities coding_team needs but does not own."""

    def build_implementation_team_lead(self, team_kind: str, llm: Any) -> Any:
        """Return an implementation team-lead engine for ``team_kind``.

        Preconditions: ``team_kind`` in ``{"frontend", "backend"}``; ``llm`` is a
        ready (text-mode) model/handle. Postconditions: returns an object exposing
        the team-lead interface a ``V2TeamWorker`` drives.
        """
        ...

    def run_build_verification(self, repo_path: Any, agent_type: str, task_id: str) -> Any:
        """Build/compile the repo. Postconditions: returns an object with
        ``success: bool`` and ``error: str``."""
        ...

    def run_linting(self, repo_path: Any, task_id: str, *, llm_getter: Any) -> Any:
        """Lint the repo (best-effort). Postconditions: returns ``None`` or a
        result; not required to raise on lint findings."""
        ...

    def run_code_review(self, **kwargs: Any) -> Any:
        """Review a task's changes. Postconditions: returns an object with
        ``approved: bool`` and ``issues: list``."""
        ...

    def run_pr_code_review(self, **kwargs: Any) -> Any:
        """Review a pull request's diff. Postconditions: returns an object with
        an ``issues`` list. Accepts an optional ``replaced_content`` kwarg
        (``{path: pre-change body}``) forwarded additively to the review
        input; absent/``None`` behaves exactly as before its introduction."""
        ...


_provider: Optional[CodeEngineProvider] = None


def set_engine_provider(provider: Optional[CodeEngineProvider]) -> None:
    """Install the process-wide default engine provider.

    The standalone coding-team service's composition root calls this once at
    startup. Passing ``None`` clears it.

    Postconditions: ``get_engine_provider()`` returns ``provider`` until replaced.
    """
    global _provider
    _provider = provider


def get_engine_provider() -> Optional[CodeEngineProvider]:
    """Return the installed default engine provider, or ``None`` if unset.

    ``run_coding_team_orchestrator`` prefers an explicitly-injected provider (e.g.
    from the software-engineering team, which passes one per call) and falls back
    to this process-wide default (set by the standalone service at startup).
    """
    return _provider
