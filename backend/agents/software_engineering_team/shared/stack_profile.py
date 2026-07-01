"""
Stack profile for the code-v2 teams.

``backend_code_v2_team`` and ``frontend_code_v2_team`` run structurally
identical phase logic that differs only in a handful of stack-specific knobs:
which language conventions to inject, how to label the detected language, and
how to detect it. :class:`StackProfile` captures those knobs as data + one
callable so the shared phase implementations can be written once and selected
per team — mirroring the ``SecurityProfile`` pattern in
``shared/security_service.py``.

This module holds **only** the dataclass — it imports nothing from either team,
so the ``shared → team → shared`` import cycle cannot form. Each team constructs
its own frozen instance (see ``<team>/phases/_profile.py``), passing its own
``detect_language`` and convention constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Protocol

from software_engineering_team.shared.models import Task


class PhaseModels(Protocol):
    """The team-local model/enum surface the shared code-v2 phase impls consume.

    Each team passes its own ``models`` module (``backend_code_v2_team.models`` /
    ``frontend_code_v2_team.models``) into the shared phase implementations, which
    read these symbols off it. Naming the surface here — instead of typing the
    parameter as an opaque ``ModuleType`` — makes the real dependency explicit in
    every signature and lets a type checker flag a renamed or removed model at
    the call site, rather than deferring the failure to a runtime
    ``AttributeError`` on the first execution of that code path.

    The members are the team's classes/enums used as values (constructed or
    compared), so they are typed ``type[Any]``. A plain module satisfies this
    Protocol structurally, so passing the team ``models`` module is unchanged at
    runtime.
    """

    DeliverResult: type[Any]
    DocumentationPhaseResult: type[Any]
    ExecutionResult: type[Any]
    Microtask: type[Any]
    MicrotaskStatus: type[Any]
    Phase: type[Any]
    PlanningResult: type[Any]
    ProblemSolvingResult: type[Any]
    ToolAgentInput: type[Any]
    ToolAgentKind: type[Any]
    ToolAgentPhaseInput: type[Any]


@dataclass(frozen=True)
class StackProfile:
    """Stack-specific configuration selecting backend vs frontend phase behavior.

    Invariants:
        - ``conventions_by_language`` always contains a ``"_default"`` key.
        - ``name`` and ``default_language`` are non-empty.
        - The instance is immutable (``frozen=True``); all fields are pure data
          except ``detect_language``, which is a pure inference callable.
    """

    name: str
    """Human-readable stack name used in log lines (e.g. ``"backend"``)."""

    default_language: str
    """Fallback language when detection yields nothing (e.g. ``"python"``)."""

    planning_language_label: str
    """Label for the language line in the planning context (``"Language"`` /
    ``"Language/stack"``)."""

    planning_progress_label: str
    """Token used in the planning progress log (``"language"`` / ``"stack"``)."""

    conventions_by_language: Dict[str, str]
    """Map of language → conventions text; must include a ``"_default"`` entry."""

    has_language_conventions: bool
    """Whether this stack's ``EXECUTION_PROMPT`` and
    ``PROBLEM_SOLVING_SINGLE_ISSUE_PROMPT`` carry a ``{language_conventions}``
    slot (backend: True, frontend: False).

    A single flag drives both prompts: the two were always set together, and a
    stack either injects language conventions into its LLM prompts or it does
    not. Keeping one field means the profile cannot disagree with itself about
    whether the ``{language_conventions}`` slot is present."""

    detect_language: Callable[[Path, Task], str]
    """Infer the project's language/stack from the repo and task."""

    def __post_init__(self) -> None:
        """Enforce the ``conventions_by_language`` invariant at construction.

        Preconditions: none.
        Postconditions: raises ``ValueError`` if ``conventions_by_language`` lacks
        a ``"_default"`` key, so :meth:`conventions_for` can never ``KeyError``.
        """
        if "_default" not in self.conventions_by_language:
            raise ValueError("conventions_by_language must contain a '_default' key")

    def conventions_for(self, language: str) -> str:
        """Return the conventions text for ``language``.

        Preconditions:
            ``language`` is a string; ``conventions_by_language`` has a
            ``"_default"`` entry (enforced by the class invariant).
        Postconditions:
            Returns the entry for ``language`` if present, else the
            ``"_default"`` entry. Pure; no side effects.
        """
        return self.conventions_by_language.get(language, self.conventions_by_language["_default"])
