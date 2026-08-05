"""Contract + integration tests for ``AgenticTeamAdapter`` and dynamic dispatch.

Locks:
1. The adapter satisfies the ``TargetTeamAdapter`` Protocol shape.
2. ``get_adapter`` parses ``"agentic_team:<id>"`` keys and threads ``process_id``.
3. The *collapse*: analysis is a no-op pass-through carrying the spec; build maps
   the agentic test-pipeline status onto the founder poll contract (a
   ``waiting_for_input`` WAIT step becomes one free-text question; the answer is
   posted to ``/input``).
4. End-to-end: a persona drives a collapsed run (spec → no-op analysis → pipeline
   build with one WAIT step answered → completed) through the real orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Reusable test doubles (mirror test_adapter_se.py shapes)
# ---------------------------------------------------------------------------


@dataclass
class _FakeRun:
    """Fake StoredRun record for adapter tests."""

    run_id: str
    status: str = "running"
    se_job_id: str | None = None
    analysis_job_id: str | None = None
    spec_content: str | None = None
    repo_path: str | None = None
    target_team_key: str = "agentic_team:t1"
    persona_id: str | None = "startup-founder"
    project_name: str | None = "growth-pod-test"
    process_id: str | None = "proc1"
    created_at: str = "2026-06-29T00:00:00+00:00"
    updated_at: str = "2026-06-29T00:00:00+00:00"
    error: str | None = None


class _FakeStore:
    """Minimal in-memory founder-store double that records calls."""

    def __init__(self, run: _FakeRun) -> None:
        self._run = run
        self.update_calls: list[dict[str, Any]] = []
        self.chat_messages: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []

    def get_run(self, run_id: str) -> _FakeRun | None:
        return self._run if self._run.run_id == run_id else None

    def update_run(self, run_id: str, **fields: Any) -> bool:
        self.update_calls.append({"run_id": run_id, **fields})
        for k, v in fields.items():
            setattr(self._run, k, v)
        return True

    def add_chat_message(
        self, run_id: str, role: str, content: str, message_type: str = "", *, metadata: Any = None
    ) -> None:
        self.chat_messages.append({"run_id": run_id, "role": role, "type": message_type})

    def add_decision(self, **fields: Any) -> None:
        self.decisions.append(fields)


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: dict | None = None,
        text: str = "",
        bad_json: bool = False,
        list_body: list | None = None,
    ) -> None:
        self.status_code = status_code
        # ``list_body`` lets a test return a *valid JSON list* (no ``.get()``),
        # distinct from ``bad_json`` (a JSON decode error).
        self._json = list_body if list_body is not None else (json_data or {})
        self.text = text
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=MagicMock(), response=MagicMock(self)
            )


class _FakeHttpxClient:
    """Records every POST/GET; returns scripted responses keyed by URL substring.

    ``post_responses`` / ``get_responses`` are matched in **insertion order**, so
    list a more specific needle (``"/input"``) before a more general one
    (``"/test-pipeline/runs"``) that is a substring of the same URL.
    """

    def __init__(
        self,
        post_responses: dict[str, _FakeResponse] | None = None,
        get_responses: dict[str, list[_FakeResponse]] | None = None,
    ) -> None:
        self.post_responses = post_responses or {}
        self.get_responses = get_responses or {}
        self._get_indices: dict[str, int] = {}
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def post(self, url: str, *, json: dict | None = None, timeout: Any = None) -> _FakeResponse:
        self.posts.append({"url": url, "json": json})
        for needle, resp in self.post_responses.items():
            if needle in url:
                return resp
        # No needle matched: fail loudly rather than silently returning a
        # plausible-looking 200. A test that forgets to register a URL should
        # fail on the missing mock, not pass on an accidental default body.
        raise AssertionError(f"_FakeHttpxClient.post: no post_responses entry matches {url!r}")

    def get(self, url: str, *, timeout: Any = None) -> _FakeResponse:
        self.gets.append({"url": url})
        for needle, queue in self.get_responses.items():
            if needle in url:
                idx = self._get_indices.get(needle, 0)
                if idx >= len(queue):
                    return queue[-1]
                self._get_indices[needle] = idx + 1
                return queue[idx]
        # No needle matched: fail loudly (see post() above) rather than masking
        # a missing get_responses entry with a fake "completed" status.
        raise AssertionError(f"_FakeHttpxClient.get: no get_responses entry matches {url!r}")


@pytest.fixture
def stub_orchestrator_io(monkeypatch):
    """Patch orchestrator I/O (sleeps, side-effecting helpers) for fast, deterministic tests."""
    from agent_team_studio.user_agent_founder import orchestrator

    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    monkeypatch.setattr(orchestrator, "ANALYSIS_POLL_INTERVAL", 0)
    monkeypatch.setattr(orchestrator, "EXECUTION_POLL_INTERVAL", 0)
    monkeypatch.setattr(orchestrator, "SPEC_HEARTBEAT_INTERVAL", 0.01)
    monkeypatch.setattr(orchestrator, "_sync_job_status", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_heartbeat", lambda _rid: None)
    return orchestrator


# ---------------------------------------------------------------------------
# Protocol contract + dynamic dispatch
# ---------------------------------------------------------------------------


def test_agentic_team_adapter_satisfies_protocol():
    """AgenticTeamAdapter satisfies the TargetTeamAdapter protocol and sets team_key/display_name."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, TargetTeamAdapter

    adapter = AgenticTeamAdapter("team-xyz", process_id="p1")
    assert isinstance(adapter, TargetTeamAdapter)
    assert adapter.team_key == "agentic_team:team-xyz"
    assert adapter.display_name


def test_adapter_rejects_empty_team_id():
    """AgenticTeamAdapter raises ValueError when team_id is empty."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    with pytest.raises(ValueError, match="team_id must be non-empty"):
        AgenticTeamAdapter("", process_id="p1")


def test_get_adapter_parses_agentic_key_and_threads_process_id():
    """get_adapter parses 'agentic_team:<id>' keys and threads process_id into the adapter."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, get_adapter

    adapter = get_adapter("agentic_team:abc", process_id="proc-7")
    assert isinstance(adapter, AgenticTeamAdapter)
    assert adapter._team_id == "abc"
    assert adapter._process_id == "proc-7"
    # Fresh instance each call — no cross-run state leakage.
    assert get_adapter("agentic_team:abc") is not adapter


def test_get_adapter_rejects_malformed_agentic_key():
    """get_adapter raises ValueError for 'agentic_team:' with a missing team id."""
    from agent_team_studio.user_agent_founder.targets import get_adapter

    with pytest.raises(ValueError, match="missing team id"):
        get_adapter("agentic_team:")


def test_get_adapter_rejects_empty_team_key():
    """An empty team_key has no resolvable adapter; get_adapter rejects it up
    front rather than threading "" into the static-registry lookup."""
    from agent_team_studio.user_agent_founder.targets import get_adapter

    with pytest.raises(ValueError, match="team_key must be non-empty"):
        get_adapter("")


def test_adapter_rejects_path_traversal_team_id():
    """team_id is user-controlled (from target_team_key) and lands in a URL path,
    so traversal / unsafe characters must be rejected at construction — get_adapter
    surfaces it as a ValueError the /start endpoint turns into a 400."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, get_adapter

    for bad in ["../../etc", "a/b", "a\\b", "..", "te am", "a;b", "a?b"]:
        with pytest.raises(ValueError, match="invalid team_id"):
            AgenticTeamAdapter(bad, process_id="p1")
    with pytest.raises(ValueError, match="invalid team_id"):
        get_adapter("agentic_team:../../sensitive")


def test_url_construction_targets_provisioning_mount():
    """Adapter._url builds the provisioning-service URL for a given path."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="p1")
    url = adapter._url("/test-pipeline/runs")
    assert url.endswith("/api/agentic-team-provisioning/teams/t1/test-pipeline/runs")


# ---------------------------------------------------------------------------
# Collapse: analysis is a no-op pass-through carrying the spec
# ---------------------------------------------------------------------------


def test_analysis_is_noop_passthrough():
    """The analysis phase is a no-op that completes without HTTP and carries no output.

    The spec reaches build via the adapter's own state (see
    test_constructor_spec_seed_reaches_build), not through the poll's phase output.
    """
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="p1")
    fake = _FakeHttpxClient()
    job_id = adapter.start_from_spec(fake, "proj", "# SPEC BODY")
    # No HTTP call; the spec is captured on the adapter, not emitted as repo_path.
    assert fake.posts == []
    assert job_id  # a non-empty sentinel
    status = adapter.poll_analysis(fake, job_id)
    assert status == {"status": "completed"}
    assert fake.gets == []
    # submit_analysis_answers is a no-op (the collapsed phase raises no questions).
    assert adapter.submit_analysis_answers(fake, job_id, [{"x": 1}]) is None


def test_constructor_spec_seed_reaches_build():
    """Resume window: the analysis sentinel was stored but the run never re-ran
    start_from_spec, so a fresh adapter carries the spec only via its constructor
    seed (from the persisted spec_content). That seed must reach start_build's
    initial_input — the analysis phase itself emits no phase output."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, get_adapter

    adapter = AgenticTeamAdapter("t1", process_id="p1", spec="# PERSISTED SPEC")
    # poll_analysis is reached directly (start_from_spec skipped on resume) and
    # carries no repo_path.
    assert adapter.poll_analysis(_FakeHttpxClient(), "noop") == {"status": "completed"}
    # The seeded spec — not the ignored repo_path arg — becomes initial_input.
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(201, {"run_id": "run-seed"})}
    )
    assert adapter.start_build(fake, "ignored-repo-path") == "run-seed"
    assert fake.posts[0]["json"] == {"process_id": "p1", "initial_input": "# PERSISTED SPEC"}
    # get_adapter threads the seed through too.
    seeded = get_adapter("agentic_team:t1", process_id="p1", spec="# VIA FACTORY")
    seeded_fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(201, {"run_id": "run-factory"})}
    )
    seeded.start_build(seeded_fake, "ignored-repo-path")
    assert seeded_fake.posts[0]["json"]["initial_input"] == "# VIA FACTORY"


# ---------------------------------------------------------------------------
# Build: start / poll-status mapping / answer submission
# ---------------------------------------------------------------------------


def test_start_build_posts_process_and_spec_returns_run_id():
    """start_build POSTs process_id + the seeded spec as initial_input and returns
    the pipeline run_id. The spec comes from the adapter's own state; the repo_path
    argument is the SE-only Protocol handoff and is ignored here."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1", spec="# SPEC")
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(201, {"run_id": "run-9"})}
    )
    run_id = adapter.start_build(fake, "ignored-repo-path")
    assert run_id == "run-9"
    assert fake.posts[0]["url"].endswith("/teams/t1/test-pipeline/runs")
    assert fake.posts[0]["json"] == {"process_id": "proc1", "initial_input": "# SPEC"}


def test_start_build_requires_process_id():
    """start_build raises StartFailed(400) when process_id is None."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id=None, spec="# SPEC")
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(_FakeHttpxClient(), "ignored-repo-path")
    assert exc.value.status_code == 400


def test_start_build_requires_spec():
    """start_build raises StartFailed(400) when the persona spec is empty — an
    empty initial_input would otherwise be rejected by the provisioning endpoint's
    min_length check with an opaque 422."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1")  # no spec seeded
    fake = _FakeHttpxClient()
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "ignored-repo-path")
    assert exc.value.status_code == 400
    # Fails fast before any HTTP call.
    assert fake.posts == []


def test_start_build_raises_on_http_error():
    """start_build raises StartFailed with the upstream status code on an HTTP error."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1", spec="# SPEC")
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(404, {}, text="no such process")}
    )
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "ignored-repo-path")
    assert exc.value.status_code == 404


def test_start_build_raises_when_response_has_no_run_id():
    """A 2xx create response missing run_id fails fast (502) instead of returning
    an empty job id that the orchestrator would poll to timeout."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1", spec="# SPEC")
    fake = _FakeHttpxClient(post_responses={"/test-pipeline/runs": _FakeResponse(201, {})})
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "ignored-repo-path")
    assert exc.value.status_code == 502


def test_start_build_raises_on_non_json_2xx():
    """A 2xx body that isn't JSON (e.g. an HTML proxy page) → StartFailed(502),
    not an unhandled JSONDecodeError crashing the worker thread."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1", spec="# SPEC")
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(200, bad_json=True)}
    )
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "ignored-repo-path")
    assert exc.value.status_code == 502


def test_poll_build_non_json_2xx_is_a_poll_error():
    """A 2xx non-JSON body during polling → a transient _poll_error (retry), not a
    crash."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(get_responses={"/runs/r9": [_FakeResponse(200, bad_json=True)]})
    assert adapter.poll_build(fake, "r9") == {"_poll_error": 502}


def test_start_build_raises_on_json_list_2xx():
    """A 2xx body that is valid JSON but a *list* (no ``.get()``) → StartFailed(502),
    not an unhandled AttributeError crashing the worker thread."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1", spec="# SPEC")
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(200, list_body=[{"run_id": "x"}])}
    )
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "ignored-repo-path")
    assert exc.value.status_code == 502


def test_poll_build_json_list_2xx_is_a_poll_error():
    """A 2xx body that is valid JSON but a *list* during polling → a transient
    _poll_error (retry), not an AttributeError crash."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        get_responses={"/runs/r9": [_FakeResponse(200, list_body=[{"status": "completed"}])]}
    )
    assert adapter.poll_build(fake, "r9") == {"_poll_error": 502}


def test_start_build_converts_transport_error_to_start_failed():
    """A transport failure (connect/timeout/DNS) during create becomes a clean
    StartFailed(502), not a raw httpx exception crashing the worker thread."""
    import httpx

    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    class _BoomClient:
        def post(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    adapter = AgenticTeamAdapter("t1", process_id="proc1", spec="# SPEC")
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(_BoomClient(), "ignored-repo-path")
    assert exc.value.status_code == 502


def test_poll_build_transport_error_is_a_poll_error():
    """A transport failure during polling becomes a retryable _poll_error, not a
    crash (the orchestrator keeps polling)."""
    import httpx

    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    class _BoomClient:
        def get(self, *a, **kw):
            raise httpx.ConnectTimeout("timed out")

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    result = adapter.poll_build(_BoomClient(), "r9")
    assert result["_poll_error"] == 502
    assert "timed out" in result["detail"]


def test_start_build_truncates_upstream_error_body():
    """An HTTP error body from the provisioning service is truncated in the
    StartFailed detail so an internal error page isn't echoed wholesale."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1", spec="# SPEC")
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(500, text="x" * 1000)}
    )
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "ignored-repo-path")
    assert exc.value.status_code == 500
    assert len(exc.value.body) <= 200


def test_poll_build_maps_waiting_for_input_to_free_text_question():
    """poll_build maps waiting_for_input to a single free-text question with a stable id."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        get_responses={
            "/test-pipeline/runs/run-9": [
                _FakeResponse(
                    200,
                    {
                        "status": "waiting_for_input",
                        "current_step_id": "step-review",
                        "human_prompt": "Which tone for the post?",
                    },
                )
            ]
        }
    )
    payload = adapter.poll_build(fake, "run-9")
    assert payload["status"] == "waiting_for_input"
    assert payload["waiting_for_answers"] is True
    questions = payload["pending_questions"]
    assert len(questions) == 1
    q = questions[0]
    assert q["id"] == "run-9:step-review"  # stable per (run, step)
    assert q["question_text"] == "Which tone for the post?"
    assert q["options"] == []  # empty ⇒ persona answers free-text via "other"
    # Context is enriched (not a bare "run X, step Y") so the persona is grounded
    # when it answers open-ended — it flows into FREE_TEXT_ANSWERING_PROMPT.
    context = q["context"]
    assert "run-9" in context
    assert "step-review" in context
    assert "open-ended" in context
    assert "resume the run" in context


def test_poll_build_waiting_without_step_id_falls_back_to_wait():
    """A WAIT step missing ``current_step_id`` falls back to the literal "wait"
    in the stable question id (the None-guard fallback)."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        get_responses={
            "/test-pipeline/runs/run-9": [
                _FakeResponse(
                    200,
                    {"status": "waiting_for_input", "human_prompt": "Pick a tone."},
                )
            ]
        }
    )
    payload = adapter.poll_build(fake, "run-9")
    assert payload["pending_questions"][0]["id"] == "run-9:wait"


def test_poll_build_waiting_without_prompt_is_treated_as_running():
    """A waiting_for_input status with no human_prompt is treated as 'running'."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        get_responses={
            "/test-pipeline/runs/run-9": [
                _FakeResponse(200, {"status": "waiting_for_input", "human_prompt": None})
            ]
        }
    )
    assert adapter.poll_build(fake, "run-9") == {"status": "running"}


def test_poll_build_terminal_and_error_mapping():
    """poll_build maps terminal statuses, errors, and HTTP failures correctly."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        get_responses={
            "/runs/done": [_FakeResponse(200, {"status": "completed"})],
            "/runs/boom": [_FakeResponse(200, {"status": "failed", "error": "kaboom"})],
            "/runs/stop": [_FakeResponse(200, {"status": "cancelled"})],
            "/runs/halt": [_FakeResponse(200, {"status": "canceled"})],
            "/runs/gone": [_FakeResponse(503, {}, text="upstream exploded")],
            "/runs/going": [_FakeResponse(200, {"status": "running"})],
        }
    )
    assert adapter.poll_build(fake, "done") == {"status": "completed", "error": None}
    assert adapter.poll_build(fake, "boom") == {"status": "failed", "error": "kaboom"}
    # 'cancelled' is a terminal status (matches the British spelling in _TERMINAL).
    assert adapter.poll_build(fake, "stop") == {"status": "cancelled", "error": None}
    # 'canceled' (American spelling) is terminal too, and the *returned* status
    # is rewritten to the canonical 'cancelled' — the upstream status string
    # is not normalized at the source, but the orchestrator's _run_phase does
    # an exact-string terminal check against one spelling, so a raw 'canceled'
    # would poll for MAX_POLL_ATTEMPTS and time out instead of failing promptly.
    assert adapter.poll_build(fake, "halt") == {"status": "cancelled", "error": None}
    # HTTP error carries the code plus a truncated body for diagnostics.
    gone = adapter.poll_build(fake, "gone")
    assert gone["_poll_error"] == 503
    assert gone["detail"] == "upstream exploded"
    assert adapter.poll_build(fake, "going") == {"status": "running"}


def test_poll_build_waiting_with_empty_step_id_falls_back_to_wait():
    """An *empty* current_step_id is treated as missing (→ "wait"), so the
    question id can't become ``run-9:`` and collide across WAIT steps."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        get_responses={
            "/test-pipeline/runs/run-9": [
                _FakeResponse(
                    200,
                    {"status": "waiting_for_input", "current_step_id": "", "human_prompt": "Q?"},
                )
            ]
        }
    )
    payload = adapter.poll_build(fake, "run-9")
    assert payload["pending_questions"][0]["id"] == "run-9:wait"


def test_submit_build_answers_posts_free_text_to_input():
    """submit_build_answers POSTs the free-text answer to the pipeline's /input route."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/input": _FakeResponse(200, {})})
    adapter.submit_build_answers(
        fake,
        "run-9",
        [{"question_id": "run-9:s1", "selected_option_id": "other", "other_text": "punchy"}],
    )
    assert fake.posts[0]["url"].endswith("/test-pipeline/runs/run-9/input")
    assert fake.posts[0]["json"] == {"input": "punchy"}


def test_submit_build_answers_falls_back_for_blank_answer():
    """submit_build_answers posts the placeholder when the answer is blank."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/input": _FakeResponse(200, {})})
    # No other_text and a whitespace selected id → never post an empty body.
    adapter.submit_build_answers(fake, "run-9", [{"selected_option_id": "  "}])
    assert fake.posts[0]["json"] == {"input": "(no answer provided)"}


def test_submit_build_answers_never_posts_literal_other():
    """When the bounded answer carries selected_option_id 'other' but no
    other_text, the placeholder is posted — never the literal token 'other'."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/input": _FakeResponse(200, {})})
    adapter.submit_build_answers(fake, "run-9", [{"selected_option_id": "other"}])
    assert fake.posts[0]["json"] == {"input": "(no answer provided)"}


def test_submit_build_answers_coerces_non_string_other_text():
    """A malformed answer with a non-string other_text (e.g. a number) is coerced
    to str rather than crashing the worker thread on ``.strip()``."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/input": _FakeResponse(200, {})})
    adapter.submit_build_answers(fake, "run-9", [{"other_text": 42}])
    assert fake.posts[0]["json"] == {"input": "42"}


def test_submit_build_answers_preserves_falsy_zero_answer():
    """A valid *falsy* answer (numeric 0 or the string "0") must be preserved, not
    dropped to the placeholder by an ``or``-style default."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/input": _FakeResponse(200, {})})
    adapter.submit_build_answers(fake, "run-9", [{"other_text": 0}])
    assert fake.posts[0]["json"] == {"input": "0"}
    adapter.submit_build_answers(fake, "run-9", [{"other_text": "0"}])
    assert fake.posts[1]["json"] == {"input": "0"}


def test_submit_build_answers_raises_on_http_error():
    """A non-2xx from /input propagates as httpx.HTTPStatusError (raise_for_status),
    so the orchestrator's retry/backoff sees the failure rather than a silent post."""
    import httpx

    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/input": _FakeResponse(500, text="boom")})
    with pytest.raises(httpx.HTTPStatusError):
        adapter.submit_build_answers(fake, "run-9", [{"other_text": "x"}])


def test_submit_build_answers_converts_transport_error_to_http_error():
    """A transport failure (connect/timeout/DNS) while posting the answer is
    re-raised as an httpx.HTTPStatusError(502) — the orchestrator's answer
    retry loop only catches HTTPStatusError, so a bare RequestError would
    otherwise escape the retry and fail the run on the first network blip."""
    import httpx

    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    class _BoomClient:
        def post(self, url, *a, **kw):
            raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    with pytest.raises(httpx.HTTPStatusError) as exc:
        adapter.submit_build_answers(_BoomClient(), "run-9", [{"other_text": "x"}])
    assert exc.value.response.status_code == 502


# ---------------------------------------------------------------------------
# End-to-end: persona drives a collapsed agentic run through the orchestrator
# ---------------------------------------------------------------------------


def test_persona_drives_agentic_team_end_to_end(stub_orchestrator_io, monkeypatch):
    """End-to-end: a persona drives a collapsed agentic run through the orchestrator."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    orchestrator = stub_orchestrator_io
    run = _FakeRun(run_id="run-e2e", spec_content=None)
    store = _FakeStore(run)
    agent = MagicMock()
    agent.generate_spec.return_value = "# Generated spec"
    agent.answer_question.return_value = {
        "selected_option_id": "other",
        "other_text": "punchy founder voice",
        "rationale": "matches the brand",
    }

    fake = _FakeHttpxClient(
        post_responses={
            # "/input" first: the input URL also contains "/test-pipeline/runs".
            "/input": _FakeResponse(200, {}),
            "/test-pipeline/runs": _FakeResponse(201, {"run_id": "run-pipe"}),
        },
        get_responses={
            "/test-pipeline/runs/run-pipe": [
                _FakeResponse(
                    200,
                    {
                        "status": "waiting_for_input",
                        "current_step_id": "write",
                        "human_prompt": "Tone?",
                    },
                ),
                _FakeResponse(200, {"status": "completed"}),
            ]
        },
    )
    monkeypatch.setattr(orchestrator.httpx, "Client", lambda *a, **kw: fake)

    orchestrator.run_workflow("run-e2e", store, agent, AgenticTeamAdapter("t1", process_id="proc1"))

    assert run.status == "completed"
    # The pipeline was created with the generated spec as initial_input...
    create = next(p for p in fake.posts if p["url"].endswith("/test-pipeline/runs"))
    assert create["json"] == {"process_id": "proc1", "initial_input": "# Generated spec"}
    # ...and the persona's free-text answer was submitted to /input to resume it.
    answered = next(p for p in fake.posts if p["url"].endswith("/runs/run-pipe/input"))
    assert answered["json"] == {"input": "punchy founder voice"}
    # The decision was recorded for the audit trail.
    assert any(d.get("answer_text") == "punchy founder voice" for d in store.decisions)


def test_persona_run_cancelled_with_american_spelling_fails_promptly(
    stub_orchestrator_io, monkeypatch
):
    """A pipeline that reports the American 'canceled' spelling must be
    recognized as terminal by the orchestrator's _run_phase (which does an
    exact-string match against 'cancelled') and fail promptly with a clear
    cancellation error — not poll for MAX_POLL_ATTEMPTS and time out."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    orchestrator = stub_orchestrator_io
    run = _FakeRun(run_id="run-cancel-american", spec_content=None)
    store = _FakeStore(run)
    agent = MagicMock()
    agent.generate_spec.return_value = "# Generated spec"

    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(201, {"run_id": "run-pipe"})},
        get_responses={
            "/test-pipeline/runs/run-pipe": [_FakeResponse(200, {"status": "canceled"})],
        },
    )
    monkeypatch.setattr(orchestrator.httpx, "Client", lambda *a, **kw: fake)

    orchestrator.run_workflow(
        "run-cancel-american", store, agent, AgenticTeamAdapter("t1", process_id="proc1")
    )

    assert run.status == "failed"
    # Fails on the *first* poll with the cancellation message, not after
    # exhausting MAX_POLL_ATTEMPTS with a generic timeout message.
    assert "cancelled" in run.error.lower()
    assert "timed out" not in run.error.lower()
    assert len(fake.gets) == 1


def test_persona_run_failed_pipeline_fails_promptly_with_pipeline_error(
    stub_orchestrator_io, monkeypatch
):
    """A pipeline that reports 'failed' must be recognized as terminal by the
    orchestrator's _run_phase exact-string match and fail promptly, carrying
    the pipeline's own error — not poll for MAX_POLL_ATTEMPTS and time out.
    Locks the adapter↔orchestrator agreement on the 'failed' terminal string,
    which the drift tripwire pins adapter-side only."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    orchestrator = stub_orchestrator_io
    run = _FakeRun(run_id="run-fail", spec_content=None)
    store = _FakeStore(run)
    agent = MagicMock()
    agent.generate_spec.return_value = "# Generated spec"

    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(201, {"run_id": "run-pipe"})},
        get_responses={
            "/test-pipeline/runs/run-pipe": [
                _FakeResponse(200, {"status": "failed", "error": "agent step exploded"})
            ],
        },
    )
    monkeypatch.setattr(orchestrator.httpx, "Client", lambda *a, **kw: fake)

    orchestrator.run_workflow(
        "run-fail", store, agent, AgenticTeamAdapter("t1", process_id="proc1")
    )

    assert run.status == "failed"
    # Fails on the first poll, carrying the pipeline's own error — not a
    # generic timeout after exhausting MAX_POLL_ATTEMPTS.
    assert "agent step exploded" in run.error
    assert "timed out" not in run.error.lower()
    assert len(fake.gets) == 1


def test_persona_run_retries_transient_poll_error_then_completes(stub_orchestrator_io, monkeypatch):
    """A transient poll HTTP error becomes '_poll_error' and the orchestrator
    keeps polling instead of failing the run — locks the retry-key agreement
    between the adapter and _run_phase, which the drift tripwire pins
    adapter-side only."""
    from agent_team_studio.user_agent_founder.targets import AgenticTeamAdapter

    orchestrator = stub_orchestrator_io
    run = _FakeRun(run_id="run-poll-blip", spec_content=None)
    store = _FakeStore(run)
    agent = MagicMock()
    agent.generate_spec.return_value = "# Generated spec"

    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(201, {"run_id": "run-pipe"})},
        get_responses={
            "/test-pipeline/runs/run-pipe": [
                _FakeResponse(502, text="proxy hiccup"),
                _FakeResponse(200, {"status": "completed"}),
            ],
        },
    )
    monkeypatch.setattr(orchestrator.httpx, "Client", lambda *a, **kw: fake)

    orchestrator.run_workflow(
        "run-poll-blip", store, agent, AgenticTeamAdapter("t1", process_id="proc1")
    )

    assert run.status == "completed"
    assert len(fake.gets) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
