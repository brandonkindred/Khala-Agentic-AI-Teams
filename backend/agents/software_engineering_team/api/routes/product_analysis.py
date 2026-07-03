"""SE team API — product-analysis (PRA) run, start-from-spec, status, answers, auto-answer and jobs routes.

Route handlers register on a module-local ``APIRouter`` that ``main`` mounts with
``app.include_router``; absolute paths are unchanged. Monkeypatched collaborators
(background runners, ``SUPERVISOR_LOG_DIR``) are dereferenced through the ``main``
module object at call time so ``monkeypatch.setattr(main, ...)`` still takes effect.
"""

import logging
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from spec_parser import SPEC_FILENAME

from software_engineering_team.api import main as _main
from software_engineering_team.api.models import (
    AutoAnswerRequest,
    AutoAnswerResponse,
    PendingQuestion,
    ProductAnalysisRunRequest,
    ProductAnalysisRunResponse,
    ProductAnalysisStatusResponse,
    QuestionOption,
    RunningJobsResponse,
    RunningJobSummary,
    StartFromSpecRequest,
    SubmitAnswersRequest,
)
from software_engineering_team.api.state import (
    PROJECT_NAME_PATTERN,
    _get_projects_root,
    _get_spec_content_for_job,
    _real_question_options,
)
from software_engineering_team.shared.job_store import (
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    create_job,
    get_job,
    list_jobs,
    start_job_heartbeat_thread,
    update_job,
)
from software_engineering_team.shared.job_store import submit_answers as store_submit_answers

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/product-analysis/run",
    response_model=ProductAnalysisRunResponse,
    summary="Start Product Requirements Analysis",
    description="Analyze product specification for completeness, identify gaps, and generate questions. "
    "If spec_content is omitted, the newest spec file (name contains '_spec') is loaded by modification time from plan/product_analysis/, plan/, or root. "
    "If the agent needs more detail and the input was validated_spec.md, it is renamed to updated_spec_vN and updates use subsequent versions. "
    "Returns job_id immediately. Poll GET /product-analysis/status/{job_id} for progress.",
)
def run_product_analysis(request: ProductAnalysisRunRequest) -> ProductAnalysisRunResponse:
    """Start the Product Requirements Analysis workflow."""
    repo = Path(request.repo_path)
    if not repo.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"repo_path does not exist or is not a directory: {request.repo_path}",
        )

    spec_content = request.spec_content
    initial_spec_path = None
    if not spec_content:
        try:
            from spec_parser import get_newest_spec_content, get_newest_spec_path

            initial_spec_path = get_newest_spec_path(repo)
            spec_content = get_newest_spec_content(repo)
        except FileNotFoundError as e:
            raise HTTPException(
                status_code=400,
                detail=f"No spec file found. {e}. Provide spec_content or add a spec file (e.g. initial_spec.md, plan/validated_spec.md).",
            ) from e

    job_id = str(uuid.uuid4())
    create_job(job_id, request.repo_path, job_type="product_analysis")

    initial_spec_path_str = str(initial_spec_path) if initial_spec_path else None
    try:  # pragma: no cover  # integration-only: spawns Temporal workflow or PRA worker thread
        from software_engineering_team.temporal.client import is_temporal_enabled
        from software_engineering_team.temporal.constants import STANDALONE_TYPE_PRODUCT_ANALYSIS
        from software_engineering_team.temporal.start_workflow import start_standalone_workflow

        if is_temporal_enabled():
            start_standalone_workflow(
                STANDALONE_TYPE_PRODUCT_ANALYSIS,
                job_id,
                request.repo_path,
                spec_content=spec_content,
                initial_spec_path=initial_spec_path_str,
            )
        else:
            thread = threading.Thread(
                target=_main._run_product_analysis_background,
                args=(job_id, request.repo_path, spec_content, initial_spec_path),
            )
            thread.daemon = True
            thread.start()
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start product-analysis workflow")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return ProductAnalysisRunResponse(
        job_id=job_id,
        status="running",
        message="Product analysis started. Poll GET /product-analysis/status/{job_id} for progress.",
    )


# Project name: no spaces, only letters, numbers, hyphen, underscore


@router.post(
    "/product-analysis/start-from-spec",
    response_model=ProductAnalysisRunResponse,
    summary="Create project from spec and start PRA",
    description="Create a new project directory with the given name, write the spec content as initial_spec.md, "
    "then start the Product Requirements Analysis workflow. Returns job_id; poll GET /product-analysis/status/{job_id}. "
    "project_name must contain no spaces and only letters, numbers, hyphens, and underscores.",
)
def start_product_analysis_from_spec(request: StartFromSpecRequest) -> ProductAnalysisRunResponse:
    """Create a project from uploaded spec content and start PRA."""
    if not PROJECT_NAME_PATTERN.match(request.project_name):
        raise HTTPException(
            status_code=400,
            detail="project_name must contain no spaces and only letters, numbers, hyphens, and underscores.",
        )

    projects_root = _get_projects_root()
    project_dir = projects_root / request.project_name
    if project_dir.exists():
        raise HTTPException(status_code=400, detail="Project already exists.")

    project_dir.mkdir(parents=True, exist_ok=False)
    spec_path = project_dir / SPEC_FILENAME
    spec_path.write_text(request.spec_content, encoding="utf-8")
    initial_spec_path_str = str(spec_path)
    repo_path_str = str(project_dir)
    spec_content = request.spec_content

    job_id = str(uuid.uuid4())
    create_job(job_id, repo_path_str, job_type="product_analysis")

    try:  # pragma: no cover  # integration-only: spawns Temporal workflow or PRA worker thread
        from software_engineering_team.temporal.client import is_temporal_enabled
        from software_engineering_team.temporal.constants import STANDALONE_TYPE_PRODUCT_ANALYSIS
        from software_engineering_team.temporal.start_workflow import start_standalone_workflow

        if is_temporal_enabled():
            start_standalone_workflow(
                STANDALONE_TYPE_PRODUCT_ANALYSIS,
                job_id,
                repo_path_str,
                spec_content=spec_content,
                initial_spec_path=initial_spec_path_str,
            )
        else:
            thread = threading.Thread(
                target=_main._run_product_analysis_background,
                args=(job_id, repo_path_str, spec_content, initial_spec_path_str),
            )
            thread.daemon = True
            thread.start()
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Failed to start product-analysis workflow from spec")
        update_job(job_id, error=str(e), status=JOB_STATUS_FAILED)
        raise HTTPException(status_code=503, detail=str(e)) from e

    start_job_heartbeat_thread(job_id)

    return ProductAnalysisRunResponse(
        job_id=job_id,
        status="running",
        message="Project created and product analysis started. Poll GET /product-analysis/status/{job_id} for progress.",
    )


@router.get(
    "/product-analysis/status/{job_id}",
    response_model=ProductAnalysisStatusResponse,
    summary="Get Product Analysis job status",
    description="Returns current phase, progress, pending questions, and completion status.",
)
def get_product_analysis_status(job_id: str) -> ProductAnalysisStatusResponse:
    """Get the status of a Product Analysis job."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    pending_questions_raw = data.get("pending_questions", [])
    pending_questions = [
        PendingQuestion(
            id=q.get("id", ""),
            question_text=q.get("question_text", ""),
            context=q.get("context"),
            recommendation=q.get("recommendation"),
            options=[
                QuestionOption(
                    id=opt.get("id", ""),
                    label=opt.get("label", ""),
                    is_default=opt.get("is_default", False),
                    rationale=opt.get("rationale"),
                    confidence=opt.get("confidence"),
                )
                for opt in q.get("options", [])
            ],
            required=q.get("required", False),
            source=q.get("source", "spec_review"),
        )
        for q in pending_questions_raw
    ]

    return ProductAnalysisStatusResponse(
        job_id=job_id,
        status=data.get("status", JOB_STATUS_PENDING),
        repo_path=data.get("repo_path"),
        current_phase=data.get("current_phase"),
        status_text=data.get("status_text"),
        progress=data.get("progress", 0),
        iterations=data.get("iterations", 0),
        pending_questions=pending_questions,
        waiting_for_answers=data.get("waiting_for_answers", False),
        error=data.get("error"),
        summary=data.get("summary"),
        validated_spec_path=data.get("validated_spec_path"),
    )


@router.post(
    "/product-analysis/{job_id}/answers",
    response_model=ProductAnalysisStatusResponse,
    summary="Submit answers to Product Analysis open questions",
    description="Submit user answers to open questions identified during spec review.",
)
def submit_product_analysis_answers(
    job_id: str, request: SubmitAnswersRequest
) -> ProductAnalysisStatusResponse:
    """Submit answers to open questions and resume Product Analysis workflow."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("job_type") != "product_analysis":
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for product-analysis jobs.",
        )

    if not data.get("waiting_for_answers"):
        raise HTTPException(
            status_code=400,
            detail="Job is not waiting for answers.",
        )

    pending_questions = data.get("pending_questions", [])
    if not pending_questions:
        raise HTTPException(status_code=400, detail="No pending questions to answer.")

    pending_ids = {q["id"] for q in pending_questions}
    required_ids = {q["id"] for q in pending_questions if q.get("required", True)}
    answered_ids = {a.question_id for a in request.answers}

    missing_required = required_ids - answered_ids
    if missing_required:
        raise HTTPException(
            status_code=400,
            detail=f"Missing answers for required questions: {', '.join(sorted(missing_required))}",
        )

    invalid_ids = answered_ids - pending_ids
    if invalid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown question IDs: {', '.join(sorted(invalid_ids))}",
        )

    answers_dicts = [
        {
            "question_id": a.question_id,
            "selected_option_id": a.selected_option_id,
            "other_text": a.other_text,
        }
        for a in request.answers
    ]
    store_submit_answers(job_id, answers_dicts)

    return get_product_analysis_status(job_id)


@router.post(
    "/product-analysis/{job_id}/auto-answer/{question_id}",
    response_model=AutoAnswerResponse,
    summary="Auto-answer a pending question for Product Analysis job",
    description="Use LLM to automatically answer a pending question based on industry best practices.",
)
def auto_answer_product_analysis_question(
    job_id: str,
    question_id: str,
    request: Optional[AutoAnswerRequest] = None,
) -> AutoAnswerResponse:
    """Auto-answer a pending question using LLM analysis."""
    data = get_job(job_id)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if data.get("job_type") != "product_analysis":
        raise HTTPException(
            status_code=400,
            detail="This endpoint is only for product-analysis jobs.",
        )

    pending_questions = data.get("pending_questions", [])
    question_data = next((q for q in pending_questions if q.get("id") == question_id), None)
    if not question_data:
        raise HTTPException(
            status_code=404,
            detail=f"Question {question_id} not found in pending questions.",
        )

    real_options = _real_question_options(question_data)
    if not real_options:
        raise HTTPException(
            status_code=422,
            detail="This question has no selectable options. Provide a free-text answer via the /answers endpoint using the other_text field.",
        )

    spec_content = _get_spec_content_for_job(data)
    additional_context = request.spec_context if request else None

    try:  # pragma: no cover  # integration-only: runs PRA's LLM auto-answer pipeline
        from product_requirements_analysis_agent import get_auto_answer_for_job

        from llm_service import get_client

        llm = get_client("backend")
        result = get_auto_answer_for_job(
            llm=llm,
            job_id=job_id,
            question_id=question_id,
            spec_content=spec_content,
            additional_context=additional_context,
        )

        if not result:
            raise HTTPException(
                status_code=500,
                detail="Auto-answer failed to produce a result.",
            )

        return AutoAnswerResponse(
            question_id=result.question_id,
            selected_option_id=result.selected_option_id,
            selected_answer=result.selected_answer,
            rationale=result.rationale,
            confidence=result.confidence,
            risks=result.risks,
            applied=False,
        )
    except (
        ImportError
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer module not available: {e}",
        )
    except (
        Exception
    ) as e:  # pragma: no cover  # integration-only: paired with integration-only try block
        logger.exception("Auto-answer failed")
        raise HTTPException(
            status_code=500,
            detail=f"Auto-answer failed: {e}",
        )


@router.get(
    "/product-analysis/jobs",
    response_model=RunningJobsResponse,
    summary="List Product Analysis jobs",
    description="Returns all product-analysis jobs with status pending or running.",
)
def get_product_analysis_jobs() -> RunningJobsResponse:
    """List running and pending product-analysis jobs."""
    raw = list_jobs(running_only=True, job_type="product_analysis")
    jobs = [
        RunningJobSummary(
            job_id=item["job_id"],
            status=item["status"],
            repo_path=item.get("repo_path"),
            job_type=item.get("job_type") or "product_analysis",
        )
        for item in raw
    ]
    return RunningJobsResponse(jobs=jobs)
