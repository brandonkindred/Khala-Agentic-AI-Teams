"""
Product Requirements Analysis Agent.

4-phase workflow: Spec Review → Communicate with User → Spec Update → Spec Cleanup.

This agent ensures the product specification is complete, consistent, and ready
for the Product Planning Agent.
"""

from __future__ import annotations

import json
import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from .models import (
    AnalysisPhase,
    AnalysisWorkflowResult,
    AnsweredQuestion,
    OpenQuestion,
    QuestionOption,
    SpecCleanupResult,
    SpecReviewResult,
)
from .prompts import (
    CONSOLIDATE_QUESTIONS_PROMPT,
    CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT,
    GENERATE_QUESTION_RECOMMENDATIONS_PROMPT,
    REVIEW_QUESTIONS_ALIGNMENT_PROMPT,
    SPEC_CLEANUP_CHUNK_PROMPT,
    SPEC_CLEANUP_PROMPT,
    SPEC_CONSISTENCY_CLARIFICATION_PROMPT,
    SPEC_REVIEW_PROMPT,
    SPEC_UPDATE_PROMPT,
    PRD_PROMPT,
)
from planning_v2_team.tool_agents.json_utils import (
    parse_json_with_recovery,
    default_decompose_by_sections,
)
from software_engineering_team.shared.deduplication import dedupe_strings as _dedupe_items
from software_engineering_team.shared.context_sizing import (
    compute_pra_spec_review_spec_chars,
    compute_prd_snippet_chars,
)

if TYPE_CHECKING:
    from llm_service import LLMClient

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_POLL_INTERVAL = 5.0
MAX_ITERATIONS = 100
MAX_DECOMPOSITION_DEPTH = 20
MAX_ISSUES = 10
MAX_GAPS = 10

# When deduplication reduces question count by this fraction or more, run consistency/clarity update and re-review.
DEDUP_REDUCTION_THRESHOLD = 0.5
MAX_CONSISTENCY_LOOPS = 3

# Subdirectory under repo where PRA writes all artifacts (validated_spec, PRD, updated_spec*, qa_history).
PRODUCT_ANALYSIS_SUBDIR = "plan/product_analysis"


def _section_title_from_chunk(chunk: str, max_len: int = 55) -> str:
    """Extract a short, meaningful title from a spec chunk (e.g. first markdown heading)."""
    if not chunk or not chunk.strip():
        return ""
    first_line = chunk.strip().split("\n")[0].strip()
    while first_line.startswith("#"):
        first_line = first_line.lstrip("#").strip()
    if not first_line:
        return ""
    return first_line[:max_len].strip()


def _context_discovery_fallback_questions() -> List[OpenQuestion]:
    """Fixed list of context/constraint questions used when LLM returns empty or invalid."""
    return [
        OpenQuestion(
            id="ctx_project_type",
            question_text="What type of organization or product context is this?",
            context="Shapes MVP scope and governance expectations.",
            options=[
                QuestionOption(id="opt_startup", label="Startup / early-stage (agility, speed)", is_default=True, rationale="Common for new products.", confidence=0.6),
                QuestionOption(id="opt_enterprise", label="Enterprise (governance, compliance)", is_default=False, rationale="For established orgs.", confidence=0.5),
            ],
            source="context_discovery",
            category="business",
        ),
        OpenQuestion(
            id="ctx_deployment",
            question_text="Where will this be deployed?",
            context="Deployment model affects infrastructure and provider choices.",
            options=[
                QuestionOption(id="opt_cloud", label="Cloud (AWS, GCP, Azure, etc.)", is_default=True, rationale="Most common for new apps.", confidence=0.7),
                QuestionOption(id="opt_onprem", label="On-premises", is_default=False, rationale="For air-gapped or regulated environments.", confidence=0.3),
                QuestionOption(id="opt_hybrid", label="Hybrid (cloud + on-prem)", is_default=False, rationale="Mix of cloud and on-prem.", confidence=0.4),
            ],
            source="context_discovery",
            category="infrastructure",
        ),
        OpenQuestion(
            id="ctx_cloud_provider",
            question_text="If cloud: which provider (or primary provider)?",
            context="Affects service selection and constraints.",
            options=[
                QuestionOption(id="opt_aws", label="AWS", is_default=True, rationale="Widely used, broad service set.", confidence=0.6),
                QuestionOption(id="opt_gcp", label="GCP", is_default=False, rationale="Strong data/ML offerings.", confidence=0.5),
                QuestionOption(id="opt_azure", label="Azure", is_default=False, rationale="Good for Microsoft ecosystem.", confidence=0.5),
                QuestionOption(id="opt_other", label="Other (Rackspace, DigitalOcean, Heroku, etc.)", is_default=False, rationale="Varies by need.", confidence=0.3),
            ],
            source="context_discovery",
            category="infrastructure",
        ),
        OpenQuestion(
            id="ctx_tenets",
            question_text="What architectural or product tenets must the build follow? (select all that apply)",
            context="Principles that shape technology and design decisions.",
            options=[
                QuestionOption(id="opt_event_driven", label="Event-driven", is_default=False, rationale="Async, decoupled systems.", confidence=0.5),
                QuestionOption(id="opt_api_driven", label="API-driven", is_default=True, rationale="Clear contracts, integrability.", confidence=0.7),
                QuestionOption(id="opt_serverless", label="Serverless / managed services", is_default=False, rationale="Reduce ops, scale to zero.", confidence=0.5),
                QuestionOption(id="opt_agility", label="Agility / ease of change", is_default=True, rationale="Fast iteration.", confidence=0.7),
                QuestionOption(id="opt_security_first", label="Security-first", is_default=False, rationale="Compliance and risk focus.", confidence=0.5),
            ],
            allow_multiple=True,
            source="context_discovery",
            category="architecture",
        ),
        OpenQuestion(
            id="ctx_sla",
            question_text="What availability/SLA target applies (if any)?",
            context="Organizational mandate for uptime.",
            options=[
                QuestionOption(id="opt_none", label="None / standard", is_default=True, rationale="No formal SLA.", confidence=0.6),
                QuestionOption(id="opt_three_nines", label="99.9% (three nines)", is_default=False, rationale="~8.7h downtime/year.", confidence=0.5),
                QuestionOption(id="opt_five_nines", label="99.99% or higher (four/five nines)", is_default=False, rationale="High availability mandate.", confidence=0.4),
            ],
            source="context_discovery",
            category="business",
        ),
        OpenQuestion(
            id="ctx_rto_rpo",
            question_text="Any RTO/RPO or disaster-recovery mandates?",
            context="Recovery time and recovery point objectives.",
            options=[
                QuestionOption(id="opt_none", label="None / standard backup", is_default=True, rationale="No strict RTO/RPO.", confidence=0.6),
                QuestionOption(id="opt_moderate", label="Moderate (e.g. RTO 4h, RPO 1h)", is_default=False, rationale="Some DR requirements.", confidence=0.5),
                QuestionOption(id="opt_strict", label="Strict (e.g. RTO <1h, RPO <15min)", is_default=False, rationale="Critical systems.", confidence=0.4),
            ],
            source="context_discovery",
            category="business",
        ),
    ]


# ---------------------------------------------------------------------------
# Constraint Domain Definitions and Analysis
# ---------------------------------------------------------------------------

CONSTRAINT_DOMAINS_CONFIG = {
    "infrastructure": {
        "name": "Deployment/Hosting",
        "max_layer": 4,
        "indicators": {
            1: [  # Platform category
                ("heroku", 2), ("render", 2), ("railway", 2),  # PaaS → skip to L2
                ("aws", 2), ("gcp", 2), ("azure", 2), ("google cloud", 2),  # Cloud → L2
                ("self-hosted", 2), ("on-premises", 2), ("docker", 2), ("kubernetes", 2),
                ("vercel", 2), ("cloudflare", 2), ("netlify", 2),  # Edge → L2
                ("paas", 1), ("platform as a service", 1),
                ("cloud infrastructure", 1), ("cloud-based", 1),
                ("edge", 1), ("serverless", 1),
            ],
            2: [  # Specific provider
                ("heroku", 3), ("render", 3), ("railway", 3), ("fly.io", 3),
                ("aws", 3), ("amazon web services", 3),
                ("gcp", 3), ("google cloud platform", 3),
                ("azure", 3), ("microsoft azure", 3),
                ("digitalocean", 3), ("linode", 3),
                ("vercel", 3), ("cloudflare workers", 3), ("netlify", 3),
            ],
            3: [  # Compute model
                ("lambda", 4), ("cloud functions", 4), ("serverless", 4),
                ("ecs", 4), ("fargate", 4), ("cloud run", 4), ("container", 4),
                ("ec2", 4), ("compute engine", 4), ("vm", 4), ("virtual machine", 4),
                ("app runner", 4), ("elastic beanstalk", 4),
            ],
            4: [  # Specific services
                ("lambda", 4), ("api gateway", 4), ("step functions", 4),
                ("ecs fargate", 4), ("ecs ec2", 4),
                ("cloud run", 4), ("app engine", 4),
                ("app runner", 4),
            ],
        },
    },
    "frontend": {
        "name": "Frontend Technology",
        "max_layer": 4,
        "indicators": {
            1: [  # Rendering strategy
                ("spa", 1), ("single page", 1), ("client-side", 1),
                ("ssr", 1), ("server-side render", 1), ("server render", 1),
                ("ssg", 1), ("static site", 1), ("static generation", 1),
                ("hybrid", 1),
                ("no frontend", 4), ("api only", 4), ("headless", 4),
            ],
            2: [  # Framework
                ("react", 2), ("angular", 2), ("vue", 2), ("svelte", 2),
                ("vanilla", 2), ("no framework", 2),
            ],
            3: [  # Meta-framework
                ("next.js", 3), ("nextjs", 3), ("remix", 3),
                ("nuxt", 3), ("sveltekit", 3),
                ("create react app", 3), ("cra", 3), ("vite", 3),
                ("angular cli", 3),
            ],
            4: [  # Styling
                ("tailwind", 4), ("css modules", 4), ("styled-components", 4),
                ("scss", 4), ("sass", 4), ("emotion", 4), ("css-in-js", 4),
                ("bootstrap", 4), ("material ui", 4), ("mui", 4), ("chakra", 4),
            ],
        },
    },
    "backend": {
        "name": "Backend Technology",
        "max_layer": 4,
        "indicators": {
            1: [  # Architecture
                ("monolith", 1), ("microservice", 1), ("serverless function", 1),
                ("bff", 1), ("backend for frontend", 1),
            ],
            2: [  # Language
                ("python", 2), ("node", 2), ("nodejs", 2), ("typescript", 2),
                ("java", 2), ("kotlin", 2), ("go", 2), ("golang", 2),
                ("rust", 2), ("c#", 2), (".net", 2), ("ruby", 2),
            ],
            3: [  # Framework
                ("fastapi", 3), ("django", 3), ("flask", 3),
                ("express", 3), ("nestjs", 3), ("fastify", 3), ("koa", 3),
                ("spring", 3), ("spring boot", 3), ("quarkus", 3),
                ("gin", 3), ("echo", 3), ("fiber", 3),
                ("actix", 3), ("axum", 3), ("rocket", 3),
                ("rails", 3), ("ruby on rails", 3),
                ("asp.net", 3),
            ],
            4: [  # API style
                ("rest", 4), ("restful", 4), ("graphql", 4), ("grpc", 4),
                ("trpc", 4), ("websocket", 4),
            ],
        },
    },
    "database": {
        "name": "Database",
        "max_layer": 4,
        "indicators": {
            1: [  # Type
                ("relational", 1), ("sql", 1),
                ("document", 1), ("nosql", 1),
                ("key-value", 1), ("graph", 1), ("time-series", 1),
            ],
            2: [  # Hosting model
                ("rds", 2), ("cloud sql", 2), ("planetscale", 2), ("managed", 2),
                ("self-managed", 2), ("self-hosted", 2),
                ("serverless", 2), ("aurora serverless", 2), ("neon", 2),
            ],
            3: [  # Specific database
                ("postgresql", 3), ("postgres", 3), ("mysql", 3), ("mariadb", 3),
                ("mongodb", 3), ("dynamodb", 3), ("firestore", 3),
                ("redis", 3), ("cassandra", 3), ("neo4j", 3),
                ("sqlite", 3), ("supabase", 3),
            ],
            4: [  # Additional stores
                ("redis", 4), ("memcached", 4), ("caching", 4),
                ("elasticsearch", 4), ("opensearch", 4), ("algolia", 4),
                ("rabbitmq", 4), ("sqs", 4), ("kafka", 4), ("message queue", 4),
            ],
        },
    },
    "auth": {
        "name": "Authentication",
        "max_layer": 4,
        "indicators": {
            1: [  # Strategy
                ("third-party auth", 1), ("auth provider", 1), ("external auth", 1),
                ("custom auth", 1), ("self-built auth", 1),
                ("hybrid auth", 1),
            ],
            2: [  # Provider
                ("auth0", 2), ("clerk", 2), ("firebase auth", 2),
                ("cognito", 2), ("aws cognito", 2),
                ("supabase auth", 2), ("keycloak", 2),
                ("okta", 2), ("fusionauth", 2),
            ],
            3: [  # Methods
                ("oauth", 3), ("oidc", 3), ("openid", 3),
                ("email/password", 3), ("email password", 3),
                ("passwordless", 3), ("magic link", 3), ("otp", 3),
                ("sso", 3), ("saml", 3), ("ldap", 3),
                ("api key", 3),
            ],
            4: [  # Security features
                ("mfa", 4), ("2fa", 4), ("two-factor", 4), ("multi-factor", 4),
                ("session", 4), ("jwt", 4), ("token refresh", 4),
                ("rbac", 4), ("role-based", 4), ("permissions", 4),
            ],
        },
    },
}


def _word_boundary_match(indicator: str, text: str) -> bool:
    """Check if indicator appears as a whole word/phrase in text.
    
    Uses regex word boundaries to avoid false positives like 'gin' in 'login'.
    """
    pattern = r'\b' + re.escape(indicator) + r'\b'
    return bool(re.search(pattern, text))


def analyze_constraint_status(
    spec_content: str,
    answered_questions: List[AnsweredQuestion],
) -> Dict[str, int]:
    """Analyze which constraint domains are resolved and to what layer.
    
    Scans the spec content and answered questions to determine the current
    resolution level for each constraint domain.
    
    Args:
        spec_content: The current specification content.
        answered_questions: List of questions that have been answered.
        
    Returns:
        Dict mapping domain name to resolved layer (0 = unresolved, 1-4 = layer resolved).
    """
    status: Dict[str, int] = {domain: 0 for domain in CONSTRAINT_DOMAINS_CONFIG}
    
    spec_lower = spec_content.lower()
    
    # Also include answered questions in the analysis
    answers_text = ""
    for aq in answered_questions:
        answers_text += f" {aq.question_text} {aq.selected_answer} "
    answers_lower = answers_text.lower()
    
    combined_text = spec_lower + " " + answers_lower
    
    for domain, config in CONSTRAINT_DOMAINS_CONFIG.items():
        max_resolved = 0
        indicators = config.get("indicators", {})
        
        # Check each layer's indicators using word boundary matching
        for layer in range(1, config["max_layer"] + 1):
            layer_indicators = indicators.get(layer, [])
            for indicator, resolves_to in layer_indicators:
                if _word_boundary_match(indicator, combined_text):
                    max_resolved = max(max_resolved, resolves_to)
        
        status[domain] = min(max_resolved, config["max_layer"])
    
    return status


def generate_constraint_hints(constraint_status: Dict[str, int]) -> str:
    """Generate hints for the LLM about which constraint layers need questions.
    
    Args:
        constraint_status: Dict mapping domain to resolved layer.
        
    Returns:
        Formatted string with hints about which domains need attention.
    """
    hints = []
    
    for domain, resolved_layer in constraint_status.items():
        config = CONSTRAINT_DOMAINS_CONFIG.get(domain, {})
        max_layer = config.get("max_layer", 4)
        domain_name = config.get("name", domain)
        
        if resolved_layer >= max_layer:
            hints.append(f"- {domain_name}: FULLY RESOLVED (Layer {max_layer}/{max_layer}) - No questions needed")
        elif resolved_layer == 0:
            hints.append(f"- {domain_name}: UNRESOLVED - Ask Layer 1 question (start from the beginning)")
        else:
            next_layer = resolved_layer + 1
            hints.append(f"- {domain_name}: Resolved to Layer {resolved_layer}/{max_layer} - Ask Layer {next_layer} question")
    
    if not hints:
        return ""
    
    return """## CONSTRAINT STATUS (from previous answers)

Based on analysis of the specification and previous answers, here is the current constraint resolution status:

""" + "\n".join(hints) + """

Focus your questions on domains that are NOT fully resolved. Ask ONLY the next layer question for each domain.
"""


class ProductRequirementsAnalysisAgent:
    """
    Product Requirements Analysis Agent with 4-phase workflow.

    Phases:
    1. Spec Review - Identify gaps and generate questions
    2. Communicate with User - Send questions, wait for answers
    3. Spec Update - Incorporate answers into spec
    4. Spec Cleanup - Validate and clean the spec

    The cycle (1-3) repeats until no open questions remain, then Spec Cleanup runs.
    """

    def __init__(self, llm_client: "LLMClient") -> None:
        if llm_client is None:
            raise ValueError("llm_client is required")
        self.llm = llm_client

    def _has_existing_pra_artifacts(self, repo_path: Path) -> bool:
        """Return True if plan/product_analysis has prior PRA output we can resume from."""
        pa_dir = repo_path / "plan" / "product_analysis"
        if not pa_dir.is_dir():
            return False
        # qa_history.md: substantive only when length > 200 and contains iteration/answer markers
        qa_path = pa_dir / "qa_history.md"
        if qa_path.is_file():
            try:
                content = qa_path.read_text(encoding="utf-8")
                if len(content) > 200 and ("## Iteration" in content or "**Answer:**" in content):
                    return True
            except OSError:
                pass
        if (pa_dir / "validated_spec.md").is_file():
            return True
        # Any updated_spec_v*.md or updated_spec.md
        for p in pa_dir.iterdir():
            if p.is_file() and p.suffix == ".md":
                name = p.name
                if name == "updated_spec.md" or (name.startswith("updated_spec_v") and name.endswith(".md")):
                    return True
        return False

    def run_workflow(
        self,
        *,
        spec_content: str,
        repo_path: Path,
        job_id: Optional[str] = None,
        job_updater: Optional[Callable[..., None]] = None,
        max_iterations: int = MAX_ITERATIONS,
        context_files: Optional[Dict[str, str]] = None,
        initial_spec_path: Optional[Path] = None,
    ) -> AnalysisWorkflowResult:
        """
        Execute the full Product Requirements Analysis workflow.

        Args:
            spec_content: The initial specification content
            repo_path: Path to the repository for storing artifacts
            job_id: Job ID for question tracking (required for user communication)
            job_updater: Callback to update job status
            max_iterations: Maximum number of spec review cycles
            context_files: Optional dict of additional context files (path -> content)
            initial_spec_path: Path to the file the spec was loaded from (for rename when needing more detail)

        Returns:
            AnalysisWorkflowResult with validated spec and answered questions
        """
        start_time = time.monotonic()
        result = AnalysisWorkflowResult()
        current_spec = spec_content
        all_answered_questions: List[AnsweredQuestion] = []
        iteration = 0
        self._context_files = context_files or {}

        def _update_job(**kwargs: Any) -> None:
            if job_updater:
                try:
                    job_updater(**kwargs)
                except Exception:
                    pass

        logger.info("Product Requirements Analysis Agent: WORKFLOW START")

        from spec_parser import get_next_updated_spec_version

        base_version = get_next_updated_spec_version(repo_path)
        product_analysis_dir = repo_path / "plan" / "product_analysis"
        product_analysis_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Initialized %s for PRA artifacts", PRODUCT_ANALYSIS_SUBDIR)

        # One-time context and constraints discovery (before first spec review) when job_id is set
        skip_context_discovery = False
        if job_id is not None:
            if self._has_existing_pra_artifacts(repo_path):
                logger.info(
                    "Skipping context discovery; plan/product_analysis has prior PRA output, picking up from there."
                )
                skip_context_discovery = True
                result.current_phase = AnalysisPhase.SPEC_REVIEW
                _update_job(
                    current_phase=AnalysisPhase.SPEC_REVIEW.value,
                    progress=5,
                    message="Resuming from prior analysis; reviewing specification...",
                    status_text="Resuming from prior analysis; reviewing specification...",
                )
                # Load current_spec from existing artifacts when resuming
                validated_spec_path = product_analysis_dir / "validated_spec.md"
                if validated_spec_path.is_file():
                    current_spec = validated_spec_path.read_text(encoding="utf-8")
                else:
                    # Latest updated_spec_v*.md or updated_spec.md by version or mtime
                    candidates: List[Path] = []
                    for p in product_analysis_dir.iterdir():
                        if not p.is_file() or p.suffix != ".md":
                            continue
                        name = p.name
                        if name == "updated_spec.md":
                            candidates.append(p)
                        elif name.startswith("updated_spec_v") and name.endswith(".md"):
                            candidates.append(p)
                    if candidates:
                        def _spec_sort_key(path: Path) -> Tuple[int, float]:
                            # Prefer higher version number; then mtime
                            name = path.stem
                            if name.startswith("updated_spec_v"):
                                try:
                                    ver = int(name.split("_v")[-1].split("_")[0])
                                    return (ver, path.stat().st_mtime)
                                except (ValueError, IndexError):
                                    pass
                            return (0, path.stat().st_mtime)
                        latest_spec_file = max(candidates, key=_spec_sort_key)
                        current_spec = latest_spec_file.read_text(encoding="utf-8")
            if not skip_context_discovery:
                result.current_phase = AnalysisPhase.CONTEXT_DISCOVERY
                _update_job(
                    current_phase=AnalysisPhase.CONTEXT_DISCOVERY.value,
                    progress=2,
                    message="Gathering project context and constraints...",
                    status_text="Gathering project context and constraints...",
                )
                context_questions = self._run_context_constraints_discovery(
                    current_spec, repo_path
                )
                if context_questions:
                    _update_job(
                        status_text=f"Waiting for answers to {len(context_questions)} context/constraint question(s)",
                    )
                    try:
                        context_answered = self._communicate_with_user(
                            job_id=job_id,
                            open_questions=context_questions,
                            repo_path=repo_path,
                            iteration=0,
                        )
                    except Exception as exc:
                        result.failure_reason = f"Context discovery communication failed: {exc}"
                        logger.error("Product Requirements Analysis: %s", result.failure_reason)
                        return result
                    if context_answered:
                        current_spec = self._inject_context_answers_into_spec(
                            current_spec, context_answered, repo_path
                        )
                        all_answered_questions.extend(context_answered)
                        self._record_answers(repo_path, context_answered, iteration=0)
                # If no context questions or no answers, proceed with current_spec unchanged
        else:
            logger.info("job_id is None; skipping context discovery")

        while iteration < max_iterations:
            iteration += 1
            result.iterations = iteration

            # Phase 1: Spec Review
            result.current_phase = AnalysisPhase.SPEC_REVIEW
            _update_job(
                current_phase=AnalysisPhase.SPEC_REVIEW.value,
                progress=5 + (iteration - 1) * 15,
                message=f"Spec review iteration {iteration}",
                status_text=f"Analyzing specification for gaps and inconsistencies (iteration {iteration})",
            )

            try:
                _update_job(status_text="Analyzing full specification for gaps and inconsistencies...")

                def _on_spec_review_progress(_chunk_index: int, _total_chunks: int) -> None:
                    _update_job(status_text="Analyzing full specification for gaps and inconsistencies...")

                spec_before_review = current_spec
                spec_review_result, current_spec = self._run_spec_review(
                    current_spec,
                    repo_path,
                    iteration=iteration,
                    spec_version=base_version + (iteration - 1),
                    answered_questions=all_answered_questions,
                    on_chunk_progress=_on_spec_review_progress,
                )
                if current_spec != spec_before_review:
                    _update_job(
                        status_text="Re-analyzing full specification after clarification..."
                    )
                    spec_review_result, current_spec = self._run_spec_review(
                        current_spec,
                        repo_path,
                        iteration=iteration,
                        spec_version=base_version + (iteration - 1),
                        answered_questions=all_answered_questions,
                        on_chunk_progress=_on_spec_review_progress,
                    )
                    logger.info("Re-ran spec review on clarified spec")
                result.spec_review_result = spec_review_result
                if spec_review_result.open_questions:
                    _update_job(
                        status_text=f"Found {len(spec_review_result.issues)} issues, {len(spec_review_result.gaps)} gaps, {len(spec_review_result.open_questions)} questions"
                    )
            except Exception as exc:
                result.failure_reason = f"Spec review failed: {exc}"
                logger.error("Product Requirements Analysis: %s", result.failure_reason)
                return result

            # Consolidate duplicate/semantically-similar questions before sending to user
            original_count = len(spec_review_result.open_questions)
            _update_job(
                status_text="Consolidating open questions (merging duplicates)...",
            )
            consolidated_questions = self._consolidate_open_questions(
                spec_review_result.open_questions
            )
            if len(consolidated_questions) < original_count:
                _update_job(
                    status_text=f"Consolidated {original_count} questions into {len(consolidated_questions)} distinct questions",
                )
                logger.info(
                    "Consolidated open questions: %d -> %d",
                    original_count,
                    len(consolidated_questions),
                )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": consolidated_questions}
            )
            open_count = len(spec_review_result.open_questions)
            count_before_dedup = open_count

            _update_job(
                status_text="Deduplicating questions whose answers we already have...",
            )
            deduped_questions = self._dedupe_questions_by_answer_similarity(
                spec_review_result.open_questions,
                all_answered_questions,
            )
            if len(deduped_questions) < open_count:
                _update_job(
                    status_text=f"Reduced to {len(deduped_questions)} questions (already have answers for the rest)",
                )
                logger.info(
                    "Deduped open questions by answer similarity: %d -> %d",
                    open_count,
                    len(deduped_questions),
                )
                spec_review_result = spec_review_result.model_copy(
                    update={"open_questions": deduped_questions}
                )
                open_count = len(spec_review_result.open_questions)

            # If deduplication reduced questions by 50%+, update spec for consistency/clarity and re-review
            reduction_ratio = (
                (count_before_dedup - len(deduped_questions)) / count_before_dedup
                if count_before_dedup > 0
                else 0.0
            )
            consistency_loops = 0
            while (
                reduction_ratio >= DEDUP_REDUCTION_THRESHOLD
                and consistency_loops < MAX_CONSISTENCY_LOOPS
                and len(spec_review_result.open_questions) > 0
            ):
                consistency_loops += 1
                _update_job(
                    status_text="Many duplicate questions found. Updating spec for clarity and to resolve conflicts using Q&A history...",
                )
                qa_history = self._read_qa_history(repo_path)
                _update_job(
                    status_text="Editing spec: clarifying answers and removing conflicting information...",
                )
                current_spec = self._update_spec_for_consistency_and_clarity(
                    current_spec,
                    repo_path,
                    qa_history,
                    all_answered_questions,
                    base_version + (iteration - 1),
                    consistency_loops,
                )
                _update_job(
                    status_text="Spec updated. Re-analyzing full specification after consistency update...",
                )
                spec_review_result, current_spec = self._run_spec_review(
                    current_spec,
                    repo_path,
                    iteration=iteration,
                    spec_version=base_version + (iteration - 1),
                    answered_questions=all_answered_questions,
                    on_chunk_progress=_on_spec_review_progress,
                )
                result.spec_review_result = spec_review_result
                # Re-consolidate and re-dedupe
                _update_job(
                    status_text="Re-consolidating and re-deduplicating questions after spec update...",
                )
                consolidated_questions = self._consolidate_open_questions(
                    spec_review_result.open_questions
                )
                spec_review_result = spec_review_result.model_copy(
                    update={"open_questions": consolidated_questions}
                )
                open_count = len(spec_review_result.open_questions)
                count_before_dedup = open_count
                deduped_questions = self._dedupe_questions_by_answer_similarity(
                    spec_review_result.open_questions,
                    all_answered_questions,
                )
                if len(deduped_questions) < open_count:
                    logger.info(
                        "After consistency loop %d: deduped %d -> %d",
                        consistency_loops,
                        open_count,
                        len(deduped_questions),
                    )
                spec_review_result = spec_review_result.model_copy(
                    update={"open_questions": deduped_questions}
                )
                open_count = len(spec_review_result.open_questions)
                reduction_ratio = (
                    (count_before_dedup - len(deduped_questions)) / count_before_dedup
                    if count_before_dedup > 0
                    else 0.0
                )
                if not spec_review_result.open_questions:
                    logger.info("No open questions after consistency update, proceeding")
                    break

            _update_job(
                status_text="Checking question and answer alignment...",
            )
            aligned_questions = self._review_question_answer_alignment(
                spec_review_result.open_questions
            )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": aligned_questions}
            )

            _update_job(
                status_text="Adding recommendations to questions...",
            )
            questions_with_recommendations = self._add_recommendations(
                spec_review_result.open_questions, current_spec
            )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": questions_with_recommendations}
            )

            logger.info(
                "Iteration %d: Found %d issues, %d gaps, %d open questions",
                iteration,
                len(spec_review_result.issues),
                len(spec_review_result.gaps),
                open_count,
            )

            if not spec_review_result.open_questions:
                logger.info("No open questions, proceeding to Spec Cleanup")
                break

            # If we need more detail and the input was validated_spec.md, rename it to
            # updated_spec_v{next} so we don't overwrite it; subsequent Q&A updates use v+1, v+2, ...
            validated_spec_path = product_analysis_dir / "validated_spec.md"
            if (
                iteration == 1
                and initial_spec_path is not None
                and initial_spec_path.resolve() == validated_spec_path.resolve()
                and validated_spec_path.exists()
            ):
                next_v = base_version
                target = product_analysis_dir / f"updated_spec_v{next_v}.md"
                validated_spec_path.rename(target)
                logger.info(
                    "Renamed validated_spec.md to %s (agent needs more detail); updates will use v%d+",
                    target.name,
                    next_v,
                )
                base_version = get_next_updated_spec_version(repo_path)

            # Phase 2: Communicate with User
            result.current_phase = AnalysisPhase.COMMUNICATE
            _update_job(
                current_phase=AnalysisPhase.COMMUNICATE.value,
                progress=10 + (iteration - 1) * 15,
                message=f"Waiting for answers to {len(spec_review_result.open_questions)} question(s)",
                status_text=f"Waiting for your input on {len(spec_review_result.open_questions)} question(s)",
            )

            try:
                answered_questions = self._communicate_with_user(
                    job_id=job_id,
                    open_questions=spec_review_result.open_questions,
                    repo_path=repo_path,
                    iteration=iteration,
                )
            except Exception as exc:
                result.failure_reason = f"Communication failed: {exc}"
                logger.error("Product Requirements Analysis: %s", result.failure_reason)
                return result

            if not answered_questions:
                raise RuntimeError(
                    "No answers received from user communication phase. "
                    "User input is required to proceed."
                )

            all_answered_questions.extend(answered_questions)
            result.answered_questions = all_answered_questions

            # Phase 3: Spec Update
            result.current_phase = AnalysisPhase.SPEC_UPDATE
            _update_job(
                current_phase=AnalysisPhase.SPEC_UPDATE.value,
                progress=15 + (iteration - 1) * 15,
                message=f"Updating spec with {len(answered_questions)} answers",
                status_text=f"Incorporating {len(answered_questions)} answer(s) into the specification",
            )

            try:
                _update_job(status_text="Generating updated specification based on your answers")
                current_spec = self._update_spec(
                    current_spec=current_spec,
                    answered_questions=answered_questions,
                    repo_path=repo_path,
                    version=base_version + (iteration - 1),
                )
                _update_job(status_text="Incorporated answers into spec")
                _update_job(status_text="Specification updated successfully")
            except Exception as exc:
                result.failure_reason = f"Spec update failed: {exc}"
                logger.error("Product Requirements Analysis: %s", result.failure_reason)
                return result

        # Phase 4: Spec Cleanup
        result.current_phase = AnalysisPhase.SPEC_CLEANUP
        _update_job(
            current_phase=AnalysisPhase.SPEC_CLEANUP.value,
            progress=90,
            message="Validating and cleaning specification",
            status_text="Validating specification completeness and consistency",
        )

        try:
            _update_job(status_text="Running final validation and cleanup on specification")
            cleanup_chunks = default_decompose_by_sections(current_spec)
            cleanup_titles = [_section_title_from_chunk(c) for c in cleanup_chunks]

            def _on_spec_cleanup_chunk(chunk_index: int, total_chunks: int) -> None:
                if chunk_index < len(cleanup_titles) and cleanup_titles[chunk_index]:
                    status_text = f"Validating: {cleanup_titles[chunk_index]}..."
                else:
                    status_text = f"Validating specification (section {chunk_index + 1}/{total_chunks})..."
                _update_job(status_text=status_text)

            cleanup_result = self._run_spec_cleanup(
                current_spec,
                repo_path,
                on_chunk_progress=_on_spec_cleanup_chunk,
            )
            result.spec_cleanup_result = cleanup_result
            _update_job(status_text="Validation complete")
            # Generate a Product Requirements Document (PRD) from the cleaned spec
            prd_content = self._generate_prd_document(
                cleaned_spec=cleanup_result.cleaned_spec,
                answered_questions=all_answered_questions,
            )
            result.final_spec_content = cleanup_result.cleaned_spec
        except Exception as exc:
            result.failure_reason = f"Spec cleanup failed: {exc}"
            logger.error("Product Requirements Analysis: %s", result.failure_reason)
            return result

        # Save validated spec (cleaned spec) and PRD separately.
        product_analysis_dir = repo_path / "plan" / "product_analysis"
        product_analysis_dir.mkdir(parents=True, exist_ok=True)
        validated_spec_path = product_analysis_dir / "validated_spec.md"
        validated_spec_path.write_text(cleanup_result.cleaned_spec, encoding="utf-8")
        result.validated_spec_path = str(validated_spec_path)

        # Also write an explicit PRD file for clarity
        try:
            prd_path = product_analysis_dir / "product_requirements_document.md"
            prd_path.write_text(prd_content, encoding="utf-8")
            logger.info("Product Requirements Analysis: PRD saved to %s", prd_path.name)
        except Exception as exc:
            logger.warning(
                "Product Requirements Analysis: Failed to write PRD alias file: %s", exc
            )

        result.success = True
        result.summary = (
            f"Analysis complete: {result.iterations} iteration(s), "
            f"{len(all_answered_questions)} questions answered. "
            f"Validated spec saved to validated_spec.md; PRD saved to product_requirements_document.md"
        )

        _update_job(
            current_phase=AnalysisPhase.SPEC_CLEANUP.value,
            progress=100,
            message=result.summary,
            status_text="Product analysis complete - validated spec and PRD generated",
        )

        elapsed = time.monotonic() - start_time
        logger.info(
            "Product Requirements Analysis Agent: WORKFLOW COMPLETE in %.1fs", elapsed
        )

        return result

    def _merge_spec_review_results(
        self, results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Combine issues, gaps, and questions from multiple chunk reviews.

        Kept for potential future chunked fallback; spec review currently uses
        a single whole-spec LLM call and does not call this.

        Args:
            results: List of parsed JSON dicts from chunk reviews

        Returns:
            Merged dict with concatenated lists
        """
        merged: Dict[str, Any] = {
            "issues": [],
            "gaps": [],
            "open_questions": [],
            "summary": "",
        }

        summaries = []
        for r in results:
            if isinstance(r.get("issues"), list):
                merged["issues"].extend(r["issues"])
            if isinstance(r.get("gaps"), list):
                merged["gaps"].extend(r["gaps"])
            if isinstance(r.get("open_questions"), list):
                merged["open_questions"].extend(r["open_questions"])
            if r.get("summary"):
                summaries.append(str(r["summary"]))

        merged["summary"] = (
            f"Reviewed {len(results)} sections. " + " ".join(summaries[:3])
        )
        return merged

    def _merge_spec_cleanup_results(
        self, results: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Combine cleanup results from multiple chunks.

        Args:
            results: List of parsed JSON dicts from chunk cleanup

        Returns:
            Merged dict with combined validation issues and cleaned spec
        """
        merged: Dict[str, Any] = {
            "is_valid": True,
            "validation_issues": [],
            "cleaned_spec": "",
            "summary": "",
        }

        cleaned_parts = []
        for r in results:
            if r.get("is_valid") is False:
                merged["is_valid"] = False
            if isinstance(r.get("validation_issues"), list):
                merged["validation_issues"].extend(r["validation_issues"])
            if r.get("cleaned_spec"):
                cleaned_parts.append(str(r["cleaned_spec"]))

        merged["cleaned_spec"] = "\n\n".join(cleaned_parts)
        merged["summary"] = f"Cleanup completed for {len(results)} sections"
        return merged

    def _format_context_for_review(self) -> str:
        """Format context files for inclusion in the spec review prompt."""
        if not self._context_files:
            return ""
        
        from spec_parser import format_context_for_prompt
        formatted = format_context_for_prompt(self._context_files)
        
        if not formatted:
            return ""
        
        return f"""

## Additional Context Files

The following additional files were provided in the project folder. Review these alongside the main specification to understand the full context:

{formatted}

---
"""

    def _run_spec_review(
        self,
        spec_content: str,
        repo_path: Path,
        iteration: int = 1,
        spec_version: Optional[int] = None,
        answered_questions: Optional[List[AnsweredQuestion]] = None,
        on_chunk_progress: Optional[Callable[[int, int], None]] = None,
    ) -> tuple[SpecReviewResult, str]:
        """Run the Spec Review phase to identify gaps and questions.

        Args:
            spec_content: Current specification content.
            repo_path: Path to the repository.
            iteration: Current iteration number (for logging/qa_history).
            spec_version: Version number for updated_spec_vN.md when writing (e.g. from duplicates). If None, iteration is used.
            answered_questions: List of previously answered questions for constraint analysis.
            on_chunk_progress: Optional callback (chunk_index, total_chunks) for progress updates during chunked LLM calls.

        Returns:
            Tuple of (SpecReviewResult, updated_spec_content). The spec may be
            updated if duplicate questions were found and clarified.
        """
        if spec_version is None:
            spec_version = iteration
        # Full Q&A for prompt: file history + in-memory answered_questions (current session)
        qa_from_file = self._read_qa_history(repo_path)
        qa_for_prompt = qa_from_file
        if answered_questions:
            session_block = self._format_answered_questions_for_prompt(answered_questions)
            if session_block:
                if qa_for_prompt:
                    qa_for_prompt += "\n\n## Current session answers\n\n" + session_block
                else:
                    qa_for_prompt = "## Current session answers\n\n" + session_block
        # Optional cap to leave room for spec + instructions (e.g. last 12k chars)
        if len(qa_for_prompt) > 12000:
            qa_for_prompt = qa_for_prompt[-12000:]
            logger.debug("Capped qa_for_prompt to last 12k chars")

        # Analyze constraint status and generate hints for the LLM
        constraint_status = analyze_constraint_status(
            spec_content,
            answered_questions or []
        )
        constraint_hints = generate_constraint_hints(constraint_status)

        logger.info(
            "Constraint status: %s",
            {d: f"L{l}" for d, l in constraint_status.items()}
        )

        # Build the full content including context files
        context_section = self._format_context_for_review()
        full_spec_content = spec_content
        if context_section:
            full_spec_content = spec_content + context_section
            logger.info(
                "Spec review: Including %d context files in review",
                len(self._context_files),
            )

        # Single whole-spec prompt; include full Q&A only when non-empty (edge-empty-qa)
        max_spec_chars = compute_pra_spec_review_spec_chars(self.llm)
        if qa_for_prompt:
            prompt = SPEC_REVIEW_PROMPT.format(
                spec_content=full_spec_content[:max_spec_chars],
                constraint_hints=constraint_hints,
            )
            prompt += """

IMPORTANT: The following questions have ALREADY been answered. Do NOT ask these questions again or any variations of them. Only ask NEW questions about topics NOT covered below. The spec and this Q&A are the source of truth.

Previously Answered Questions:
---
""" + qa_for_prompt + """
---
"""
        else:
            prompt = SPEC_REVIEW_PROMPT.format(
                spec_content=full_spec_content[:max_spec_chars],
                constraint_hints=constraint_hints,
            )

        if on_chunk_progress is not None:
            on_chunk_progress(0, 1)

        # Single LLM call for whole-spec review (no decomposition or merge)
        raw = parse_json_with_recovery(
            self.llm,
            prompt,
            agent_name="PRA_spec_review",
        )

        if not raw:
            logger.warning(
                "PRA spec_review: No JSON recovered, will retry in next iteration"
            )
            return (
                SpecReviewResult(
                    summary="Spec review JSON parsing failed - will retry",
                    issues=["JSON parsing failed - response may have been truncated"],
                    gaps=[],
                    open_questions=[],
                ),
                spec_content,
            )

        result = self._parse_spec_review_response(raw)
        updated_spec = spec_content

        # Filter duplicates and clarify spec using full qa_for_prompt (file + session)
        if qa_for_prompt and result.open_questions:
            filtered, duplicates = self._filter_duplicate_questions(
                result.open_questions, qa_for_prompt
            )
            result.open_questions = filtered

            if duplicates:
                logger.info(
                    "Found %d duplicate questions - clarifying spec with existing answers",
                    len(duplicates),
                )
                updated_spec = self._update_spec_from_duplicates(
                    duplicates, qa_for_prompt, spec_content, repo_path, spec_version
                )

        result.open_questions = self._filter_organizational_questions(
            result.open_questions
        )
        return result, updated_spec

    def _format_answered_questions_for_prompt(
        self, answered_questions: List[AnsweredQuestion]
    ) -> str:
        """Format in-memory answered questions in qa_history.md style for inclusion in the LLM prompt.

        Handles empty list and optional fields (rationale, other_text, was_auto_answered, was_default).
        """
        if not answered_questions:
            return ""
        lines: List[str] = []
        for aq in answered_questions:
            lines.append(f"### {aq.question_text}")
            lines.append(f"**Answer:** {aq.selected_answer}")
            if aq.rationale:
                lines.append(f"**Rationale:** {aq.rationale}")
            if aq.was_auto_answered:
                lines.append(f"*Auto-answered with {aq.confidence:.0%} confidence*")
            elif aq.was_default:
                lines.append("*(Default applied)*")
            if aq.other_text:
                lines.append(f"*Custom text:* {aq.other_text}")
            lines.append("")
        return "\n".join(lines)

    def _read_qa_history(self, repo_path: Path) -> str:
        """Read the QA history file if it exists (from plan/product_analysis)."""
        qa_file = repo_path / "plan" / "product_analysis" / "qa_history.md"
        if qa_file.exists():
            try:
                return qa_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Failed to read qa_history.md: %s", e)
        return ""

    def _filter_duplicate_questions(
        self,
        new_questions: List[OpenQuestion],
        qa_history: str,
    ) -> tuple[List[OpenQuestion], List[OpenQuestion]]:
        """Filter out questions that appear to be duplicates of answered ones.
        
        Uses normalized word stems (e.g. token/tokens, store/stored). Only filters
        as duplicate when match to qa_history is >= 95%; 50–95% similar questions
        are kept and may be consolidated elsewhere. Treats spec + Q&A as source of truth.
        
        Returns:
            Tuple of (filtered_questions, duplicate_questions).
            - filtered_questions: Questions that are NOT duplicates (should be asked)
            - duplicate_questions: Questions that ARE duplicates (already answered)
        """
        qa_history_lower = qa_history.lower()
        filtered = []
        duplicates = []

        def _stem(w: str) -> str:
            """Normalize word for matching (e.g. tokens->token, stored->store)."""
            w = w.strip()
            if len(w) <= 3:
                return w
            if w.endswith("ed") and len(w) > 4:
                return w[:-2]  # stored -> store
            if w.endswith("s") and not w.endswith("ss") and len(w) > 4:
                return w[:-1]  # tokens -> token
            return w

        for q in new_questions:
            q_text_lower = q.question_text.lower()
            # Key words: length > 3, normalized to stems for plural/tense
            words = [w for w in q_text_lower.split() if len(w) > 3]
            key_stems = set(_stem(w) for w in words)
            if not key_stems:
                filtered.append(q)
                continue
            # Count how many stems (or their plural) appear in qa_history
            matches = sum(
                1
                for stem in key_stems
                if stem in qa_history_lower
                or (stem + "s") in qa_history_lower
                or (stem + "ed") in qa_history_lower
            )
            match_ratio = matches / len(key_stems)
            # Only treat as duplicate of an answered question when match >= 90%.
            # Lower similarity (50–90%) may be consolidated but should not be filtered out.
            if match_ratio >= 0.90:
                logger.info(
                    "Filtering duplicate question (%.0f%% match): %s",
                    match_ratio * 100,
                    q.question_text[:60],
                )
                duplicates.append(q)
                continue
            filtered.append(q)

        if duplicates:
            logger.info(
                "Filtered %d duplicate questions based on qa_history",
                len(duplicates),
            )

        return filtered, duplicates

    def _filter_organizational_questions(
        self, questions: List[OpenQuestion]
    ) -> List[OpenQuestion]:
        """Remove questions about organizational structure, approval processes, or decision hierarchy.

        The client/user is the source of truth; we do not ask who approves, how decisions
        are made, or about org structure. A question is considered organizational if any
        of the configured phrases appear in question_text or (if present) context.
        """
        ORGANIZATIONAL_PHRASES = [
            "decision process",
            "approval process",
            "who makes",
            "final decision",
            "consensus",
            "product manager",
            "stakeholder approval",
            "organizational structure",
            "who approves",
            "sign-off",
            "sign off",
            "hierarchy",
            "reporting",
        ]
        kept: List[OpenQuestion] = []
        for q in questions:
            text_norm = (q.question_text or "").lower().strip()
            context_norm = (q.context or "").lower().strip() if q.context else ""
            is_org = False
            for phrase in ORGANIZATIONAL_PHRASES:
                if phrase in text_norm or (context_norm and phrase in context_norm):
                    is_org = True
                    break
            if not is_org:
                kept.append(q)
        removed = len(questions) - len(kept)
        if removed:
            logger.info(
                "Filtered %d organizational/process question(s)",
                removed,
            )
        return kept

    def _extract_answer_from_qa_history(
        self,
        question: OpenQuestion,
        qa_history: str,
    ) -> Optional[AnsweredQuestion]:
        """Extract a previously recorded answer from qa_history.md for a duplicate question.
        
        Parses the qa_history.md markdown format to find the best matching Q&A pair.
        
        Args:
            question: The duplicate question to find an answer for.
            qa_history: Raw content of qa_history.md file.
            
        Returns:
            AnsweredQuestion if a matching answer was found, None otherwise.
        """
        import re
        
        if not qa_history:
            return None
        
        q_text_lower = question.question_text.lower()
        key_words = [w for w in q_text_lower.split() if len(w) > 4]
        
        if not key_words:
            return None
        
        # Parse qa_history.md sections - format is:
        # ### Question text
        # **Answer:** Answer text
        # **Rationale:** Optional rationale
        # *(Auto-answered with X% confidence)* or *(Default applied)*
        
        # Split into Q&A blocks by "### " headers
        blocks = re.split(r'\n###\s+', qa_history)
        
        best_match: Optional[tuple[float, str, str, str]] = None  # (score, question, answer, rationale)
        
        for block in blocks[1:]:  # Skip first block (header)
            lines = block.strip().split('\n')
            if not lines:
                continue
            
            recorded_question = lines[0].strip()
            recorded_question_lower = recorded_question.lower()
            
            # Calculate match score
            matches = sum(1 for w in key_words if w in recorded_question_lower)
            match_ratio = matches / len(key_words) if key_words else 0
            
            if match_ratio > 0.5:  # Good enough match
                # Extract answer from block
                answer = ""
                rationale = ""
                
                for line in lines[1:]:
                    if line.startswith("**Answer:**"):
                        answer = line.replace("**Answer:**", "").strip()
                    elif line.startswith("**Rationale:**"):
                        rationale = line.replace("**Rationale:**", "").strip()
                
                if answer and (best_match is None or match_ratio > best_match[0]):
                    best_match = (match_ratio, recorded_question, answer, rationale)
        
        if best_match:
            _, matched_q, answer, rationale = best_match
            logger.debug(
                "Extracted answer for duplicate question: '%s' -> '%s'",
                question.question_text[:40],
                answer[:40],
            )
            return AnsweredQuestion(
                question_id=question.id,
                question_text=question.question_text,
                selected_option_id="from_history",
                selected_answer=answer,
                was_auto_answered=False,
                was_default=False,
                rationale=rationale or f"Previously answered (matched: {matched_q[:50]})",
                confidence=0.9,  # High confidence since it was user-answered before
            )
        
        return None

    def _parse_spec_review_response(self, raw: Any) -> SpecReviewResult:
        """Parse LLM response into SpecReviewResult.
        
        Applies deduplication and enforces max limits on issues/gaps to prevent
        runaway repetitive output from the LLM.
        """
        if not isinstance(raw, dict):
            return SpecReviewResult(summary="Spec review completed (no structured output)")

        raw_issues = raw.get("issues", [])
        raw_gaps = raw.get("gaps", [])
        raw_questions = raw.get("open_questions", [])

        # Deduplicate and limit issues/gaps to prevent repetitive LLM output
        issues = list(raw_issues) if isinstance(raw_issues, list) else []
        gaps = list(raw_gaps) if isinstance(raw_gaps, list) else []
        
        original_issue_count = len(issues)
        original_gap_count = len(gaps)
        
        issues = _dedupe_items(issues)[:MAX_ISSUES]
        gaps = _dedupe_items(gaps)[:MAX_GAPS]
        
        if len(issues) < original_issue_count or len(gaps) < original_gap_count:
            logger.info(
                "Deduplicated spec review results: issues %d->%d, gaps %d->%d",
                original_issue_count, len(issues),
                original_gap_count, len(gaps),
            )

        open_questions = []
        if isinstance(raw_questions, list):
            for i, q in enumerate(raw_questions):
                open_questions.append(self._parse_open_question(q, i))

        return SpecReviewResult(
            issues=issues,
            gaps=gaps,
            open_questions=open_questions,
            summary=str(raw.get("summary", "") or "Spec review complete"),
        )

    def _parse_open_question(self, q_data: Any, index: int) -> OpenQuestion:
        """Parse a single open question from LLM output."""
        if isinstance(q_data, dict):
            raw_options = q_data.get("options", [])
            options = []
            for i, opt in enumerate(raw_options):
                options.append(self._parse_question_option(opt, i))

            if options and not any(opt.is_default for opt in options):
                sorted_opts = sorted(options, key=lambda o: o.confidence, reverse=True)
                sorted_opts[0] = QuestionOption(
                    id=sorted_opts[0].id,
                    label=sorted_opts[0].label,
                    is_default=True,
                    rationale=sorted_opts[0].rationale,
                    confidence=sorted_opts[0].confidence,
                )
                options = sorted_opts

            raw_depends = q_data.get("depends_on")
            if isinstance(raw_depends, (list, tuple)):
                depends_on = str(raw_depends[0]) if raw_depends else None
            elif isinstance(raw_depends, str):
                depends_on = raw_depends
            else:
                depends_on = None

            return OpenQuestion(
                id=str(q_data.get("id", f"q{index}")),
                question_text=str(q_data.get("question_text", "")),
                context=str(q_data.get("context", "")),
                recommendation=str(q_data.get("recommendation", "") or ""),
                options=options,
                allow_multiple=bool(q_data.get("allow_multiple", False)),
                source=str(q_data.get("source", "spec_review")),
                category=str(q_data.get("category", "general")),
                priority=str(q_data.get("priority", "medium")),
                constraint_domain=str(q_data.get("constraint_domain", "")),
                constraint_layer=int(q_data.get("constraint_layer", 0) or 0),
                depends_on=depends_on,
                blocking=bool(q_data.get("blocking", True)),
                owner=str(q_data.get("owner", "user")),
                section_impact=list(q_data.get("section_impact", []) or []),
                due_date=str(q_data.get("due_date", "")),
                status=str(q_data.get("status", "open")),
                asked_via=list(q_data.get("asked_via", []) or []),
            )

        return OpenQuestion(
            id=f"q{index}",
            question_text=str(q_data),
            context="This question was identified during spec review.",
            recommendation="",
            options=[
                QuestionOption(
                    id="opt1", label="Yes", is_default=True, rationale="", confidence=0.5
                ),
                QuestionOption(
                    id="opt2", label="No", is_default=False, rationale="", confidence=0.5
                ),
            ],
            allow_multiple=False,
            source="spec_review",
            blocking=True,
            owner="user",
            section_impact=[],
            due_date="",
            status="open",
            asked_via=[],
        )

    def _run_context_constraints_discovery(
        self, spec_content: str, repo_path: Path
    ) -> List[OpenQuestion]:
        """Formulate context/constraint questions (project context, deployment, tenets, mandates).
        Uses LLM with CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT; on empty or invalid response
        returns a fixed fallback list.
        """
        spec_excerpt = (spec_content or "")[:4000]
        prompt = CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT.format(spec_excerpt=spec_excerpt)
        try:
            raw = self.llm.complete_text(prompt)
            if not raw or not raw.strip():
                return _context_discovery_fallback_questions()
            # Try to extract JSON (allow optional markdown code fence)
            text = raw.strip()
            parsed = None
            if "```" in text:
                for part in text.split("```"):
                    part = part.strip()
                    if part.lower().startswith("json"):
                        part = part[4:].strip()
                    if part.startswith("{"):
                        try:
                            parsed = json.loads(part)
                            break
                        except json.JSONDecodeError:
                            continue
            if parsed is None:
                parsed = json.loads(text)
            questions_data = parsed.get("open_questions") if isinstance(parsed, dict) else None
            if not questions_data or not isinstance(questions_data, list):
                return _context_discovery_fallback_questions()
            out: List[OpenQuestion] = []
            for i, q_data in enumerate(questions_data):
                q = self._parse_open_question(q_data, i)
                if q.source == "spec_review":
                    q = q.model_copy(update={"source": "context_discovery"})
                out.append(q)
            return out if out else _context_discovery_fallback_questions()
        except Exception as e:
            logger.warning(
                "Context constraints discovery LLM failed, using fallback: %s",
                str(e)[:200],
            )
            return _context_discovery_fallback_questions()

    def _inject_context_answers_into_spec(
        self,
        current_spec: str,
        answered_questions: List[AnsweredQuestion],
        repo_path: Path,
    ) -> str:
        """Build '## Project context and constraints' section from Q&A and prepend to current_spec."""
        if not answered_questions:
            return current_spec
        section = "## Project context and constraints\n\n"
        section += self._format_answered_questions(answered_questions)
        section += "\n\n---\n\n"
        return section + current_spec

    def _parse_question_option(self, opt_data: Any, index: int) -> QuestionOption:
        """Parse a single question option from LLM output."""
        if isinstance(opt_data, dict):
            return QuestionOption(
                id=str(opt_data.get("id", f"opt{index}")),
                label=str(opt_data.get("label", "")),
                is_default=bool(opt_data.get("is_default", False)),
                rationale=str(opt_data.get("rationale", "")),
                confidence=float(opt_data.get("confidence", 0.5)),
            )
        return QuestionOption(
            id=f"opt{index}",
            label=str(opt_data),
            is_default=index == 0,
            rationale="",
            confidence=0.5,
        )

    def _dedupe_questions_by_answer_similarity(
        self,
        open_questions: List[OpenQuestion],
        answered_questions: List[AnsweredQuestion],
    ) -> List[OpenQuestion]:
        """Drop open questions whose answer we already have.

        Compares answers (selected_answer from answered_questions) to the option
        labels of each open question. If any option of an open question is
        semantically the same as an answer we already have, we do not ask that
        question again. Preserves order of open_questions.
        """
        if not open_questions:
            return list(open_questions)

        def norm(t: str) -> str:
            return " ".join((t or "").lower().split()).strip()

        # Build set of existing answers (normalized) we already have
        existing_answers: List[str] = []
        for aq in answered_questions:
            s = norm(aq.selected_answer)
            if s:
                existing_answers.append(s)
            if getattr(aq, "other_text", None) and aq.other_text.strip():
                o = norm(aq.other_text)
                if o and o not in existing_answers:
                    existing_answers.append(o)

        if not existing_answers:
            return list(open_questions)

        # Same threshold as shared deduplication for "same meaning"
        SIMILARITY_THRESHOLD = 0.85
        kept: List[OpenQuestion] = []

        for q in open_questions:
            if not q.options:
                # No options: we cannot know what answer this would get; keep it
                kept.append(q)
                continue
            option_labels = [norm(opt.label) for opt in q.options if opt.label]
            if not option_labels:
                kept.append(q)
                continue
            # If any option is the same as an answer we already have, skip this question
            already_covered = False
            for opt_label in option_labels:
                if not opt_label:
                    continue
                for existing in existing_answers:
                    if SequenceMatcher(None, opt_label, existing).ratio() >= SIMILARITY_THRESHOLD:
                        logger.info(
                            "Skipping open question (answer already have): question_id=%s option=%r ~ existing=%r",
                            q.id,
                            opt_label[:50],
                            existing[:50],
                        )
                        already_covered = True
                        break
                if already_covered:
                    break
            if not already_covered:
                kept.append(q)

        return kept

    def _consolidate_open_questions(
        self, open_questions: List[OpenQuestion]
    ) -> List[OpenQuestion]:
        """Merge duplicate or semantically equivalent questions before sending to user.

        Uses a single LLM call to identify questions that ask the same thing
        (e.g. OAuth provider asked multiple ways) and consolidate them into
        one question per distinct decision, with merged options.
        """
        if len(open_questions) <= 1:
            return list(open_questions)

        questions_json = json.dumps(
            [
                {
                    "question_text": q.question_text,
                    "context": q.context,
                    "category": q.category,
                    "priority": q.priority,
                    "allow_multiple": q.allow_multiple,
                    "options": [
                        {
                            "id": o.id,
                            "label": o.label,
                            "is_default": o.is_default,
                            "rationale": o.rationale,
                            "confidence": o.confidence,
                        }
                        for o in q.options
                    ],
                }
                for q in open_questions
            ],
            indent=2,
        )
        prompt = CONSOLIDATE_QUESTIONS_PROMPT.format(questions_json=questions_json)
        try:
            raw = self.llm.complete_json(prompt, temperature=0.1)
            if not isinstance(raw, dict):
                return list(open_questions)
            consolidated = raw.get("consolidated_questions", [])
            if not isinstance(consolidated, list) or len(consolidated) == 0:
                return list(open_questions)
            result = []
            for i, q_data in enumerate(consolidated):
                result.append(self._parse_open_question(q_data, i))
            return result
        except Exception as e:
            logger.warning(
                "Question consolidation failed, using original list: %s",
                str(e)[:200],
            )
            return list(open_questions)

    def _review_question_answer_alignment(
        self, open_questions: List[OpenQuestion]
    ) -> List[OpenQuestion]:
        """Ensure each question and its options make sense together (e.g. no Yes/No for open-ended questions)."""
        if len(open_questions) == 0:
            return []
        questions_payload = [
            {
                "id": q.id,
                "question_text": q.question_text,
                "context": q.context,
                "category": q.category,
                "priority": q.priority,
                "allow_multiple": q.allow_multiple,
                "constraint_domain": q.constraint_domain,
                "constraint_layer": q.constraint_layer,
                "depends_on": q.depends_on,
                "blocking": q.blocking,
                "owner": q.owner,
                "section_impact": q.section_impact,
                "due_date": q.due_date,
                "status": q.status,
                "asked_via": q.asked_via,
                "options": [
                    {
                        "id": o.id,
                        "label": o.label,
                        "is_default": o.is_default,
                        "rationale": o.rationale,
                        "confidence": o.confidence,
                    }
                    for o in q.options
                ],
            }
            for q in open_questions
        ]
        questions_json = json.dumps(questions_payload, indent=2)
        prompt = REVIEW_QUESTIONS_ALIGNMENT_PROMPT.format(questions_json=questions_json)
        try:
            raw = self.llm.complete_json(prompt, temperature=0.1)
            if not isinstance(raw, dict):
                return list(open_questions)
            aligned = raw.get("aligned_questions", [])
            if not isinstance(aligned, list) or len(aligned) == 0:
                return list(open_questions)
            result = []
            for i, q_data in enumerate(aligned):
                result.append(self._parse_open_question(q_data, i))
            return result
        except Exception as e:
            logger.warning(
                "Question-answer alignment review failed, using original list: %s",
                str(e)[:200],
            )
            return list(open_questions)

    def _add_recommendations(
        self, open_questions: List[OpenQuestion], spec_content: str
    ) -> List[OpenQuestion]:
        """Add a short recommendation (which option and why) to each question."""
        if len(open_questions) == 0:
            return list(open_questions)
        questions_payload = [
            {
                "id": q.id,
                "question_text": q.question_text,
                "context": q.context,
                "options": [
                    {
                        "id": o.id,
                        "label": o.label,
                        "rationale": o.rationale,
                    }
                    for o in q.options
                ],
            }
            for q in open_questions
        ]
        questions_json = json.dumps(questions_payload, indent=2)
        spec_excerpt = (spec_content or "")[:15000]
        prompt = GENERATE_QUESTION_RECOMMENDATIONS_PROMPT.format(
            spec_excerpt=spec_excerpt,
            questions_json=questions_json,
        )
        try:
            raw = self.llm.complete_json(prompt, temperature=0.1)
            if not isinstance(raw, dict):
                return list(open_questions)
            recs = raw.get("recommendations", [])
            if not isinstance(recs, list):
                return list(open_questions)
            rec_by_id = {r.get("id"): str(r.get("recommendation", "") or "") for r in recs if isinstance(r, dict) and r.get("id")}
            result = []
            for q in open_questions:
                rec = rec_by_id.get(q.id, "")
                result.append(
                    q.model_copy(update={"recommendation": rec})
                )
            return result
        except Exception as e:
            logger.warning(
                "Recommendation generation failed, leaving recommendations empty: %s",
                str(e)[:200],
            )
            return list(open_questions)

    def _communicate_with_user(
        self,
        job_id: Optional[str],
        open_questions: List[OpenQuestion],
        repo_path: Path,
        iteration: int,
    ) -> List[AnsweredQuestion]:
        """Send questions to user and wait for response."""
        if not job_id:
            raise RuntimeError(
                "No job_id provided - cannot communicate with user for answers. "
                "A job_id is required to collect user input."
            )

        from software_engineering_team.shared.job_store import (
            add_pending_questions,
            get_submitted_answers,
            is_waiting_for_answers,
            update_job,
        )

        pending = self._convert_to_pending_questions(open_questions)
        add_pending_questions(job_id, pending)
        try:
            from unified_api.slack_notifier import notify_open_questions
            notify_open_questions(job_id, pending, source="product-analysis")
        except ImportError:
            pass

        update_job(
            job_id,
            waiting_for_answers=True,
            message=f"Waiting for answers to {len(open_questions)} question(s)",
        )

        logger.info(
            "Communicate with user: Sent %d questions, waiting for response",
            len(open_questions),
        )

        if not self._wait_for_answers(job_id):
            raise RuntimeError("Job was cancelled or failed while waiting for user answers")

        submitted = get_submitted_answers(job_id)
        answered = self._apply_answers(open_questions, submitted)

        update_job(job_id, waiting_for_answers=False)
        self._record_answers(repo_path, answered, iteration)

        return answered

    def _wait_for_answers(self, job_id: str) -> bool:
        """Wait indefinitely for user to submit answers."""
        from software_engineering_team.shared.job_store import get_job, is_waiting_for_answers

        while True:
            if not is_waiting_for_answers(job_id):
                return True

            job_data = get_job(job_id)
            if job_data and job_data.get("status") in ("failed", "completed", "cancelled"):
                return False

            time.sleep(OPEN_QUESTIONS_POLL_INTERVAL)

    def _convert_to_pending_questions(
        self,
        open_questions: List[OpenQuestion],
    ) -> List[Dict[str, Any]]:
        """Convert OpenQuestion models to pending question dicts for job store."""
        pending = []
        for q in open_questions:
            options = [
                {
                    "id": opt.id,
                    "label": opt.label,
                    "is_default": opt.is_default,
                    "rationale": opt.rationale,
                    "confidence": opt.confidence,
                }
                for opt in q.options
            ]
            if not options:
                options = [{"id": "other", "label": "Provide answer in text field"}]

            rec = getattr(q, "recommendation", None) or ""
            context_str = q.context + ("\n\nRecommendation: " + rec if rec else "")
            pending.append(
                {
                    "id": q.id,
                    "question_text": q.question_text,
                    "context": context_str,
                    "options": options,
                    "allow_multiple": q.allow_multiple,
                    "required": True,
                    "source": q.source,
                    "category": q.category,
                    "priority": q.priority,
                    "constraint_domain": q.constraint_domain,
                    "constraint_layer": q.constraint_layer,
                    "depends_on": q.depends_on,
                    "blocking": q.blocking,
                    "owner": q.owner,
                    "section_impact": q.section_impact,
                    "due_date": q.due_date,
                    "status": q.status,
                    "asked_via": q.asked_via,
                }
            )
        return pending

    def _apply_all_defaults(
        self,
        open_questions: List[OpenQuestion],
    ) -> List[AnsweredQuestion]:
        """Apply default answers to all questions."""
        answered = []
        for q in open_questions:
            default_opt = self._get_default_option(q)
            answered.append(
                AnsweredQuestion(
                    question_id=q.id,
                    question_text=q.question_text,
                    selected_option_id=default_opt.id if default_opt else "unknown",
                    selected_answer=default_opt.label
                    if default_opt
                    else "No default available",
                    was_default=True,
                    rationale=default_opt.rationale if default_opt else "",
                    confidence=default_opt.confidence if default_opt else 0.0,
                )
            )
        return answered

    def _apply_answers(
        self,
        open_questions: List[OpenQuestion],
        submitted: List[Dict[str, Any]],
    ) -> List[AnsweredQuestion]:
        """Merge submitted answers with defaults for unanswered questions."""
        submitted_by_id = {s.get("question_id"): s for s in submitted}
        answered = []

        for q in open_questions:
            sub = submitted_by_id.get(q.id)
            if sub:
                other_text = sub.get("other_text") or ""
                was_auto = sub.get("was_auto_answered", False)
                
                # Handle multi-select questions
                selected_ids = sub.get("selected_option_ids", [])
                selected_id = sub.get("selected_option_id", "")
                
                if selected_ids:
                    # Multi-select: build combined answer from all selected options
                    selected_labels = []
                    for opt_id in selected_ids:
                        if opt_id == "other" and other_text:
                            selected_labels.append(other_text)
                        else:
                            opt = next((o for o in q.options if o.id == opt_id), None)
                            if opt:
                                selected_labels.append(opt.label)
                    selected_answer = "; ".join(selected_labels) if selected_labels else "Unknown"
                    # Use first selected ID for backward compatibility
                    primary_selected_id = selected_ids[0] if selected_ids else ""
                else:
                    # Single-select: use the single selected option
                    selected_ids = [selected_id] if selected_id else []
                    primary_selected_id = selected_id
                    if selected_id == "other" and other_text:
                        selected_answer = other_text
                    else:
                        opt = next((o for o in q.options if o.id == selected_id), None)
                        selected_answer = opt.label if opt else other_text or "Unknown"

                answered.append(
                    AnsweredQuestion(
                        question_id=q.id,
                        question_text=q.question_text,
                        selected_option_id=primary_selected_id,
                        selected_option_ids=selected_ids,
                        selected_answer=selected_answer,
                        was_auto_answered=was_auto,
                        was_default=False,
                        rationale=sub.get("rationale") or "",
                        confidence=float(sub.get("confidence") or 0.0),
                        other_text=other_text,
                    )
                )
            else:
                default_opt = self._get_default_option(q)
                answered.append(
                    AnsweredQuestion(
                        question_id=q.id,
                        question_text=q.question_text,
                        selected_option_id=default_opt.id if default_opt else "unknown",
                        selected_option_ids=[default_opt.id] if default_opt else [],
                        selected_answer=default_opt.label
                        if default_opt
                        else "No default available",
                        was_default=True,
                        rationale=default_opt.rationale if default_opt else "",
                        confidence=default_opt.confidence if default_opt else 0.0,
                    )
                )

        return answered

    def _get_default_option(self, q: OpenQuestion) -> Optional[QuestionOption]:
        """Get the default option for a question."""
        default = next((opt for opt in q.options if opt.is_default), None)
        if default:
            return default

        if q.options:
            sorted_by_confidence = sorted(
                q.options, key=lambda o: o.confidence, reverse=True
            )
            return sorted_by_confidence[0]

        return None

    def _update_spec(
        self,
        current_spec: str,
        answered_questions: List[AnsweredQuestion],
        repo_path: Path,
        version: int,
    ) -> str:
        """Update the spec with answered questions. version is used for updated_spec_v{version}.md filename."""
        answered_text = self._format_answered_questions(answered_questions)

        prompt = SPEC_UPDATE_PROMPT.format(
            spec_content=current_spec,
            answered_questions=answered_text,
        )

        try:
            updated_spec = self.llm.complete_text(prompt)
        except Exception as e:
            logger.error("Failed to update spec with LLM: %s", e)
            return current_spec

        plan_dir = repo_path / "plan" / "product_analysis"
        plan_dir.mkdir(parents=True, exist_ok=True)

        spec_file = plan_dir / f"updated_spec_v{version}.md"
        spec_file.write_text(updated_spec, encoding="utf-8")
        logger.info("Saved updated spec to %s", spec_file)

        latest_file = plan_dir / "updated_spec.md"
        latest_file.write_text(updated_spec, encoding="utf-8")

        return updated_spec

    def _format_answered_questions(
        self,
        answered_questions: List[AnsweredQuestion],
    ) -> str:
        """Format answered questions for the LLM prompt."""
        lines = []
        for aq in answered_questions:
            lines.append(f"Q: {aq.question_text}")
            lines.append(f"A: {aq.selected_answer}")
            if aq.rationale:
                lines.append(f"Rationale: {aq.rationale}")
            if aq.was_auto_answered:
                lines.append(f"(Auto-answered with {aq.confidence:.0%} confidence)")
            elif aq.was_default:
                lines.append("(Default applied)")
            lines.append("")
        return "\n".join(lines)


    def _build_specialist_collaboration_plan(
        self,
        cleaned_spec: str,
        answered_questions: List[AnsweredQuestion],
    ) -> str:
        """Build deterministic recommendations for specialist agents/tooling.

        This gives the PRD writer concrete handoff guidance for areas that often
        require cross-team collaboration (UX, architecture, risk, data, security).
        """
        spec_text = (cleaned_spec + "\n" + self._format_answered_questions(answered_questions)).lower()

        recommendations: List[str] = []

        def include(label: str, reason: str) -> None:
            recommendations.append(f"- {label}: {reason}")

        # Always include these core spokes for higher-quality PRDs.
        include("Requirements Analyst Agent", "Own FR/NFR decomposition, prioritization, and traceability mapping.")
        include("QA and Acceptance Criteria Agent", "Ensure every Must requirement has verifiable acceptance criteria.")
        include("PRD Critic (Gatekeeper) Agent", "Run completeness/consistency/testability/traceability/pragmatism gates before Final.")

        if any(k in spec_text for k in ["ui", "ux", "screen", "design", "workflow", "journey", "persona", "onboarding"]):
            include("UX and Flows Agent", "Define textual workflows, edge cases, accessibility baseline, and screen/IA notes.")
            include("Design System Tool Agent", "Capture reusable component patterns, interaction states, and consistency rules.")
            include("Branding Guidance Agent", "Document tone, visual direction, and brand constraints for product surfaces.")

        if any(k in spec_text for k in ["architecture", "api", "integration", "service", "event", "database", "deployment"]):
            include("Architecture Agent", "Define high-level components, interfaces, and data flow boundaries.")
            include("API and Integration Agent", "Specify integration contracts, failure modes, and auth patterns.")

        if any(k in spec_text for k in ["risk", "assumption", "dependency", "migration", "rollout", "timeline"]):
            include("Risk Analysis Agent", "Maintain risk register with owners, probabilities, impacts, and mitigations.")
            include("Scope and Milestones Planner Agent", "Align MVP/V1/VNext scope to dependencies and timeline options.")

        if any(k in spec_text for k in ["security", "privacy", "compliance", "pii", "retention", "audit", "auth"]):
            include("Security, Privacy, and Compliance Agent", "Define data handling, retention, authz, and compliance questions.")

        if any(k in spec_text for k in ["analytics", "kpi", "metric", "dashboard", "event tracking"]):
            include("Data and Analytics Agent", "Define events, KPI ownership, and dashboards tied to goals.")

        include("Question Concierge (Human Interface) Agent", "Bundle unresolved questions by owner/impact with due dates and escalation policy.")

        # Keep deterministic output order and avoid duplicates.
        seen = set()
        deduped: List[str] = []
        for item in recommendations:
            if item not in seen:
                seen.add(item)
                deduped.append(item)

        return "\n".join(deduped)

    def _generate_prd_document(
        self,
        cleaned_spec: str,
        answered_questions: List[AnsweredQuestion],
    ) -> str:
        """Generate a Product Requirements Document (PRD) from the spec and answers.

        Uses the cleaned, validated spec as the base and integrates resolved answers
        (including constraint decisions) into a structured PRD suitable for Planning V2.
        """
        # Summarize answered questions for the prompt; this may be empty on the first run
        answered_summary = self._format_answered_questions(answered_questions)

        # Keep prompt size reasonable while fitting within model context (e.g. 256K)
        max_chars = compute_prd_snippet_chars(self.llm)
        cleaned_spec_snippet = cleaned_spec[:max_chars]
        answered_summary_snippet = answered_summary[:max_chars]
        specialist_plan = self._build_specialist_collaboration_plan(
            cleaned_spec=cleaned_spec_snippet,
            answered_questions=answered_questions,
        )
        specialist_plan_snippet = specialist_plan[:max_chars]

        prompt = PRD_PROMPT.format(
            cleaned_spec=cleaned_spec_snippet,
            answered_questions_summary=answered_summary_snippet,
            specialist_collaboration_plan=specialist_plan_snippet,
        )

        try:
            prd_content = self.llm.complete_text(prompt)
        except Exception as e:
            logger.error("Failed to generate PRD with LLM: %s", e)
            return cleaned_spec

        if not isinstance(prd_content, str) or not prd_content.strip():
            logger.warning(
                "Product Requirements Analysis: PRD generation returned empty output, "
                "falling back to cleaned specification"
            )
            return cleaned_spec

        return prd_content

    def _update_spec_from_duplicates(
        self,
        duplicate_questions: List[OpenQuestion],
        qa_history: str,
        current_spec: str,
        repo_path: Path,
        version: int,
    ) -> str:
        """Update spec using answers from qa_history for duplicate questions.
        
        When a question is re-asked but was previously answered, this indicates
        the spec wasn't updated clearly enough. This method extracts the existing
        answers and re-applies them with emphasis on clarity.
        
        Args:
            duplicate_questions: Questions that were filtered as duplicates.
            qa_history: Raw content of qa_history.md file.
            current_spec: Current specification content.
            repo_path: Path to the repository.
            version: Version number for updated_spec_v{version}.md filename.
            
        Returns:
            Updated specification content.
        """
        from .prompts import SPEC_CLARIFICATION_PROMPT
        
        # Extract answers from qa_history for each duplicate
        extracted_answers: List[AnsweredQuestion] = []
        for q in duplicate_questions:
            answer = self._extract_answer_from_qa_history(q, qa_history)
            if answer:
                extracted_answers.append(answer)
        
        if not extracted_answers:
            logger.debug("No answers extracted from qa_history for duplicates")
            return current_spec
        
        logger.info(
            "Clarifying spec with %d previously answered questions that were re-asked",
            len(extracted_answers),
        )
        
        # Format the Q&A pairs for the clarification prompt
        qa_pairs = self._format_answered_questions(extracted_answers)
        
        prompt = SPEC_CLARIFICATION_PROMPT.format(
            spec_content=current_spec,
            duplicate_qa_pairs=qa_pairs,
        )
        
        try:
            clarified_spec = self.llm.complete_text(prompt)
        except Exception as e:
            logger.error("Failed to clarify spec with LLM: %s", e)
            return current_spec
        
        # Save the clarified spec using the same versioned pattern as _update_spec
        plan_dir = repo_path / "plan" / "product_analysis"
        plan_dir.mkdir(parents=True, exist_ok=True)
        
        spec_file = plan_dir / f"updated_spec_v{version}.md"
        spec_file.write_text(clarified_spec, encoding="utf-8")
        logger.info("Saved updated spec (clarification) to %s", spec_file)
        
        latest_file = plan_dir / "updated_spec.md"
        latest_file.write_text(clarified_spec, encoding="utf-8")
        
        return clarified_spec

    def _update_spec_for_consistency_and_clarity(
        self,
        current_spec: str,
        repo_path: Path,
        qa_history: str,
        all_answered_questions: List[AnsweredQuestion],
        version: int,
        consistency_loop: int,
    ) -> str:
        """Update spec for clarity and consistency; use QA as source of truth for conflicts.

        Called when deduplication reduces questions by 50%+ so the spec is edited to
        clarify answers and resolve conflicting information, then re-reviewed.
        """
        qa_source = qa_history.strip() if qa_history else ""
        if all_answered_questions:
            formatted = self._format_answered_questions(all_answered_questions)
            qa_source = (qa_source + "\n\n" + formatted).strip() if qa_source else formatted
        if not qa_source:
            qa_source = "(No prior Q&A yet; focus on removing internal conflicts and clarifying ambiguous wording.)"

        prompt = SPEC_CONSISTENCY_CLARIFICATION_PROMPT.format(
            spec_content=current_spec,
            qa_source=qa_source,
        )
        try:
            updated_spec = self.llm.complete_text(prompt)
        except Exception as e:
            logger.error("Failed to update spec for consistency with LLM: %s", e)
            return current_spec

        plan_dir = repo_path / "plan" / "product_analysis"
        plan_dir.mkdir(parents=True, exist_ok=True)
        spec_file = plan_dir / f"updated_spec_consistency_v{version}_loop{consistency_loop}.md"
        spec_file.write_text(updated_spec, encoding="utf-8")
        logger.info("Saved consistency-updated spec to %s", spec_file.name)
        latest_file = plan_dir / "updated_spec.md"
        latest_file.write_text(updated_spec, encoding="utf-8")
        return updated_spec

    def _parse_qa_history_blocks(
        self, qa_history: str
    ) -> List[Tuple[int, str, str, str]]:
        """Parse qa_history.md content into blocks for pruning and rewriting.

        Returns:
            List of (iteration, question_text, answer, full_block_text).
        """
        if not qa_history or not qa_history.strip():
            return []
        blocks_out: List[Tuple[int, str, str, str]] = []
        current_iteration = 1
        lines = qa_history.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            iter_match = re.match(r"^##\s+Iteration\s+(\d+)", line.strip())
            if iter_match:
                current_iteration = int(iter_match.group(1))
                i += 1
                continue
            block_match = re.match(r"^###\s+(.*)$", line)
            if block_match:
                question_text = block_match.group(1).strip()
                answer = ""
                rationale = ""
                block_lines = [line]
                i += 1
                while i < len(lines):
                    next_line = lines[i]
                    if next_line.strip().startswith("### ") or re.match(
                        r"^##\s+Iteration", next_line.strip()
                    ):
                        break
                    block_lines.append(next_line)
                    if next_line.strip().startswith("**Answer:**"):
                        answer = next_line.replace("**Answer:**", "").strip()
                    elif next_line.strip().startswith("**Rationale:**"):
                        rationale = next_line.replace("**Rationale:**", "").strip()
                    i += 1
                full_block_text = "\n".join(block_lines)
                if question_text or answer:
                    blocks_out.append(
                        (current_iteration, question_text, answer, full_block_text)
                    )
                continue
            i += 1
        return blocks_out

    def _is_same_decision(self, existing_question: str, new_question: str) -> bool:
        """Return True if the two questions are about the same decision (new answer supersedes old)."""
        if not existing_question.strip() or not new_question.strip():
            return False
        existing_norm = " ".join(existing_question.lower().split())
        new_norm = " ".join(new_question.lower().split())
        if existing_norm in new_norm or new_norm in existing_norm:
            return True
        # Word overlap ratio
        def words(t: str) -> set:
            return set(re.sub(r"[^\w\s]", " ", t.lower()).split()) - {"", "the", "a", "an"}

        existing_w = words(existing_question)
        new_w = words(new_question)
        if not existing_w or not new_w:
            return False
        overlap = len(existing_w & new_w) / max(len(existing_w), len(new_w))
        return overlap >= 0.5

    def _record_answers(
        self,
        repo_path: Path,
        answered_questions: List[AnsweredQuestion],
        iteration: int,
    ) -> None:
        """Save answered questions to plan/product_analysis/qa_history.md.

        Removes any existing qa_history entry that is the same decision as a new
        answer (new directive replaces old); then writes pruned history + new iteration.
        """
        plan_dir = repo_path / "plan" / "product_analysis"
        plan_dir.mkdir(parents=True, exist_ok=True)
        qa_file = plan_dir / "qa_history.md"

        # New iteration section (same format as before)
        new_section = f"\n## Iteration {iteration}\n\n"
        for aq in answered_questions:
            new_section += f"### {aq.question_text}\n"
            new_section += f"**Answer:** {aq.selected_answer}\n"
            if aq.rationale:
                new_section += f"**Rationale:** {aq.rationale}\n"
            if aq.was_auto_answered:
                new_section += f"*Auto-answered with {aq.confidence:.0%} confidence*\n"
            elif aq.was_default:
                new_section += "*(Default applied)*\n"
            if aq.other_text:
                new_section += f"*Custom text:* {aq.other_text}\n"
            new_section += "\n"

        if not qa_file.exists():
            content = (
                "# Q&A History\n\n"
                "This file records all questions and answers from Product Requirements Analysis.\n"
                + new_section
            )
            with open(qa_file, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info("Recorded %d answers to %s", len(answered_questions), qa_file)
            return

        existing_content = qa_file.read_text(encoding="utf-8")
        blocks = self._parse_qa_history_blocks(existing_content)
        remove_indices: set = set()
        for aq in answered_questions:
            for idx, (_, block_question, _, _) in enumerate(blocks):
                if self._is_same_decision(block_question, aq.question_text):
                    remove_indices.add(idx)
        kept_blocks = [
            (it, qt, ans, full)
            for idx, (it, qt, ans, full) in enumerate(blocks)
            if idx not in remove_indices
        ]
        header = (
            "# Q&A History\n\n"
            "This file records all questions and answers from Product Requirements Analysis.\n"
        )
        parts = [header]
        current_iter: Optional[int] = None
        for it, _qt, _ans, full_block_text in kept_blocks:
            if current_iter != it:
                current_iter = it
                parts.append(f"\n## Iteration {it}\n\n")
            parts.append(full_block_text)
            if not full_block_text.endswith("\n"):
                parts.append("\n")
        parts.append(new_section)
        content = "".join(parts)
        with open(qa_file, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("Recorded %d answers to %s", len(answered_questions), qa_file)

    def _run_spec_cleanup(
        self,
        spec_content: str,
        repo_path: Path,
        on_chunk_progress: Optional[Callable[[int, int], None]] = None,
    ) -> SpecCleanupResult:
        """Run the Spec Cleanup phase to validate and clean the spec."""
        prompt = SPEC_CLEANUP_PROMPT.format(spec_content=spec_content)

        raw = parse_json_with_recovery(
            llm=self.llm,
            prompt=prompt,
            agent_name="PRA_spec_cleanup",
            decompose_fn=default_decompose_by_sections,
            merge_fn=self._merge_spec_cleanup_results,
            original_content=spec_content,
            chunk_prompt_template=SPEC_CLEANUP_CHUNK_PROMPT,
            on_chunk_progress=on_chunk_progress,
        )

        if not raw:
            # All recovery failed - return the original spec as valid
            logger.warning(
                "PRA spec_cleanup: No JSON recovered, returning original spec"
            )
            return SpecCleanupResult(
                is_valid=True,
                cleaned_spec=spec_content,
                summary="Spec cleanup skipped - JSON parsing failed",
            )

        return self._parse_spec_cleanup_response(raw, spec_content)

    def _parse_spec_cleanup_response(
        self,
        raw: Any,
        fallback_spec: str,
    ) -> SpecCleanupResult:
        """Parse LLM response into SpecCleanupResult."""
        if not isinstance(raw, dict):
            return SpecCleanupResult(
                is_valid=True,
                cleaned_spec=fallback_spec,
                summary="Spec cleanup completed (no structured output)",
            )

        return SpecCleanupResult(
            is_valid=bool(raw.get("is_valid", True)),
            validation_issues=list(raw.get("validation_issues", []))
            if isinstance(raw.get("validation_issues"), list)
            else [],
            cleaned_spec=str(raw.get("cleaned_spec", fallback_spec)),
            summary=str(raw.get("summary", "Spec cleanup complete")),
        )
