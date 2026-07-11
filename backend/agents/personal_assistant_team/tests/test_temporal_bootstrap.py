"""Bootstrap/wiring regression tests for the personal-assistant Temporal setup.

Guards the same failure modes the canonical teams protect against:

1. Importing the ``temporal`` package must NOT spin up a worker thread (that
   would race the first ``start_assistant_workflow`` call).
2. The workflow module + package ``__init__`` must not call ``os.getenv`` at
   import time — the temporalio sandbox replays them during workflow
   registration.
3. The ``TEAM_TEMPORAL_WORKER_FUNC`` contract pinned in docker-compose
   (``start_pa_temporal_worker_thread``) must stay in place.
4. The export contract (``WORKFLOWS``/``ACTIVITIES``/``TASK_QUEUE``) and the
   shared-bridge delegation must be intact.
"""

from __future__ import annotations

import importlib
import sys
import unittest.mock as mock


def _purge(prefix: str) -> None:
    for name in list(sys.modules):
        if name == prefix or name.startswith(prefix + "."):
            del sys.modules[name]


def test_importing_temporal_package_does_not_call_start_team_worker():
    """Loading the package must NOT spin up a worker thread."""
    import shared_temporal

    _purge("personal_assistant_team.temporal")
    with mock.patch.object(shared_temporal, "start_team_worker") as patched:
        importlib.import_module("personal_assistant_team.temporal")
        importlib.import_module("personal_assistant_team.temporal.workflows")
        importlib.import_module("personal_assistant_team.temporal.start_workflow")
        assert patched.call_count == 0, (
            f"Module-level start_team_worker bootstrap re-introduced "
            f"(call count = {patched.call_count}); this races the first request "
            f"and trips the temporalio sandbox during workflow registration."
        )


def test_workflows_and_package_do_not_call_os_getenv_at_import_time():
    """Neither the workflow module nor the package __init__ may invoke
    ``os.getenv`` at import — it has to live inside activity bodies or the
    worker bootstrap, or the temporalio sandbox aborts registration."""
    _purge("personal_assistant_team.temporal")
    import os

    importlib.import_module("personal_assistant_team.temporal.workflows")
    with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:
        importlib.import_module("personal_assistant_team.temporal")
        assert spy.call_count == 0, (
            f"personal_assistant_team.temporal.__init__ called os.getenv "
            f"{spy.call_count} time(s) at import — this trips the temporalio "
            f"workflow sandbox during workflow registration."
        )


def test_export_contract():
    """WORKFLOWS/ACTIVITIES/TASK_QUEUE expose the full decomposed team."""
    from personal_assistant_team.temporal import (
        ACTIVITIES,
        TASK_QUEUE,
        WORKFLOW_ID_PREFIX_ASSISTANT,
        WORKFLOWS,
        PaAssistantWorkflow,
    )
    from personal_assistant_team.temporal.activities import (
        check_profile_updates_activity,
        classify_intent_activity,
        fail_job_activity,
        finalize_success_activity,
        generate_response_activity,
        handle_calendar_activity,
        handle_deals_activity,
        handle_documentation_activity,
        handle_email_activity,
        handle_general_activity,
        handle_profile_activity,
        handle_reservations_activity,
        handle_tasks_activity,
        run_assistant_activity,
    )

    assert WORKFLOWS == [PaAssistantWorkflow]
    assert TASK_QUEUE == "personal-assistant"
    assert WORKFLOW_ID_PREFIX_ASSISTANT == "pa-assistant-"

    expected_activities = {
        classify_intent_activity,
        handle_email_activity,
        handle_calendar_activity,
        handle_tasks_activity,
        handle_deals_activity,
        handle_reservations_activity,
        handle_documentation_activity,
        handle_profile_activity,
        handle_general_activity,
        check_profile_updates_activity,
        generate_response_activity,
        finalize_success_activity,
        fail_job_activity,
        # Legacy single activity retained for deterministic replay/drain of
        # pre-decomposition workflow executions.
        run_assistant_activity,
    }
    assert set(ACTIVITIES) == expected_activities
    assert len(ACTIVITIES) == len(expected_activities) == 14


def test_worker_module_exposes_team_service_entrypoint():
    """team_service/entrypoint.py resolves ``TEAM_TEMPORAL_WORKER_FUNC`` on
    ``TEAM_TEMPORAL_WORKER_MODULE`` — keep that contract pinned so a rename
    can't silently break docker-compose."""
    from personal_assistant_team.temporal import worker

    fn = getattr(worker, "start_pa_temporal_worker_thread", None)
    assert callable(fn), (
        "team_service entrypoint expects a no-arg start_pa_temporal_worker_thread() "
        "in personal_assistant_team.temporal.worker"
    )


def test_worker_start_is_no_op_when_temporal_disabled(monkeypatch):
    """Standalone dev path: with TEMPORAL_ADDRESS unset the bootstrap returns
    False instead of raising or starting a thread."""
    monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
    from personal_assistant_team.temporal.worker import start_pa_temporal_worker_thread

    assert start_pa_temporal_worker_thread() is False


def test_worker_start_delegates_to_start_team_worker(monkeypatch):
    """When enabled, the no-arg func delegates to ``start_team_worker`` with the
    team's own task queue and returns its result."""
    from personal_assistant_team.temporal import ACTIVITIES, TASK_QUEUE, WORKFLOWS
    from personal_assistant_team.temporal import worker as worker_mod

    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)
    captured: dict = {}

    def _fake_start(team, workflows, activities, *, task_queue):
        captured.update(
            team=team, workflows=workflows, activities=activities, task_queue=task_queue
        )
        return True

    monkeypatch.setattr(worker_mod, "start_team_worker", _fake_start)

    assert worker_mod.start_pa_temporal_worker_thread() is True
    assert captured == {
        "team": "personal_assistant",
        "workflows": WORKFLOWS,
        "activities": ACTIVITIES,
        "task_queue": TASK_QUEUE,
    }


def test_not_registered_in_shared_registry():
    """PA is intentionally NOT in the shared registry: ``start_all_team_workers``
    polls ``f"{team}-queue"`` (personal_assistant-queue), which differs from PA's
    fixed ``personal-assistant`` queue and would split-brain the worker. PA boots
    via its own docker-compose hook on the correct queue instead."""
    from shared_temporal.teams_registry import TEAM_TEMPORAL_MODULES

    assert "personal_assistant" not in TEAM_TEMPORAL_MODULES
