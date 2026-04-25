"""TargetTeamAdapter Protocol.

Lifts team-specific HTTP coupling out of the founder orchestrator. Each
adapter wraps a target team's start/poll/answer endpoints behind the
same shape, so the orchestrator can drive any registered team without
hardcoding URLs. Defined verbatim in
``system_design/FEATURE_SPEC_testing_personas.md`` §4.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx


class StartFailed(RuntimeError):
    """Raised by an adapter's ``start_*`` method when the target team's
    submit endpoint returns an HTTP error.

    Lives in ``base.py`` so the orchestrator can catch a single
    adapter-agnostic exception without reaching into a specific
    adapter's internals.
    """

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"{status_code} {body[:500]}")
        self.status_code = status_code
        self.body = body


@runtime_checkable
class TargetTeamAdapter(Protocol):
    team_key: str
    display_name: str

    def start_from_spec(self, client: httpx.Client, project_name: str, spec: str) -> str: ...
    def poll_analysis(self, client: httpx.Client, job_id: str) -> dict[str, Any]: ...
    def submit_analysis_answers(
        self, client: httpx.Client, job_id: str, answers: list[dict[str, Any]]
    ) -> None: ...
    def start_build(self, client: httpx.Client, repo_path: str) -> str: ...
    def poll_build(self, client: httpx.Client, job_id: str) -> dict[str, Any]: ...
    def submit_build_answers(
        self, client: httpx.Client, job_id: str, answers: list[dict[str, Any]]
    ) -> None: ...
