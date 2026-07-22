"""Unit tests for DevOpsTeamLeadAgent status-hook wiring (status migration)."""

from __future__ import annotations

import logging

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.devops_team import DevOpsTeamLeadAgent
from software_engineering_team.shared.team_lead_base import TeamLeadSharedState


def test_devops_lead_is_team_lead_shared_state() -> None:
    """Preconditions: DummyLLMClient is a valid LLM client.
    Postconditions: DevOpsTeamLeadAgent instances are TeamLeadSharedState subclasses.
    """
    lead = DevOpsTeamLeadAgent(DummyLLMClient())
    assert isinstance(lead, TeamLeadSharedState)


def test_report_status_logs_when_callback_unset(caplog) -> None:
    """Preconditions: lead with ``_status_callback`` left at the mixin default (None).
    Postconditions: ``_report_status`` still emits the detail at INFO.
    """
    lead = DevOpsTeamLeadAgent(DummyLLMClient())
    assert lead._status_callback is None
    message = "DevOps team pipeline: phase 2 - change design (parallel)"
    with caplog.at_level(logging.INFO, logger="software_engineering_team.devops_team.orchestrator"):
        lead._report_status("phase2", detail=message)
    assert message in caplog.text


def test_custom_status_callback_receives_payload_and_still_logs(caplog) -> None:
    """Preconditions: lead._status_callback replaced with a recording callable.
    Postconditions: callback receives phase/detail/progress; INFO log still emits.
    """
    lead = DevOpsTeamLeadAgent(DummyLLMClient())
    calls: list[dict] = []
    lead._status_callback = lambda **kw: calls.append(kw)
    message = "DevOps team pipeline: phase 5 - completion package assembly"
    with caplog.at_level(logging.INFO, logger="software_engineering_team.devops_team.orchestrator"):
        lead._report_status("phase5", detail=message, progress=0.9)
    assert calls == [{"phase": "phase5", "detail": message, "progress": 0.9}]
    assert message in caplog.text


def test_logging_survives_callback_cleanup(caplog) -> None:
    """Preconditions: consumer binds then clears ``_status_callback`` (per-run contract).
    Postconditions: subsequent ``_report_status`` calls still emit INFO logs.
    """
    lead = DevOpsTeamLeadAgent(DummyLLMClient())
    lead._status_callback = lambda **_kw: None
    lead._status_callback = None
    message = "DevOps team pipeline: phase 4 - validation and review"
    with caplog.at_level(logging.INFO, logger="software_engineering_team.devops_team.orchestrator"):
        lead._report_status("phase4", detail=message)
    assert message in caplog.text
