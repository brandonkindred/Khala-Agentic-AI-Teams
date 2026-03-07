import asyncio
from unittest.mock import AsyncMock, Mock, patch

from accessibility_audit_team.models import AuditRequest, WCAGLevel
from accessibility_audit_team.strands_integration import (
    StrandsAuditInvocation,
    create_accessibility_audit_orchestrator,
    get_team_spec,
    run_audit_async,
)


def test_get_team_spec_contract() -> None:
    spec = get_team_spec()

    assert spec["name"] == "accessibility_audit_team"
    assert spec["input_model"] is StrandsAuditInvocation
    assert spec["handler_factory"]
    assert spec["async_handler_factory"]


def test_create_accessibility_audit_orchestrator() -> None:
    orchestrator = create_accessibility_audit_orchestrator()
    assert orchestrator is not None


def test_strands_invocation_validation_defaults() -> None:
    invocation = StrandsAuditInvocation.model_validate(
        {
            "audit_request": {
                "name": "Smoke",
                "web_urls": ["https://example.com"],
                "critical_journeys": ["login"],
                "wcag_levels": ["A", "AA"],
            }
        }
    )

    assert invocation.tech_stack == {"web": "other", "mobile": "other"}
    assert invocation.audit_request.wcag_levels == [WCAGLevel.A, WCAGLevel.AA]


def test_async_handler_uses_orchestrator() -> None:
    payload = {
        "audit_request": AuditRequest(
            audit_id="audit_test",
            name="Integration",
            web_urls=["https://example.com"],
            critical_journeys=["login"],
        ).model_dump(),
        "tech_stack": {"web": "react", "mobile": "swift"},
    }

    fake_result = Mock(success=True, total_findings=0)
    fake_orchestrator = Mock()
    fake_orchestrator.run_audit = AsyncMock(return_value=fake_result)

    with patch(
        "accessibility_audit_team.strands_integration.create_accessibility_audit_orchestrator",
        return_value=fake_orchestrator,
    ):
        result = asyncio.run(run_audit_async(llm_client=None, payload=payload))

    fake_orchestrator.run_audit.assert_awaited_once()
    called_request = fake_orchestrator.run_audit.await_args.kwargs["audit_request"]
    called_stack = fake_orchestrator.run_audit.await_args.kwargs["tech_stack"]
    assert called_request.audit_id == "audit_test"
    assert called_stack == {"web": "react", "mobile": "swift"}
    assert result is fake_result
