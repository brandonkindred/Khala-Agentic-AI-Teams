"""SoftwareEngineeringAdapter — wraps the SE team's start/poll/answer endpoints."""

from __future__ import annotations

import os
from typing import Any

import httpx

from user_agent_founder.targets.base import StartFailed

UNIFIED_API_BASE = os.environ.get("UNIFIED_API_BASE_URL", "http://localhost:8080")
SE_PREFIX = "/api/software-engineering"

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class SoftwareEngineeringAdapter:
    """Adapter for the Software Engineering team.

    Wraps the existing endpoints under ``/api/software-engineering`` —
    no SE-side changes required.
    """

    team_key = "software_engineering"
    display_name = "Software Engineering"

    def _url(self, path: str) -> str:
        return f"{UNIFIED_API_BASE}{SE_PREFIX}{path}"

    # ── Phase 2: product analysis ─────────────────────────────────────

    def start_from_spec(self, client: httpx.Client, project_name: str, spec: str) -> str:
        resp = client.post(
            self._url("/product-analysis/start-from-spec"),
            json={"project_name": project_name, "spec_content": spec},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise StartFailed(resp.status_code, resp.text)
        return resp.json().get("job_id", "")

    def poll_analysis(self, client: httpx.Client, job_id: str) -> dict[str, Any]:
        resp = client.get(
            self._url(f"/product-analysis/status/{job_id}"),
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            return {"_poll_error": resp.status_code}
        return resp.json()

    def submit_analysis_answers(
        self, client: httpx.Client, job_id: str, answers: list[dict[str, Any]]
    ) -> None:
        resp = client.post(
            self._url(f"/product-analysis/{job_id}/answers"),
            json={"answers": answers},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()

    # ── Phase 3: build ────────────────────────────────────────────────

    def start_build(self, client: httpx.Client, repo_path: str) -> str:
        resp = client.post(
            self._url("/run-team"),
            json={"repo_path": repo_path},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise StartFailed(resp.status_code, resp.text)
        return resp.json().get("job_id", "")

    def poll_build(self, client: httpx.Client, job_id: str) -> dict[str, Any]:
        resp = client.get(
            self._url(f"/run-team/{job_id}"),
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            return {"_poll_error": resp.status_code}
        return resp.json()

    def submit_build_answers(
        self, client: httpx.Client, job_id: str, answers: list[dict[str, Any]]
    ) -> None:
        resp = client.post(
            self._url(f"/run-team/{job_id}/answers"),
            json={"answers": answers},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
