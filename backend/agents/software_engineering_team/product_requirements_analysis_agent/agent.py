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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

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
    SPEC_CLEANUP_CHUNK_PROMPT,
    SPEC_CLEANUP_PROMPT,
    SPEC_REVIEW_CHUNK_PROMPT,
    SPEC_REVIEW_PROMPT,
    SPEC_UPDATE_PROMPT,
    PRD_PROMPT,
)
from planning_v2_team.tool_agents.json_utils import (
    parse_json_with_recovery,
    default_decompose_by_sections,
)
from software_engineering_team.shared.deduplication import dedupe_strings as _dedupe_items

if TYPE_CHECKING:
    from software_engineering_team.shared.llm import LLMClient

logger = logging.getLogger(__name__)

OPEN_QUESTIONS_POLL_INTERVAL = 5.0
MAX_ITERATIONS = 100
MAX_DECOMPOSITION_DEPTH = 20
MAX_ISSUES = 10
MAX_GAPS = 10

# Subdirectory under repo where PRA writes all artifacts (validated_spec, PRD, updated_spec*, qa_history).
PRODUCT_ANALYSIS_SUBDIR = "plan/product_analysis"


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
                _update_job(status_text="Performing gap analysis on the specification")
                spec_review_result, current_spec = self._run_spec_review(
                    current_spec,
                    repo_path,
                    iteration=iteration,
                    spec_version=base_version + (iteration - 1),
                    answered_questions=all_answered_questions,
                )
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
            consolidated_questions = self._consolidate_open_questions(
                spec_review_result.open_questions
            )
            if len(consolidated_questions) < original_count:
                logger.info(
                    "Consolidated open questions: %d -> %d",
                    original_count,
                    len(consolidated_questions),
                )
            spec_review_result = spec_review_result.model_copy(
                update={"open_questions": consolidated_questions}
            )
            open_count = len(spec_review_result.open_questions)

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
            cleanup_result = self._run_spec_cleanup(current_spec, repo_path)
            result.spec_cleanup_result = cleanup_result
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
    ) -> tuple[SpecReviewResult, str]:
        """Run the Spec Review phase to identify gaps and questions.
        
        Args:
            spec_content: Current specification content.
            repo_path: Path to the repository.
            iteration: Current iteration number (for logging/qa_history).
            spec_version: Version number for updated_spec_vN.md when writing (e.g. from duplicates). If None, iteration is used.
            answered_questions: List of previously answered questions for constraint analysis.
            
        Returns:
            Tuple of (SpecReviewResult, updated_spec_content). The spec may be
            updated if duplicate questions were found and clarified.
        """
        if spec_version is None:
            spec_version = iteration
        # Read previously answered questions to avoid asking duplicates
        qa_history = self._read_qa_history(repo_path)

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

        if qa_history:
            prompt = SPEC_REVIEW_PROMPT.format(
                spec_content=full_spec_content[:20000],
                constraint_hints=constraint_hints,
            )
            prompt += f"""

IMPORTANT: The following questions have ALREADY been answered. Do NOT ask these questions again or any variations of them. Only ask NEW questions about topics NOT covered below:

Previously Answered Questions:
---
{qa_history}
---
"""
        else:
            prompt = SPEC_REVIEW_PROMPT.format(
                spec_content=full_spec_content[:20000],
                constraint_hints=constraint_hints,
            )

        raw = parse_json_with_recovery(
            llm=self.llm,
            prompt=prompt,
            agent_name="PRA_spec_review",
            decompose_fn=default_decompose_by_sections,
            merge_fn=self._merge_spec_review_results,
            original_content=spec_content,
            chunk_prompt_template=SPEC_REVIEW_CHUNK_PROMPT,
        )

        if not raw:
            # All recovery failed - return a result indicating retry is needed
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

        # Filter out any questions that are duplicates of previously answered ones
        if qa_history and result.open_questions:
            filtered, duplicates = self._filter_duplicate_questions(
                result.open_questions, qa_history
            )
            result.open_questions = filtered
            
            # If duplicates found, update spec with their existing answers
            # This fills gaps that caused questions to be re-asked
            if duplicates:
                logger.info(
                    "Found %d duplicate questions - clarifying spec with existing answers",
                    len(duplicates),
                )
                updated_spec = self._update_spec_from_duplicates(
                    duplicates, qa_history, spec_content, repo_path, spec_version
                )

        return result, updated_spec

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
        
        Returns:
            Tuple of (filtered_questions, duplicate_questions).
            - filtered_questions: Questions that are NOT duplicates (should be asked)
            - duplicate_questions: Questions that ARE duplicates (already answered)
        """
        qa_history_lower = qa_history.lower()
        filtered = []
        duplicates = []
        
        for q in new_questions:
            q_text_lower = q.question_text.lower()
            
            # Check for exact or near-exact matches in qa_history
            # Extract key phrases from the question (simplified heuristic)
            key_words = [w for w in q_text_lower.split() if len(w) > 4]
            
            # If most key words appear in qa_history, likely a duplicate
            if key_words:
                matches = sum(1 for w in key_words if w in qa_history_lower)
                match_ratio = matches / len(key_words)
                
                if match_ratio > 0.6:
                    logger.debug(
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

            return OpenQuestion(
                id=str(q_data.get("id", f"q{index}")),
                question_text=str(q_data.get("question_text", "")),
                context=str(q_data.get("context", "")),
                options=options,
                allow_multiple=bool(q_data.get("allow_multiple", False)),
                source="spec_review",
                category=str(q_data.get("category", "general")),
                priority=str(q_data.get("priority", "medium")),
                constraint_domain=str(q_data.get("constraint_domain", "")),
                constraint_layer=int(q_data.get("constraint_layer", 0) or 0),
                depends_on=q_data.get("depends_on"),
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

            pending.append(
                {
                    "id": q.id,
                    "question_text": q.question_text,
                    "context": q.context,
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

        # Keep prompt size reasonable while still providing enough context
        max_chars = 20000
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

    def _record_answers(
        self,
        repo_path: Path,
        answered_questions: List[AnsweredQuestion],
        iteration: int,
    ) -> None:
        """Save answered questions to plan/product_analysis/qa_history.md."""
        plan_dir = repo_path / "plan" / "product_analysis"
        plan_dir.mkdir(parents=True, exist_ok=True)

        qa_file = plan_dir / "qa_history.md"

        content = f"\n## Iteration {iteration}\n\n"
        for aq in answered_questions:
            content += f"### {aq.question_text}\n"
            content += f"**Answer:** {aq.selected_answer}\n"
            if aq.rationale:
                content += f"**Rationale:** {aq.rationale}\n"
            if aq.was_auto_answered:
                content += f"*Auto-answered with {aq.confidence:.0%} confidence*\n"
            elif aq.was_default:
                content += "*(Default applied)*\n"
            if aq.other_text:
                content += f"*Custom text:* {aq.other_text}\n"
            content += "\n"

        mode = "a" if qa_file.exists() else "w"
        if mode == "w":
            content = (
                "# Q&A History\n\n"
                "This file records all questions and answers from Product Requirements Analysis.\n"
                + content
            )

        with open(qa_file, mode, encoding="utf-8") as f:
            f.write(content)

        logger.info("Recorded %d answers to %s", len(answered_questions), qa_file)

    def _run_spec_cleanup(
        self,
        spec_content: str,
        repo_path: Path,
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
