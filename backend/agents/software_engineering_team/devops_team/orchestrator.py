"""DevOps team orchestrator (DevOpsTeamLeadAgent)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Optional

from llm_service import LLMClient
from llm_service.clients.dummy import is_dummy_llm_client_wrapped
from shared.git.git_utils import (
    abort_merge,
    checkout_branch,
    commit_working_tree,
    create_feature_branch,
    delete_branch,
    ensure_development_branch,
    get_head_sha,
    merge_branch,
)
from software_engineering_team.shared.deliver_utils import DeliverGitOps
from software_engineering_team.shared.repo_writer import write_agent_output
from software_engineering_team.shared.team_lead_base import BaseTeamLead, TeamLeadSharedState

from . import debug_patch, tool_dispatch
from .change_review_agent import ChangeReviewAgent
from .cicd_pipeline_agent import CICDPipelineAgent
from .debug_patch import (  # noqa: F401 (re-exported for test_devops_debug_patch.py)
    MAX_INFRA_FIX_ITERATIONS,
    _DebugPatchState,
)
from .deployment_strategy_agent import DeploymentStrategyAgent
from .devsecops_review_agent import DevSecOpsReviewAgent
from .doc_runbook_agent import DocumentationRunbookAgent
from .iac_agent import InfrastructureAsCodeAgent
from .infra_debug_agent import InfraDebugAgent
from .infra_patch_agent import InfraPatchAgent
from .models import DevOpsCompletionPackage, DevOpsTaskSpec, DevOpsTeamResult, SubtaskContract
from .phases import (
    criterion_traces_from_phase4,  # noqa: F401 (public re-export; test_devops_team.py imports it here)
    run_phase1_intake_clarify,
    run_phase2_design_fanout,
    run_phase4_quality_gate,
    run_phase5_deliver_merge,
)
from .task_clarifier import DevOpsTaskClarifierAgent
from .test_validation_agent import DevOpsTestValidationAgent
from .tool_agents import (
    CDKExecutionToolAgent,
    CICDLintPipelineValidationToolAgent,
    DeploymentDryRunPlanToolAgent,
    DockerComposeExecutionToolAgent,
    HelmExecutionToolAgent,
    IaCValidationToolAgent,
    PolicyAsCodeToolAgent,
    RepoNavigatorToolAgent,
    TerraformExecutionToolAgent,
)

# Static defaults for the legacy DevOpsTaskSpec adapter (_build_legacy_spec).
# Tuples enforce the read-only contract — callers get list(...) copies when needed.
_DEFAULT_LEGACY_CLOUD = "on-premises"
_DEFAULT_LEGACY_APP_REPO = "application"
_DEFAULT_LEGACY_INFRA_REPO = "platform-infra"
_DEFAULT_LEGACY_SECRETS_SOURCE = "managed_secret_store"

_DEFAULT_LEGACY_ACCEPTANCE_CRITERIA = (
    "CI/CD workflow exists and validates",
    "Deployment strategy and rollback documented",
    "Security and policy review executed",
)
_DEFAULT_LEGACY_ROLLBACK_REQUIREMENTS = ("Rollback to previous known good release",)
_DEFAULT_LEGACY_SECURITY_CONSTRAINTS = (
    "No plaintext credentials",
    "Least privilege IAM",
)
_DEFAULT_LEGACY_COMPLIANCE_CONSTRAINTS = ("Audit trail required",)

MAX_LEGACY_TITLE_LENGTH: int = 120

_NEGATION_TOKENS = frozenset({"not", "no", "non"})
# Skippable fillers between a negation and a prod token ("not deploy to production",
# "not in production").
_NEGATION_INTERVENING_TOKENS = frozenset(
    {
        "to",
        "for",
        "into",
        "on",
        "in",
        "at",
        "with",
        "by",
        "from",
        "deploy",
        "deploying",
        "deployment",
        "the",
        "a",
        "an",
    }
)
# After a ``no``/``not`` + prod token, these may indicate a production *control*
# under discussion rather than excluding production as a target.
_PROD_SAFEGUARD_TOKENS = frozenset(
    {
        "approval",
        "approvals",
        "gate",
        "gates",
        "credential",
        "credentials",
        "access",
        "policy",
        "policies",
        "check",
        "checks",
        "control",
        "controls",
        "requirement",
        "requirements",
        "signoff",
        "authorization",
        "authorized",
        "permission",
        "permissions",
    }
)
# Negative prohibition predicates (``production is forbidden``).
_PROD_NEGATIVE_PROHIBITION_TOKENS = frozenset(
    {
        "forbidden",
        "denied",
        "prohibited",
        "excluded",
        "exclude",
    }
)
# Positive permission words — exclusion only when negated
# (``production is not allowed`` vs ``production is allowed``).
_PROD_POSITIVE_PERMISSION_TOKENS = frozenset(
    {
        "allowed",
        "allow",
        "permitted",
        "permit",
    }
)
# Union used by environment-negation safeguard logic.
_PROD_PROHIBITION_TOKENS = _PROD_NEGATIVE_PROHIBITION_TOKENS | _PROD_POSITIVE_PERMISSION_TOKENS
# With a safeguard noun, these mark a missing/absent control → production work.
_PROD_MISSING_CONTROL_TOKENS = frozenset(
    {
        "configured",
        "missing",
        "absent",
        "needed",
        "need",
        "add",
        "adds",
        "require",
        "requires",
        "required",
        "lacking",
        "lack",
    }
)
# ``no production <attribute>`` negates the attribute, not the environment.
_PROD_ATTRIBUTE_TOKENS = frozenset(
    {
        "downtime",
        "interruption",
        "interruptions",
        "outage",
        "outages",
        "latency",
        "degradation",
        "impact",
        "disruption",
        "traffic",  # only when paired with interruption-style attrs nearby; see helper
    }
)
# Word tokens compatible with the former ``\\b(prod|production)\\b`` matcher.
_LEGACY_WORD_TOKEN = re.compile(r"[a-z0-9_]+")
# Split so ``No. Deploy to production`` does not let ``No`` govern the next sentence.
_LEGACY_CLAUSE_SPLIT = re.compile(r"[.!?;]+")
_PROD_CONTEXT_LOOKAHEAD = 5


def _negation_token_before(tokens: List[str], prod_index: int) -> Optional[str]:
    """Return the negation token governing ``tokens[prod_index]``, if any.

    Preconditions:
        - ``tokens`` is a list of lowercase word tokens from a single clause.
        - ``0 <= prod_index < len(tokens)``.
    Postconditions:
        - Returns ``non`` / ``no`` / ``not`` when that token immediately
          precedes the prod token, or when only intervening fillers from
          ``_NEGATION_INTERVENING_TOKENS`` sit between them
          (e.g. ``not deploy to production``, ``not for production``).
        - Returns ``None`` when no such negation governs the prod token.
    """
    assert 0 <= prod_index < len(tokens), "prod_index out of range"
    j = prod_index - 1
    while j >= 0 and tokens[j] in _NEGATION_INTERVENING_TOKENS:
        j -= 1
    if j >= 0 and tokens[j] in _NEGATION_TOKENS:
        return tokens[j]
    return None


def _is_environment_negation(tokens: List[str], prod_index: int) -> bool:
    """Return True when ``tokens[prod_index]`` is an excluded-environment phrase.

    Preconditions:
        - ``tokens`` is a list of lowercase word tokens from a single clause.
        - ``0 <= prod_index < len(tokens)`` and ``tokens[prod_index]`` is
          ``prod`` or ``production``.
    Postconditions:
        - ``non`` governing the prod token always counts as negation.
        - ``no`` / ``not`` governing the prod token (allowing intervening
          fillers) counts as negation unless following context shows:
          a missing production control, a conditional approval gate
          (``until`` / ``unless``), or a negated production *attribute*
          (``downtime``, ``interruption``, …) rather than excluding production.
        - Explicit prohibitions after a safeguard noun
          (``no production access is allowed``) remain negation / staging.
        - Otherwise returns False.
    """
    assert 0 <= prod_index < len(tokens), "prod_index out of range"
    assert tokens[prod_index] in ("prod", "production"), "token must be prod|production"
    neg = _negation_token_before(tokens, prod_index)
    if neg is None:
        return False
    if neg == "non":
        return True
    window = tokens[prod_index + 1 : prod_index + 1 + _PROD_CONTEXT_LOOKAHEAD]
    # Conditional gate: "do not deploy to production until/unless approved"
    # or "… without/pending authorization".
    if any(t in ("until", "unless") for t in window):
        return False
    if any(t in ("without", "pending") for t in window) and any(
        t in _PROD_SAFEGUARD_TOKENS for t in window
    ):
        return False
    # Attribute negation: "no production downtime" / "no production traffic interruption".
    if any(t in _PROD_ATTRIBUTE_TOKENS for t in window) and not any(
        t in _PROD_PROHIBITION_TOKENS for t in window
    ):
        # "no production traffic" alone (exclusion) vs "traffic interruption" (attribute).
        if window and window[0] == "traffic" and not any(
            t in {"interruption", "interruptions", "disruption", "impact"} for t in window[1:]
        ):
            return True
        return False
    has_safeguard = any(t in _PROD_SAFEGUARD_TOKENS for t in window)
    if not has_safeguard:
        return True
    if any(t in _PROD_PROHIBITION_TOKENS for t in window):
        return True
    if any(t in _PROD_MISSING_CONTROL_TOKENS for t in window):
        return False
    # Safeguard noun without prohibition/missing cue: treat as production concern.
    return False


def _is_post_token_exclusion(tokens: List[str], prod_index: int) -> bool:
    """Return True when production is excluded by a following prohibition predicate.

    Preconditions:
        - ``tokens`` is a list of lowercase word tokens from a single clause.
        - ``0 <= prod_index < len(tokens)`` and ``tokens[prod_index]`` is
          ``prod`` or ``production``.
    Postconditions:
        - True for negative prohibitions (``production is prohibited``,
          ``prod is forbidden``).
        - True for positive permission words only when negated
          (``production is not allowed``); ``production is allowed`` is False.
        - False when no such post-token prohibition is present.
    """
    assert 0 <= prod_index < len(tokens), "prod_index out of range"
    assert tokens[prod_index] in ("prod", "production"), "token must be prod|production"
    after = tokens[prod_index + 1 : prod_index + 1 + _PROD_CONTEXT_LOOKAHEAD]
    if not after:
        return False
    if any(t in _PROD_NEGATIVE_PROHIBITION_TOKENS for t in after):
        return True
    if any(t in _PROD_POSITIVE_PERMISSION_TOKENS for t in after):
        first_perm_idx = next(
            i for i, t in enumerate(after) if t in _PROD_POSITIVE_PERMISSION_TOKENS
        )
        # Negation must govern the permission word ("is not allowed"), not
        # appear after it ("is allowed, not required").
        if any(t in _NEGATION_TOKENS for t in after[:first_perm_idx]):
            return True
    return False


def _clause_implies_production(clause: str) -> bool:
    """Return True when a single clause positively implies production.

    Preconditions: ``clause`` is a lowercase str (may be empty).
    Postconditions: True iff a non-excluded ``prod``/``production`` token appears.
    """
    assert isinstance(clause, str), "clause must be a str"
    tokens = _LEGACY_WORD_TOKEN.findall(clause)
    for i, token in enumerate(tokens):
        if token not in ("prod", "production"):
            continue
        if _is_environment_negation(tokens, i):
            continue
        if _is_post_token_exclusion(tokens, i):
            continue
        return True
    return False


def _legacy_environment_from_text(combined_text: str) -> str:
    """Infer ``production`` vs ``staging`` from legacy free text.

    Preconditions:
        - ``combined_text`` is a str (may be empty); caller lowercases input.
    Postconditions:
        - Splits on clause boundaries (``.`` / ``!`` / ``?`` / ``;``) so a
          standalone ``No.`` cannot negate a later ``Deploy to production``.
        - Returns ``\"production\"`` iff any clause positively implies production
          (see :func:`_clause_implies_production` / :func:`_is_environment_negation`).
        - Exclusion phrases include ``non-production``, ``not prod``,
          ``no production traffic``, ``do not deploy to production``,
          ``not for production``, and ``no production access is allowed``.
        - Still production: missing-control wording, conditional
          ``until approved``, and attribute constraints like
          ``no production downtime``.
        - Otherwise returns ``\"staging\"``. Does not treat ``produce`` as prod.
    """
    assert isinstance(combined_text, str), "combined_text must be a str"
    assert combined_text == combined_text.lower(), "combined_text must be lowercase"
    for clause in _LEGACY_CLAUSE_SPLIT.split(combined_text):
        if _clause_implies_production(clause.strip()):
            return "production"
    return "staging"


# Public re-exports for reuse outside this module (see devops_team_worker.py,
# the coding-team handoff adapter) -- the underscore-prefixed originals above
# stay the names used within this module (and by run_workflow/_build_legacy_spec),
# so callers of this module's own API are unaffected. Reusing the same tuples/
# regex/function objects (not copies) means a change here can never silently
# drift out of sync with what a public importer sees.
LEGACY_WORD_TOKEN = _LEGACY_WORD_TOKEN
NEGATION_TOKENS = _NEGATION_TOKENS
legacy_environment_from_text = _legacy_environment_from_text
DEFAULT_LEGACY_CLOUD = _DEFAULT_LEGACY_CLOUD
DEFAULT_LEGACY_APP_REPO = _DEFAULT_LEGACY_APP_REPO
DEFAULT_LEGACY_INFRA_REPO = _DEFAULT_LEGACY_INFRA_REPO
DEFAULT_LEGACY_SECRETS_SOURCE = _DEFAULT_LEGACY_SECRETS_SOURCE
DEFAULT_LEGACY_ACCEPTANCE_CRITERIA = _DEFAULT_LEGACY_ACCEPTANCE_CRITERIA
DEFAULT_LEGACY_ROLLBACK_REQUIREMENTS = _DEFAULT_LEGACY_ROLLBACK_REQUIREMENTS
DEFAULT_LEGACY_SECURITY_CONSTRAINTS = _DEFAULT_LEGACY_SECURITY_CONSTRAINTS
DEFAULT_LEGACY_COMPLIANCE_CONSTRAINTS = _DEFAULT_LEGACY_COMPLIANCE_CONSTRAINTS


# Fillers allowed between a negation and ``approval`` (``no formal approval``).
_APPROVAL_INTERVENING_TOKENS = frozenset(
    {
        "formal",
        "any",
        "prior",
        "explicit",
        "required",
        "gate",
        "gates",
        "the",
        "a",
        "an",
    }
)


def _scope_item_mentions_approval(item: str) -> bool:
    """Return True when ``item`` positively mentions an approval gate.

    Preconditions: ``item`` is a str.
    Postconditions:
        - True when ``approval`` appears as a word and is not governed by a
          preceding ``no`` / ``not`` / ``non`` (allowing intervening fillers
          such as ``formal`` / ``prior``; e.g. ``prod approval``).
        - False for negated forms (``no approval``, ``no formal approval``,
          ``non-approval``) or when the word is absent.
    """
    assert isinstance(item, str), "item must be a str"
    tokens = _LEGACY_WORD_TOKEN.findall(item.lower())
    for i, token in enumerate(tokens):
        if token != "approval":
            continue
        j = i - 1
        while j >= 0 and tokens[j] in _APPROVAL_INTERVENING_TOKENS:
            j -= 1
        if j >= 0 and tokens[j] in _NEGATION_TOKENS:
            continue
        return True
    return False


def _git_ops() -> DeliverGitOps:
    """Bundle this module's git/write callables for the shared deliver helper.

    Postconditions:
        - Returns a ``DeliverGitOps`` containing only the git/write callables
          required by ``deliver_inline_merge`` (``abort_merge``,
          ``checkout_branch``, ``commit_working_tree``, ``create_feature_branch``,
          ``delete_branch``, ``merge_branch``, ``write_agent_output``); each is
          the name bound in this module, so tests can monkeypatch the
          ``devops_team.orchestrator`` boundary (e.g. ``merge_branch``) exactly
          as the v2 teams do. ``ensure_development_branch`` and ``get_head_sha``
          are intentionally excluded — they're used directly elsewhere in the
          pipeline, not by the deliver helper.
    """
    return DeliverGitOps(
        abort_merge=abort_merge,
        checkout_branch=checkout_branch,
        commit_working_tree=commit_working_tree,
        create_feature_branch=create_feature_branch,
        delete_branch=delete_branch,
        merge_branch=merge_branch,
        write_agent_output=write_agent_output,
    )


DEVOPS_REQUIRED_GATE_NAMES = (
    "iac_validate",
    "iac_validate_fmt",
    "policy_checks",
    "pipeline_lint",
    "pipeline_gate_check",
    "deployment_dry_run",
    "security_review",
    "change_review",
)

ENV_POLICY = MappingProxyType(
    {
        "dev": MappingProxyType(
            {
                "auto_deploy_allowed": True,
                "approval_required": False,
                "rollback_test_required": False,
                "policy_strictness": "low",
            }
        ),
        "staging": MappingProxyType(
            {
                "auto_deploy_allowed": True,
                "approval_required": False,
                "rollback_test_required": True,
                "policy_strictness": "medium",
            }
        ),
        "production": MappingProxyType(
            {
                "auto_deploy_allowed": False,
                "approval_required": True,
                "rollback_test_required": True,
                "policy_strictness": "high",
            }
        ),
    }
)

logger = logging.getLogger(__name__)


class DevOpsTeamLeadAgent(BaseTeamLead):
    """Coordinates specialized DevOps agents with hard gates.

    Inherits ``BaseTeamLead`` (and, transitively, ``TeamLeadSharedState``) for
    LLM resolution and the optional per-run status hook (``_report_status`` /
    ``_status_callback``). Pipeline phase status always emits INFO logs via
    :meth:`_log_pipeline_status`; the optional callback is a separate forward
    channel and may be set/cleared per run without losing historical log
    output. DevOps does not use ``BaseTeamLead``'s per-repo briefing cache
    (:meth:`BaseTeamLead._repo_context_cache_for`), so ``__init__`` passes
    empty extension/exclude-dir sets and a zero char budget for that unused
    feature.

    Invariants: ``self.llm`` is the client passed to ``__init__``; specialist
    agents and tools are constructed once; ``_status_callback`` defaults to
    None (mixin default) and is independent of fallback logging.
    """

    # Tool-dispatch logic lives in ``tool_dispatch.py``; aliased here so
    # ``self._run_execution_tools(...)`` keeps its existing bound-method call
    # shape (see devops_team/tool_dispatch.py for the implementation).
    _run_execution_tools = tool_dispatch.run_execution_tools

    # Debug-patch retry logic lives in ``debug_patch.py``; aliased here so
    # ``self._debug_patch_once(...)`` keeps its existing bound-method call
    # shape (see devops_team/debug_patch.py for the implementation).
    _debug_patch_once = debug_patch.debug_patch_once

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize the DevOps team lead and its specialist agents/tools.

        Preconditions:
            - ``llm_client`` is non-None.
        Postconditions:
            - All specialist agents and execution/validation tools are
              constructed and bound on ``self``.
            - ``_status_callback`` remains the mixin default (``None``) until
              a caller assigns it for a run.
        """
        assert llm_client is not None, "llm_client is required"
        BaseTeamLead.__init__(
            self,
            llm_client,
            extensions=frozenset(),
            exclude_dirs=frozenset(),
            max_chars=0,
        )
        assert self._status_callback is None, "_status_callback must start as None"
        self.task_clarifier = DevOpsTaskClarifierAgent(llm_client)
        self.iac_agent = InfrastructureAsCodeAgent(llm_client)
        self.cicd_agent = CICDPipelineAgent(llm_client)
        self.deployment_agent = DeploymentStrategyAgent(llm_client)
        self.devsecops_review_agent = DevSecOpsReviewAgent(llm_client)
        self.test_validation_agent = DevOpsTestValidationAgent(llm_client)
        self.change_review_agent = ChangeReviewAgent(llm_client)
        self.doc_runbook_agent = DocumentationRunbookAgent(llm_client)

        self.repo_navigator_tool = RepoNavigatorToolAgent()
        self.iac_validation_tool = IaCValidationToolAgent()
        self.policy_tool = PolicyAsCodeToolAgent()
        self.cicd_lint_tool = CICDLintPipelineValidationToolAgent()
        self.deploy_dry_run_tool = DeploymentDryRunPlanToolAgent()

        self.terraform_exec_tool = TerraformExecutionToolAgent()
        self.cdk_exec_tool = CDKExecutionToolAgent()
        self.compose_exec_tool = DockerComposeExecutionToolAgent()
        self.helm_exec_tool = HelmExecutionToolAgent()
        self.infra_debug_agent = InfraDebugAgent(llm_client)
        self.infra_patch_agent = InfraPatchAgent(llm_client)

    @staticmethod
    def _log_pipeline_status(
        *,
        phase: str,
        detail: str = "",
        progress: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Emit the historical pipeline status line at INFO.

        Preconditions: ``phase`` is a non-empty str (caller's responsibility;
          :meth:`_report_status` asserts this before delegating; direct callers
          of this staticmethod must satisfy it too — enforced below).
        Postconditions: logs ``detail`` when non-empty, otherwise logs
          ``DevOps team pipeline: {phase}``; ``progress`` and ``extra`` are ignored
          (reserved for external consumers). Never raises when preconditions hold.
        """
        assert isinstance(phase, str) and phase, "phase must be a non-empty str"
        if detail:
            logger.info("%s", detail)
        else:
            logger.info("DevOps team pipeline: %s", phase)

    def _report_status(
        self,
        phase: str,
        detail: str = "",
        progress: Optional[float] = None,
        **extra: Any,
    ) -> None:
        """Log phase status, then forward to the optional ``_status_callback``.

        Fallback INFO logging is independent of ``_status_callback`` so clearing
        the callback after an instrumented run (the shared per-run contract) does
        not silence later pipeline status on a reused lead.

        Preconditions: ``phase`` is a non-empty str.
        Postconditions: emits the historical INFO line via ``_log_pipeline_status``;
          then invokes ``TeamLeadSharedState._report_status`` (no-op when callback
          is None; forwards kwargs when set; errors are logged and swallowed by
          ``TeamLeadSharedState._report_status``). Never raises when
          preconditions hold.
        """
        assert isinstance(phase, str) and phase, "phase must be a non-empty str"
        self._log_pipeline_status(phase=phase, detail=detail, progress=progress, **extra)
        TeamLeadSharedState._report_status(self, phase, detail=detail, progress=progress, **extra)

    @staticmethod
    def _build_legacy_spec(
        *,
        task_id: str,
        task_description: str,
        requirements: str,
        target_repo: Optional[Any] = None,
    ) -> DevOpsTaskSpec:
        """Build a ``DevOpsTaskSpec`` from the legacy free-text workflow args.

        Preconditions:
            - ``task_id`` is a non-empty string.
            - ``task_description`` and ``requirements`` are strings (may be empty).
        Postconditions:
            - Returns a valid ``DevOpsTaskSpec`` using module-level defaults for
              all fields not derivable from the arguments.
            - ``title`` is the stripped ``task_description[:MAX_LEGACY_TITLE_LENGTH]``,
              or the stripped ``task_id[:MAX_LEGACY_TITLE_LENGTH]`` when that
              description slice is empty/whitespace-only.
            - ``environment`` is inferred from the combined text of
              ``task_description`` and ``requirements`` via
              ``_legacy_environment_from_text``; defaults to ``\"staging\"``.
            - Module-level ``_DEFAULT_LEGACY_*`` tuples supply acceptance
              criteria, rollback requirements, security and compliance
              constraints, and secret-source defaults; each call receives a
              fresh ``list(...)`` copy of the mutable fields.
        """
        assert isinstance(task_id, str) and task_id, "task_id must be a non-empty string"
        assert isinstance(task_description, str), "task_description must be a string"
        assert isinstance(requirements, str), "requirements must be a string"
        repo_name = (
            target_repo.value
            if hasattr(target_repo, "value")
            else (str(target_repo) if target_repo else "")
        )
        combined_text = f"{task_description} {requirements}".lower()
        env = _legacy_environment_from_text(combined_text)
        return DevOpsTaskSpec(
            task_id=task_id,
            title=(
                (task_description[:MAX_LEGACY_TITLE_LENGTH]).strip()
                or (task_id[:MAX_LEGACY_TITLE_LENGTH]).strip()
            ),
            platform_scope={"cloud": _DEFAULT_LEGACY_CLOUD, "environments": ["dev", env]},
            repo_context={
                "app_repo": repo_name or _DEFAULT_LEGACY_APP_REPO,
                "infra_repo": _DEFAULT_LEGACY_INFRA_REPO,
                "pipeline_repo": repo_name or _DEFAULT_LEGACY_APP_REPO,
            },
            goal={"summary": task_description},
            scope={"included": [requirements], "excluded": []},
            constraints={"secrets": {"source": _DEFAULT_LEGACY_SECRETS_SOURCE}},
            acceptance_criteria=list(_DEFAULT_LEGACY_ACCEPTANCE_CRITERIA),
            rollback_requirements=list(_DEFAULT_LEGACY_ROLLBACK_REQUIREMENTS),
            security_constraints=list(_DEFAULT_LEGACY_SECURITY_CONSTRAINTS),
            compliance_constraints=list(_DEFAULT_LEGACY_COMPLIANCE_CONSTRAINTS),
            environment=env,
        )

    def run(self, input_data: DevOpsTaskSpec) -> DevOpsCompletionPackage:
        """Execute a contract-first model run without orchestrator artifact writes.

        ``write_changes=False`` skips this team's ``write_agent_output`` / branch
        commits. Phase 4.5 execution tools (e.g. ``terraform init``, ``cdk synth``,
        ``helm lint``, ``docker-compose config``) may still write under the working
        directory as validation side effects.

        Preconditions:
            - ``input_data`` is a non-None ``DevOpsTaskSpec``.
        Postconditions:
            - Returns a non-None ``DevOpsCompletionPackage`` on success.
        Raises:
            ValueError: if the pipeline completes without a completion package.
        """
        assert input_data is not None, "input_data is required"
        result = self._run_pipeline(
            repo_path=Path("."),
            task_spec=input_data,
            build_verifier=None,
            write_changes=False,
        )
        if result.completion_package is None:
            raise ValueError(result.failure_reason or "DevOps team run failed")
        return result.completion_package

    def run_workflow(
        self,
        *,
        repo_path: Path,
        task_description: str,
        requirements: str,
        target_repo: Optional[Any] = None,
        build_verifier: Optional[Any] = None,
        task_id: str = "devops",
        subdir: str = "",
    ) -> DevOpsTeamResult:
        """Legacy adapter: repo/task free-text → ``_build_legacy_spec`` → ``run_task``.

        Preconditions:
            - ``repo_path`` is a path to an existing directory initialised as a git repo.
            - ``task_description`` and ``requirements`` are strings (may be empty).
            - ``task_id`` is a non-empty string when provided; defaults to ``"devops"``.
            - ``build_verifier``, when provided, is callable and returns ``(bool, str)``.
        Postconditions:
            - Returns a ``DevOpsTeamResult`` reflecting the full pipeline outcome.
            - Artifacts are written to the repo on a feature branch and merged into
              ``development`` when the pipeline completes successfully.
        """
        assert isinstance(task_id, str) and task_id, "task_id must be a non-empty string"
        task_spec = DevOpsTeamLeadAgent._build_legacy_spec(
            task_id=task_id,
            task_description=task_description,
            requirements=requirements,
            target_repo=target_repo,
        )
        return self.run_task(
            task_spec,
            repo_path=repo_path,
            build_verifier=build_verifier,
            subdir=subdir,
        )

    def run_task(
        self,
        task_spec: DevOpsTaskSpec,
        *,
        repo_path: Path,
        build_verifier: Optional[Any] = None,
        merge_to_development: bool = True,
        subdir: str = "",
    ) -> DevOpsTeamResult:
        """Structured entry point: run the full pipeline against a real repo.

        The write-capable counterpart to ``run``, and the structured
        counterpart to ``run_workflow``: takes a pre-built ``DevOpsTaskSpec``
        directly instead of free text, and (unlike ``run()``) writes, commits,
        and by default merges the result. ``merge_to_development=False`` commits
        the feature branch and leaves it unmerged for an external Tech Lead
        review — the mode the coding-team swarm uses when dispatching a
        ``target_team="devops"`` task from a per-task git worktree.

        Preconditions:
            - ``task_spec`` is a valid ``DevOpsTaskSpec``.
            - ``repo_path`` is a path to an existing directory initialised as a git repo.
            - ``build_verifier``, when provided, is callable and returns ``(bool, str)``.
        Postconditions:
            - Returns a ``DevOpsTeamResult`` reflecting the full pipeline outcome.
            - Artifacts are written to the repo on a feature branch; the branch is
              merged into ``development`` when ``merge_to_development`` is ``True``
              (the default) and left in place otherwise.
        """
        assert task_spec is not None, "task_spec is required"
        repo_path_obj = Path(repo_path).resolve()
        assert repo_path_obj.is_dir(), f"repo_path must be an existing directory: {repo_path_obj}"
        if build_verifier is not None:
            assert callable(build_verifier), "build_verifier must be callable"
        return self._run_pipeline(
            repo_path=repo_path_obj,
            task_spec=task_spec,
            build_verifier=build_verifier,
            write_changes=True,
            merge_to_development=merge_to_development,
            subdir=subdir,
        )

    @staticmethod
    def _build_subtask_contracts(task_spec: DevOpsTaskSpec) -> List[SubtaskContract]:
        """Create the IaC, CI/CD, and deployment subtask contracts for a run.

        Preconditions:
            - ``task_spec`` is not None.
            - ``task_spec.task_id`` is a non-empty string.
        Postconditions:
            - Returns exactly three ``SubtaskContract`` objects owned by
              ``InfrastructureAsCodeAgent``, ``CICDPipelineAgent``, and
              ``DeploymentStrategyAgent`` respectively.
        """
        assert task_spec is not None, "task_spec is required"
        assert isinstance(task_spec.task_id, str) and task_spec.task_id, (
            "task_spec.task_id must be a non-empty string"
        )
        return [
            SubtaskContract(
                subtask_id=f"{task_spec.task_id}-T1",
                owner="InfrastructureAsCodeAgent",
                objective="Implement IaC changes for task scope",
                inputs=["validated_task_spec", "repo_context"],
                constraints=["no destructive changes without approval", "no secrets in code"],
                expected_artifact=["iac_files"],
                completion_criteria=["IaC validates", "no wildcard IAM"],
            ),
            SubtaskContract(
                subtask_id=f"{task_spec.task_id}-T2",
                owner="CICDPipelineAgent",
                objective="Create CI/CD workflow with gates",
                inputs=["validated_task_spec", "repo_context", "deployment_strategy_spec"],
                constraints=["OIDC preferred", "no prod deploy without approval gate"],
                expected_artifact=["workflow_file", "pipeline_job_graph_summary"],
                completion_criteria=["workflow syntax valid", "required gates present"],
            ),
            SubtaskContract(
                subtask_id=f"{task_spec.task_id}-T3",
                owner="DeploymentStrategyAgent",
                objective="Define rollout and rollback mechanics",
                inputs=["validated_task_spec"],
                constraints=["health checks required", "rollback path defined"],
                expected_artifact=["deploy_manifests", "rollback_plan"],
                completion_criteria=["strategy defined", "rollback steps documented"],
            ),
        ]

    @staticmethod
    def _enforce_env_policy(task_spec: DevOpsTaskSpec) -> Optional[str]:
        """Return a blocking reason if environment policy is violated, else None.

        Preconditions:
            - ``task_spec`` is a fully populated ``DevOpsTaskSpec``.
            - ``task_spec.platform_scope.environments`` is iterable.
            - ``task_spec.scope.included`` is an iterable of strings.

        Postconditions:
            - Returns ``None`` if no configured environment policy is violated.
            - Returns a human-readable blocking reason string if any environment
              requires an approval gate or rollback requirements that are missing.

        Invariants:
            - The method does not mutate ``task_spec``.
        """
        assert task_spec is not None, "task_spec is required"
        assert task_spec.platform_scope is not None, "task_spec.platform_scope must be set"
        assert task_spec.scope is not None, "task_spec.scope must be set"
        assert task_spec.platform_scope.environments is not None, (
            "task_spec.platform_scope.environments must be set"
        )
        assert not isinstance(task_spec.platform_scope.environments, str), (
            "task_spec.platform_scope.environments must be a collection, not a string"
        )
        assert hasattr(task_spec.platform_scope.environments, "__iter__"), (
            "task_spec.platform_scope.environments must be iterable"
        )
        assert all(isinstance(env, str) for env in task_spec.platform_scope.environments), (
            "task_spec.platform_scope.environments must be an iterable of strings"
        )
        assert task_spec.scope.included is not None, "task_spec.scope.included must be set"
        assert not isinstance(task_spec.scope.included, str), (
            "task_spec.scope.included must be an iterable of strings, not a single string"
        )
        assert all(isinstance(item, str) for item in task_spec.scope.included), (
            "task_spec.scope.included must be an iterable of strings"
        )
        for env in task_spec.platform_scope.environments:
            policy = ENV_POLICY.get(env)
            if policy is None:
                continue
            if policy["approval_required"] and not any(
                _scope_item_mentions_approval(item) for item in task_spec.scope.included
            ):
                return (
                    f"Environment '{env}' requires explicit approval gate but none found in scope"
                )
            if policy["rollback_test_required"] and not task_spec.rollback_requirements:
                return f"Environment '{env}' requires rollback requirements but none specified"
        return None

    def _run_pipeline(
        self,
        *,
        repo_path: Path,
        task_spec: DevOpsTaskSpec,
        build_verifier: Optional[Any],
        write_changes: bool,
        merge_to_development: bool = True,
        subdir: str = "",
    ) -> DevOpsTeamResult:
        """Sequence the 5 DevOps phases via the shared gated-phase framework.

        Each phase's logic lives in a standalone function under ``phases/``
        (or ``debug_patch.py`` for Phase 3); this method is a thin adapter
        that builds the small closures below (to thread shared nonlocal state
        between phases) and sequences them with ``self._run_gated_phases``,
        the generic ``BaseTeamLead`` sequencing helper (run in order, stop at
        the first non-``None`` failure). This intentionally does not use
        ``BaseV2DevelopmentAgent._run_development_workflow``: that method is
        a fixed Pre-flight/Planning/microtask-review-gated-Execution/
        Documentation/Deliver state machine built for the code-v2 teams'
        LLM-planning contract, and this team's phases (env-policy gating,
        3-way parallel design fan-out, a debug-patch retry loop, gate-name
        tracking) don't share that shape.

        Preconditions:
            - ``task_spec`` is a valid ``DevOpsTaskSpec`` with a non-empty ``task_id``.
            - ``repo_path`` is a ``Path`` (need not exist when ``write_changes=False``).
        Postconditions:
            - On success: returns a ``DevOpsTeamResult`` with ``success=True`` and
              ``completion_package`` set; ``completion_package`` is never ``None``.
            - On any phase failure: returns a ``DevOpsTeamResult`` with
              ``success=False`` and ``failure_reason`` set.
            - Raises ``RuntimeError`` if Phase 5 returns without assigning the
              completion package (internal contract violation).
            - ``merge_to_development`` only matters when ``write_changes=True``:
              ``True`` (default) merges and deletes the feature branch, matching
              ``run_workflow``'s existing behavior; ``False`` commits the branch
              and leaves it in place for external review (required when running
              from a detached per-task git worktree, where merging back into
              ``development`` is not possible).
        Invariants:
            - ``task_spec`` is not mutated by this method or its phase closures.
        """
        assert isinstance(repo_path, Path), "repo_path must be a pathlib.Path"
        assert isinstance(task_spec, DevOpsTaskSpec), "task_spec must be a DevOpsTaskSpec"
        assert isinstance(task_spec.task_id, str) and task_spec.task_id, (
            "task_spec.task_id must be a non-empty string"
        )
        self._report_status(
            "start",
            detail=f"DevOps team pipeline: starting task {task_spec.task_id}",
        )

        # Phase outputs shared with Phase 4+ (set by the gated phase callables).
        iac_result: Any = None
        cicd_result: Any = None
        deploy_result: Any = None
        aggregated_artifacts: Dict[str, str] = {}
        quality_gates: Dict[str, str] = {}
        acceptance_trace: List[Dict[str, object]] = []
        completion: Any = None  # filled by Phase 5 on success
        # Phase 4.6 debug-patch attempts consumed; 1 = no retry needed.
        infra_fix_iterations = 1

        def _phase1_intake_clarify() -> Optional[DevOpsTeamResult]:
            """Phase 1: environment policy + task clarification gates.

            Preconditions: ``task_spec`` is the pipeline input for this run.
            Postconditions: returns a failed ``DevOpsTeamResult`` on env-policy or
              clarifier rejection; otherwise builds subtask contracts, logs their
              count, and returns ``None`` so later phases run.

            Thin wrapper around the standalone ``run_phase1_intake_clarify``;
            converts its typed result into this pipeline's gate contract.
            """
            result = run_phase1_intake_clarify(
                task_spec=task_spec,
                task_clarifier=self.task_clarifier,
                enforce_env_policy=self._enforce_env_policy,
                build_subtask_contracts=self._build_subtask_contracts,
            )
            if result.blocked_reason:
                return DevOpsTeamResult(success=False, failure_reason=result.blocked_reason)

            logger.info(
                "DevOps team pipeline: %d subtask contracts generated",
                len(result.subtask_contracts),
            )
            return None

        def _phase2_parallel_design() -> Optional[DevOpsTeamResult]:
            """Phase 2: change design / implementation (3-way parallel fan-out).

            Preconditions: Phase 1 returned ``None``.
            Postconditions: sets ``iac_result``, ``cicd_result``, ``deploy_result``,
              and ``aggregated_artifacts`` from the parallel fan-out; always returns
              ``None`` (this phase has no early-exit gate today).
            """
            nonlocal iac_result, cicd_result, deploy_result, aggregated_artifacts
            self._report_status(
                "phase2",
                detail="DevOps team pipeline: phase 2 - change design (parallel)",
            )
            # Enable parallel execution unless the backing LLM client is (or
            # wraps, e.g. a Strands LLMClientModel) a DummyLLMClient — scripted
            # test clients use a shared sequential response list that breaks
            # under concurrent access. Shared with
            # shared.v2_review._review_steps_run_sequentially and
            # code_review_agent.coordinator._tail_passes_run_sequentially.
            use_parallel = not is_dummy_llm_client_wrapped(self.llm)
            phase2 = run_phase2_design_fanout(
                task_spec=task_spec,
                repo_path=repo_path,
                iac_agent=self.iac_agent,
                cicd_agent=self.cicd_agent,
                deployment_agent=self.deployment_agent,
                repo_navigator_tool=self.repo_navigator_tool,
                parallel=use_parallel,
            )
            iac_result = phase2.iac_result
            cicd_result = phase2.cicd_result
            deploy_result = phase2.deploy_result
            aggregated_artifacts = phase2.aggregated_artifacts
            return None

        def _phase3_branch_write() -> Optional[DevOpsTeamResult]:
            """Phase 3: feature branch + artifact write gates.

            Delegates to :func:`debug_patch.run_phase3_branch_write`; passes
            ``ensure_development_branch``/``create_feature_branch`` through
            from this module's globals so existing test monkeypatches on
            those names keep working.
            """
            return debug_patch.run_phase3_branch_write(
                write_changes=write_changes,
                aggregated_artifacts=aggregated_artifacts,
                repo_path=repo_path,
                task_spec=task_spec,
                subdir=subdir,
                ensure_development_branch=ensure_development_branch,
                create_feature_branch=create_feature_branch,
                report_status=self._report_status,
            )

        def _phase4_validation_review() -> Optional[DevOpsTeamResult]:
            """Phase 4: tool validation, reviews, and early-exit gates.

            Preconditions: Phases 1–3 returned ``None`` (``aggregated_artifacts``
              may be empty).
            Postconditions: runs tool validation, execution verification, the
              debug-patch loop, and independent reviews; sets nonlocal
              ``quality_gates``, ``acceptance_trace``, and
              ``infra_fix_iterations`` (Phase 4.6 attempts consumed; stays 1
              when no retry was needed). Returns a failed ``DevOpsTeamResult``
              on quality-gate or build-verifier failure; otherwise returns
              ``None`` so Phase 5 runs.

            Thin wrapper around the standalone ``run_phase4_quality_gate``;
            converts its typed result into this pipeline's gate contract.
            """
            nonlocal quality_gates, acceptance_trace, infra_fix_iterations

            result = run_phase4_quality_gate(
                self,
                task_spec=task_spec,
                repo_path=repo_path,
                aggregated_artifacts=aggregated_artifacts,
                write_changes=write_changes,
                subdir=subdir,
                build_verifier=build_verifier,
            )
            quality_gates = result.quality_gates
            acceptance_trace = result.acceptance_trace
            infra_fix_iterations = result.infra_fix_iterations
            return result.blocked_result

        def _phase5_completion_deliver() -> Optional[DevOpsTeamResult]:
            """Phase 5: completion package assembly + deliver/merge.

            Preconditions: Phases 1–4 returned ``None``; ``quality_gates``,
              ``acceptance_trace``, ``aggregated_artifacts``, and Phase 2 results
              are set (artifacts / trace may be empty).
            Postconditions: on merge failure returns a failed ``DevOpsTeamResult``
              via ``build_team_failure_result`` with the blocked completion
              package; otherwise assigns nonlocal ``completion`` (completed status,
              git ops, handoff, quality gates) and returns ``None`` so the thin
              success envelope after the sequencer runs.

            Thin wrapper around the standalone ``run_phase5_deliver_merge``;
            converts its typed result into this pipeline's gate contract.
            """
            nonlocal completion

            self._report_status(
                "phase5",
                detail="DevOps team pipeline: phase 5 - completion package assembly",
            )
            result = run_phase5_deliver_merge(
                task_spec=task_spec,
                repo_path=repo_path,
                quality_gates=quality_gates,
                acceptance_trace=acceptance_trace,
                aggregated_artifacts=aggregated_artifacts,
                iac_result=iac_result,
                cicd_result=cicd_result,
                deploy_result=deploy_result,
                write_changes=write_changes,
                doc_runbook_agent=self.doc_runbook_agent,
                git_ops=_git_ops(),
                get_head_sha=get_head_sha,
                merge_to_development=merge_to_development,
            )
            if result.blocked_result is not None:
                return result.blocked_result
            completion = result.completion
            return None

        early_exit = self._run_gated_phases(
            [
                _phase1_intake_clarify,
                _phase2_parallel_design,
                _phase3_branch_write,
                _phase4_validation_review,
                _phase5_completion_deliver,
            ]
        )
        if early_exit is not None:
            return early_exit

        if completion is None:
            raise RuntimeError("Phase 5 did not assign a completion package")
        return DevOpsTeamResult(
            success=True, iterations=infra_fix_iterations, completion_package=completion
        )
