"""Tests for devops_team specialist agents' shared LLM-response caches.

Mirrors ``test_qa_agent_cache.py``'s / ``test_security_agent_cache.py``'s
conventions for the analogous single-shot whole-input cache: a
``_CountingClient`` counts LLM invocations so a hit (no call) can be
distinguished from a miss (a call). Every devops_team specialist that makes
its own single-shot LLM call shares one implementation
(``software_engineering_team.shared.review_result_cache``, also used by
``qa_agent`` and ``security_agent``), so the two representative agents below
— ``InfrastructureAsCodeAgent`` (design-phase, wired through the shared
``DevOpsSingleShotAgent.run()``) and ``DevSecOpsReviewAgent`` (review-phase,
wired at its own call site around ``run_single_shot_review``) — get the full
hit/miss/fallback suite, while the remaining agents get a lighter hit+miss
check since they exercise the same shared helper.

Each agent module resolves and holds its own ``get_shared_cache`` (from
``shared.cache``) at its call site rather than through the shared helper
module, so that is the seam backend-error tests monkeypatch — matching
``test_qa_agent_cache.py``'s convention of patching ``agent_mod.get_shared_cache``.

The caches themselves are cleared around every test by the autouse
``_reset_devops_llm_caches`` fixture in ``conftest.py``, so tests do not
observe cross-test cache hits.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from llm_service.clients.dummy import DummyLLMClient
from llm_service.strands_model import model_fingerprint
from shared.cache import MemoryBackend, get_shared_cache, reset_shared_cache_state
from shared.cache import factory as factory_mod
from software_engineering_team.devops_team import _agent_template
from software_engineering_team.devops_team.cicd_pipeline_agent import (
    CICDPipelineAgent,
    CICDPipelineAgentInput,
)
from software_engineering_team.devops_team.deployment_strategy_agent import (
    DeploymentStrategyAgent,
    DeploymentStrategyAgentInput,
)
from software_engineering_team.devops_team.devsecops_review_agent import (
    DevSecOpsReviewAgent,
    DevSecOpsReviewInput,
)
from software_engineering_team.devops_team.devsecops_review_agent import (
    agent as devsecops_agent_mod,
)
from software_engineering_team.devops_team.doc_runbook_agent import (
    DocumentationRunbookAgent,
    DocumentationRunbookInput,
)
from software_engineering_team.devops_team.iac_agent import IaCAgentInput, InfrastructureAsCodeAgent
from software_engineering_team.devops_team.infra_debug_agent import IaCDebugInput, InfraDebugAgent
from software_engineering_team.devops_team.infra_debug_agent.models import IaCDebugOutput
from software_engineering_team.devops_team.infra_patch_agent import IaCPatchInput, InfraPatchAgent
from software_engineering_team.devops_team.models import DevOpsTaskSpec
from software_engineering_team.devops_team.task_clarifier import (
    DevOpsTaskClarifierAgent,
    DevOpsTaskClarifierInput,
)
from software_engineering_team.shared import review_result_cache as cache_mod


class _CountingClient(DummyLLMClient):
    """Returns a fixed canned response; counts ``complete_json`` calls.

    Same shape as ``test_qa_agent_cache.py``'s ``_CountingClient`` — routes
    through the same ``complete_json`` override every devops_team call path
    (``complete_json_with_continuation`` and ``run_single_shot_review``'s
    schema-validated branch) ends up invoking.
    """

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__()
        self._response = response
        self.calls = 0

    def complete_json(
        self, prompt: str, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
    ) -> Dict[str, Any]:  # type: ignore[override]
        self.calls += 1
        return dict(self._response)


class _ScriptedClient(DummyLLMClient):
    """Returns a different canned response on each ``complete_json`` call."""

    def __init__(self, responses: list) -> None:
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    def complete_json(
        self, prompt: str, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
    ) -> Dict[str, Any]:  # type: ignore[override]
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return dict(resp)


class _RaisingCache:
    """Cache backend that raises on every mutating/read operation."""

    def get(self, key: str) -> None:
        raise RuntimeError("boom")

    def set(self, key: str, value: bytes, *, max_entries: int) -> None:
        raise RuntimeError("boom")

    def delete(self, key: str) -> None:
        raise RuntimeError("boom")

    def clear(self) -> None:
        pass


def _task_spec(**overrides: object) -> DevOpsTaskSpec:
    """Minimal ``DevOpsTaskSpec`` that clears every specialist's input checks."""
    defaults: Dict[str, object] = dict(
        task_id="DO-CACHE-1",
        title="Cache test task",
        platform_scope={"cloud": "aws", "runtime": "eks", "environments": ["dev"]},
        goal={"summary": "Ship a small change"},
        constraints={"secrets": {"source": "aws_secrets_manager"}},
        acceptance_criteria=["Works"],
    )
    defaults.update(overrides)
    return DevOpsTaskSpec(**defaults)  # type: ignore[arg-type]


_IAC_RESPONSE: Dict[str, Any] = {
    "artifacts": {"infra/main.tf": "resource {}"},
    "summary": "created main.tf",
    "destructive_changes_detected": False,
    "blast_radius_notes": [],
}


# ---------------------------------------------------------------------------
# Design-phase representative: InfrastructureAsCodeAgent (shared base class)
# ---------------------------------------------------------------------------


def test_iac_identical_input_hits_cache_and_skips_llm_call() -> None:
    client = _CountingClient(_IAC_RESPONSE)
    agent = InfrastructureAsCodeAgent(client)
    input_data = IaCAgentInput(task_spec=_task_spec())

    first = agent.run(input_data)
    second = agent.run(input_data)

    assert client.calls == 1
    assert first.model_dump() == second.model_dump()


def test_iac_changed_task_spec_busts_cache() -> None:
    client = _CountingClient(_IAC_RESPONSE)
    agent = InfrastructureAsCodeAgent(client)

    agent.run(IaCAgentInput(task_spec=_task_spec()))
    agent.run(IaCAgentInput(task_spec=_task_spec(title="A different task")))

    assert client.calls == 2


def test_iac_redis_unavailable_falls_back_to_memory_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(factory_mod, "_build_redis_client", lambda: None)
    reset_shared_cache_state()
    try:
        namespace = cache_mod.cache_namespace_for(InfrastructureAsCodeAgent.CACHE_NAMESPACE)
        assert isinstance(get_shared_cache(namespace), MemoryBackend)

        client = _CountingClient(_IAC_RESPONSE)
        agent = InfrastructureAsCodeAgent(client)
        input_data = IaCAgentInput(task_spec=_task_spec())

        first = agent.run(input_data)
        second = agent.run(input_data)

        assert client.calls == 1
        assert first.model_dump() == second.model_dump()
    finally:
        reset_shared_cache_state()


def test_iac_cache_backend_error_falls_open_to_correct_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_agent_template, "get_shared_cache", lambda namespace: _RaisingCache())

    client = _CountingClient(_IAC_RESPONSE)
    agent = InfrastructureAsCodeAgent(client)
    result = agent.run(IaCAgentInput(task_spec=_task_spec()))

    assert result.artifacts == _IAC_RESPONSE["artifacts"]
    assert client.calls == 1


def test_iac_cache_disabled_via_env_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_IAC_CACHE_SIZE", "0")
    client = _CountingClient(_IAC_RESPONSE)
    agent = InfrastructureAsCodeAgent(client)
    input_data = IaCAgentInput(task_spec=_task_spec())

    agent.run(input_data)
    agent.run(input_data)

    assert client.calls == 2


# ---------------------------------------------------------------------------
# Review-phase representative: DevSecOpsReviewAgent (own call site)
# ---------------------------------------------------------------------------

_DEVSECOPS_CLEAN_RESPONSE: Dict[str, Any] = {
    "approved": True,
    "findings": [],
    "summary": "all good",
}


def test_devsecops_identical_input_hits_cache_and_skips_llm_call() -> None:
    client = _CountingClient(_DEVSECOPS_CLEAN_RESPONSE)
    agent = DevSecOpsReviewAgent(client)
    input_data = DevSecOpsReviewInput(task_description="test", artifacts={})

    first = agent.run(input_data)
    second = agent.run(input_data)

    assert client.calls == 1
    assert first.model_dump() == second.model_dump()


def test_devsecops_changed_input_busts_cache() -> None:
    client = _CountingClient(_DEVSECOPS_CLEAN_RESPONSE)
    agent = DevSecOpsReviewAgent(client)

    agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))
    agent.run(DevSecOpsReviewInput(task_description="a different task", artifacts={}))

    assert client.calls == 2


def test_devsecops_redis_unavailable_falls_back_to_memory_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(factory_mod, "_build_redis_client", lambda: None)
    reset_shared_cache_state()
    try:
        namespace = cache_mod.cache_namespace_for(DevSecOpsReviewAgent.CACHE_NAMESPACE)
        assert isinstance(get_shared_cache(namespace), MemoryBackend)

        client = _CountingClient(_DEVSECOPS_CLEAN_RESPONSE)
        agent = DevSecOpsReviewAgent(client)
        input_data = DevSecOpsReviewInput(task_description="test", artifacts={})

        first = agent.run(input_data)
        second = agent.run(input_data)

        assert client.calls == 1
        assert first.model_dump() == second.model_dump()
    finally:
        reset_shared_cache_state()


def test_devsecops_cache_backend_error_falls_open_to_correct_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(devsecops_agent_mod, "get_shared_cache", lambda namespace: _RaisingCache())

    client = _CountingClient(_DEVSECOPS_CLEAN_RESPONSE)
    agent = DevSecOpsReviewAgent(client)
    result = agent.run(DevSecOpsReviewInput(task_description="test", artifacts={}))

    assert result.approved is True
    assert client.calls == 1


def test_devsecops_fallback_result_is_never_cached() -> None:
    """Two schema-invalid replies in a row exhaust ``run_single_shot_review``'s
    corrective retry and raise, landing in the ``except`` fallback branch --
    that fallback must never be written to the cache."""
    client = _ScriptedClient(
        [
            {"findings": []},  # missing required "summary" -- schema-invalid
            {"findings": []},  # retry also schema-invalid -- exhausts correction
        ]
    )
    agent = DevSecOpsReviewAgent(client)
    input_data = DevSecOpsReviewInput(task_description="test", artifacts={})

    result = agent.run(input_data)
    assert result.approved is False

    key = cache_mod.build_review_cache_key(input_data, model_fingerprint(agent._model))
    cache = get_shared_cache(cache_mod.cache_namespace_for(DevSecOpsReviewAgent.CACHE_NAMESPACE))
    assert cache.get(key) is None


def test_devsecops_cache_disabled_via_env_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVOPS_DEVSECOPS_CACHE_SIZE", "0")
    client = _CountingClient(_DEVSECOPS_CLEAN_RESPONSE)
    agent = DevSecOpsReviewAgent(client)
    input_data = DevSecOpsReviewInput(task_description="test", artifacts={})

    agent.run(input_data)
    agent.run(input_data)

    assert client.calls == 2


# ---------------------------------------------------------------------------
# Lighter hit/miss coverage for the remaining six specialist agents -- each
# exercises the same shared ``review_result_cache`` helper already given full
# fail-open/corrupt-entry/disabled-cache coverage above.
# ---------------------------------------------------------------------------


def test_cicd_identical_input_hits_cache_and_skips_llm_call() -> None:
    response = {
        "artifacts": {".github/workflows/ci.yml": "on: push"},
        "pipeline_job_graph_summary": "build -> test -> deploy",
        "required_gates_present": True,
        "summary": "pipeline created",
    }
    client = _CountingClient(response)
    agent = CICDPipelineAgent(client)
    input_data = CICDPipelineAgentInput(task_spec=_task_spec())

    agent.run(input_data)
    agent.run(input_data)

    assert client.calls == 1


def test_cicd_changed_input_busts_cache() -> None:
    client = _CountingClient({"artifacts": {}, "summary": "pipeline"})
    agent = CICDPipelineAgent(client)

    agent.run(CICDPipelineAgentInput(task_spec=_task_spec()))
    agent.run(CICDPipelineAgentInput(task_spec=_task_spec(title="different")))

    assert client.calls == 2


def test_deployment_strategy_identical_input_hits_cache_and_skips_llm_call() -> None:
    response = {
        "artifacts": {},
        "strategy": "rolling",
        "rollback_plan": ["rb"],
        "health_checks": [],
        "rollout_timeout_minutes": 15,
        "alerting_configured": True,
        "summary": "strategy set",
    }
    client = _CountingClient(response)
    agent = DeploymentStrategyAgent(client)
    input_data = DeploymentStrategyAgentInput(task_spec=_task_spec())

    agent.run(input_data)
    agent.run(input_data)

    assert client.calls == 1


def test_deployment_strategy_changed_input_busts_cache() -> None:
    client = _CountingClient({"artifacts": {}, "summary": "strategy"})
    agent = DeploymentStrategyAgent(client)

    agent.run(DeploymentStrategyAgentInput(task_spec=_task_spec()))
    agent.run(DeploymentStrategyAgentInput(task_spec=_task_spec(title="different")))

    assert client.calls == 2


def test_task_clarifier_identical_input_hits_cache_and_skips_llm_call() -> None:
    response = {
        "approved_for_execution": True,
        "checklist": [],
        "gaps": [],
        "clarification_requests": [],
    }
    client = _CountingClient(response)
    agent = DevOpsTaskClarifierAgent(client)
    input_data = DevOpsTaskClarifierInput(task_spec=_task_spec())

    agent.run(input_data)
    agent.run(input_data)

    assert client.calls == 1


def test_task_clarifier_changed_input_busts_cache() -> None:
    client = _CountingClient({"approved_for_execution": True})
    agent = DevOpsTaskClarifierAgent(client)

    agent.run(DevOpsTaskClarifierInput(task_spec=_task_spec()))
    agent.run(DevOpsTaskClarifierInput(task_spec=_task_spec(title="different")))

    assert client.calls == 2


def test_task_clarifier_gap_short_circuit_never_calls_llm_or_cache() -> None:
    """A deterministic gap early-return must not consult the cache at all --
    only the LLM-backed tail past the gaps check is cached."""
    client = _CountingClient({"approved_for_execution": True})
    agent = DevOpsTaskClarifierAgent(client)
    input_data = DevOpsTaskClarifierInput(task_spec=_task_spec(acceptance_criteria=[]))

    out = agent.run(input_data)

    assert out.approved_for_execution is False
    assert client.calls == 0


def test_infra_debug_identical_input_hits_cache_and_skips_llm_call() -> None:
    response = {
        "errors": [{"error_type": "syntax", "tool": "terraform", "error_message": "bad hcl"}],
        "summary": "one syntax error",
        "fixable": True,
    }
    client = _CountingClient(response)
    agent = InfraDebugAgent(client)
    input_data = IaCDebugInput(execution_output="boom", tool_name="terraform", command="plan")

    agent.run(input_data)
    agent.run(input_data)

    assert client.calls == 1


def test_infra_debug_changed_input_busts_cache() -> None:
    client = _CountingClient({"errors": [], "summary": "", "fixable": False})
    agent = InfraDebugAgent(client)

    agent.run(IaCDebugInput(execution_output="boom", tool_name="terraform", command="plan"))
    agent.run(
        IaCDebugInput(execution_output="different boom", tool_name="terraform", command="plan")
    )

    assert client.calls == 2


def test_infra_patch_identical_input_hits_cache_and_skips_llm_call() -> None:
    response = {
        "patched_artifacts": {"main.tf": "resource {}"},
        "summary": "patched",
        "edits_applied": 1,
    }
    client = _CountingClient(response)
    agent = InfraPatchAgent(client)
    input_data = IaCPatchInput(
        debug_output=IaCDebugOutput(errors=[], summary="", fixable=True),
        original_artifacts={"main.tf": "broken"},
    )

    agent.run(input_data)
    agent.run(input_data)

    assert client.calls == 1


def test_infra_patch_not_fixable_short_circuit_never_calls_llm_or_cache() -> None:
    """The ``not fixable`` early return must not consult the cache at all."""
    client = _CountingClient({"patched_artifacts": {}, "summary": "", "edits_applied": 0})
    agent = InfraPatchAgent(client)
    input_data = IaCPatchInput(
        debug_output=IaCDebugOutput(errors=[], summary="", fixable=False),
        original_artifacts={"main.tf": "broken"},
    )

    out = agent.run(input_data)

    assert out.summary == "Errors are not fixable via code changes"
    assert client.calls == 0


def test_doc_runbook_identical_input_hits_cache_and_skips_llm_call() -> None:
    client = _CountingClient({"files": {"RUNBOOK.md": "steps"}, "summary": "runbook created"})
    agent = DocumentationRunbookAgent(client)
    input_data = DocumentationRunbookInput(task_id="DO-1", task_title="Ship it")

    agent.run(input_data)
    agent.run(input_data)

    assert client.calls == 1


def test_doc_runbook_changed_input_busts_cache() -> None:
    client = _CountingClient({"files": {}, "summary": "runbook"})
    agent = DocumentationRunbookAgent(client)

    agent.run(DocumentationRunbookInput(task_id="DO-1", task_title="Ship it"))
    agent.run(DocumentationRunbookInput(task_id="DO-2", task_title="Ship it"))

    assert client.calls == 2
