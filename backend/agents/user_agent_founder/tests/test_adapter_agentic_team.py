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
    ) -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self._bad_json = bad_json

    def json(self) -> dict:
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
        return _FakeResponse(200, {"run_id": "default-run"})

    def get(self, url: str, *, timeout: Any = None) -> _FakeResponse:
        self.gets.append({"url": url})
        for needle, queue in self.get_responses.items():
            if needle in url:
                idx = self._get_indices.get(needle, 0)
                if idx >= len(queue):
                    return queue[-1]
                self._get_indices[needle] = idx + 1
                return queue[idx]
        return _FakeResponse(200, {"status": "completed"})


@pytest.fixture
def stub_orchestrator_io(monkeypatch):
    from user_agent_founder import orchestrator

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
    from user_agent_founder.targets import AgenticTeamAdapter, TargetTeamAdapter

    adapter = AgenticTeamAdapter("team-xyz", process_id="p1")
    assert isinstance(adapter, TargetTeamAdapter)
    assert adapter.team_key == "agentic_team:team-xyz"
    assert adapter.display_name


def test_adapter_rejects_empty_team_id():
    from user_agent_founder.targets import AgenticTeamAdapter

    with pytest.raises(ValueError, match="team_id must be non-empty"):
        AgenticTeamAdapter("", process_id="p1")


def test_get_adapter_parses_agentic_key_and_threads_process_id():
    from user_agent_founder.targets import AgenticTeamAdapter, get_adapter

    adapter = get_adapter("agentic_team:abc", process_id="proc-7")
    assert isinstance(adapter, AgenticTeamAdapter)
    assert adapter._team_id == "abc"
    assert adapter._process_id == "proc-7"
    # Fresh instance each call — no cross-run state leakage.
    assert get_adapter("agentic_team:abc") is not adapter


def test_get_adapter_rejects_malformed_agentic_key():
    from user_agent_founder.targets import get_adapter

    with pytest.raises(ValueError, match="missing team id"):
        get_adapter("agentic_team:")


def test_adapter_rejects_path_traversal_team_id():
    """team_id is user-controlled (from target_team_key) and lands in a URL path,
    so traversal / unsafe characters must be rejected at construction — get_adapter
    surfaces it as a ValueError the /start endpoint turns into a 400."""
    from user_agent_founder.targets import AgenticTeamAdapter, get_adapter

    for bad in ["../../etc", "a/b", "a\\b", "..", "te am", "a;b", "a?b"]:
        with pytest.raises(ValueError, match="invalid team_id"):
            AgenticTeamAdapter(bad, process_id="p1")
    with pytest.raises(ValueError, match="invalid team_id"):
        get_adapter("agentic_team:../../sensitive")


def test_url_construction_targets_provisioning_mount():
    from user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="p1")
    url = adapter._url("/test-pipeline/runs")
    assert url.endswith("/api/agentic-team-provisioning/teams/t1/test-pipeline/runs")


# ---------------------------------------------------------------------------
# Collapse: analysis is a no-op pass-through carrying the spec
# ---------------------------------------------------------------------------


def test_analysis_is_noop_passthrough():
    from user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="p1")
    fake = _FakeHttpxClient()
    job_id = adapter.start_from_spec(fake, "proj", "# SPEC BODY")
    # No HTTP call; spec carried forward as repo_path on the next poll.
    assert fake.posts == []
    assert job_id  # a non-empty sentinel
    status = adapter.poll_analysis(fake, job_id)
    assert status == {"status": "completed", "repo_path": "# SPEC BODY"}
    assert fake.gets == []
    # submit_analysis_answers is a no-op (the collapsed phase raises no questions).
    assert adapter.submit_analysis_answers(fake, job_id, [{"x": 1}]) is None


def test_constructor_spec_seed_survives_resume_without_start_from_spec():
    """Resume window: the analysis sentinel was stored but repo_path wasn't, so a
    fresh adapter never sees start_from_spec — the constructor seed (from the
    persisted run row) must still carry the spec to the build phase."""
    from user_agent_founder.targets import AgenticTeamAdapter, get_adapter

    adapter = AgenticTeamAdapter("t1", process_id="p1", spec="# PERSISTED SPEC")
    # poll_analysis is reached directly (start_from_spec skipped on resume).
    assert adapter.poll_analysis(_FakeHttpxClient(), "noop") == {
        "status": "completed",
        "repo_path": "# PERSISTED SPEC",
    }
    # get_adapter threads the seed through too.
    seeded = get_adapter("agentic_team:t1", process_id="p1", spec="# VIA FACTORY")
    assert seeded.poll_analysis(_FakeHttpxClient(), "noop")["repo_path"] == "# VIA FACTORY"


# ---------------------------------------------------------------------------
# Build: start / poll-status mapping / answer submission
# ---------------------------------------------------------------------------


def test_start_build_posts_process_and_spec_returns_run_id():
    from user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(201, {"run_id": "run-9"})}
    )
    run_id = adapter.start_build(fake, "# SPEC")
    assert run_id == "run-9"
    assert fake.posts[0]["url"].endswith("/teams/t1/test-pipeline/runs")
    assert fake.posts[0]["json"] == {"process_id": "proc1", "initial_input": "# SPEC"}


def test_start_build_requires_process_id():
    from user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id=None)
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(_FakeHttpxClient(), "# SPEC")
    assert exc.value.status_code == 400


def test_start_build_raises_on_http_error():
    from user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(404, {}, text="no such process")}
    )
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "# SPEC")
    assert exc.value.status_code == 404


def test_start_build_raises_when_response_has_no_run_id():
    """A 2xx create response missing run_id fails fast (502) instead of returning
    an empty job id that the orchestrator would poll to timeout."""
    from user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/test-pipeline/runs": _FakeResponse(201, {})})
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "# SPEC")
    assert exc.value.status_code == 502


def test_start_build_raises_on_non_json_2xx():
    """A 2xx body that isn't JSON (e.g. an HTML proxy page) → StartFailed(502),
    not an unhandled JSONDecodeError crashing the worker thread."""
    from user_agent_founder.targets import AgenticTeamAdapter, StartFailed

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        post_responses={"/test-pipeline/runs": _FakeResponse(200, bad_json=True)}
    )
    with pytest.raises(StartFailed) as exc:
        adapter.start_build(fake, "# SPEC")
    assert exc.value.status_code == 502


def test_poll_build_non_json_2xx_is_a_poll_error():
    """A 2xx non-JSON body during polling → a transient _poll_error (retry), not a
    crash."""
    from user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(get_responses={"/runs/r9": [_FakeResponse(200, bad_json=True)]})
    assert adapter.poll_build(fake, "r9") == {"_poll_error": 502}


def test_poll_build_maps_waiting_for_input_to_free_text_question():
    from user_agent_founder.targets import AgenticTeamAdapter

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


def test_poll_build_waiting_without_prompt_is_treated_as_running():
    from user_agent_founder.targets import AgenticTeamAdapter

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
    from user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(
        get_responses={
            "/runs/done": [_FakeResponse(200, {"status": "completed"})],
            "/runs/boom": [_FakeResponse(200, {"status": "failed", "error": "kaboom"})],
            "/runs/gone": [_FakeResponse(503, {})],
            "/runs/going": [_FakeResponse(200, {"status": "running"})],
        }
    )
    assert adapter.poll_build(fake, "done") == {"status": "completed", "error": None}
    assert adapter.poll_build(fake, "boom") == {"status": "failed", "error": "kaboom"}
    assert adapter.poll_build(fake, "gone") == {"_poll_error": 503}
    assert adapter.poll_build(fake, "going") == {"status": "running"}


def test_submit_build_answers_posts_free_text_to_input():
    from user_agent_founder.targets import AgenticTeamAdapter

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
    from user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/input": _FakeResponse(200, {})})
    # No other_text and a whitespace selected id → never post an empty body.
    adapter.submit_build_answers(fake, "run-9", [{"selected_option_id": "  "}])
    assert fake.posts[0]["json"] == {"input": "(no answer provided)"}


def test_submit_build_answers_never_posts_literal_other():
    """When the bounded answer carries selected_option_id 'other' but no
    other_text, the placeholder is posted — never the literal token 'other'."""
    from user_agent_founder.targets import AgenticTeamAdapter

    adapter = AgenticTeamAdapter("t1", process_id="proc1")
    fake = _FakeHttpxClient(post_responses={"/input": _FakeResponse(200, {})})
    adapter.submit_build_answers(fake, "run-9", [{"selected_option_id": "other"}])
    assert fake.posts[0]["json"] == {"input": "(no answer provided)"}


# ---------------------------------------------------------------------------
# End-to-end: persona drives a collapsed agentic run through the orchestrator
# ---------------------------------------------------------------------------


def test_persona_drives_agentic_team_end_to_end(stub_orchestrator_io, monkeypatch):
    from user_agent_founder.targets import AgenticTeamAdapter

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
