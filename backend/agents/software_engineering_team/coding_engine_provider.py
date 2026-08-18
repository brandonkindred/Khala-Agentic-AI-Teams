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

sys.path invariant
------------------
SE's engines use bare, team-local absolute imports (``from code_review_agent
import ...``, ``from quality_gate_tools import ...``) that resolve only when the
SE team directory is on ``sys.path``. SE's own FastAPI app guarantees that via
``software_engineering_team.api._paths``, but this provider is imported by
out-of-package composition roots — the standalone coding-team service and the
coding_team Temporal worker — that never import ``software_engineering_team.api``.
Without the bootstrap below, the first deferred engine call (e.g. ``run_code_review``)
raises ``ModuleNotFoundError: No module named 'code_review_agent'``, which the
quality-gate tools swallow into a failed review — stalling the coding pipeline.
So we restore SE's path invariant here, at the SE→coding_team bridge, once at
import time. The insert is idempotent and triggers no engine import itself.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# The two-insert idiom is inlined (rather than delegating to
# ``shared.app.paths.bootstrap_syspath``) because importing ``shared.app`` runs its
# package ``__init__``, which pulls in FastAPI — too heavy for a provider whose
# contract is to be cheap and import-safe. ``__file__`` lives in the SE team root.
_TEAM_DIR = Path(__file__).resolve().parent
for _p in (_TEAM_DIR / "architect_agents", _TEAM_DIR):
    if _p.exists():
        _s = str(_p)
        if _s not in sys.path:
            sys.path.insert(0, _s)


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
        pre_numbered: bool = False,
        task_description: str,
        task_requirements: str,
        language: str,
        progress_callback: Any,
        files: Any = None,
        existing_codebase: Any = None,
        repo_reader: Any = None,
        replaced_content: Optional[Dict[str, str]] = None,
        job_id: str = "",
    ) -> Any:
        """Run the PR code-review agent over a pull request's changes.

        Preconditions:
            - ``files`` is a non-empty ``{path: content}`` mapping. ``pre_numbered``
              describes whether its content already carries ``N| `` line-number
              prefixes (diff-hunk submissions) or is whole-file content.
            - ``repo_reader`` is None or a duck-typed ``RepoReader`` (``list_files``
              /``read_file``) giving the false-positive verifier read access to
              existing repository files outside the diff.
            - ``replaced_content``, when not None, is a ``{path: pre-change body}``
              mapping forwarded to ``CodeReviewInput.replaced_content`` unchanged;
              the caller derives it (e.g. from diff removed-hunk sides) and this
              method does not validate its shape beyond passing it through.
            - ``job_id``, when non-blank, is the caller's persisted review job id
              (e.g. a ``code_review_runs`` row) — forwarded so the reviewer's LLM
              calls can record into that job's durable transcript. ``""`` (the
              default) means no caller-tracked job; transcript recording is then
              a no-op.

        Postconditions: returns the reviewer's output (carries an ``issues`` list).
        """
        from software_engineering_team.code_review_agent import CodeReviewAgent
        from software_engineering_team.code_review_agent.models import build_code_review_input

        review_input = build_code_review_input(
            files=files,
            pre_numbered=pre_numbered,
            task_description=task_description,
            task_requirements=task_requirements,
            language=language,
            existing_codebase=existing_codebase,
            replaced_content=replaced_content,
            job_id=job_id,
        )
        run_kwargs: dict = {"progress_callback": progress_callback}
        # Forward the reader only when present: passing ``repo_reader=None`` is a
        # no-op for the real agent, and omitting it keeps duck-typed reviewer
        # stubs (which may not declare the kwarg) working.
        #
        # The PR reader is a live ``GitHubRepoReader`` — it cannot be rebuilt from a
        # serializable field (its auth token is a per-request secret), so it would
        # be dropped on the agent's default Temporal path. Force the in-process
        # coordinator whenever a reader is supplied so the reader is actually used
        # for false-positive verification; the only cost is forfeiting Temporal
        # durability for that one review. Reader-less reviews keep the default
        # (Temporal-durable) dispatch.
        force_in_process = False
        if repo_reader is not None:
            run_kwargs["repo_reader"] = repo_reader
            force_in_process = True
        return CodeReviewAgent(force_in_process=force_in_process).run(review_input, **run_kwargs)

    def classify_issue_scope(
        self,
        findings: Sequence[Any],
        changed_context: Optional[Dict[str, str]],
        task_description: str,
    ) -> List[Any]:
        """Classify each finding in/out-of-scope, delegating to ``scope_classifier``.

        Preconditions: ``findings`` is a sequence of ``CodeReviewIssue``-like
            objects. ``changed_context`` is ``None``/empty or a non-empty
            ``{path: content}`` mapping of current file content;
            ``CodeReviewInput`` requires non-empty ``files``, so an empty
            mapping is treated the same as ``None`` (no grounding context,
            matching ``api.pr_review._tag_review_issues_for_scope``'s existing
            ``if scope_files: input_data = CodeReviewInput(...)`` pattern).

        Postconditions: returns ``scope_classifier.classify_scope(findings,
            ...)`` unchanged — a list positionally aligned 1:1 with
            ``findings``. Resolves the ``code_review_verify`` client itself so
            callers need no SE or ``llm_service`` imports; a client-resolution
            failure degrades to ``llm=None`` (all findings verdict to
            "unknown"), preserving ``classify_scope``'s never-raises guarantee
            at this boundary too.
        """
        from llm_service import get_client
        from software_engineering_team.code_review_agent.models import CodeReviewInput
        from software_engineering_team.code_review_agent.scope_classifier import classify_scope

        input_data = None
        if changed_context:
            input_data = CodeReviewInput(
                files=dict(changed_context), task_description=task_description
            )

        try:
            llm = get_client("code_review_verify")
        except Exception:  # noqa: BLE001 — never raise; classify_scope treats llm=None as UNKNOWN
            llm = None

        return classify_scope(findings, llm=llm, input_data=input_data)
