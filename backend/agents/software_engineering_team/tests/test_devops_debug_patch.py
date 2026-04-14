"""Tests for DevOps infra debug and patch agents (Phase 5)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from devops_team.infra_debug_agent import (
    IaCDebugInput,
    IaCDebugOutput,
    IaCExecutionError,
    InfraDebugAgent,
)
from devops_team.infra_patch_agent import IaCPatchInput, InfraPatchAgent

from llm_service.clients.dummy import DummyLLMClient


class _StubClient(DummyLLMClient):
    """Returns a canned response for every ``complete_json``.

    Routes transparently through the Strands adapter path
    (``chat_json_round`` → ``StructuredOutputTool`` detection → the
    ``complete_json`` override below)."""

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__()
        self._response = response

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        return self._response


class _ScriptedClient(DummyLLMClient):
    """Returns a different canned response on each ``complete_json`` call."""

    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._idx = 0

    def complete_json(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: Optional[str] = None,
        tools: Optional[list] = None,
        think: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return self._responses[-1] if self._responses else {}


# ---------------------------------------------------------------------------
# Debug Agent tests
# ---------------------------------------------------------------------------


class TestInfraDebugAgent:
    def test_classifies_syntax_error(self) -> None:
        client = _StubClient(
            {
                "errors": [
                    {
                        "error_type": "syntax",
                        "tool": "terraform",
                        "file_path": "main.tf",
                        "line_number": 10,
                        "error_message": "Missing closing brace",
                    }
                ],
                "summary": "Syntax error in main.tf",
                "fixable": True,
            }
        )
        agent = InfraDebugAgent(llm_client=client)
        result = agent.run(
            IaCDebugInput(
                execution_output="Error: Missing closing brace at main.tf:10",
                tool_name="terraform",
                command="validate",
                artifacts={"main.tf": "resource {\n"},
            )
        )
        assert len(result.errors) == 1
        assert result.errors[0].error_type == "syntax"
        assert result.fixable

    def test_classifies_unknown_error(self) -> None:
        client = _StubClient(
            {
                "errors": [{"error_type": "unknown", "error_message": "Unexpected"}],
                "summary": "Unknown error",
                "fixable": False,
            }
        )
        agent = InfraDebugAgent(llm_client=client)
        result = agent.run(
            IaCDebugInput(
                execution_output="Something went wrong",
                tool_name="terraform",
                command="plan",
                artifacts={},
            )
        )
        assert result.errors[0].error_type == "unknown"
        assert not result.fixable

    def test_sets_fixable_true_for_all_syntax_validation(self) -> None:
        client = _StubClient(
            {
                "errors": [
                    {"error_type": "syntax", "error_message": "bad syntax"},
                    {"error_type": "validation", "error_message": "bad value"},
                ],
                "summary": "Two fixable errors",
            }
        )
        agent = InfraDebugAgent(llm_client=client)
        result = agent.run(
            IaCDebugInput(
                execution_output="errors",
                tool_name="cdk",
                command="synth",
                artifacts={},
            )
        )
        assert result.fixable

    def test_sets_fixable_false_when_runtime_present(self) -> None:
        client = _StubClient(
            {
                "errors": [
                    {"error_type": "syntax", "error_message": "bad syntax"},
                    {"error_type": "runtime", "error_message": "timeout"},
                ],
                "summary": "Mixed errors",
                "fixable": False,
            }
        )
        agent = InfraDebugAgent(llm_client=client)
        result = agent.run(
            IaCDebugInput(
                execution_output="errors",
                tool_name="terraform",
                command="apply",
                artifacts={},
            )
        )
        assert not result.fixable


# ---------------------------------------------------------------------------
# Patch Agent tests
# ---------------------------------------------------------------------------


class TestInfraPatchAgent:
    def test_produces_patched_artifacts(self) -> None:
        client = _StubClient(
            {
                "patched_artifacts": {
                    "main.tf": 'resource "aws_s3_bucket" "b" {\n  bucket = "my-bucket"\n}\n',
                },
                "summary": "Fixed missing brace",
                "edits_applied": 1,
            }
        )
        debug_out = IaCDebugOutput(
            errors=[IaCExecutionError(error_type="syntax", error_message="Missing brace")],
            summary="Syntax error",
            fixable=True,
        )
        agent = InfraPatchAgent(llm_client=client)
        result = agent.run(
            IaCPatchInput(
                debug_output=debug_out,
                original_artifacts={
                    "main.tf": 'resource "aws_s3_bucket" "b" {\n  bucket = "my-bucket"\n'
                },
            )
        )
        assert "main.tf" in result.patched_artifacts
        assert result.edits_applied == 1

    def test_returns_empty_when_not_fixable(self) -> None:
        """The patch agent short-circuits on ``fixable=False`` and never
        calls the LLM — verified by a trip-wire client that raises if
        ``complete_json`` is invoked."""
        debug_out = IaCDebugOutput(
            errors=[IaCExecutionError(error_type="permissions", error_message="Access denied")],
            summary="Not fixable",
            fixable=False,
        )

        class _TripWire(DummyLLMClient):
            def complete_json(self, *a: Any, **kw: Any) -> Dict[str, Any]:  # type: ignore[override]
                raise AssertionError("LLM must not be called when debug_output.fixable is False")

            def chat_json_round(self, *a: Any, **kw: Any) -> Dict[str, Any]:  # type: ignore[override]
                raise AssertionError("LLM must not be called when debug_output.fixable is False")

        agent = InfraPatchAgent(llm_client=_TripWire())
        result = agent.run(
            IaCPatchInput(
                debug_output=debug_out,
                original_artifacts={"main.tf": "content"},
            )
        )
        assert not result.patched_artifacts


# ---------------------------------------------------------------------------
# Pipeline loop tests
# ---------------------------------------------------------------------------


class TestDevOpsPipelineDebugPatchLoop:
    def test_loop_terminates_after_max_iterations(self) -> None:
        """Execution always fails -> loop must stop at MAX_INFRA_FIX_ITERATIONS."""
        from devops_team.orchestrator import DevOpsTeamLeadAgent

        client = _ScriptedClient(
            [
                # Task clarifier
                {"approved_for_execution": True, "clarification_requests": []},
                # IaC agent
                {"artifacts": {"main.tf": "resource {}"}, "summary": "infra"},
                # CICD
                {"artifacts": {}, "summary": "cicd", "pipeline_yaml": ""},
                # Deployment
                {"artifacts": {}, "summary": "deploy", "strategy": "rolling", "rollback_plan": ""},
                # Debug agent (will be called up to 3 times)
                {
                    "errors": [{"error_type": "syntax", "error_message": "bad"}],
                    "summary": "err",
                    "fixable": True,
                },
                {
                    "patched_artifacts": {"main.tf": "resource { }"},
                    "summary": "fix",
                    "edits_applied": 1,
                },
                {
                    "errors": [{"error_type": "syntax", "error_message": "bad"}],
                    "summary": "err",
                    "fixable": True,
                },
                {
                    "patched_artifacts": {"main.tf": "resource { }"},
                    "summary": "fix",
                    "edits_applied": 1,
                },
                {
                    "errors": [{"error_type": "syntax", "error_message": "bad"}],
                    "summary": "err",
                    "fixable": True,
                },
                {
                    "patched_artifacts": {"main.tf": "resource { }"},
                    "summary": "fix",
                    "edits_applied": 1,
                },
                # DevSecOps review
                {"approved": True, "summary": "ok", "findings": []},
                # Change review
                {"approved": True, "summary": "ok"},
                # Test validation
                {"quality_gates": {}, "summary": "ok"},
                # Doc runbook
                {"files": {}, "summary": "doc ok"},
            ]
        )

        agent = DevOpsTeamLeadAgent(llm_client=client)

        def always_fail_exec(repo_str: str, artifacts: Dict[str, str]) -> List[Dict[str, Any]]:
            return [
                {
                    "tool": "terraform",
                    "command": "validate",
                    "success": False,
                    "checks": {"terraform_validate": "fail"},
                    "findings": ["Error"],
                    "failure_class": "execution",
                }
            ]

        agent._run_execution_tools = always_fail_exec  # type: ignore[assignment]

        from devops_team.models import DevOpsTaskSpec

        spec = DevOpsTaskSpec(
            task_id="t1",
            title="Test",
            goal={"summary": "test"},
            platform_scope={"cloud": "on-premises", "environments": ["dev"]},
            acceptance_criteria=["IaC validates"],
            constraints={"secrets": {"source": "env"}},
        )

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            result = agent._run_pipeline(
                repo_path=Path(td),
                task_spec=spec,
                build_verifier=None,
                write_changes=False,
            )
        # Loop ran and completed (didn't hang)
        assert result is not None

    def test_loop_converges_on_fixable_error(self) -> None:
        """Execution fails once, patch fixes it, second execution succeeds."""
        from devops_team.orchestrator import DevOpsTeamLeadAgent

        client = _ScriptedClient(
            [
                {"approved_for_execution": True, "clarification_requests": []},
                {"artifacts": {"main.tf": "resource {"}, "summary": "infra"},
                {"artifacts": {}, "summary": "cicd", "pipeline_yaml": ""},
                {"artifacts": {}, "summary": "deploy", "strategy": "rolling", "rollback_plan": ""},
                # Debug
                {
                    "errors": [{"error_type": "syntax", "error_message": "missing brace"}],
                    "summary": "err",
                    "fixable": True,
                },
                # Patch
                {
                    "patched_artifacts": {"main.tf": "resource {}"},
                    "summary": "fixed",
                    "edits_applied": 1,
                },
                # DevSecOps review
                {"approved": True, "summary": "ok", "findings": []},
                # Change review
                {"approved": True, "summary": "ok"},
                # Test validation
                {"quality_gates": {}, "summary": "ok"},
                # Doc runbook
                {"files": {}, "summary": "doc ok"},
            ]
        )

        agent = DevOpsTeamLeadAgent(llm_client=client)

        call_count = [0]

        def exec_tools(repo_str: str, artifacts: Dict[str, str]) -> List[Dict[str, Any]]:
            call_count[0] += 1
            if call_count[0] == 1:
                return [
                    {
                        "tool": "terraform",
                        "command": "validate",
                        "success": False,
                        "checks": {"terraform_validate": "fail"},
                        "findings": ["Error: missing brace"],
                        "failure_class": "execution",
                    }
                ]
            return [
                {
                    "tool": "terraform",
                    "command": "validate",
                    "success": True,
                    "checks": {"terraform_validate": "pass"},
                    "findings": [],
                    "failure_class": "",
                }
            ]

        agent._run_execution_tools = exec_tools  # type: ignore[assignment]

        from devops_team.models import DevOpsTaskSpec

        spec = DevOpsTaskSpec(
            task_id="t1",
            title="Test",
            goal={"summary": "test"},
            platform_scope={"cloud": "on-premises", "environments": ["dev"]},
            acceptance_criteria=["IaC validates"],
            constraints={"secrets": {"source": "env"}},
        )

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            result = agent._run_pipeline(
                repo_path=Path(td),
                task_spec=spec,
                build_verifier=None,
                write_changes=False,
            )
        assert result.success
