"""AgenticTeamAdapter — lets a testing persona drive any assembled agentic team.

The founder ``TargetTeamAdapter`` Protocol (``base.py``) assumes a three-phase
target (spec -> analysis -> build) with batched multiple-choice questions. An
agentic-team test pipeline is shaped differently: a single linear DAG run that
occasionally pauses on a free-text ``WAIT`` step. This adapter *collapses* the
two shapes onto the Protocol so the orchestrator drives an agentic team with no
orchestrator changes:

* **Analysis is a no-op pass-through.** ``start_from_spec`` records the persona
  spec on the adapter; ``poll_analysis`` reports immediate completion carrying no
  phase output. The spec reaches the build phase via the adapter's ``self._spec``
  — set live by ``start_from_spec`` or, on a resumed run where that call is
  skipped, seeded at construction from the persisted ``spec_content`` column. The
  Protocol's ``repo_path`` analysis→build slot is *not* used here (it stays
  ``None``/NULL for agentic runs — it means a real filesystem path, which an
  agentic team has none of).
* **Build is the test-pipeline run.** ``start_build`` POSTs the spec as the
  pipeline's ``initial_input`` and returns the pipeline ``run_id``. ``poll_build``
  maps the pipeline status onto the founder poll contract — a
  ``waiting_for_input`` WAIT step becomes a *single free-text question* the
  persona answers, and ``submit_build_answers`` posts that free text back to the
  pipeline's ``/input`` endpoint.

It targets the *existing* provisioning endpoints under
``/api/agentic-team-provisioning`` — no new provisioning routes are required.

The decision to collapse rather than generalize the Protocol — and the exact
contract boundary this adapter depends on — is recorded in
``system_design/adr/ADR-007-founder-agentic-team-adapter-collapse.md``;
``tests/test_adapter_agentic_team_contract_drift.py`` is the tripwire that
fails when either side's shape drifts.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from agent_team_studio.user_agent_founder.targets.base import StartFailed

UNIFIED_API_BASE = os.environ.get("UNIFIED_API_BASE_URL", "http://localhost:8080")
PROVISIONING_PREFIX = "/api/agentic-team-provisioning"

HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# A team id is a server-minted opaque slug — alphanumerics plus hyphen and
# underscore (it is *not* strictly a bare UUID: provisioning ids may carry an
# underscore-bearing prefix). It is **user-controlled** (parsed from
# ``target_team_key`` on ``/start``) and goes into a URL path, so it is validated
# against this safe charset to block path traversal (``..``, ``/``, ``\``) into
# other provisioning endpoints. Reject anything else at construction.
_TEAM_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Sentinel job id returned by the collapsed (no-op) analysis phase. The
# orchestrator stores it as ``analysis_job_id`` but never calls back into the
# analysis endpoints, so its only job is to be a non-empty, recognizable marker.
_ANALYSIS_NOOP_JOB_ID = "agentic-team-analysis-noop"

# Posted to the pipeline's ``/input`` when the persona's WAIT answer is genuinely
# empty, so the run still advances and the endpoint's min-length check passes.
_NO_ANSWER_PLACEHOLDER = "(no answer provided)"

# Terminal pipeline statuses. ``waiting_for_input`` is handled separately (it
# becomes a question). Both British ("cancelled") and American ("canceled")
# spellings are accepted as *input* because the upstream pipeline status string
# is not normalized at the source; ``poll_build`` below rewrites either to the
# single canonical "cancelled" before returning, so callers (the orchestrator's
# ``_run_phase``, shared across every target-team adapter) only ever need to
# match one spelling.
_TERMINAL = {"completed", "failed", "cancelled", "canceled"}
_CANCELLED_SPELLINGS = {"cancelled", "canceled"}


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
        # Reject path-traversal / unsafe characters up front: ``team_id`` is
        # user-controlled and lands in a URL path. ``get_adapter`` surfaces this
        # as a 400 on ``/start`` rather than letting a crafted id reach the
        # provisioning service.
        if not _TEAM_ID_RE.match(team_id):
            raise ValueError(f"AgenticTeamAdapter: invalid team_id {team_id!r}")
        self._team_id = team_id
        self._process_id = process_id
        # Carries the spec from analysis → build. Seeded at construction so a
        # resumed run (where start_from_spec is skipped) still has it from the
        # persisted run row; start_from_spec overwrites it on the live path.
        self._spec = spec or ""
        self.team_key = f"agentic_team:{team_id}"
        self.display_name = f"Agentic Team {team_id}"

    def _url(self, path: str) -> str:
        # rstrip the base so a trailing-slash env value can't yield ``//api``;
        # percent-encode the team-id path segment (defense-in-depth alongside the
        # constructor charset check) so it can never traverse the path.
        base = UNIFIED_API_BASE.rstrip("/")
        team = quote(self._team_id, safe="")
        return f"{base}{PROVISIONING_PREFIX}/teams/{team}{path}"

    # ── Phase 2: product analysis (collapsed to a no-op pass-through) ──────

    def start_from_spec(self, client: httpx.Client, project_name: str, spec: str) -> str:
        """No-op: an agentic team has no separate analysis phase.

        Postconditions: captures ``spec`` on ``self._spec`` for the build phase;
            returns a sentinel job id; performs no HTTP call.
        """
        # Live path: capture the spec so start_build sends it as the pipeline's
        # initial_input. (On resume start_from_spec is skipped — the spec instead
        # comes from the constructor seed, fed from the persisted run row's
        # spec_content.)
        self._spec = spec
        return _ANALYSIS_NOOP_JOB_ID

    def poll_analysis(self, client: httpx.Client, job_id: str) -> dict[str, Any]:
        """Report immediate completion with no phase output.

        Postconditions: returns ``{"status": "completed"}``; performs no HTTP
            call and carries no ``repo_path`` (the spec reaches build via
            ``self._spec``, not the Protocol's analysis→build slot). The
            orchestrator treats the phase as succeeded and leaves ``repo_path``
            NULL for this run.
        """
        return {"status": "completed"}

    def submit_analysis_answers(
        self, client: httpx.Client, job_id: str, answers: list[dict[str, Any]]
    ) -> None:
        """No-op: the collapsed analysis phase never raises questions."""
        return None

    # ── Phase 3: build == the agentic-team test-pipeline run ──────────────

    def start_build(self, client: httpx.Client, repo_path: str) -> str:
        """Start a test-pipeline run for the team's process. Returns the run id.

        The persona spec sent as the pipeline ``initial_input`` comes from
        ``self._spec`` (set live by :meth:`start_from_spec`, or seeded at
        construction from the persisted ``spec_content`` on resume). The
        ``repo_path`` argument is the Protocol's analysis→build handoff and is
        meaningful only to the software-engineering target; it is ``None`` for an
        agentic run and is intentionally ignored here.

        Preconditions: ``self._process_id`` and ``self._spec`` are both non-empty.
        Postconditions: a pipeline run is created for ``(team, process)`` and its
            non-empty ``run_id`` is returned. Raises :class:`StartFailed` if
            ``process_id`` or the spec is missing, a transport error occurs
            (connect/timeout/DNS), the create endpoint returns an HTTP error, or
            the response carries no ``run_id`` (so a malformed response fails fast
            instead of polling an empty job id to timeout). Never lets a raw
            transport exception escape — the orchestrator marks the run failed
            cleanly.
        """
        if not self._process_id:
            raise StartFailed(400, "AgenticTeamAdapter: process_id is required to start a run")
        # Fail fast on an empty spec rather than posting an empty initial_input
        # that the provisioning endpoint would reject with an opaque 422
        # (StartPipelineRunRequest enforces min_length=1). In practice the spec is
        # always populated (Phase 1 writes spec_content before Phase 2), so an
        # empty value signals a malformed run that should fail with a clear cause.
        if not self._spec:
            raise StartFailed(400, "AgenticTeamAdapter: persona spec is required to start a run")
        try:
            resp = client.post(
                self._url("/test-pipeline/runs"),
                json={"process_id": self._process_id, "initial_input": self._spec},
                timeout=HTTP_TIMEOUT,
            )
        except httpx.RequestError as exc:
            # Transient transport failure (connect/timeout/DNS): surface a clean
            # StartFailed so the orchestrator marks the run failed rather than the
            # raw exception crashing the worker thread.
            raise StartFailed(502, f"Pipeline create request failed: {str(exc)[:200]}") from exc
        if resp.status_code >= 400:
            # Truncate the upstream body so an internal error page / stack trace
            # from the provisioning service isn't echoed wholesale to the caller.
            raise StartFailed(resp.status_code, (resp.text or "")[:200])
        try:
            body = resp.json()
        except ValueError as exc:  # non-JSON 2xx (e.g. an HTML proxy page)
            raise StartFailed(502, f"Invalid JSON from pipeline create: {exc}") from exc
        if not isinstance(body, dict):  # valid JSON but a list/scalar ⇒ no .get()
            raise StartFailed(502, "Provisioning create response is not a JSON object")
        run_id = body.get("run_id")
        if not run_id:
            raise StartFailed(502, "Provisioning response missing run_id")
        return run_id

    def poll_build(self, client: httpx.Client, job_id: str) -> dict[str, Any]:
        """Poll the pipeline run, mapping its status onto the founder contract.

        Postconditions: returns one of —
            * ``{"_poll_error": <code>, "detail": <truncated body>}`` on HTTP
              error, or ``{"_poll_error": 502}`` on a non-JSON/non-object body
              (orchestrator retries — a transient proxy page shouldn't fail the run);
            * ``{"status": "completed"|"failed"|"cancelled", ...}`` at a terminal
              state (``error`` carried through on failure);
            * ``{"status": "waiting_for_input", "waiting_for_answers": True,
              "pending_questions": [<one free-text question>]}`` at a WAIT step;
            * ``{"status": <other>}`` while still running (orchestrator keeps
              polling).
        """
        try:
            resp = client.get(
                self._url(f"/test-pipeline/runs/{quote(job_id, safe='')}"),
                timeout=HTTP_TIMEOUT,
            )
        except httpx.RequestError as exc:
            # Transient transport failure ⇒ a retryable poll error, not a crash
            # (the orchestrator keeps polling on ``_poll_error``).
            return {"_poll_error": 502, "detail": str(exc)[:200]}
        if resp.status_code >= 400:
            # Carry a truncated body for diagnostics; the orchestrator keys off
            # ``_poll_error`` and ignores extra keys.
            return {"_poll_error": resp.status_code, "detail": (resp.text or "")[:200]}
        try:
            run = resp.json()
        except ValueError:  # non-JSON 2xx ⇒ treat as a transient poll error
            return {"_poll_error": 502}
        if not isinstance(run, dict):  # valid JSON but a list/scalar ⇒ no .get()
            return {"_poll_error": 502}
        status = run.get("status", "")

        if status == "waiting_for_input":
            prompt = run.get("human_prompt")
            if not prompt:
                # Paused but no prompt yet — treat as still running so the next
                # tick re-checks rather than surfacing an empty question.
                return {"status": "running"}
            # Treat a missing *or empty* step id as absent → fall back to "wait"
            # so the question id can't become ``"run-9:"`` (an empty step
            # component would collide across distinct WAIT steps in the
            # orchestrator's id-set dedup). A non-empty falsy-looking id (e.g.
            # the literal "0") is still a valid distinct step and is preserved.
            step_id = run.get("current_step_id")
            if step_id is None or step_id == "":
                step_id = "wait"
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
                        # Richer context than a bare "run X, step Y" so the
                        # persona is grounded when it answers open-ended: it flows
                        # verbatim into FREE_TEXT_ANSWERING_PROMPT's {context}
                        # (agent.answer_question takes the free-text branch on the
                        # empty options below). Names the open-ended nature and the
                        # no-human-in-the-loop resume so the answer is authored to
                        # be decisive and self-contained.
                        "context": (
                            f"This is an open-ended request from step '{step_id}' of "
                            f"automated pipeline run {job_id}. There are no predefined "
                            f"choices — write the answer yourself. Your reply is "
                            f"submitted as-is to resume the run; no one will ask a "
                            f"follow-up."
                        ),
                        "options": [],
                    }
                ],
            }

        if status in _TERMINAL:
            # Rewrite either cancellation spelling to the canonical "cancelled"
            # so _run_phase's exact-string terminal check (shared by every
            # adapter) recognizes it — otherwise a pipeline that reports the
            # American spelling would poll for MAX_POLL_ATTEMPTS and time out
            # instead of failing promptly with a clear "was cancelled" error.
            if status in _CANCELLED_SPELLINGS:
                status = "cancelled"
            return {"status": status, "error": run.get("error")}

        return {"status": status or "running"}

    def submit_build_answers(
        self, client: httpx.Client, job_id: str, answers: list[dict[str, Any]]
    ) -> None:
        """Post the persona's free-text answer to the pipeline's ``/input`` route.

        Preconditions: ``answers`` is the orchestrator's single-question batch for
            a WAIT step (``poll_build`` only ever raises one question at a time).
        Postconditions: the first answer's free text is submitted to resume the
            run. Raises ``httpx.HTTPStatusError`` on a non-2xx response; the
            orchestrator retries every failure with backoff except a 409, which it
            treats as terminal (the run is no longer resumable).
        """
        first = answers[0] if answers else {}
        # The WAIT question carries empty options, so ``selected_option_id`` is
        # always the synthetic ``"other"`` — only ``other_text`` is a meaningful
        # answer. Never fall back to ``selected_option_id`` (that would post the
        # literal "other"); use the placeholder when the free text is genuinely
        # absent so the run still advances and the /input min-length check passes.
        # Explicit None check (not ``or``): a valid *falsy* free-text answer (the
        # string "0", or a numeric 0) is preserved rather than dropped. ``str()``
        # coerces so a non-string value can't crash ``.strip()``.
        raw = first.get("other_text")
        text = str(raw).strip() if raw is not None else ""
        if not text:
            text = _NO_ANSWER_PLACEHOLDER
        try:
            resp = client.post(
                self._url(f"/test-pipeline/runs/{quote(job_id, safe='')}/input"),
                json={"input": text},
                timeout=HTTP_TIMEOUT,
            )
        except httpx.RequestError as exc:
            # Transient transport failure (connect/timeout/DNS): re-raise as an
            # HTTPStatusError(502) so the orchestrator's answer-submission retry
            # loop (which only catches HTTPStatusError) backs off and retries,
            # consistent with start_build/poll_build. A bare RequestError would
            # otherwise escape the retry and fail the run on the first blip.
            raise httpx.HTTPStatusError(
                f"Answer submission request failed: {str(exc)[:200]}",
                request=exc.request,
                response=httpx.Response(502, request=exc.request),
            ) from exc
        resp.raise_for_status()
