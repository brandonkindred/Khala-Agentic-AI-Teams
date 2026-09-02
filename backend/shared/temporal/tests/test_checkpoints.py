"""Unit tests for ``shared.temporal.checkpoints``.

Every helper reaches the job record through the module-private ``_manager``
factory, so these tests monkeypatch that single seam with the repository's
established in-memory job client (``job_service_client_fake``), per the test
layering contract in ``backend/conftest.py``. No job service, Postgres, or
Temporal cluster is involved.

Using the shared fake rather than a local stand-in matters here: its
``update_job`` mirrors production's ``PATCH /jobs/{team}/{job_id}``, which
silently does nothing when no row matches instead of auto-creating the job.
The missing-record cases below pin that behavior.
"""

from __future__ import annotations

from typing import Any

import pytest

from job_service_client_fake import FakeJobServiceClient
from shared.temporal import checkpoints


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> FakeJobServiceClient:
    """Install the shared in-memory job client behind ``checkpoints._manager``."""
    fake = FakeJobServiceClient(team="blogging")
    monkeypatch.setattr(checkpoints, "_manager", lambda _team: fake)
    return fake


def _seed(client: FakeJobServiceClient, job_id: str, **fields: Any) -> None:
    """Create ``job_id`` carrying ``fields`` so a PATCH against it lands."""
    client.create_job(job_id, **fields)


def _never_sleep(_seconds: float) -> None:
    """Fail loudly if a test reaches a real sleep it did not plan for."""
    raise AssertionError("unexpected time.sleep in wait_for_input")


# ---------------------------------------------------------------------------
# save_checkpoint / load_checkpoint
# ---------------------------------------------------------------------------


def test_save_checkpoint_records_payload_and_last_phase(client: FakeJobServiceClient) -> None:
    _seed(client, "job-1")

    checkpoints.save_checkpoint("blogging", "job-1", "research", payload={"sources": 3})

    job = client.get_job("job-1")
    assert job is not None
    stored = job["checkpoints"]["research"]
    assert stored["payload"] == {"sources": 3}
    assert stored["completed_at"]
    assert job["last_phase"] == "research"


def test_save_checkpoint_preserves_earlier_phases(client: FakeJobServiceClient) -> None:
    _seed(client, "job-1", checkpoints={"research": {"payload": 1, "completed_at": "t0"}})

    checkpoints.save_checkpoint("blogging", "job-1", "draft")

    job = client.get_job("job-1")
    assert job is not None
    assert set(job["checkpoints"]) == {"research", "draft"}
    assert job["checkpoints"]["draft"]["payload"] is None


def test_save_checkpoint_is_a_no_op_for_a_missing_job(client: FakeJobServiceClient) -> None:
    """Mirrors production: a PATCH against a nonexistent job writes nothing.

    The helper must not raise, but it also must not conjure a job record — the
    real ``PATCH /jobs/{team}/{job_id}`` is a bare SQL UPDATE that matches no row.
    """
    checkpoints.save_checkpoint("blogging", "unknown", "research")

    assert client.get_job("unknown") is None


def test_load_checkpoint_returns_the_stored_entry(client: FakeJobServiceClient) -> None:
    _seed(client, "job-1", checkpoints={"research": {"payload": "p", "completed_at": "t"}})

    assert checkpoints.load_checkpoint("blogging", "job-1", "research") == {
        "payload": "p",
        "completed_at": "t",
    }


@pytest.mark.parametrize(
    "seed_fields",
    [
        pytest.param(None, id="no-job-record"),
        pytest.param({}, id="no-checkpoints-key"),
        pytest.param({"checkpoints": None}, id="null-checkpoints"),
        pytest.param({"checkpoints": {"other": {}}}, id="other-phase-only"),
    ],
)
def test_load_checkpoint_returns_none_when_absent(
    client: FakeJobServiceClient, seed_fields: dict[str, Any] | None
) -> None:
    if seed_fields is not None:
        _seed(client, "job-1", **seed_fields)

    assert checkpoints.load_checkpoint("blogging", "job-1", "research") is None


def test_save_then_load_round_trips(client: FakeJobServiceClient) -> None:
    _seed(client, "job-1")

    checkpoints.save_checkpoint("blogging", "job-1", "draft", payload=[1, 2])
    loaded = checkpoints.load_checkpoint("blogging", "job-1", "draft")

    assert loaded is not None
    assert loaded["payload"] == [1, 2]


# ---------------------------------------------------------------------------
# submit_input
# ---------------------------------------------------------------------------


def test_submit_input_records_value_and_clears_the_wait(client: FakeJobServiceClient) -> None:
    _seed(client, "job-1", waiting_for={"title": {"prompt": "pick one"}})

    checkpoints.submit_input("blogging", "job-1", "title", "Chosen Title")

    job = client.get_job("job-1")
    assert job is not None
    assert job["inputs"] == {"title": "Chosen Title"}
    assert job["waiting_for"] == {}


def test_submit_input_leaves_other_waits_pending(client: FakeJobServiceClient) -> None:
    _seed(
        client,
        "job-1",
        inputs={"outline": "kept"},
        waiting_for={"title": {}, "image": {}},
    )

    checkpoints.submit_input("blogging", "job-1", "title", "T")

    job = client.get_job("job-1")
    assert job is not None
    assert job["inputs"] == {"outline": "kept", "title": "T"}
    assert set(job["waiting_for"]) == {"image"}


def test_submit_input_is_a_no_op_for_a_missing_job(client: FakeJobServiceClient) -> None:
    """Same production contract as save_checkpoint: no row, no write, no raise."""
    checkpoints.submit_input("blogging", "unknown", "title", "T")

    assert client.get_job("unknown") is None


# ---------------------------------------------------------------------------
# wait_for_input
# ---------------------------------------------------------------------------


def test_wait_for_input_returns_immediately_when_already_submitted(
    client: FakeJobServiceClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(client, "job-1", inputs={"title": "Ready"})
    monkeypatch.setattr(checkpoints.time, "sleep", _never_sleep)

    assert checkpoints.wait_for_input("blogging", "job-1", "title") == "Ready"

    job = client.get_job("job-1")
    assert job is not None
    # Marked waiting on entry, then flipped back on resolution.
    assert job["waiting_for"] == {}
    assert job["status"] == "running"


def test_wait_for_input_records_the_prompt_while_waiting(
    client: FakeJobServiceClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiting marker is observable mid-flight, before the input resolves it."""
    _seed(client, "job-1")
    observed: list[dict[str, Any]] = []

    def fake_sleep(_seconds: float) -> None:
        job = client.get_job("job-1")
        assert job is not None
        observed.append({"waiting_for": job["waiting_for"], "status": job["status"]})
        client.update_job("job-1", inputs={"title": "Arrived"})

    monkeypatch.setattr(checkpoints.time, "sleep", fake_sleep)

    checkpoints.wait_for_input("blogging", "job-1", "title", prompt="Pick a title")

    assert observed[0]["status"] == "waiting"
    waiting = observed[0]["waiting_for"]["title"]
    assert waiting["prompt"] == "Pick a title"
    assert waiting["since"]


def test_wait_for_input_polls_until_the_value_arrives(
    client: FakeJobServiceClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(client, "job-1")
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            client.update_job("job-1", inputs={"title": "Arrived late"})

    monkeypatch.setattr(checkpoints.time, "sleep", fake_sleep)

    result = checkpoints.wait_for_input("blogging", "job-1", "title", poll_interval=0.25)

    assert result == "Arrived late"
    assert sleeps == [0.25, 0.25]


def test_wait_for_input_times_out(client: FakeJobServiceClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait stays registered on timeout, so a late submitter can still resolve it."""
    _seed(client, "job-1")
    # First reading is the deadline baseline; every later one is "much later", so
    # the test does not pin how many times wait_for_input reads the clock.
    readings = iter([0.0])
    monkeypatch.setattr(checkpoints.time, "monotonic", lambda: next(readings, 5.0))
    monkeypatch.setattr(checkpoints.time, "sleep", _never_sleep)

    with pytest.raises(TimeoutError, match="wait_for_input timed out: job=job-1 key=title"):
        checkpoints.wait_for_input("blogging", "job-1", "title", timeout_seconds=1)

    job = client.get_job("job-1")
    assert job is not None
    assert set(job["waiting_for"]) == {"title"}
    assert job["status"] == "waiting"


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
