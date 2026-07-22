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


def test_default_status_callback_logs_detail(caplog) -> None:
    """Preconditions: lead constructed with default callback installed.
    Postconditions: _report_status with a non-empty detail emits that detail at INFO.
    """
    lead = DevOpsTeamLeadAgent(DummyLLMClient())
    message = "DevOps team pipeline: phase 2 - change design (parallel)"
    with caplog.at_level(logging.INFO, logger="software_engineering_team.devops_team.orchestrator"):
        lead._report_status("phase2", detail=message)
    assert message in caplog.text


def test_custom_status_callback_receives_payload() -> None:
    """Preconditions: lead._status_callback replaced with a recording callable.
    Postconditions: _report_status forwards phase, detail, and progress kwargs.
    """
    lead = DevOpsTeamLeadAgent(DummyLLMClient())
    calls: list[dict] = []
    lead._status_callback = lambda **kw: calls.append(kw)
    lead._report_status("phase5", detail="completion", progress=0.9)
    assert calls == [{"phase": "phase5", "detail": "completion", "progress": 0.9}]
