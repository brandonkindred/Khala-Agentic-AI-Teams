"""
Shared base for the code-v2 Development Agents (backend + frontend).

``BackendDevelopmentAgent`` and ``FrontendDevelopmentAgent`` share their
constructor, their repo-briefing read (including the incremental
:class:`~software_engineering_team.shared.repo_context_cache.RepoContextCache`
fast path), and their tool-runner construction verbatim; only the per-team
tool-agent roster, tooling detection, repo extension/exclude sets, and the
integration-only ``run_workflow`` differ. This base holds the shared members;
each team subclasses it and supplies the divergent parts.

The bulk-divergent ``run_workflow`` bodies deliberately stay per-team: they are
``# pragma: no cover`` integration code carrying ~100 lines of team-specific
status/progress/result wiring, so converging them safely is a separate,
test-guarded change rather than part of this base.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from llm_service import LLMClient
from software_engineering_team.shared.repo_context_cache import RepoContextCache
from software_engineering_team.shared.tool_agent_runners import build_tool_runners


class BaseV2DevelopmentAgent:
    """Shared base for the code-v2 Development Agents.

    Subclasses provide the per-team ``_read_repo_code`` (extension/exclude sets +
    briefing budget), ``_detect_tooling``, tool-agent roster, and ``run_workflow``.

    Invariants: instance state is limited to ``llm`` and ``_repo_context_cache``,
    so a subclass built via ``__new__`` and given those two attributes behaves
    identically to a constructed one.
    """

    def __init__(self, llm_client: LLMClient) -> None:
        assert llm_client is not None, "llm_client is required"
        self.llm = llm_client
        # Optional incremental repo-context cache threaded in by the team lead so
        # the per-task briefing re-reads only changed files instead of re-walking
        # the whole repo. None (the direct-construction/test path) falls back to
        # the fresh-walk ``_read_repo_code``.
        self._repo_context_cache: Optional[RepoContextCache] = None

    def _build_tool_runners(self, tool_agents: Dict[Any, Any]) -> Dict[Any, Callable[..., Any]]:
        """Build run callables from tool agent instances (for the Execution phase)."""
        return build_tool_runners(tool_agents)

    def _read_repo_code(self, repo_path: Path, max_chars: Optional[int] = None) -> str:
        """Per-team repo briefing reader.

        Subclasses override this (as a ``@staticmethod``) with their own extension
        / exclude sets and default briefing budget; the base only declares the
        contract that :meth:`_read_existing_code` depends on.
        """
        raise NotImplementedError  # pragma: no cover - always overridden

    def _read_existing_code(self, repo_path: Path) -> str:
        """Return the repo briefing, consulting the incremental cache when one is threaded in.

        Preconditions: ``repo_path`` is an existing directory.
        Postconditions: returns a briefing byte-identical to
          ``_read_repo_code(repo_path)`` for the current on-disk state; when a
          cache is present it re-reads only changed eligible files. Raises
          ``AssertionError`` if the precondition is violated (caller bug).
        Invariants: with no cache the fresh walk runs each call; with a cache the
          output never differs from the fresh walk, only the amount of file I/O.

        The no-cache branch calls ``_read_repo_code(repo_path)`` with no kwargs
        deliberately: callers (and tests) monkeypatch it with a no-kwargs
        signature, and the cache carries its own char budget, so forwarding one
        here would both break that patch surface and be ignored.
        """
        assert repo_path.is_dir(), "repo_path must be an existing directory"
        if self._repo_context_cache is not None:
            return self._repo_context_cache.read(repo_path)
        return self._read_repo_code(repo_path)
