"""Blogging API — job artifact listing and content retrieval."""

from __future__ import annotations

import json as json_module
from pathlib import Path

from agents.blogging.api.models import (
    ArtifactContentResponse,
    ArtifactListResponse,
    ArtifactMeta,
)
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

router = APIRouter()


@router.get(
    "/job/{job_id}/artifacts",
    response_model=ArtifactListResponse,
    summary="List job artifacts",
    description="List artifact filenames that exist for a pipeline job. Returns 404 if the job is missing or has no work_dir.",
)
def list_job_artifacts(job_id: str) -> ArtifactListResponse:
    """List existing artifact names for a job."""
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None:
        raise HTTPException(
            status_code=501,
            detail="Job store not available",
        )
    job = _main.get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    work_dir = job.get("work_dir")
    if not work_dir:
        raise HTTPException(status_code=404, detail="Job has no artifact directory")
    work_path = Path(work_dir)
    existing_names = [name for name in _main.ARTIFACT_NAMES if (work_path / name).exists()]
    meta_list = []
    for name in existing_names:
        producer = _main.ARTIFACT_PRODUCER.get(name, {}) if _main.ARTIFACT_PRODUCER else {}
        meta_list.append(
            ArtifactMeta(
                name=name,
                producer_phase=producer.get("producer_phase"),
                producer_agent=producer.get("producer_agent"),
            )
        )
    return ArtifactListResponse(artifacts=meta_list)


@router.get(
    "/job/{job_id}/artifacts/{artifact_name}",
    summary="Get job artifact content or download",
    description="Return the content of a single artifact (JSON body), or with ?download=true return as attachment. Path traversal is blocked; artifact_name must be in the allowed list.",
    response_model=None,
)
def get_job_artifact_content(
    job_id: str,
    artifact_name: str,
    download: bool = Query(
        False, description="If true, return content as attachment with Content-Disposition"
    ),
) -> ArtifactContentResponse | Response:
    """Return content of one artifact for a job, or as download attachment."""
    from agents.blogging.api import main as _main

    if _main.get_blog_job is None or _main.read_artifact is None:
        raise HTTPException(
            status_code=501,
            detail="Job store or artifact reader not available",
        )
    job = _main.get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    work_dir = job.get("work_dir")
    if not work_dir:
        raise HTTPException(status_code=404, detail="Job has no artifact directory")
    if artifact_name not in _main.ARTIFACT_NAMES:
        raise HTTPException(status_code=404, detail=f"Unknown artifact: {artifact_name!r}")
    parse_json = artifact_name.endswith(".json")
    content = _main.read_artifact(work_dir, artifact_name, default=None, parse_json=parse_json)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Artifact {artifact_name!r} not found")

    if download:
        if isinstance(content, (dict, list)):
            raw = json_module.dumps(content, indent=2)
            media_type = "application/json"
        else:
            raw = content if isinstance(content, str) else str(content)
            if artifact_name.endswith(".json"):
                media_type = "application/json"
            elif artifact_name.endswith(".yaml") or artifact_name.endswith(".yml"):
                media_type = "text/yaml"
            else:
                media_type = "text/plain; charset=utf-8"
        return Response(
            content=raw.encode("utf-8"),
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact_name}"'},
        )
    return ArtifactContentResponse(name=artifact_name, content=content)
