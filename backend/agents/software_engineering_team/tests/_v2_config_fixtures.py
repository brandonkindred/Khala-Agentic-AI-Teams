"""Shared synthetic-fixture builders for V2TeamConfig-related tests.

Not a test module itself — its name doesn't match pytest's default
``test_*.py``/``*_test.py`` collection pattern, so it is never collected as a
test file (mirroring ``tests/submission_pass_two_call_client.py``, imported
the same way via ``from tests.<module> import ...``). ``test_v2_team_config.py``
and ``test_v2_config_orchestrator.py`` both import ``make_stack_profile`` from
here instead of each defining their own copy, so a ``StackProfile`` field or
default change only needs updating in one place.
"""

from __future__ import annotations

from typing import Callable, Dict, FrozenSet, Optional, Tuple

from software_engineering_team.shared.stack_profile import StackProfile


def make_stack_profile(
    *,
    default_language: str = "python",
    conventions_by_language: Optional[Dict[str, str]] = None,
    repo_extensions: Optional[FrozenSet[str]] = None,
    repo_exclude_dirs: Optional[FrozenSet[str]] = None,
    repo_max_chars: int = 1000,
    detect_tooling: Optional[Callable[..., Tuple[bool, bool]]] = None,
) -> StackProfile:
    """Build a minimal synthetic ``StackProfile`` for tests that don't need a real team's.

    Preconditions: none — every parameter has a test-friendly default.
    Postconditions: returns a valid ``StackProfile`` instance (satisfies its
      own ``"_default"``-key invariant via the ``conventions_by_language``
      default). Pure; no side effects.
    """
    return StackProfile(
        name="test",
        default_language=default_language,
        planning_language_label="Language",
        planning_progress_label="language",
        conventions_by_language=conventions_by_language or {"_default": "PY"},
        has_language_conventions=True,
        build_verify_label="test_code_v2",
        detect_language=lambda _p, _t: default_language,
        repo_extensions=repo_extensions or frozenset({".py"}),
        repo_exclude_dirs=repo_exclude_dirs or frozenset({".git"}),
        repo_max_chars=repo_max_chars,
        detect_tooling=detect_tooling or (lambda _p: (True, True)),
    )
