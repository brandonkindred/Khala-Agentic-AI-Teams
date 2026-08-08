"""Tests for the centralized-jobs endpoints on the user_agent_founder API.

These drive ``start_founder_workflow``, ``resume_job``, ``restart_job``,
``cancel_job``, and ``delete_job`` directly (not through FastAPI TestClient)
so Postgres, Temporal, and the orchestrator can all be stubbed out.
"""

from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeJobStore:
    """In-memory stand-in for ``agent_team_studio.user_agent_founder.shared.job_store``."""

    RESUMABLE_STATUSES = frozenset({"pending", "running", "failed", "interrupted", "agent_crash"})
    RESTARTABLE_STATUSES = frozenset(
        {"completed", "failed", "cancelled", "interrupted", "agent_crash"}
    )

    def __init__(self) -> None:
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.create_calls: list[tuple[str, dict]] = []
        self.update_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.reset_calls: list[str] = []

    # API mirrors ``shared.job_store``
    def create_job(self, job_id: str, *, status: str = "pending", **fields: Any) -> None:
        self.create_calls.append((job_id, {"status": status, **fields}))
        self.jobs[job_id] = {"job_id": job_id, "status": status, **fields}

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.jobs.get(job_id)

    def update_job(self, job_id: str, **fields: Any) -> None:
        self.update_calls.append((job_id, dict(fields)))
        if job_id in self.jobs:
            self.jobs[job_id].update(fields)

    def delete_job(self, job_id: str) -> bool:
        self.delete_calls.append(job_id)
        return self.jobs.pop(job_id, None) is not None

    def reset_job(self, job_id: str) -> None:
        self.reset_calls.append(job_id)
        if job_id in self.jobs:
            self.jobs[job_id].update(
                {"status": "pending", "error": None, "current_phase": "starting"}
            )

    def list_jobs(self, statuses: Optional[list[str]] = None) -> list[Dict[str, Any]]:
        if statuses is None:
            return list(self.jobs.values())
        return [j for j in self.jobs.values() if j.get("status") in set(statuses)]

    @staticmethod
    def validate_job_for_action(
        job_data: Optional[Dict[str, Any]],
        job_id: str,
        allowed_statuses: frozenset,
        action_label: str = "action",
    ) -> Dict[str, Any]:
        if not job_data:
            raise ValueError(f"Job {job_id} not found")
        status = job_data.get("status", "pending")
        if status not in allowed_statuses:
            raise ValueError(f"Job cannot be {action_label} (status={status}).")
        return job_data


@pytest.fixture
def fake_job_store(monkeypatch):
    """Patch ``agent_team_studio.user_agent_founder.shared.job_store`` with an in-memory fake."""
    import agent_team_studio.user_agent_founder.shared.job_store as real_job_store

    fake = FakeJobStore()
    for attr in (
        "create_job",
        "get_job",
        "update_job",
        "delete_job",
        "reset_job",
        "list_jobs",
        "validate_job_for_action",
        "RESUMABLE_STATUSES",
        "RESTARTABLE_STATUSES",
    ):
        monkeypatch.setattr(
            real_job_store,
            attr,
            getattr(fake, attr) if not attr.endswith("STATUSES") else getattr(fake, attr),
        )
    return fake


@pytest.fixture
def fake_store(monkeypatch):
    """Patch the founder Postgres store with a MagicMock.

    ``/start`` mints the run_id up-front (uuid hex) and passes it into
    ``create_run`` as a kwarg, so create_run.return_value is irrelevant.
    The deterministic id used by tests is set via the ``fixed_run_id``
    fixture below — keep this store stub free of return-value assumptions.
    """
    from agent_team_studio.user_agent_founder.api import main as api_main

    store = MagicMock()
    store.create_run.return_value = None
    store.delete_run.return_value = True
    monkeypatch.setattr(api_main, "get_founder_store", lambda: store)
    return store


@pytest.fixture
def fake_persona_store(monkeypatch):
    """Patch the persona store; ``startup-founder`` is always present."""
    from agent_team_studio.user_agent_founder.api import main as api_main
    from agent_team_studio.user_agent_founder.store import StoredPersona

    p = StoredPersona(
        persona_id="startup-founder",
        name="Startup Founder",
        description="d",
        icon="rocket_launch",
        system_prompt="s",
        spec_generation_prompt="g",
        is_builtin=True,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store = MagicMock()
    store.get_persona.side_effect = lambda pid: p if pid == "startup-founder" else None
    store.list_personas.return_value = [p]
    monkeypatch.setattr(api_main, "get_persona_store", lambda: store)
    return store


@pytest.fixture
def fixed_run_id(monkeypatch):
    """Make ``uuid4().hex`` deterministic so tests can assert on the run id."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    class _FixedUUID:
        hex = "deadbeefdeadbeefdeadbeefdeadbeef"

    monkeypatch.setattr(api_main, "uuid4", lambda: _FixedUUID())
    return _FixedUUID.hex


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Patch the dispatcher so we never touch Temporal or spawn a real thread."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    dispatched: list[str] = []

    def _dispatch(run_id: str) -> str:
        dispatched.append(run_id)
        return "thread"

    monkeypatch.setattr(api_main, "_dispatch_founder_run", _dispatch)
    return dispatched


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


def test_start_creates_job_and_dispatches(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store, fixed_run_id
):
    from agent_team_studio.user_agent_founder.api.main import start_founder_workflow

    resp = start_founder_workflow()

    assert resp.job_id == fixed_run_id
    assert resp.status == "running"
    assert fake_dispatch == [fixed_run_id]
    assert fake_job_store.create_calls == [
        (
            fixed_run_id,
            {
                "status": "running",
                "label": "Testing Personas workflow",
                "current_phase": "starting",
            },
        )
    ]
    assert fake_job_store.jobs[fixed_run_id]["status"] == "running"
    # Default target_team_key + persona_id are recorded on create_run, with
    # a slug+suffix project name when the caller didn't supply one.
    fake_store.create_run.assert_called_once_with(
        target_team_key="software_engineering",
        run_id=fixed_run_id,
        persona_id="startup-founder",
        project_name=f"startup-founder-{fixed_run_id[:8]}",
        process_id=None,
    )


def test_start_passes_explicit_persona_and_project_name(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store, fixed_run_id
):
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    resp = start_founder_workflow(
        StartRunRequest(
            target_team_key="software_engineering",
            project_name="my-custom-name",
        )
    )

    assert resp.job_id == fixed_run_id
    fake_store.create_run.assert_called_once_with(
        target_team_key="software_engineering",
        run_id=fixed_run_id,
        persona_id="startup-founder",
        project_name="my-custom-name",
        process_id=None,
    )


def test_start_rejects_unknown_target_team_key(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store
):
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    with pytest.raises(HTTPException) as excinfo:
        start_founder_workflow(StartRunRequest(target_team_key="not_a_real_team"))
    assert excinfo.value.status_code == 400
    # No dispatch when validation rejects the request.
    assert fake_dispatch == []


def test_start_rejects_unknown_persona(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store
):
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    with pytest.raises(HTTPException) as excinfo:
        start_founder_workflow(StartRunRequest(persona_id="ghost"))
    assert excinfo.value.status_code == 404
    assert fake_dispatch == []


def test_start_agentic_team_requires_process_id(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store
):
    """An agentic-team target with no process_id is rejected before dispatch."""
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    with pytest.raises(HTTPException) as excinfo:
        start_founder_workflow(StartRunRequest(target_team_key="agentic_team:team-1"))
    assert excinfo.value.status_code == 400
    assert "process_id" in excinfo.value.detail
    assert fake_dispatch == []
    fake_store.create_run.assert_not_called()


@pytest.mark.parametrize("blank", ["", "   "])
def test_start_agentic_team_rejects_blank_process_id(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store, blank
):
    """An empty or whitespace-only process_id is rejected with the same 400 as a
    missing one — it can't address a real process, so it must not start a run."""
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    with pytest.raises(HTTPException) as excinfo:
        start_founder_workflow(
            StartRunRequest(target_team_key="agentic_team:team-1", process_id=blank)
        )
    assert excinfo.value.status_code == 400
    assert "process_id" in excinfo.value.detail
    assert fake_dispatch == []
    fake_store.create_run.assert_not_called()


def test_start_rejects_path_traversal_team_key(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store
):
    """A crafted target_team_key with traversal characters is rejected at
    get_adapter (400) before any cross-service call or dispatch."""
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    with pytest.raises(HTTPException) as excinfo:
        start_founder_workflow(
            StartRunRequest(target_team_key="agentic_team:../../x", process_id="p1")
        )
    assert excinfo.value.status_code == 400
    assert fake_dispatch == []
    fake_store.create_run.assert_not_called()


def test_start_agentic_team_persists_process_id(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store, fixed_run_id, monkeypatch
):
    """A well-formed agentic-team run (complete process) threads process_id into create_run."""
    from agent_team_studio.user_agent_founder.api import main as api_main
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    monkeypatch.setattr(api_main, "_agentic_process_status", lambda _t, _p: "complete")
    resp = start_founder_workflow(
        StartRunRequest(target_team_key="agentic_team:team-1", process_id="proc-9")
    )

    assert resp.job_id == fixed_run_id
    assert fake_dispatch == [fixed_run_id]
    fake_store.create_run.assert_called_once_with(
        target_team_key="agentic_team:team-1",
        run_id=fixed_run_id,
        persona_id="startup-founder",
        project_name=f"startup-founder-{fixed_run_id[:8]}",
        process_id="proc-9",
    )


def test_start_agentic_team_rejects_non_complete_process(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store, monkeypatch
):
    """The Stage-3 → Stage-4 gate is enforced server-side: a draft/archived/missing
    process is a 422, even for a direct API caller bypassing the UI."""
    from agent_team_studio.user_agent_founder.api import main as api_main
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    monkeypatch.setattr(api_main, "_agentic_process_status", lambda _t, _p: "draft")
    with pytest.raises(HTTPException) as excinfo:
        start_founder_workflow(
            StartRunRequest(target_team_key="agentic_team:team-1", process_id="proc-draft")
        )
    assert excinfo.value.status_code == 422
    assert "not testable" in excinfo.value.detail
    assert fake_dispatch == []
    fake_store.create_run.assert_not_called()


def test_start_agentic_team_not_found_detail_distinguishes_missing(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store, monkeypatch
):
    """A 'not_found' gate result (team or process absent) yields a 422 whose detail
    names both ids — distinct from the generic 'not testable' wording reserved for
    a present-but-non-complete process."""
    from agent_team_studio.user_agent_founder.api import main as api_main
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    monkeypatch.setattr(api_main, "_agentic_process_status", lambda _t, _p: "not_found")
    with pytest.raises(HTTPException) as excinfo:
        start_founder_workflow(
            StartRunRequest(target_team_key="agentic_team:team-x", process_id="proc-missing")
        )
    assert excinfo.value.status_code == 422
    assert "not found" in excinfo.value.detail
    assert "team-x" in excinfo.value.detail
    assert "proc-missing" in excinfo.value.detail
    assert "not testable" not in excinfo.value.detail
    assert fake_dispatch == []
    fake_store.create_run.assert_not_called()


def test_start_agentic_team_allows_when_status_undeterminable(
    fake_job_store, fake_store, fake_dispatch, fake_persona_store, fixed_run_id, monkeypatch
):
    """Best-effort: a provisioning outage (status None) must not hard-block the
    start — the run proceeds and surfaces a real failure later if unrunnable."""
    from agent_team_studio.user_agent_founder.api import main as api_main
    from agent_team_studio.user_agent_founder.api.main import (
        StartRunRequest,
        start_founder_workflow,
    )

    monkeypatch.setattr(api_main, "_agentic_process_status", lambda _t, _p: None)
    resp = start_founder_workflow(
        StartRunRequest(target_team_key="agentic_team:team-1", process_id="proc-9")
    )
    assert resp.job_id == fixed_run_id
    assert fake_dispatch == [fixed_run_id]
    # The best-effort path must still thread process_id into the run (a regression
    # that dropped it would otherwise pass this test silently).
    assert fake_store.create_run.call_args.kwargs.get("process_id") == "proc-9"


def test_agentic_process_status_maps_team_detail(monkeypatch):
    """_agentic_process_status returns the process status, 'missing' when absent,
    and None on a cross-service outage."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    detail = {"team": {"processes": [{"process_id": "p1", "status": "complete"}]}}
    monkeypatch.setattr(
        api_main.httpx, "Client", lambda *a, **kw: _FakeTeamsClient([], {"t1": detail})
    )
    # Reuse the _FakeTeamsClient: it returns the detail for /teams/{id}.
    assert api_main._agentic_process_status("t1", "p1") == "complete"
    assert api_main._agentic_process_status("t1", "nope") == "not_found"

    # Outage → None (never raises).
    class _BoomClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *a, **kw):
            raise RuntimeError("provisioning down")

    monkeypatch.setattr(api_main.httpx, "Client", lambda *a, **kw: _BoomClient())
    assert api_main._agentic_process_status("t1", "p1") is None


# ---------------------------------------------------------------------------
# /testable-teams — static targets + agentic teams with a complete process
# ---------------------------------------------------------------------------


class _FakeTeamsClient:
    """Fake httpx.Client for the cross-service agentic-teams enumeration.

    Returns the teams list for ``/teams`` and a per-team detail for
    ``/teams/{id}``; ``raise_on_list`` simulates the provisioning service being
    unreachable.
    """

    def __init__(self, teams_list, details, raise_on_list=False):
        self._teams_list = teams_list
        self._details = details
        self._raise_on_list = raise_on_list

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, *, timeout=None):
        if url.endswith("/teams"):
            if self._raise_on_list:
                raise RuntimeError("provisioning down")
            return _TeamsResp(200, self._teams_list)
        team_id = url.rsplit("/teams/", 1)[1]
        return _TeamsResp(200, self._details.get(team_id, {}))


class _TeamsResp:
    """Fake httpx.Response for team endpoints: a fixed status code and JSON body."""

    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


class _StatusClient:
    """Fake httpx.Client whose every GET returns a fixed status code."""

    def __init__(self, code, body=None):
        self._code = code
        self._body = body or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, *, timeout=None):
        return _TeamsResp(self._code, self._body)


def test_agentic_process_status_404_is_not_found_but_5xx_is_none(monkeypatch):
    """A definitive 404 (team not found) is a gate rejection ('not_found'); a 5xx
    or other non-404 HTTP error is an outage ('None') and must not hard-block."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    monkeypatch.setattr(api_main.httpx, "Client", lambda *a, **kw: _StatusClient(404))
    assert api_main._agentic_process_status("t1", "p1") == "not_found"

    monkeypatch.setattr(api_main.httpx, "Client", lambda *a, **kw: _StatusClient(503))
    assert api_main._agentic_process_status("t1", "p1") is None

    monkeypatch.setattr(api_main.httpx, "Client", lambda *a, **kw: _StatusClient(401))
    assert api_main._agentic_process_status("t1", "p1") is None


def test_agentic_process_status_non_dict_body_is_not_found(monkeypatch):
    """A 2xx whose body is a JSON list/scalar (no ``.get('team')``) degrades to
    'not_found' rather than raising — the process simply can't be resolved."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    monkeypatch.setattr(
        api_main.httpx, "Client", lambda *a, **kw: _StatusClient(200, ["unexpected", "list"])
    )
    assert api_main._agentic_process_status("t1", "p1") == "not_found"


class _PartialClient:
    """List succeeds with teams A and B (both have a complete process), but B's
    detail GET raises — simulating a transient mid-loop transport fault."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, *, timeout=None):
        if url.endswith("/teams"):
            return _TeamsResp(
                200,
                [
                    {"team_id": "A", "name": "Alpha", "process_count": 1},
                    {"team_id": "B", "name": "Beta", "process_count": 1},
                ],
            )
        if url.endswith("/teams/A"):
            return _TeamsResp(
                200, {"team": {"name": "Alpha", "processes": [{"status": "complete"}]}}
            )
        raise RuntimeError("B detail transport reset")


def test_list_agentic_testable_teams_keeps_others_when_one_detail_fails(monkeypatch):
    """A per-team detail fetch that raises skips only that team — the teams
    already collected are kept (not discarded to [])."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    monkeypatch.setattr(api_main.httpx, "Client", lambda *a, **kw: _PartialClient())
    teams = api_main._list_agentic_testable_teams()
    assert {t.team_key for t in teams} == {"agentic_team:A"}


class _NoCountClient:
    """A /teams summary that OMITS process_count, but the team detail has a
    complete process — the team must still be enumerated (the count is only an
    optimization, not an eligibility signal)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, *, timeout=None):
        if url.endswith("/teams"):
            return _TeamsResp(200, [{"team_id": "A", "name": "Alpha"}])  # no process_count
        return _TeamsResp(200, {"team": {"name": "Alpha", "processes": [{"status": "complete"}]}})


def test_list_agentic_testable_teams_lists_team_when_process_count_absent(monkeypatch):
    """A team summary that omits ``process_count`` is still enumerated when its
    detail shows a complete process (a missing count is not a 'no processes' signal)."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    monkeypatch.setattr(api_main.httpx, "Client", lambda *a, **kw: _NoCountClient())
    teams = api_main._list_agentic_testable_teams()
    assert {t.team_key for t in teams} == {"agentic_team:A"}


def test_list_agentic_testable_teams_handles_non_list_response(monkeypatch):
    """A /teams response that isn't a list degrades to [] (and is logged) rather
    than raising into the broad except."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    monkeypatch.setattr(
        api_main.httpx, "Client", lambda *a, **kw: _StatusClient(200, {"unexpected": "dict"})
    )
    assert api_main._list_agentic_testable_teams() == []


class _MixedListClient:
    """A /teams list with a stray non-dict element alongside a valid team. The
    non-dict must be skipped (not raise AttributeError on ``.get`` and discard the
    valid team via the broad except)."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, *, timeout=None):
        if url.endswith("/teams"):
            return _TeamsResp(200, ["not-a-dict", {"team_id": "A", "name": "Alpha"}])
        return _TeamsResp(200, {"team": {"name": "Alpha", "processes": [{"status": "complete"}]}})


def test_list_agentic_testable_teams_skips_non_dict_summary_elements(monkeypatch):
    """A non-dict element in the /teams list is filtered out, leaving the valid
    agentic team enumerated rather than the whole result collapsing to []."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    monkeypatch.setattr(api_main.httpx, "Client", lambda *a, **kw: _MixedListClient())
    teams = api_main._list_agentic_testable_teams()
    assert {t.team_key for t in teams} == {"agentic_team:A"}


def test_fetch_agentic_team_coerces_non_dict_team_to_empty_dict():
    """A truthy *non-dict* ``team`` value (e.g. a list from an API shape change)
    is coerced to {} so callers doing ``team.get(...)`` can't AttributeError —
    honoring the function's documented dict contract."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    class _Client:
        def get(self, url, *, timeout=None):
            return _TeamsResp(200, {"team": ["not", "a", "dict"]})

    code, team = api_main._fetch_agentic_team(_Client(), "t1")
    assert code == 200
    assert team == {}


def test_testable_teams_includes_agentic_teams_with_complete_process(monkeypatch):
    """/testable-teams returns the static registry targets plus any agentic team
    that has at least one ``complete`` process (keyed ``agentic_team:<id>``)."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    teams_list = [
        {"team_id": "A", "name": "Growth Pod", "process_count": 1},
        {"team_id": "B", "name": "Draft Pod", "process_count": 1},
        {"team_id": "C", "name": "Empty Pod", "process_count": 0},
    ]
    details = {
        "A": {"team": {"name": "Growth Pod", "processes": [{"status": "complete"}]}},
        "B": {"team": {"name": "Draft Pod", "processes": [{"status": "draft"}]}},
    }
    monkeypatch.setattr(
        api_main.httpx, "Client", lambda *a, **kw: _FakeTeamsClient(teams_list, details)
    )

    resp = api_main.list_testable_teams()
    keys = {t.team_key for t in resp.teams}
    # Static SE target is always present.
    assert "software_engineering" in keys
    # Only team A (a complete process) is offered; B (draft) and C (no process) are not.
    assert "agentic_team:A" in keys
    assert "agentic_team:B" not in keys
    assert "agentic_team:C" not in keys
    growth = next(t for t in resp.teams if t.team_key == "agentic_team:A")
    assert growth.display_name == "Growth Pod"


def test_testable_teams_survives_provisioning_outage(monkeypatch):
    """When the cross-service agentic enumeration fails, /testable-teams still
    returns the static registry targets (best-effort: the outage isn't fatal)."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    monkeypatch.setattr(
        api_main.httpx,
        "Client",
        lambda *a, **kw: _FakeTeamsClient([], {}, raise_on_list=True),
    )

    resp = api_main.list_testable_teams()
    # Cross-service failure must not break the static listing.
    assert any(t.team_key == "software_engineering" for t in resp.teams)
    assert not any(t.team_key.startswith("agentic_team:") for t in resp.teams)


def test_testable_teams_handles_non_dict_team_configs(monkeypatch):
    """If TEAM_CONFIGS imports as a non-dict (e.g. after a refactor), the endpoint
    degrades to generated display names rather than 500-ing on ``.get``."""
    import sys
    import types

    from agent_team_studio.user_agent_founder.api import main as api_main

    fake_cfg = types.ModuleType("unified_api.config")
    fake_cfg.TEAM_CONFIGS = ["not", "a", "dict"]  # wrong shape on purpose
    monkeypatch.setitem(sys.modules, "unified_api.config", fake_cfg)
    # Skip the cross-service enumeration; this test is about the config guard.
    monkeypatch.setattr(api_main, "_list_agentic_testable_teams", lambda: [])

    resp = api_main.list_testable_teams()
    se = next(t for t in resp.teams if t.team_key == "software_engineering")
    assert se.display_name  # generated fallback, no AttributeError


def test_dispatch_uses_temporal_when_enabled(fake_store, monkeypatch):
    """When Temporal is enabled, _dispatch_founder_run starts the workflow
    (not a thread) and reports the "Temporal" mode label."""
    import shared.temporal
    from agent_team_studio.user_agent_founder.api import main as api_main
    from agent_team_studio.user_agent_founder.temporal import start_workflow as sw

    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    started: list[str] = []
    monkeypatch.setattr(sw, "start_founder_workflow", lambda rid: started.append(rid))

    mode = api_main._dispatch_founder_run("run-x")

    assert mode == "Temporal"
    assert started == ["run-x"]


def test_dispatch_thread_mode_threads_process_id_and_spec(fake_store, monkeypatch):
    """Regression: the thread path builds the adapter itself (bypassing
    run_workflow's fallback), so it must thread the run's process_id (and the
    spec seed) — otherwise an agentic run hits start_build with process_id=None."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    class _Run:
        target_team_key = "agentic_team:t1"
        process_id = "proc-9"
        spec_content = "# spec"
        persona_id = None

    fake_store.get_run.return_value = _Run()

    captured: dict = {}

    def fake_get_adapter(team_key, *, process_id=None, spec=None):
        captured.update(team_key=team_key, process_id=process_id, spec=spec)
        return object()

    class _FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            pass

    monkeypatch.setattr(api_main, "get_adapter", fake_get_adapter)
    monkeypatch.setattr(api_main, "_build_agent_for_run", lambda _rid: object())
    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)

    mode = api_main._dispatch_founder_run("run-x")
    assert mode == "thread"
    assert captured == {"team_key": "agentic_team:t1", "process_id": "proc-9", "spec": "# spec"}


def test_start_marks_job_failed_when_dispatch_raises(
    fake_job_store, fake_store, fake_persona_store, fixed_run_id, monkeypatch
):
    """If dispatch raises after the job row is created, /start marks the job
    'failed' (with the error recorded) and surfaces a 500 rather than leaving a
    dangling 'running' row."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    def _boom(run_id: str) -> str:
        raise RuntimeError("no worker")

    monkeypatch.setattr(api_main, "_dispatch_founder_run", _boom)

    with pytest.raises(HTTPException) as excinfo:
        api_main.start_founder_workflow()

    assert excinfo.value.status_code == 500
    assert fake_job_store.jobs[fixed_run_id]["status"] == "failed"
    assert "no worker" in fake_job_store.jobs[fixed_run_id]["error"]


# ---------------------------------------------------------------------------
# /job/{id}/resume
# ---------------------------------------------------------------------------


def test_resume_rejects_missing_job(fake_job_store, fake_store, fake_dispatch):
    from agent_team_studio.user_agent_founder.api.main import resume_job

    with pytest.raises(HTTPException) as excinfo:
        resume_job("missing")
    assert excinfo.value.status_code == 404
    assert fake_dispatch == []


def test_resume_rejects_completed_job(fake_job_store, fake_store, fake_dispatch):
    from agent_team_studio.user_agent_founder.api.main import resume_job

    fake_job_store.create_job("run-done", status="completed")

    with pytest.raises(HTTPException) as excinfo:
        resume_job("run-done")
    assert excinfo.value.status_code == 400
    assert fake_dispatch == []


def test_resume_redispatches_failed_job(fake_job_store, fake_store, fake_dispatch):
    from agent_team_studio.user_agent_founder.api.main import resume_job

    fake_job_store.create_job("run-bad", status="failed", error="boom")

    resp = resume_job("run-bad")

    assert resp.job_id == "run-bad"
    assert fake_dispatch == ["run-bad"]
    assert fake_job_store.jobs["run-bad"]["status"] == "running"
    assert fake_job_store.jobs["run-bad"]["error"] is None


def test_resume_mirrors_dispatch_failure_into_founder_store(
    fake_job_store, fake_store, monkeypatch
):
    """Codex P1: if redispatch raises, both the central job and the founder
    store row must be marked failed — otherwise the Testing Personas dashboard
    leaves the run at ``pending`` in its Running section."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    fake_job_store.create_job("run-bad", status="failed", error="boom")
    monkeypatch.setattr(
        api_main,
        "_dispatch_founder_run",
        lambda _run_id: (_ for _ in ()).throw(RuntimeError("no worker")),
    )

    with pytest.raises(HTTPException) as excinfo:
        api_main.resume_job("run-bad")

    assert excinfo.value.status_code == 500
    assert fake_job_store.jobs["run-bad"]["status"] == "failed"
    assert "no worker" in fake_job_store.jobs["run-bad"]["error"]
    fake_store.update_run.assert_any_call(
        "run-bad", status="failed", error="Resume dispatch failed: no worker"
    )


# ---------------------------------------------------------------------------
# /job/{id}/restart
# ---------------------------------------------------------------------------


def test_restart_rejects_running_job(fake_job_store, fake_store, fake_dispatch):
    from agent_team_studio.user_agent_founder.api.main import restart_job

    fake_job_store.create_job("run-live", status="running")

    with pytest.raises(HTTPException) as excinfo:
        restart_job("run-live")
    assert excinfo.value.status_code == 400
    assert fake_dispatch == []


def test_restart_resets_and_redispatches_completed_job(fake_job_store, fake_store, fake_dispatch):
    from agent_team_studio.user_agent_founder.api.main import restart_job

    fake_job_store.create_job("run-done", status="completed", error=None)

    resp = restart_job("run-done")

    assert resp.job_id == "run-done"
    assert fake_dispatch == ["run-done"]
    assert "run-done" in fake_job_store.reset_calls
    assert fake_job_store.jobs["run-done"]["status"] == "running"


def test_restart_clears_founder_store_checkpoint_columns(fake_job_store, fake_store, fake_dispatch):
    """Restart must NULL every column the resume short-circuit reads, otherwise
    a restarted run skips spec/analysis or polls a stale SE job id (#347)."""
    from agent_team_studio.user_agent_founder.api.main import restart_job

    fake_job_store.create_job("run-done", status="completed", error=None)

    restart_job("run-done")

    fake_store.update_run.assert_any_call(
        "run-done",
        status="pending",
        error=None,
        spec_content=None,
        analysis_job_id=None,
        repo_path=None,
        se_job_id=None,
    )


def test_restart_mirrors_dispatch_failure_into_founder_store(
    fake_job_store, fake_store, monkeypatch
):
    """Codex P1: same invariant as test_resume_mirrors_dispatch_failure_into_founder_store
    but for /job/{id}/restart."""
    from agent_team_studio.user_agent_founder.api import main as api_main

    fake_job_store.create_job("run-done", status="completed", error=None)
    monkeypatch.setattr(
        api_main,
        "_dispatch_founder_run",
        lambda _run_id: (_ for _ in ()).throw(RuntimeError("no worker")),
    )

    with pytest.raises(HTTPException) as excinfo:
        api_main.restart_job("run-done")

    assert excinfo.value.status_code == 500
    assert fake_job_store.jobs["run-done"]["status"] == "failed"
    assert "no worker" in fake_job_store.jobs["run-done"]["error"]
    fake_store.update_run.assert_any_call(
        "run-done", status="failed", error="Restart dispatch failed: no worker"
    )


# ---------------------------------------------------------------------------
# /job/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_rejects_missing_job(fake_job_store, fake_store):
    from agent_team_studio.user_agent_founder.api.main import cancel_job

    with pytest.raises(HTTPException) as excinfo:
        cancel_job("ghost")
    assert excinfo.value.status_code == 404


def test_cancel_rejects_completed_job(fake_job_store, fake_store):
    from agent_team_studio.user_agent_founder.api.main import cancel_job

    fake_job_store.create_job("run-done", status="completed")
    with pytest.raises(HTTPException) as excinfo:
        cancel_job("run-done")
    assert excinfo.value.status_code == 400


def test_cancel_updates_job_and_store(fake_job_store, fake_store):
    from agent_team_studio.user_agent_founder.api.main import cancel_job

    fake_job_store.create_job("run-live", status="running")

    result = cancel_job("run-live")

    assert result == {"status": "cancelled", "job_id": "run-live"}
    assert fake_job_store.jobs["run-live"]["status"] == "cancelled"
    fake_store.update_run.assert_called_once_with(
        "run-live", status="failed", error="Cancelled by user"
    )


def test_cancel_signals_temporal_workflow_when_enabled(fake_job_store, fake_store, monkeypatch):
    """When Temporal is enabled, cancel also signals the workflow so its poll
    loops stop at the next tick (thread mode has no workflow to signal)."""
    import shared.temporal
    from agent_team_studio.user_agent_founder.api.main import cancel_job
    from agent_team_studio.user_agent_founder.temporal import start_workflow as sw

    fake_job_store.create_job("run-live", status="running")
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)
    signalled: list[str] = []
    monkeypatch.setattr(sw, "cancel_founder_workflow", lambda rid: signalled.append(rid))

    result = cancel_job("run-live")

    assert result == {"status": "cancelled", "job_id": "run-live"}
    assert signalled == ["run-live"]


def test_cancel_temporal_signal_failure_is_non_fatal(fake_job_store, fake_store, monkeypatch):
    """A failed cancel signal (no worker, already terminal) must not break the
    cancel — the store already recorded the terminal state."""
    import shared.temporal
    from agent_team_studio.user_agent_founder.api.main import cancel_job
    from agent_team_studio.user_agent_founder.temporal import start_workflow as sw

    fake_job_store.create_job("run-live", status="running")
    monkeypatch.setattr(shared.temporal, "is_temporal_enabled", lambda: True)

    def _boom(_rid):
        raise RuntimeError("no worker")

    monkeypatch.setattr(sw, "cancel_founder_workflow", _boom)

    result = cancel_job("run-live")

    assert result == {"status": "cancelled", "job_id": "run-live"}
    assert fake_job_store.jobs["run-live"]["status"] == "cancelled"


# ---------------------------------------------------------------------------
# DELETE /job/{id}
# ---------------------------------------------------------------------------


def test_delete_returns_404_when_missing(fake_job_store, fake_store):
    from agent_team_studio.user_agent_founder.api.main import delete_job

    with pytest.raises(HTTPException) as excinfo:
        delete_job("ghost")
    assert excinfo.value.status_code == 404


def test_delete_removes_from_both_stores(fake_job_store, fake_store):
    from agent_team_studio.user_agent_founder.api.main import delete_job

    fake_job_store.create_job("run-done", status="completed")

    result = delete_job("run-done")

    assert result == {"deleted": "true", "job_id": "run-done"}
    assert "run-done" in fake_job_store.delete_calls
    fake_store.delete_run.assert_called_once_with("run-done")
