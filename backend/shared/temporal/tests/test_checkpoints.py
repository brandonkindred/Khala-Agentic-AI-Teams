"""Unit tests for ``shared.temporal.checkpoints``.

Every helper reaches the job record through the module-private ``_manager``
factory, so these tests monkeypatch that single seam with an in-memory fake
job client. No job service, Postgres, or Temporal cluster is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.temporal import checkpoints


class FakeJobClient:
    """In-memory stand-in for ``JobServiceClient``.

    Invariants:
        - ``jobs`` maps job id -> the job record dict; ``update_job`` merges
          keyword fields into it, mirroring the real client's semantics.
    """

    def __init__(self, jobs: dict[str, dict[str, Any]] | None = None) -> None:
        self.jobs: dict[str, dict[str, Any]] = jobs if jobs is not None else {}
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def update_job(self, job_id: str, **fields: Any) -> None:
        self.updates.append((job_id, dict(fields)))
        self.jobs.setdefault(job_id, {}).update(fields)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> FakeJobClient:
    """Install a fake job client behind ``checkpoints._manager`` for one test."""
    fake = FakeJobClient()
    monkeypatch.setattr(checkpoints, "_manager", lambda _team: fake)
    return fake


# ---------------------------------------------------------------------------
# save_checkpoint / load_checkpoint
# ---------------------------------------------------------------------------


def test_save_checkpoint_records_payload_and_last_phase(client: FakeJobClient) -> None:
    client.jobs["job-1"] = {}

    checkpoints.save_checkpoint("blogging", "job-1", "research", payload={"sources": 3})

    stored = client.jobs["job-1"]["checkpoints"]["research"]
    assert stored["payload"] == {"sources": 3}
    assert stored["completed_at"]
    assert client.jobs["job-1"]["last_phase"] == "research"


def test_save_checkpoint_preserves_earlier_phases(client: FakeJobClient) -> None:
    client.jobs["job-1"] = {"checkpoints": {"research": {"payload": 1, "completed_at": "t0"}}}

    checkpoints.save_checkpoint("blogging", "job-1", "draft")

    assert set(client.jobs["job-1"]["checkpoints"]) == {"research", "draft"}
    assert client.jobs["job-1"]["checkpoints"]["draft"]["payload"] is None


def test_save_checkpoint_tolerates_a_missing_job_record(client: FakeJobClient) -> None:
    checkpoints.save_checkpoint("blogging", "unknown", "research")

    assert client.jobs["unknown"]["checkpoints"]["research"]["payload"] is None


def test_load_checkpoint_returns_the_stored_entry(client: FakeJobClient) -> None:
    client.jobs["job-1"] = {"checkpoints": {"research": {"payload": "p", "completed_at": "t"}}}

    assert checkpoints.load_checkpoint("blogging", "job-1", "research") == {
        "payload": "p",
        "completed_at": "t",
    }


@pytest.mark.parametrize(
    "job",
    [
        pytest.param(None, id="no-job-record"),
        pytest.param({}, id="no-checkpoints-key"),
        pytest.param({"checkpoints": None}, id="null-checkpoints"),
        pytest.param({"checkpoints": {"other": {}}}, id="other-phase-only"),
    ],
)
def test_load_checkpoint_returns_none_when_absent(client: FakeJobClient, job: dict[str, Any] | None) -> None:
    if job is not None:
        client.jobs["job-1"] = job

    assert checkpoints.load_checkpoint("blogging", "job-1", "research") is None


def test_save_then_load_round_trips(client: FakeJobClient) -> None:
    checkpoints.save_checkpoint("blogging", "job-1", "draft", payload=[1, 2])

    loaded = checkpoints.load_checkpoint("blogging", "job-1", "draft")

    assert loaded is not None
    assert loaded["payload"] == [1, 2]


# ---------------------------------------------------------------------------
# submit_input
# ---------------------------------------------------------------------------


def test_submit_input_records_value_and_clears_the_wait(client: FakeJobClient) -> None:
    client.jobs["job-1"] = {"waiting_for": {"title": {"prompt": "pick one"}}}

    checkpoints.submit_input("blogging", "job-1", "title", "Chosen Title")

    assert client.jobs["job-1"]["inputs"] == {"title": "Chosen Title"}
    assert client.jobs["job-1"]["waiting_for"] == {}


def test_submit_input_leaves_other_waits_pending(client: FakeJobClient) -> None:
    client.jobs["job-1"] = {
        "inputs": {"outline": "kept"},
        "waiting_for": {"title": {}, "image": {}},
    }

    checkpoints.submit_input("blogging", "job-1", "title", "T")

    assert client.jobs["job-1"]["inputs"] == {"outline": "kept", "title": "T"}
    assert set(client.jobs["job-1"]["waiting_for"]) == {"image"}


def test_submit_input_tolerates_a_missing_job_record(client: FakeJobClient) -> None:
    checkpoints.submit_input("blogging", "unknown", "title", "T")

    assert client.jobs["unknown"]["inputs"] == {"title": "T"}


# ---------------------------------------------------------------------------
# wait_for_input
# ---------------------------------------------------------------------------


def test_wait_for_input_returns_immediately_when_already_submitted(
    client: FakeJobClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.jobs["job-1"] = {"inputs": {"title": "Ready"}}
    monkeypatch.setattr(checkpoints.time, "sleep", _never_sleep)

    assert checkpoints.wait_for_input("blogging", "job-1", "title") == "Ready"
    # Marked waiting on entry, then flipped back to running on resolution.
    assert client.updates[0][1]["status"] == "waiting"
    assert client.updates[-1][1] == {"waiting_for": {}, "status": "running"}


def test_wait_for_input_records_the_prompt_while_waiting(
    client: FakeJobClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.jobs["job-1"] = {"inputs": {"title": "Ready"}}
    monkeypatch.setattr(checkpoints.time, "sleep", _never_sleep)

    checkpoints.wait_for_input("blogging", "job-1", "title", prompt="Pick a title")

    waiting = client.updates[0][1]["waiting_for"]["title"]
    assert waiting["prompt"] == "Pick a title"
    assert waiting["since"]


def test_wait_for_input_polls_until_the_value_arrives(client: FakeJobClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.jobs["job-1"] = {}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            client.jobs["job-1"]["inputs"] = {"title": "Arrived late"}

    monkeypatch.setattr(checkpoints.time, "sleep", fake_sleep)

    result = checkpoints.wait_for_input("blogging", "job-1", "title", poll_interval=0.25)

    assert result == "Arrived late"
    assert sleeps == [0.25, 0.25]


def test_wait_for_input_times_out(client: FakeJobClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.jobs["job-1"] = {}
    ticks = iter([0.0, 5.0])
    monkeypatch.setattr(checkpoints.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(checkpoints.time, "sleep", _never_sleep)

    with pytest.raises(TimeoutError, match="wait_for_input timed out: job=job-1 key=title"):
        checkpoints.wait_for_input("blogging", "job-1", "title", timeout_seconds=1)


def _never_sleep(_seconds: float) -> None:
    """Fail loudly if a test reaches a real sleep it did not plan for."""
    raise AssertionError("unexpected time.sleep in wait_for_input")


# ---------------------------------------------------------------------------
# _manager
# ---------------------------------------------------------------------------


def test_manager_builds_a_job_client_for_the_team(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lazy import keeps ``job_service_client`` off the module import path."""
    import job_service_client

    seen: list[str] = []

    class _Recorder:
        def __init__(self, team: str) -> None:
            seen.append(team)

    monkeypatch.setattr(job_service_client, "JobServiceClient", _Recorder)

    assert isinstance(checkpoints._manager("blogging"), _Recorder)
    assert seen == ["blogging"]
