"""Integration tests for startup advisor specialist routing and gate evaluation."""

from __future__ import annotations

from studiogrid.runtime.orchestrator import Orchestrator, RunContext
from studiogrid.runtime.storage.postgres_store import PostgresStore
from studiogrid.runtime.storage.s3_store import S3Store


class _CapturingRegistry:
    """Mock registry that records which agent was called and returns a valid artifact."""

    def __init__(self, artifact_type: str = "startup_advice_plan") -> None:
        self.called_agent_ids: list[str] = []
        self.received_task_envelopes: list[dict] = []
        self._artifact_type = artifact_type

    def run(self, agent_id: str, task_envelope: dict) -> dict:
        self.called_agent_ids.append(agent_id)
        self.received_task_envelopes.append(task_envelope)
        return {
            "kind": "ARTIFACT",
            "payload": {
                "artifact_type": self._artifact_type,
                "format": "json",
                "payload": {
                    "recommended_focus": "customer_discovery",
                    "next_steps": [{"action": "run interviews", "owner": "founder", "timeline": "this week"}],
                },
            },
        }


def _orchestrator(registry: object) -> Orchestrator:
    return Orchestrator(PostgresStore(), S3Store(), registry, {}, None, {})


def _run_with_intake(intake: dict) -> tuple[Orchestrator, RunContext, _CapturingRegistry]:
    reg = _CapturingRegistry()
    orch = _orchestrator(reg)
    project_id = orch.create_project(name="Test Project", idempotency_key="proj:test")
    ctx = orch.create_run(project_id=project_id, idempotency_key="run:test")
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "intake", "format": "json", "payload": intake},
        raw_bytes=None,
        idempotency_key="intake:test",
    )
    return orch, ctx, reg


# ---------------------------------------------------------------------------
# Specialist routing tests
# ---------------------------------------------------------------------------


def test_routes_to_orchestrator_when_no_domain_signal() -> None:
    orch, ctx, reg = _run_with_intake({"brief": "help me build a startup"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "startup_advisor_orchestrator"


def test_routes_to_fundraising_advisor_by_domain_field() -> None:
    orch, ctx, reg = _run_with_intake({"domain": "fundraising", "brief": "need to raise seed round"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "fundraising_finance_advisor"


def test_routes_to_customer_discovery_advisor_by_domain_field() -> None:
    orch, ctx, reg = _run_with_intake({"domain": "customer_discovery", "brief": "understanding our users"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "customer_discovery_advisor"


def test_routes_to_growth_gtm_advisor_by_domain_field() -> None:
    orch, ctx, reg = _run_with_intake({"domain": "growth"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "growth_gtm_advisor"


def test_routes_to_gtm_advisor_by_gtm_keyword_in_brief() -> None:
    orch, ctx, reg = _run_with_intake({"brief": "We need help planning our gtm channels"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "growth_gtm_advisor"


def test_routes_to_operations_advisor_by_legal_keyword() -> None:
    orch, ctx, reg = _run_with_intake({"description": "setting up legal entity and compliance for EU"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "operations_legal_advisor"


def test_routes_to_product_advisor_by_keyword_in_goals() -> None:
    orch, ctx, reg = _run_with_intake({"goals": "define mvp scope and product roadmap"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "product_strategy_advisor"


def test_routes_to_founder_coach_by_leadership_keyword() -> None:
    orch, ctx, reg = _run_with_intake({"challenge": "struggling with leadership decisions as a founder"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "founder_coach_advisor"


def test_domain_field_takes_precedence_over_brief_keyword() -> None:
    # domain=fundraising should win over 'product' keyword in brief
    orch, ctx, reg = _run_with_intake({"domain": "fundraising", "brief": "need product roadmap and fundraising"})
    tasks = orch.build_phase_tasks(ctx=ctx)
    assert tasks[0]["owner_agent"] == "fundraising_finance_advisor"


# ---------------------------------------------------------------------------
# Prior artifact context passing tests
# ---------------------------------------------------------------------------


def test_prior_artifacts_passed_to_second_task() -> None:
    reg = _CapturingRegistry()
    orch = _orchestrator(reg)
    project_id = orch.create_project(name="Multi-phase", idempotency_key="proj:multi")
    ctx = orch.create_run(project_id=project_id, idempotency_key="run:multi")

    # Persist intake and first specialist artifact
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "intake", "format": "json", "payload": {"domain": "product"}},
        raw_bytes=None,
        idempotency_key="intake:multi",
    )
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={
            "artifact_type": "discovery_brief",
            "format": "json",
            "payload": {"target_segment": "SMB finance teams"},
        },
        raw_bytes=None,
        idempotency_key="discovery:multi",
    )

    tasks = orch.build_phase_tasks(ctx=ctx)
    prior = tasks[0]["prior_artifacts"]
    prior_types = [p["artifact_type"] for p in prior]

    assert "discovery_brief" in prior_types
    # intake should not appear in prior_artifacts (it's the input, not a prior output)
    assert "intake" not in prior_types


def test_prior_artifact_payloads_included_in_task_envelope() -> None:
    reg = _CapturingRegistry()
    orch = _orchestrator(reg)
    project_id = orch.create_project(name="Context passing", idempotency_key="proj:ctx")
    ctx = orch.create_run(project_id=project_id, idempotency_key="run:ctx")

    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={"artifact_type": "intake", "format": "json", "payload": {"domain": "growth"}},
        raw_bytes=None,
        idempotency_key="intake:ctx",
    )
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={
            "artifact_type": "discovery_brief",
            "format": "json",
            "payload": {"target_segment": "enterprise"},
        },
        raw_bytes=None,
        idempotency_key="discovery:ctx",
    )

    tasks = orch.build_phase_tasks(ctx=ctx)
    orch.dispatch_task_to_agent(ctx=ctx, task=tasks[0], idempotency_key="dispatch:ctx")

    envelope = reg.received_task_envelopes[0]
    prior_payloads = envelope.get("prior_artifacts", [])
    assert any(p.get("artifact_type") == "discovery_brief" for p in prior_payloads)
    enterprise_found = any(
        isinstance(p.get("payload"), dict) and p["payload"].get("target_segment") == "enterprise"
        for p in prior_payloads
    )
    assert enterprise_found


# ---------------------------------------------------------------------------
# Gate evaluation tests
# ---------------------------------------------------------------------------


def test_gate_schema_passes_when_artifacts_have_required_fields() -> None:
    orch, ctx, _ = _run_with_intake({"domain": "product"})
    orch.persist_artifact(
        ctx=ctx,
        artifact_payload={
            "artifact_type": "product_strategy_memo",
            "format": "json",
            "payload": {"recommended_mvp": "auth flow only"},
        },
        raw_bytes=None,
        idempotency_key="memo:schema",
    )
    results = orch.run_gates_for_phase(ctx=ctx, gates=["schema"], idempotency_key="gate:schema")
    assert len(results) == 1
    assert results[0].passed is True


def test_gate_completeness_fails_for_empty_payload() -> None:
    orch, ctx, _ = _run_with_intake({"domain": "product"})
    # Manually insert an artifact with an empty payload to simulate a broken agent output
    artifact_id = "art_test_empty"
    orch.store.artifacts[artifact_id] = {
        "artifact_id": artifact_id,
        "run_id": ctx.run_id,
        "artifact_type": "product_strategy_memo",
        "version": 1,
        "format": "json",
        "uri": f"s3://studiogrid/{ctx.run_id}/product_strategy_memo/v1.json",
        "payload": {},  # deliberately empty
    }
    orch.store.artifact_latest[(ctx.run_id, "product_strategy_memo")] = artifact_id

    results = orch.run_gates_for_phase(ctx=ctx, gates=["completeness"], idempotency_key="gate:completeness")
    assert results[0].passed is False


def test_gate_force_fail_always_fails() -> None:
    orch, ctx, _ = _run_with_intake({})
    results = orch.run_gates_for_phase(ctx=ctx, gates=["force_fail"], idempotency_key="gate:ff")
    assert results[0].passed is False


def test_gate_unknown_name_passes() -> None:
    orch, ctx, _ = _run_with_intake({})
    results = orch.run_gates_for_phase(ctx=ctx, gates=["some_future_gate"], idempotency_key="gate:unknown")
    assert results[0].passed is True


def test_multiple_gates_evaluated_independently() -> None:
    orch, ctx, _ = _run_with_intake({})
    results = orch.run_gates_for_phase(
        ctx=ctx, gates=["schema", "completeness", "force_fail"], idempotency_key="gate:multi"
    )
    assert len(results) == 3
    gate_map = {r.gate: r.passed for r in results}
    assert gate_map["schema"] is True
    assert gate_map["completeness"] is True  # no non-intake artifacts → nothing to fail
    assert gate_map["force_fail"] is False


# ---------------------------------------------------------------------------
# Registry — design_lead removed from startup registry
# ---------------------------------------------------------------------------


def test_design_lead_not_in_startup_registry() -> None:
    from pathlib import Path

    from studiogrid.runtime.registry_loader import RegistryLoader

    root = Path(__file__).resolve().parents[1] / "src" / "studiogrid"
    registry = RegistryLoader(root)
    agent_ids = [a["agent_id"] for a in registry.list_agents()]
    assert "design_lead" not in agent_ids, "design_lead should be in design_registry.yaml, not agent_registry.yaml"
