"""Tests for DevOps infra debug/patch agents and Phase 4.6 debug-patch loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from llm_service.clients.dummy import DummyLLMClient
from software_engineering_team.devops_team.infra_debug_agent import (
    IaCDebugInput,
    IaCDebugOutput,
    IaCExecutionError,
    InfraDebugAgent,
)
from software_engineering_team.devops_team.infra_patch_agent import IaCPatchInput, InfraPatchAgent
from software_engineering_team.devops_team.orchestrator import (
    MAX_INFRA_FIX_ITERATIONS,
    _DebugPatchState,
)


class _StubClient(DummyLLMClient):
    """Returns one canned JSON response for every LLM call.

    Usage::

        client = _StubClient({
            "errors": [...],
            "summary": "...",
            "fixable": True,
        })

    Constraints:
      - Always returns the same ``response`` dict regardless of prompt/kwargs
      - Does not validate temperature, tools, or other call parameters
      - Routes through the Strands adapter path (``chat_json_round`` →
        ``StructuredOutputTool`` detection → ``complete_json``)
    """

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
    """Returns a different canned JSON response for each LLM call (FIFO).

    Usage::

        client = _ScriptedClient([
            {"errors": [...], "summary": "first"},
            {"errors": [], "summary": "second"},
        ])

    Constraints:
      - Responses are consumed in order from the provided list
      - Extra calls after the list is exhausted return the last response
        (or ``{}`` if the list was empty)
      - Does not validate temperature, tools, or other call parameters
    """

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


def _failing_debug_patch_state() -> _DebugPatchState:
    """Build a ``_DebugPatchState`` with one failing terraform validate result."""
    return _DebugPatchState(
        exec_results=[
            {
                "success": False,
                "tool": "terraform",
                "command": "validate",
                "checks": {"terraform_validate": "fail"},
                "findings": ["e"],
            }
        ],
    )


class TestDebugPatchStateMalformedResults:
    """Coverage for defensive handling of malformed ``exec_results`` entries."""

    def test_refresh_aggregates_skips_malformed_entries(self) -> None:
        """Non-dict entries and non-dict/list ``checks``/``findings`` are skipped, not raised."""
        state = _DebugPatchState(
            exec_results=[
                None,
                "bad",
                {"checks": "not-a-dict", "findings": "not-a-list"},
                {"success": False, "checks": {"a": "fail"}, "findings": ["x"]},
            ],
        )
        assert state.exec_gate_map == {"a": "fail"}
        assert state.exec_findings == ["x"]
        # Non-dict entries are excluded from exec_failures entirely (not
        # merely tolerated), since the Phase 4.6 debug-patch loop calls
        # `.get()` on each exec_failures entry without further checks.
        assert state.exec_failures == [
            {"success": False, "checks": {"a": "fail"}, "findings": ["x"]}
        ]

    def test_exec_failures_excludes_non_dict_entries(self) -> None:
        """A non-dict entry (e.g. None) is logged and dropped, not surfaced as a failure."""
        state = _DebugPatchState(exec_results=[None, "bad", {"success": True, "checks": {}}])
        assert state.exec_failures == []


# ---------------------------------------------------------------------------
# Debug Agent tests
# ---------------------------------------------------------------------------


class TestInfraDebugAgent:
    """Tests for InfraDebugAgent error classification and fixable-flag logic.

    Covers:
      - Classifying syntax vs unknown errors from execution output
      - Setting ``fixable=True`` when only syntax/validation issues are present
      - Setting ``fixable=False`` when any non-fixable (runtime) error exists
    """

    def test_classifies_syntax_error(self) -> None:
        """Classifies a terraform syntax failure as ``error_type='syntax'`` and fixable."""
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
        """Classifies a generic failure as ``error_type='unknown'`` and not fixable."""
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
        """Marks results fixable when every error is syntax or validation."""
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
        assert len(result.errors) == 2
        assert result.errors[0].error_type == "syntax"
        assert result.errors[1].error_type == "validation"
        assert result.fixable
        assert result.summary == "Two fixable errors"

    def test_sets_fixable_false_when_runtime_present(self) -> None:
        """Marks results not fixable when any runtime error is mixed in."""
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
    """Tests for InfraPatchAgent artifact generation and short-circuit behavior.

    Covers:
      - Producing patched artifacts when ``fixable=True``
      - Returning empty patches without calling the LLM when ``fixable=False``
    """

    def test_produces_patched_artifacts(self) -> None:
        """Generates patched file contents when debug output is fixable."""
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
        """Short-circuits on ``fixable=False`` and never calls the LLM."""
        debug_out = IaCDebugOutput(
            errors=[IaCExecutionError(error_type="permissions", error_message="Access denied")],
            summary="Not fixable",
            fixable=False,
        )

        class _TripWire(DummyLLMClient):
            """Raises if the patch agent invokes the LLM on a non-fixable debug result."""

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
    """Pipeline-level tests for the Phase 4.6 debug-patch retry loop.

    Covers termination at the iteration bound, soft-abort on unfixable
    debug output, and convergence when a patch resolves the execution failure.
    """

    def test_loop_terminates_after_max_iterations(self) -> None:
        """Always-failing execution runs exactly MAX_INFRA_FIX_ITERATIONS debug attempts.

        Also spot-checks Phase 4.6 status details contain
        ``iteration {i}/{MAX_INFRA_FIX_ITERATIONS}`` for each attempt
        (i from 1 through ``MAX_INFRA_FIX_ITERATIONS``).
        """
        from software_engineering_team.devops_team.orchestrator import (
            MAX_INFRA_FIX_ITERATIONS,
            DevOpsTeamLeadAgent,
        )

        debug_patch_pair = [
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
        ]
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
                # Debug + patch agents (one pair per MAX_INFRA_FIX_ITERATIONS)
                *[
                    response
                    for _ in range(MAX_INFRA_FIX_ITERATIONS)
                    for response in debug_patch_pair
                ],
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

        debug_calls = [0]
        original_debug_run = agent.infra_debug_agent.run

        def counting_debug_run(*args: Any, **kwargs: Any) -> Any:
            debug_calls[0] += 1
            return original_debug_run(*args, **kwargs)

        agent.infra_debug_agent.run = counting_debug_run  # type: ignore[method-assign]

        phase46_details: List[str] = []

        def capture_status(phase: str, detail: str = "", **_kwargs: Any) -> None:
            if phase == "phase4.6" and detail:
                phase46_details.append(detail)

        agent._status_callback = capture_status  # type: ignore[assignment]

        from software_engineering_team.devops_team.models import DevOpsTaskSpec

        spec = DevOpsTaskSpec(
            task_id="t1",
            title="Test",
            goal={"summary": "test"},
            platform_scope={"cloud": "on-premises", "environments": ["dev"]},
            acceptance_criteria=["IaC validates"],
            constraints={"secrets": {"source": "env"}},
        )

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = agent._run_pipeline(
                repo_path=Path(td),
                task_spec=spec,
                build_verifier=None,
                write_changes=False,
            )
        assert result is not None
        assert debug_calls[0] == MAX_INFRA_FIX_ITERATIONS
        assert len(phase46_details) == MAX_INFRA_FIX_ITERATIONS
        for i, detail in enumerate(phase46_details, start=1):
            assert f"iteration {i}/{MAX_INFRA_FIX_ITERATIONS}" in detail

    def test_loop_soft_aborts_when_debug_not_fixable(self) -> None:
        """Unfixable debug result aborts the retry loop after a single attempt."""
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

        client = _ScriptedClient(
            [
                {"approved_for_execution": True, "clarification_requests": []},
                {"artifacts": {"main.tf": "resource {}"}, "summary": "infra"},
                {"artifacts": {}, "summary": "cicd", "pipeline_yaml": ""},
                {"artifacts": {}, "summary": "deploy", "strategy": "rolling", "rollback_plan": ""},
                {
                    "errors": [{"error_type": "permissions", "error_message": "denied"}],
                    "summary": "not fixable",
                    "fixable": False,
                },
                {"approved": True, "summary": "ok", "findings": []},
                {"approved": True, "summary": "ok"},
                {"quality_gates": {}, "summary": "ok"},
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
                    "findings": ["Access denied"],
                    "failure_class": "execution",
                }
            ]

        agent._run_execution_tools = always_fail_exec  # type: ignore[assignment]

        debug_calls = [0]
        original_debug_run = agent.infra_debug_agent.run

        def counting_debug_run(*args: Any, **kwargs: Any) -> Any:
            debug_calls[0] += 1
            return original_debug_run(*args, **kwargs)

        agent.infra_debug_agent.run = counting_debug_run  # type: ignore[method-assign]

        patch_calls = [0]
        original_patch_run = agent.infra_patch_agent.run

        def counting_patch_run(*args: Any, **kwargs: Any) -> Any:
            patch_calls[0] += 1
            return original_patch_run(*args, **kwargs)

        agent.infra_patch_agent.run = counting_patch_run  # type: ignore[method-assign]

        from software_engineering_team.devops_team.models import DevOpsTaskSpec

        spec = DevOpsTaskSpec(
            task_id="t1",
            title="Test",
            goal={"summary": "test"},
            platform_scope={"cloud": "on-premises", "environments": ["dev"]},
            acceptance_criteria=["IaC validates"],
            constraints={"secrets": {"source": "env"}},
        )

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = agent._run_pipeline(
                repo_path=Path(td),
                task_spec=spec,
                build_verifier=None,
                write_changes=False,
            )
        assert result is not None
        assert debug_calls[0] == 1
        assert patch_calls[0] == 0
        # Soft-abort leaves unresolved exec failures in the gate map, but those
        # are not currently folded into quality_gates — so the pipeline may still
        # complete. Termination is verified by the single debug / zero patch counts.

    def test_loop_converges_on_fixable_error(self) -> None:
        """Execution fails once, patch fixes it, second execution succeeds."""
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

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

        debug_calls = [0]
        original_debug_run = agent.infra_debug_agent.run

        def counting_debug_run(*args: Any, **kwargs: Any) -> Any:
            debug_calls[0] += 1
            return original_debug_run(*args, **kwargs)

        agent.infra_debug_agent.run = counting_debug_run  # type: ignore[method-assign]

        from software_engineering_team.devops_team.models import DevOpsTaskSpec

        spec = DevOpsTaskSpec(
            task_id="t1",
            title="Test",
            goal={"summary": "test"},
            platform_scope={"cloud": "on-premises", "environments": ["dev"]},
            acceptance_criteria=["IaC validates"],
            constraints={"secrets": {"source": "env"}},
        )

        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = agent._run_pipeline(
                repo_path=Path(td),
                task_spec=spec,
                build_verifier=None,
                write_changes=False,
            )
        assert result.success
        assert debug_calls[0] >= 1
        assert call_count[0] >= 2


# ---------------------------------------------------------------------------
# _debug_patch_once unit tests
# ---------------------------------------------------------------------------


class TestDebugPatchOnce:
    """Unit coverage for DevOpsTeamLeadAgent._debug_patch_once soft-abort and success paths."""

    def test_returns_none_when_debug_not_fixable(self) -> None:
        """Soft-aborts (returns None) when ``IaCDebugOutput.fixable`` is False."""
        from software_engineering_team.devops_team.infra_debug_agent import IaCDebugOutput
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                return IaCDebugOutput(errors=[], summary="nope", fixable=False)

        class _TripWirePatch:
            def run(self, *_a, **_k):
                raise AssertionError("patch agent must not run when debug is not fixable")

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        lead.infra_patch_agent = _TripWirePatch()  # type: ignore[assignment]
        out = lead._debug_patch_once(
            0,
            state=_failing_debug_patch_state(),
            aggregated_artifacts={"main.tf": "x"},
            repo_path=Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
        )
        assert out is None

    def test_returns_none_when_debug_agent_raises(self) -> None:
        """Soft-aborts when the debug agent raises unexpectedly."""
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                raise RuntimeError("debug boom")

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        out = lead._debug_patch_once(
            0,
            state=_failing_debug_patch_state(),
            aggregated_artifacts={"main.tf": "x"},
            repo_path=Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
        )
        assert out is None

    def test_returns_none_when_patch_agent_raises(self) -> None:
        """Soft-aborts when the patch agent raises unexpectedly."""
        from software_engineering_team.devops_team.infra_debug_agent import IaCDebugOutput
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                return IaCDebugOutput(errors=[], summary="fixable", fixable=True)

        class _Patch:
            def run(self, *_a, **_k):
                raise RuntimeError("patch boom")

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        lead.infra_patch_agent = _Patch()  # type: ignore[assignment]
        out = lead._debug_patch_once(
            0,
            state=_failing_debug_patch_state(),
            aggregated_artifacts={"main.tf": "x"},
            repo_path=Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
        )
        assert out is None

    def test_returns_none_when_patch_artifacts_empty(self) -> None:
        """Soft-aborts when the patch agent returns no patched artifacts."""
        from software_engineering_team.devops_team.infra_debug_agent import IaCDebugOutput
        from software_engineering_team.devops_team.infra_patch_agent import IaCPatchOutput
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                return IaCDebugOutput(errors=[], summary="fixable", fixable=True)

        class _Patch:
            def run(self, *_a, **_k):
                return IaCPatchOutput(patched_artifacts={}, summary="no edits", edits_applied=0)

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        lead.infra_patch_agent = _Patch()  # type: ignore[assignment]
        out = lead._debug_patch_once(
            0,
            state=_failing_debug_patch_state(),
            aggregated_artifacts={"main.tf": "x"},
            repo_path=Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
        )
        assert out is None

    def test_continues_reexec_when_patch_write_fails(self, monkeypatch) -> None:
        """Write failure logs a warning but still re-execs and returns state."""
        from software_engineering_team.devops_team import orchestrator as orch_mod
        from software_engineering_team.devops_team.infra_debug_agent import IaCDebugOutput
        from software_engineering_team.devops_team.infra_patch_agent import IaCPatchOutput
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                return IaCDebugOutput(errors=[], summary="fixable", fixable=True)

        class _Patch:
            def run(self, *_a, **_k):
                return IaCPatchOutput(
                    patched_artifacts={"main.tf": "fixed"},
                    summary="patched",
                    edits_applied=1,
                )

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        lead.infra_patch_agent = _Patch()  # type: ignore[assignment]
        monkeypatch.setattr(
            orch_mod,
            "write_agent_output",
            lambda **_kwargs: (False, "disk full"),
        )
        # Mutable single-element list so the nested stub can update call count.
        execution_tools_call_count = [0]

        def _reexec(_repo: str, _arts: Dict[str, str]) -> List[Dict[str, Any]]:
            execution_tools_call_count[0] += 1
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

        lead._run_execution_tools = _reexec  # type: ignore[assignment]
        artifacts = {"main.tf": "broken"}
        out = lead._debug_patch_once(
            0,
            state=_failing_debug_patch_state(),
            aggregated_artifacts=artifacts,
            repo_path=Path("."),
            repo_str=".",
            write_changes=True,
            subdir="",
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
        )
        assert out is not None
        assert execution_tools_call_count[0] == 1
        assert artifacts["main.tf"] == "fixed"
        assert out.exec_failures == []
        assert out.exec_gate_map.get("terraform_validate") == "pass"

    def test_returns_state_unchanged_when_exec_failures_empty(self) -> None:
        """No-op when there are no exec failures; agents are not invoked."""
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _TripWireDebug:
            def run(self, *_a, **_k):
                raise AssertionError("debug agent must not run when exec_failures is empty")

        class _TripWirePatch:
            def run(self, *_a, **_k):
                raise AssertionError("patch agent must not run when exec_failures is empty")

        lead.infra_debug_agent = _TripWireDebug()  # type: ignore[assignment]
        lead.infra_patch_agent = _TripWirePatch()  # type: ignore[assignment]
        state = _DebugPatchState(
            exec_results=[
                {
                    "success": True,
                    "tool": "terraform",
                    "command": "validate",
                    "checks": {"terraform_validate": "pass"},
                    "findings": [],
                }
            ],
        )
        out = lead._debug_patch_once(
            0,
            state=state,
            aggregated_artifacts={"main.tf": "x"},
            repo_path=Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
        )
        assert out is state
        assert out.exec_failures == []
        assert out.exec_gate_map.get("terraform_validate") == "pass"

    def test_returns_state_with_cleared_failures_on_success(self) -> None:
        """On a successful patch + re-exec, clears failures and updates aggregates."""
        from software_engineering_team.devops_team.infra_debug_agent import IaCDebugOutput
        from software_engineering_team.devops_team.infra_patch_agent import IaCPatchOutput
        from software_engineering_team.devops_team.orchestrator import DevOpsTeamLeadAgent

        lead = DevOpsTeamLeadAgent(llm_client=DummyLLMClient())

        class _Debug:
            def run(self, *_a, **_k):
                return IaCDebugOutput(errors=[], summary="fixable", fixable=True)

        class _Patch:
            def run(self, *_a, **_k):
                return IaCPatchOutput(
                    patched_artifacts={"main.tf": "fixed"},
                    summary="patched",
                    edits_applied=1,
                )

        lead.infra_debug_agent = _Debug()  # type: ignore[assignment]
        lead.infra_patch_agent = _Patch()  # type: ignore[assignment]
        lead._run_execution_tools = (  # type: ignore[assignment]
            lambda _repo, _arts: [
                {
                    "tool": "terraform",
                    "command": "validate",
                    "success": True,
                    "checks": {"terraform_validate": "pass"},
                    "findings": [],
                    "failure_class": "",
                }
            ]
        )
        artifacts = {"main.tf": "broken"}
        out = lead._debug_patch_once(
            0,
            state=_failing_debug_patch_state(),
            aggregated_artifacts=artifacts,
            repo_path=Path("."),
            repo_str=".",
            write_changes=False,
            subdir="",
            max_iterations=MAX_INFRA_FIX_ITERATIONS,
        )
        assert out is not None
        assert out.exec_failures == []
        assert artifacts["main.tf"] == "fixed"
        assert out.exec_gate_map.get("terraform_validate") == "pass"
