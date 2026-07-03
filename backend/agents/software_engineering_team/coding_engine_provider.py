"""SE-backed ``CodeEngineProvider``: supplies coding_team with SE's engines.

This lives in ``software_engineering_team`` (not ``coding_team``) so that
coding_team stays free of SE imports. The software-engineering team injects an
instance when it drives ``run_coding_team_orchestrator``, and the standalone
coding-team service installs one via ``set_engine_provider`` at startup.

It structurally satisfies ``coding_team.engine_provider.CodeEngineProvider`` (a
Protocol) without importing it — SE depends on coding_team only through the
already-acyclic ``SE → coding_team`` direction, and this class needs no base.

Every engine import is deferred to call time (they pull in strands / the v2 team
stacks); constructing the provider itself is cheap and import-safe.
"""

from __future__ import annotations

from typing import Any


class SECodeEngineProvider:
    """Concrete engine provider backed by the software-engineering team's engines."""

    def build_implementation_team_lead(self, team_kind: str, llm: Any) -> Any:
        """Construct the frontend/backend code-v2 team lead for ``team_kind``.

        Preconditions: ``team_kind`` in ``{"frontend", "backend"}``.
        Postconditions: returns a code-v2 team-lead instance built from ``llm``.
        """
        if team_kind == "frontend":
            from software_engineering_team.frontend_code_v2_team import FrontendCodeV2TeamLead

            return FrontendCodeV2TeamLead(llm)
        from software_engineering_team.backend_code_v2_team import BackendCodeV2TeamLead

        return BackendCodeV2TeamLead(llm)

    def run_build_verification(self, repo_path: Any, agent_type: str, task_id: str) -> Any:
        from software_engineering_team.quality_gate_tools import run_build_verification

        return run_build_verification(repo_path, agent_type, task_id)

    def run_linting(self, repo_path: Any, task_id: str, *, llm_getter: Any) -> Any:
        from software_engineering_team.quality_gate_tools import run_linting

        return run_linting(repo_path, task_id, llm_getter=llm_getter)

    def run_code_review(self, **kwargs: Any) -> Any:
        from software_engineering_team.quality_gate_tools import run_code_review

        return run_code_review(**kwargs)

    def run_pr_code_review(
        self,
        *,
        code: str,
        pre_numbered: bool,
        task_description: str,
        task_requirements: str,
        language: str,
        progress_callback: Any,
    ) -> Any:
        """Run the PR code-review agent over a pull request's diff.

        Postconditions: returns the reviewer's output (carries an ``issues`` list).
        """
        from software_engineering_team.code_review_agent import CodeReviewAgent, CodeReviewInput

        review_input = CodeReviewInput(
            code=code,
            pre_numbered=pre_numbered,
            task_description=task_description,
            task_requirements=task_requirements,
            language=language,
        )
        return CodeReviewAgent().run(review_input, progress_callback=progress_callback)
