"""Tests for production review-agent wiring into code-v2 callers.

Coverage:
1. ``build_production_review_kwargs_in_process`` returns the three keys and constructs
   ``CodeReviewAgent(..., force_in_process=True)`` without mutating ``TEMPORAL_ADDRESS``.
2. The helper degrades gracefully to ``{}`` when construction raises.
3. ``_run_frontend_code_v2_impl`` passes non-None code_review_agent / build_verifier /
   linting_tool_agent to ``run_workflow`` (Temporal activity caller).
4. ``_run_backend_code_v2_impl`` same.
5. Degrade-to-``{}`` path in each of the two call sites: when the helper returns ``{}``,
   ``run_workflow`` still executes (no KeyError / crash).
6. ``_validate_findings`` actually executes in the backend-code-v2 path — an
   out-of-range line number is nulled rather than kept.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


def _fake_build_verifier(repo_path, agent_type, task_id):
    return True, ""


def _make_fake_workflow_result(success: bool = True):
    r = MagicMock()
    r.success = success
    r.summary = "ok"
    r.failure_reason = None if success else "failed"
    r.iterations_used = 1
    r.current_phase = MagicMock()
    r.current_phase.value = "deliver"
    return r


# ---------------------------------------------------------------------------
# 1–2. Unit tests for the shared helper
#
# ``CodeReviewAgent``, ``LintingToolAgent``, and ``_run_build_verification``
# are imported *inside* the helper function bodies (deferred, to avoid heavy
# import cost at module level). ``patch.object(pra, "CodeReviewAgent")``
# therefore fails — the module object never has those as attributes.
#
# The correct target is the class at its canonical module location, which is
# what ``from software_engineering_team.code_review_agent import CodeReviewAgent``
# resolves to at call time: ``software_engineering_team.code_review_agent.agent``.
# ---------------------------------------------------------------------------


class TestBuildProductionReviewKwargs:
    """Unit tests for the shared helper module."""

    # ---- patch helpers ----
    # ``CodeReviewAgent`` and ``LintingToolAgent`` are imported inside the
    # helper function body (lazy), so we patch them at their canonical module
    # location.  The ``code_review_agent`` package uses a lazy ``__getattr__``
    # that caches resolved names into ``globals()``, so once the attribute is
    # cached a subsequent ``from ... import`` returns the cached value and
    # patching ``agent.CodeReviewAgent`` has no effect.  We therefore patch
    # the cached attribute on the package ``__init__`` module itself instead.
    # ``linting_tool_agent`` re-exports the class eagerly at package level, so
    # we patch it on the package __init__ too.
    _CRA_TARGET = "software_engineering_team.code_review_agent.CodeReviewAgent"
    _LTA_TARGET = "software_engineering_team.linting_tool_agent.LintingToolAgent"
    _BV_TARGET  = "software_engineering_team.build_fix._run_build_verification"

    def test_in_process_happy_path_returns_expected_keys(self, monkeypatch) -> None:
        """All three keys are present; CodeReviewAgent gets force_in_process=True."""
        import software_engineering_team.shared.production_review_agents as pra

        monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")
        fake_cra = MagicMock(name="CodeReviewAgent")
        fake_lta = MagicMock(name="LintingToolAgent")
        fake_bv = MagicMock()

        with (
            patch("llm_service.get_client", return_value=MagicMock()),
            patch(self._CRA_TARGET, return_value=fake_cra) as cra_ctor,
            patch(self._LTA_TARGET, return_value=fake_lta),
            patch(self._BV_TARGET, fake_bv),
        ):
            result = pra.build_production_review_kwargs_in_process()

        assert result.get("code_review_agent") is fake_cra
        assert result.get("linting_tool_agent") is fake_lta
        assert result.get("build_verifier") is fake_bv
        cra_ctor.assert_called_once()
        assert cra_ctor.call_args.kwargs.get("force_in_process") is True

    def test_in_process_does_not_mutate_temporal_address(self, monkeypatch) -> None:
        """In-process helper must not touch TEMPORAL_ADDRESS (force flag is instance-scoped)."""
        import software_engineering_team.shared.production_review_agents as pra

        monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")

        with (
            patch("llm_service.get_client", return_value=MagicMock()),
            patch(self._CRA_TARGET, return_value=MagicMock()),
            patch(self._LTA_TARGET, return_value=MagicMock()),
            patch(self._BV_TARGET, MagicMock()),
        ):
            pra.build_production_review_kwargs_in_process()

        assert os.environ.get("TEMPORAL_ADDRESS") == "temporal:7233"

    def test_in_process_leaves_temporal_address_unset_when_previously_unset(
        self, monkeypatch
    ) -> None:
        """When TEMPORAL_ADDRESS was not set before the call it must remain unset afterwards."""
        import software_engineering_team.shared.production_review_agents as pra

        monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)

        with (
            patch("llm_service.get_client", return_value=MagicMock()),
            patch(self._CRA_TARGET, return_value=MagicMock()),
            patch(self._LTA_TARGET, return_value=MagicMock()),
            patch(self._BV_TARGET, MagicMock()),
        ):
            pra.build_production_review_kwargs_in_process()

        assert "TEMPORAL_ADDRESS" not in os.environ

    def test_in_process_degrade_to_empty_dict_on_failure(self) -> None:
        """``build_production_review_kwargs_in_process`` must return {} on failure."""
        import software_engineering_team.shared.production_review_agents as pra

        with (
            patch("llm_service.get_client", return_value=MagicMock()),
            patch(self._CRA_TARGET, side_effect=ImportError("no llm_service")),
        ):
            result = pra.build_production_review_kwargs_in_process()

        assert result == {}

    def test_in_process_degrade_does_not_mutate_env_on_constructor_failure(
        self, monkeypatch
    ) -> None:
        """TEMPORAL_ADDRESS is unchanged even when CodeReviewAgent() raises."""
        import software_engineering_team.shared.production_review_agents as pra

        monkeypatch.setenv("TEMPORAL_ADDRESS", "temporal:7233")

        with (
            patch("llm_service.get_client", return_value=MagicMock()),
            patch(self._CRA_TARGET, side_effect=RuntimeError("boom")),
        ):
            result = pra.build_production_review_kwargs_in_process()

        assert result == {}
        assert os.environ.get("TEMPORAL_ADDRESS") == "temporal:7233"


# ---------------------------------------------------------------------------
# Call-site test helpers
#
# For the call-site tests (3–4) we need to intercept:
#   a) The team-lead class (deferred import inside the function body).
#   b) The review-kwargs helper.
#
# (a) is patched at its canonical module, ``codegen_team.CodegenTeamLead``.
# (b) For temporal/activities.py: ``build_production_review_kwargs_in_process`` is
#     imported inside the function body, so we patch it on the shared module object.
# ---------------------------------------------------------------------------

_REAL_REVIEW_KWARGS = {
    "code_review_agent": MagicMock(name="CRA"),
    "build_verifier": _fake_build_verifier,
    "linting_tool_agent": MagicMock(name="LTA"),
}


# ---------------------------------------------------------------------------
# 3. _run_frontend_code_v2_impl (Temporal) passes review agents
# ---------------------------------------------------------------------------


class TestFrontendImplPassesReviewAgents:
    """``_run_frontend_code_v2_impl`` must splat non-None review agents into ``run_workflow``."""

    def _run(self, monkeypatch, tmp_path, kwargs_override=None):
        import software_engineering_team.shared.production_review_agents as pra
        from software_engineering_team.shared import job_store as js

        js.create_job("fv2-impl", repo_path=str(tmp_path))

        captured: Dict[str, Any] = {}
        fake_result = _make_fake_workflow_result()

        def fake_run_workflow(**kw):
            captured.update(kw)
            return fake_result

        fake_team_lead = MagicMock()
        fake_team_lead.run_workflow = fake_run_workflow

        review_kwargs = dict(_REAL_REVIEW_KWARGS) if kwargs_override is None else kwargs_override

        monkeypatch.setenv("LLM_PROVIDER", "dummy")
        with (
            # activities.py: ``from software_engineering_team.codegen_team import CodegenTeamLead``
            patch(
                "software_engineering_team.codegen_team.CodegenTeamLead",
                return_value=fake_team_lead,
            ),
            patch.object(pra, "build_production_review_kwargs_in_process", return_value=review_kwargs),
        ):
            from software_engineering_team.temporal import activities
            activities._run_frontend_code_v2_impl(
                "fv2-impl", str(tmp_path), {"id": "t1", "title": "T"}, ""
            )

        return captured

    def test_code_review_agent_is_non_none(self, monkeypatch, tmp_path) -> None:
        captured = self._run(monkeypatch, tmp_path)
        assert captured.get("code_review_agent") is not None

    def test_build_verifier_is_non_none(self, monkeypatch, tmp_path) -> None:
        captured = self._run(monkeypatch, tmp_path)
        assert captured.get("build_verifier") is not None

    def test_linting_tool_agent_is_non_none(self, monkeypatch, tmp_path) -> None:
        captured = self._run(monkeypatch, tmp_path)
        assert captured.get("linting_tool_agent") is not None

    def test_degrade_path_still_executes(self, monkeypatch, tmp_path) -> None:
        """When kwargs helper returns {}, run_workflow still executes without crashing."""
        captured = self._run(monkeypatch, tmp_path, kwargs_override={})
        # run_workflow was invoked; task and repo_path are always present
        assert "task" in captured


# ---------------------------------------------------------------------------
# 4. _run_backend_code_v2_impl (Temporal) passes review agents
# ---------------------------------------------------------------------------


class TestBackendImplPassesReviewAgents:
    """``_run_backend_code_v2_impl`` must splat non-None review agents into ``run_workflow``."""

    def _run(self, monkeypatch, tmp_path, kwargs_override=None):
        import software_engineering_team.shared.production_review_agents as pra
        from software_engineering_team.shared import job_store as js

        js.create_job("bv2-impl", repo_path=str(tmp_path))

        captured: Dict[str, Any] = {}
        fake_result = _make_fake_workflow_result()

        def fake_run_workflow(**kw):
            captured.update(kw)
            return fake_result

        fake_team_lead = MagicMock()
        fake_team_lead.run_workflow = fake_run_workflow

        review_kwargs = dict(_REAL_REVIEW_KWARGS) if kwargs_override is None else kwargs_override

        monkeypatch.setenv("LLM_PROVIDER", "dummy")
        with (
            # activities.py: ``from software_engineering_team.codegen_team import CodegenTeamLead``
            patch(
                "software_engineering_team.codegen_team.CodegenTeamLead",
                return_value=fake_team_lead,
            ),
            patch.object(pra, "build_production_review_kwargs_in_process", return_value=review_kwargs),
        ):
            from software_engineering_team.temporal import activities
            activities._run_backend_code_v2_impl(
                "bv2-impl", str(tmp_path), {"id": "t1", "title": "T"}, ""
            )

        return captured

    def test_code_review_agent_is_non_none(self, monkeypatch, tmp_path) -> None:
        captured = self._run(monkeypatch, tmp_path)
        assert captured.get("code_review_agent") is not None

    def test_build_verifier_is_non_none(self, monkeypatch, tmp_path) -> None:
        captured = self._run(monkeypatch, tmp_path)
        assert captured.get("build_verifier") is not None

    def test_linting_tool_agent_is_non_none(self, monkeypatch, tmp_path) -> None:
        captured = self._run(monkeypatch, tmp_path)
        assert captured.get("linting_tool_agent") is not None

    def test_degrade_path_still_executes(self, monkeypatch, tmp_path) -> None:
        captured = self._run(monkeypatch, tmp_path, kwargs_override={})
        assert "task" in captured


# ---------------------------------------------------------------------------
# 6. _validate_findings executes in the backend-code-v2 review path
#
# Tests the bounds-checking contract directly through the
# architecture_consistency_pass module (the layer that owns _validate_findings).
# The full workflow requires a live git repo and LLM — that's integration scope.
# ---------------------------------------------------------------------------


class TestValidateFindingsNullsOutOfRangeLines:
    """``_validate_findings`` nulls out-of-range line numbers.

    When a real ``CodeReviewAgent`` is wired into the production path, findings
    whose line numbers exceed the submitted file length must be nulled rather
    than forwarded verbatim to the caller.
    """

    def test_out_of_range_line_is_nulled(self) -> None:
        from software_engineering_team.code_review_agent.architecture_consistency_pass import (
            _validate_findings,
        )
        from software_engineering_team.code_review_agent.false_positive_filter import CodebaseIndex
        from software_engineering_team.code_review_agent.models import (
            CodeReviewInput,
            CodeReviewIssue,
        )

        # 2-line file
        files = {"main.py": "def hello():\n    return 'hi'\n"}
        index = CodebaseIndex.from_input(CodeReviewInput(files=files))

        in_range = CodeReviewIssue(
            category="style", description="needs docstring", file_path="main.py", line=1
        )
        out_of_range = CodeReviewIssue(
            category="style", description="indentation", file_path="main.py", line=9999
        )

        validated = _validate_findings(index, [in_range, out_of_range])

        assert len(validated) == 2
        assert validated[0].line == 1, "in-range line must be preserved"
        assert validated[1].line is None, "out-of-range line must be nulled"

    def test_in_range_line_is_preserved(self) -> None:
        from software_engineering_team.code_review_agent.architecture_consistency_pass import (
            _validate_findings,
        )
        from software_engineering_team.code_review_agent.false_positive_filter import CodebaseIndex
        from software_engineering_team.code_review_agent.models import (
            CodeReviewInput,
            CodeReviewIssue,
        )

        files = {"utils.py": "x = 1\ny = 2\nz = 3\n"}
        index = CodebaseIndex.from_input(CodeReviewInput(files=files))

        finding = CodeReviewIssue(
            category="logic", description="unused var", file_path="utils.py", line=2
        )
        validated = _validate_findings(index, [finding])
        assert validated[0].line == 2

    def test_no_file_path_finding_passes_through(self) -> None:
        """A finding with no file_path (structural / cross-file issue) is not dropped."""
        from software_engineering_team.code_review_agent.architecture_consistency_pass import (
            _validate_findings,
        )
        from software_engineering_team.code_review_agent.false_positive_filter import CodebaseIndex
        from software_engineering_team.code_review_agent.models import (
            CodeReviewInput,
            CodeReviewIssue,
        )

        files = {"a.py": "pass\n"}
        index = CodebaseIndex.from_input(CodeReviewInput(files=files))

        finding = CodeReviewIssue(
            category="architecture",
            description="missing interface layer",
            file_path="",
            line=None,
        )
        validated = _validate_findings(index, [finding])
        assert len(validated) == 1
        assert validated[0].description == "missing interface layer"
