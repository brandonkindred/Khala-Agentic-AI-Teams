from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from job_service import repository as repo
from job_service.db import ensure_schema
from job_service.settings import (
    get_api_key,
    get_heartbeat_stale_seconds,
    get_stale_check_interval_seconds,
)

logger = logging.getLogger(__name__)

STALE_REASON = (
    "Job heartbeat stale while pending/running (no heartbeat within JOB_HEARTBEAT_STALE_SECONDS)"
)

_stale_stop: threading.Event | None = None


def verify_api_key(request: Request) -> None:
    expected = get_api_key()
    if not expected:
        return
    got = request.headers.get("X-Job-Service-Key", "").strip()
    if got != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key"
        )


def _stale_loop(stop: threading.Event) -> None:
    while not stop.wait(get_stale_check_interval_seconds()):
        try:
            repo.mark_stale_failed(
                stale_after_seconds=get_heartbeat_stale_seconds(),
                reason=STALE_REASON,
            )
        except Exception as exc:
            logger.warning("stale job monitor error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _stale_stop
    logging.basicConfig(level=logging.INFO)
    ensure_schema()
    _stale_stop = threading.Event()
    t = threading.Thread(
        target=_stale_loop, args=(_stale_stop,), name="job-service-stale", daemon=True
    )
    t.start()
    try:
        yield
    finally:
        if _stale_stop:
            _stale_stop.set()
        t.join(timeout=5.0)


app = FastAPI(title="Strands Job Service", version="1.0.0", lifespan=lifespan)


def require_auth(request: Request) -> None:
    verify_api_key(request)


class PatchBody(BaseModel):
    fields: Dict[str, Any] = Field(default_factory=dict)
    heartbeat: bool = True
    expected_version: Optional[int] = None


class EventBody(BaseModel):
    action: str
    outcome: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    status: Optional[str] = None
    expected_version: Optional[int] = None


class FailActiveBody(BaseModel):
    reason: str = "Server shutdown"


def _parse_if_match(header: Optional[str]) -> Optional[int]:
    if not header:
        return None
    h = header.strip()
    if h.startswith("W/"):
        h = h[2:].strip()
    if h.startswith('"') and h.endswith('"'):
        h = h[1:-1]
    try:
        return int(h)
    except ValueError:
        return None


@app.get("/health")
def health() -> Dict[str, str]:
    if repo.health_check():
        return {"status": "ok"}
    raise HTTPException(status_code=503, detail="database unavailable")


@app.get("/v1/jobs/{team}/{job_id}")
def get_job(
    team: str,
    job_id: str,
    response: Response,
    _auth: None = Depends(require_auth),
) -> Dict[str, Any]:
    data = repo.get_job(team, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job not found")
    v = data.pop("_version", None)
    if v is not None:
        response.headers["X-Job-Version"] = str(v)
    return data


@app.put("/v1/jobs/{team}/{job_id}")
def put_job(
    team: str,
    job_id: str,
    body: Dict[str, Any],
    response: Response,
    _auth: None = Depends(require_auth),
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
    unconditional: bool = Query(default=False),
) -> Dict[str, Any]:
    ev = _parse_if_match(if_match)
    ok, new_v = repo.put_job(
        team,
        job_id,
        body,
        expected_version=ev,
        unconditional=unconditional,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="version conflict or precondition failed")
    response.headers["X-Job-Version"] = str(new_v)
    return {"ok": True, "version": new_v}


@app.patch("/v1/jobs/{team}/{job_id}")
def patch_job(
    team: str,
    job_id: str,
    patch: PatchBody,
    response: Response,
    _auth: None = Depends(require_auth),
) -> Dict[str, Any]:
    ok, new_v = repo.patch_job(
        team,
        job_id,
        patch.fields,
        expected_version=patch.expected_version,
        heartbeat=patch.heartbeat,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="version conflict or not found")
    response.headers["X-Job-Version"] = str(new_v)
    return {"ok": True, "version": new_v}


@app.delete("/v1/jobs/{team}/{job_id}")
def delete_job(team: str, job_id: str, _auth: None = Depends(require_auth)) -> Response:
    if not repo.delete_job(team, job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/v1/jobs")
def list_jobs(
    _auth: None = Depends(require_auth),
    team: Optional[str] = Query(default=None),
    status_in: Optional[str] = Query(default=None, alias="status"),
) -> Dict[str, List[Dict[str, Any]]]:
    statuses = [s.strip() for s in status_in.split(",")] if status_in else None
    jobs = repo.list_jobs(team=team, statuses=statuses)
    return {"jobs": jobs}


@app.post("/v1/jobs/{team}/{job_id}/heartbeat")
def heartbeat(
    team: str,
    job_id: str,
    response: Response,
    _auth: None = Depends(require_auth),
    if_match: Optional[str] = Header(default=None, alias="If-Match"),
) -> Dict[str, Any]:
    ev = _parse_if_match(if_match)
    ok, new_v = repo.heartbeat(team, job_id, expected_version=ev)
    if not ok:
        raise HTTPException(status_code=409, detail="version conflict or not found")
    response.headers["X-Job-Version"] = str(new_v)
    return {"ok": True, "version": new_v}


@app.post("/v1/jobs/{team}/fail-active")
def fail_active_for_team(
    team: str,
    body: FailActiveBody,
    _auth: None = Depends(require_auth),
) -> Dict[str, Any]:
    ids = repo.fail_active_jobs_for_team(team, reason=body.reason)
    return {"job_ids": ids, "count": len(ids)}


@app.post("/v1/jobs/{team}/{job_id}/events")
def post_event(
    team: str,
    job_id: str,
    body: EventBody,
    response: Response,
    _auth: None = Depends(require_auth),
) -> Dict[str, Any]:
    ok, new_v = repo.append_event(
        team,
        job_id,
        action=body.action,
        outcome=body.outcome,
        details=body.details,
        status=body.status,
        expected_version=body.expected_version,
    )
    if not ok:
        raise HTTPException(status_code=409, detail="version conflict or not found")
    response.headers["X-Job-Version"] = str(new_v)
    return {"ok": True, "version": new_v}
