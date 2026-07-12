"""
Shared base for the code-v2 Team Leads (backend + frontend).

``BackendCodeV2TeamLead`` and ``FrontendCodeV2TeamLead`` share their
constructor, their per-repo incremental briefing cache lookup
(:meth:`BaseTeamLead._repo_context_cache_for`), and the field-copy tail that
overlays their inner ``*DevelopmentAgent`` result onto their own result
object; only the injected repo-briefing extension/exclude/char-budget
constants and the bulk of ``run_workflow`` (setup-phase orchestration,
status/progress wiring) differ. This base holds the shared members; each team
subclasses it and supplies the divergent parts.

``run_workflow`` itself deliberately stays per-team: it is ``# pragma: no
cover`` integration code carrying team-specific setup/status/progress wiring,
so converging it into a template method is a separate, test-guarded change
rather than part of this base (``test_team_lead_propagates_development_handoff_fields``
monkeypatches ``run_setup``/``*DevelopmentAgent`` as module-level attributes of
each team's own orchestrator module, which any future unification must keep
resolving per-subclass-module).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, FrozenSet

from llm_service import LLMClient
from software_engineering_team.shared.repo_context_cache import RepoContextCache

# The 13 fields a code-v2 development-agent result hands off to its team
# lead's own result object. ``setup_result`` is deliberately excluded: it is
# the team lead's own setup phase (run before delegating), and copying the
# inner development agent's (always-empty) setup_result would clobber it.
_DEVELOPMENT_RESULT_FIELDS = (
    "success",
    "current_phase",
    "iterations_used",
    "planning_result",
    "execution_result",
    "review_result",
    "problem_solving_result",
    "documentation_result",
    "deliver_result",
    "final_files",
    "summary",
    "failure_reason",
    "needs_followup",
)


def copy_development_result_fields(dst: Any, src: Any) -> None:
    """Overlay the shared development-handoff fields from ``src`` onto ``dst``.

    Preconditions: ``dst`` and ``src`` each expose every attribute named in
      ``_DEVELOPMENT_RESULT_FIELDS`` (both backend and frontend
      ``*CodeV2WorkflowResult`` models do, despite being distinct classes).
    Postconditions: each of the 13 shared fields on ``dst`` equals the
      corresponding field on ``src``; every other attribute of ``dst``
      (notably ``setup_result``) is left untouched.
    """
    for field in _DEVELOPMENT_RESULT_FIELDS:
        setattr(dst, field, getattr(src, field))


class BaseTeamLead:
    """Shared base for the code-v2 Team Leads.

    Subclasses provide the per-team repo-briefing filter constants (via
    ``__init__``) and their own ``run_workflow``.

    Invariants: instance state is limited to ``llm``, the injected
    extensions/exclude_dirs/max_chars, and ``_repo_context_caches``.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        *,
        extensions: FrozenSet[str],
        exclude_dirs: FrozenSet[str],
        max_chars: int,
    ) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        self._extensions = extensions
        self._exclude_dirs = exclude_dirs
        self._max_chars = max_chars
        # Per-repo incremental briefing cache, reused across the team lead's
        # run_workflow calls (the coding-team worker reuses one team lead for all
        # tasks in a job), so the N tasks of a job re-read only the files each
        # merge touched instead of re-walking the whole repo N times.
        self._repo_context_caches: Dict[Path, RepoContextCache] = {}

    def _repo_context_cache_for(self, repo_path: Path) -> RepoContextCache:
        """Return the incremental briefing cache for ``repo_path``, creating it lazily.

        Preconditions: ``repo_path`` is a directory the development agent will scan.
        Postconditions: returns a ``RepoContextCache`` configured with this team's
          repo-briefing contract (extensions / exclude dirs / char budget); the same
          instance is returned for the same resolved repo across calls. Raises
          ``AssertionError`` if the precondition is violated (caller bug).
        """
        assert repo_path.is_dir(), "repo_path must be a directory"
        key = repo_path.resolve()
        cache = self._repo_context_caches.get(key)
        if cache is None:
            cache = RepoContextCache(
                extensions=self._extensions,
                exclude_dirs=self._exclude_dirs,
                max_chars=self._max_chars,
            )
            self._repo_context_caches[key] = cache
        return cache
