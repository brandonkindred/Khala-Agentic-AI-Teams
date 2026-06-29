"""AgenticTeamAdapter — lets a testing persona drive any assembled agentic team.

The founder ``TargetTeamAdapter`` Protocol (``base.py``) assumes a three-phase
target (spec -> analysis -> build) with batched multiple-choice questions. An
agentic-team test pipeline is shaped differently: a single linear DAG run that
occasionally pauses on a free-text ``WAIT`` step. This adapter *collapses* the
two shapes onto the Protocol so the orchestrator drives an agentic team with no
orchestrator changes:

* **Analysis is a no-op pass-through.** ``start_from_spec`` records the persona
  spec; ``poll_analysis`` reports immediate completion and hands the spec
  forward as the phase's ``repo_path`` output (the value the Protocol threads
  from analysis into build). This carries the spec through the persisted run row
  so a resumed run still has it — the orchestrator stores ``repo_path`` and, on
  resume, feeds it straight to ``start_build``.
* **Build is the test-pipeline run.** ``start_build`` POSTs the spec as the
  pipeline's ``initial_input`` and returns the pipeline ``run_id``. ``poll_build``
  maps the pipeline status onto the founder poll contract — a
  ``waiting_for_input`` WAIT step becomes a *single free-text question* the
  persona answers, and ``submit_build_answers`` posts that free text back to the
  pipeline's ``/input`` endpoint.

It targets the *existing* provisioning endpoints under
``/api/agentic-team-provisioning`` — no new provisioning routes are required.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from user_agent_founder.targets.base import StartFailed

UNIFIED_API_BASE = os.environ.get("UNIFIED_API_BASE_URL", "http://localhost:8080")
PROVISIONING_PREFIX = "/api/agentic-team-provisioning"

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Sentinel job id returned by the collapsed (no-op) analysis phase. The
# orchestrator stores it as ``analysis_job_id`` but never calls back into the
# analysis endpoints, so its only job is to be a non-empty, recognizable marker.
_ANALYSIS_NOOP_JOB_ID = "agentic-team-analysis-noop"

# Terminal pipeline statuses, normalized to the founder poll contract's terminal
# states. ``waiting_for_input`` is handled separately (it becomes a question).
_TERMINAL = {"completed", "failed", "cancelled"}


class AgenticTeamAdapter:
    """Adapter that drives one provisioned agentic team's test pipeline.

    Invariants:
        * ``team_key == f"agentic_team:{team_id}"`` and ``process_id`` identify
          exactly one team + process; both are fixed for the adapter's lifetime.

    Preconditions (construction):
        * ``team_id`` is non-empty. ``process_id`` may be ``None`` at construction
          but MUST be set before ``start_build`` (enforced there with a
          ``StartFailed`` so the orchestrator marks the run failed cleanly rather
          than crashing the worker thread).
        * ``spec`` is the persona spec for the run when already known (resume
          path — see :meth:`poll_analysis`); ``None`` on a fresh start, where
          :meth:`start_from_spec` supplies it live.
    """

    def __init__(
        self, team_id: str, process_id: str | None = None, spec: str | None = None
    ) -> None:
        if not team_id:
            raise ValueError("AgenticTeamAdapter: team_id must be non-empty")
        self._team_id = team_id
        self._process_id = process_id
        # Carries the spec from analysis → build. Seeded at construction so a
        # resumed run (where start_from_spec is skipped) still has it from the
        # persisted run row; start_from_spec overwrites it on the live path.
        self._spec = spec or ""
        self.team_key = f"agentic_team:{team_id}"
        self.display_name = f"Agentic Team {team_id}"

    def _url(self, path: str) -> str:
        return f"{UNIFIED_API_BASE}{PROVISIONING_PREFIX}/teams/{self._team_id}{path}"

    # ── Phase 2: product analysis (collapsed to a no-op pass-through) ──────

    def start_from_spec(self, client: httpx.Client, project_name: str, spec: str) -> str:
        """No-op: an agentic team has no separate analysis phase.

        Postconditions: returns a sentinel job id; performs no HTTP call. The
            spec is surfaced to the build phase via :meth:`poll_analysis`'s
            ``repo_path``.
        """
        # Live path: capture the spec so poll_analysis hands it to the build
        # phase. (On resume start_from_spec is skipped — the spec instead comes
        # from the constructor seed, fed from the persisted run row.)
        self._spec = spec
        return _ANALYSIS_NOOP_JOB_ID

    def poll_analysis(self, client: httpx.Client, job_id: str) -> dict[str, Any]:
        """Report immediate completion, passing the spec forward as ``repo_path``.

        Postconditions: returns ``{"status": "completed", "repo_path": <spec>}``;
            performs no HTTP call. The orchestrator persists ``repo_path`` and
            feeds it to :meth:`start_build`. The spec comes from
            :meth:`start_from_spec` (live) or the constructor seed (resume), so a
            run resumed in the window between the sentinel ``analysis_job_id``
            being stored and ``repo_path`` being written still carries it.
        """
        return {"status": "completed", "repo_path": self._spec}

    def submit_analysis_answers(
        self, client: httpx.Client, job_id: str, answers: list[dict[str, Any]]
    ) -> None:
        """No-op: the collapsed analysis phase never raises questions."""
        return None

    # ── Phase 3: build == the agentic-team test-pipeline run ──────────────

    def start_build(self, client: httpx.Client, repo_path: str) -> str:
        """Start a test-pipeline run for the team's process. Returns the run id.

        ``repo_path`` carries the persona spec (see :meth:`poll_analysis`); it is
        sent as the pipeline ``initial_input``.

        Preconditions: ``self._process_id`` is set.
        Postconditions: a pipeline run is created for ``(team, process)`` and its
            ``run_id`` is returned. Raises :class:`StartFailed` if ``process_id``
            is missing or the create endpoint returns an HTTP error.
        """
        if not self._process_id:
            raise StartFailed(400, "AgenticTeamAdapter: process_id is required to start a run")
        resp = client.post(
            self._url("/test-pipeline/runs"),
            json={"process_id": self._process_id, "initial_input": repo_path},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise StartFailed(resp.status_code, resp.text)
        return resp.json().get("run_id", "")

    def poll_build(self, client: httpx.Client, job_id: str) -> dict[str, Any]:
        """Poll the pipeline run, mapping its status onto the founder contract.

        Postconditions: returns one of —
            * ``{"_poll_error": <code>}`` on HTTP error (orchestrator retries);
            * ``{"status": "completed"|"failed"|"cancelled", ...}`` at a terminal
              state (``error`` carried through on failure);
            * ``{"status": "waiting_for_input", "waiting_for_answers": True,
              "pending_questions": [<one free-text question>]}`` at a WAIT step;
            * ``{"status": <other>}`` while still running (orchestrator keeps
              polling).
        """
        resp = client.get(
            self._url(f"/test-pipeline/runs/{job_id}"),
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code >= 400:
            return {"_poll_error": resp.status_code}
        run = resp.json()
        status = run.get("status", "")

        if status == "waiting_for_input":
            prompt = run.get("human_prompt")
            if not prompt:
                # Paused but no prompt yet — treat as still running so the next
                # tick re-checks rather than surfacing an empty question.
                return {"status": "running"}
            step_id = run.get("current_step_id") or "wait"
            return {
                "status": "waiting_for_input",
                "waiting_for_answers": True,
                "pending_questions": [
                    {
                        # Id is stable per (run, step) so the orchestrator's
                        # failed-submission dedup (``failed_question_sets``, keyed
                        # by the question-id set) counts retries of the *same*
                        # WAIT prompt correctly. The pipeline clears
                        # ``waiting_for_input`` synchronously on ``/input``
                        # (``submit_human_input``), so a successfully-answered
                        # step doesn't re-surface the same id. Empty options force
                        # a free-text ("other") answer from the persona.
                        "id": f"{job_id}:{step_id}",
                        "question_text": prompt,
                        "context": f"Pipeline run {job_id}, step {step_id}.",
                        "options": [],
                    }
                ],
            }

        if status in _TERMINAL:
            return {"status": status, "error": run.get("error")}

        return {"status": status or "running"}

    def submit_build_answers(
        self, client: httpx.Client, job_id: str, answers: list[dict[str, Any]]
    ) -> None:
        """Post the persona's free-text answer to the pipeline's ``/input`` route.

        Preconditions: ``answers`` is the orchestrator's single-question batch for
            a WAIT step (``poll_build`` only ever raises one question at a time).
        Postconditions: the first answer's free text is submitted to resume the
            run. Raises ``httpx.HTTPStatusError`` on a non-2xx response (the
            orchestrator retries with backoff).
        """
        first = answers[0] if answers else {}
        text = first.get("other_text") or first.get("selected_option_id") or ""
        if not text.strip():
            # The pipeline /input endpoint requires a non-empty body; never post
            # an empty string (it would 422). A blank persona answer is degenerate
            # but we still advance the run with an explicit placeholder.
            text = "(no answer provided)"
        resp = client.post(
            self._url(f"/test-pipeline/runs/{job_id}/input"),
            json={"input": text},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
