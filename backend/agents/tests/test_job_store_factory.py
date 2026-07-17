"""Branch coverage for the shared ``job_store_factory.make_status_job_store``."""

from __future__ import annotations

import pytest

from job_service_client_fake import FakeJobServiceClient
from job_store_factory import make_status_job_store


@pytest.fixture
def fake() -> FakeJobServiceClient:
    return FakeJobServiceClient(team="test_team")


@pytest.fixture
def store(fake: FakeJobServiceClient):
    return make_status_job_store(lambda: fake)


def test_make_status_job_store_requires_callable() -> None:
    with pytest.raises(AssertionError):
        make_status_job_store(object())  # type: ignore[arg-type]


def test_create_defaults_to_pending_and_merges_fields(store, fake) -> None:
    store.create_job("j1", message="hi")
    job = store.get_job("j1")
    assert job is not None
    assert job["status"] == "pending"
    assert job["message"] == "hi"


def test_get_missing_returns_none(store) -> None:
    assert store.get_job("nope") is None


def test_update_merges_fields(store) -> None:
    store.create_job("j1")
    store.update_job("j1", progress=42)
    assert store.get_job("j1")["progress"] == 42


def test_list_jobs_filters_by_statuses(store) -> None:
    store.create_job("running")
    store.update_job("running", status="running")
    store.create_job("pending")
    assert len(store.list_jobs()) == 2
    running = store.list_jobs(statuses=["running"])
    assert [j["job_id"] for j in running] == ["running"]


def test_cancel_active_job_transitions_and_reports_true(store) -> None:
    store.create_job("j1")
    assert store.cancel_job("j1") is True
    assert store.is_job_cancelled("j1") is True


def test_cancel_terminal_job_is_noop(store) -> None:
    store.create_job("j1")
    store.update_job("j1", status="completed")
    assert store.cancel_job("j1") is False
    assert store.get_job("j1")["status"] == "completed"


def test_cancel_missing_job_returns_false(store) -> None:
    assert store.cancel_job("missing") is False


def test_is_cancelled_missing_returns_false(store) -> None:
    assert store.is_job_cancelled("missing") is False


def test_update_job_if_not_cancelled_writes_when_active(store) -> None:
    store.create_job("j1")
    assert store.update_job_if_not_cancelled("j1", status="running") is True
    assert store.get_job("j1")["status"] == "running"


def test_update_job_if_not_cancelled_noop_when_cancelled(store) -> None:
    store.create_job("j1")
    store.update_job("j1", status="cancelled")
    assert store.update_job_if_not_cancelled("j1", status="running") is False
    assert store.get_job("j1")["status"] == "cancelled"


def test_update_job_if_not_cancelled_missing_job_returns_none(store) -> None:
    """Distinct from the cancelled case (False) — a missing row can't be
    distinguished from a cancelled one by the caller unless the tri-state
    (True/False/None) is preserved end-to-end through the factory."""
    assert store.update_job_if_not_cancelled("missing", status="running") is None


def test_delete_reports_whether_removed(store) -> None:
    store.create_job("j1")
    assert store.delete_job("j1") is True
    assert store.delete_job("j1") is False


def test_mark_all_running_jobs_failed_marks_active(store, fake) -> None:
    store.create_job("a")
    store.update_job("a", status="running")
    store.mark_all_running_jobs_failed("shutdown")
    assert store.get_job("a")["status"] == "failed"


def test_mark_all_running_jobs_failed_swallows_client_errors(
    monkeypatch: pytest.MonkeyPatch, store, fake
) -> None:
    def _boom(*_: object, **__: object) -> None:
        raise RuntimeError("client down")

    monkeypatch.setattr(fake, "mark_all_active_jobs_failed", _boom)
    # Must not raise — best-effort contract.
    store.mark_all_running_jobs_failed("network outage")


def test_client_getter_is_resolved_at_call_time() -> None:
    """Rebinding what the getter returns is observed by later operations."""
    first = FakeJobServiceClient(team="first")
    second = FakeJobServiceClient(team="second")
    holder = {"client": first}
    store = make_status_job_store(lambda: holder["client"])

    store.create_job("j1")
    assert first.get_job("j1") is not None

    holder["client"] = second
    store.create_job("j2")
    assert second.get_job("j2") is not None
    assert first.get_job("j2") is None


def test_update_job_if_not_cancelled_client_getter_resolved_at_call_time() -> None:
    """Same convention as ``test_client_getter_is_resolved_at_call_time``, for
    the new field — no closure may cache the client returned at bind time."""
    first = FakeJobServiceClient(team="first")
    second = FakeJobServiceClient(team="second")
    holder = {"client": first}
    store = make_status_job_store(lambda: holder["client"])

    first.create_job("j1")
    holder["client"] = second
    second.create_job("j1")

    assert store.update_job_if_not_cancelled("j1", status="running") is True
    assert second.get_job("j1")["status"] == "running"
    assert first.get_job("j1")["status"] == "pending"
