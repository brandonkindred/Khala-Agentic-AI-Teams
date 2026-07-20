"""Shared FastAPI dependencies for blogging API routers.

Consolidates the guard sequence repeated across ``routers/interactive.py``,
``routers/jobs.py``, and ``routers/artifacts.py``: job-store-available check
(501) -> job-found check (404) -> optional job-in-expected-state check (400).

Every dependency here re-imports ``agents.blogging.api.main`` at call time
rather than capturing a reference at declaration time. Route handlers rely on
this same late-binding so tests can monkeypatch individual helpers on the
``main`` module to force the 501/404/400 branches; capturing a reference up
front would silently stop observing those monkeypatches.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from fastapi import Depends, HTTPException


def require_job_store(
    *helper_names: str,
    detail: str = "Job store not available",
) -> Callable[[], None]:
    """Build a dependency that 501s if any named ``api.main`` helper is unset.

    Preconditions:
        - ``helper_names`` are attribute names expected on ``agents.blogging.api.main``.
    Postconditions:
        - Returns a zero-arg callable suitable for ``Depends(...)``. Calling it
          raises ``HTTPException(501, detail=detail)`` if any named attribute on
          ``main`` is ``None`` or missing; otherwise it returns ``None``.
    """

    def _dependency() -> None:
        from agents.blogging.api import main as _main

        for name in helper_names:
            if getattr(_main, name, None) is None:
                raise HTTPException(status_code=501, detail=detail)

    return _dependency


def get_job_or_404(job_id: str) -> Dict[str, Any]:
    """Fetch a blog job by id, or raise 404.

    Preconditions:
        - ``job_id`` is the path parameter; ``agents.blogging.api.main.get_blog_job``
          must be callable (not ``None`` — pair with ``require_job_store`` when
          store availability isn't already guaranteed).
    Postconditions:
        - Returns the job dict when found; raises ``HTTPException(404, detail=f"Job {job_id} not found")``
          otherwise.
    """
    from agents.blogging.api import main as _main

    job = _main.get_blog_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


def require_job_waiting_for(
    flag_name: str,
    detail: str,
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Build a dependency that 400s unless the job's ``flag_name`` field is truthy.

    Preconditions:
        - ``flag_name`` is a boolean-valued key on the job dict (e.g.
          ``"waiting_for_title_selection"``).
    Postconditions:
        - Returns a callable depending on ``get_job_or_404``. Calling it raises
          ``HTTPException(400, detail=detail)`` when ``job.get(flag_name)`` is
          falsy; otherwise returns the job dict unchanged.
    """

    def _dependency(job: Dict[str, Any] = Depends(get_job_or_404)) -> Dict[str, Any]:
        if not job.get(flag_name):
            raise HTTPException(status_code=400, detail=detail)
        return job

    return _dependency


def get_job(
    *helper_names: str,
    store_detail: str = "Job store not available",
    waiting_for: Optional[Tuple[str, str]] = None,
) -> Callable[[str], Dict[str, Any]]:
    """Build the combined store-available -> job-found -> optional-state-check dependency.

    Preconditions:
        - ``helper_names`` are additional ``api.main`` attributes the route needs
          available, beyond ``get_blog_job`` — which is always checked
          automatically, since ``get_job_or_404`` unconditionally calls it.
        - ``waiting_for``, when given, is ``(flag_name, detail)`` for the
          job-in-expected-state check (e.g. ``("waiting_for_title_selection",
          "Job is not currently waiting for title selection")``).
    Postconditions:
        - Returns a single dependency taking ``job_id`` from the path. Calling it
          raises 501 (store unavailable), 404 (job not found), or 400 (state
          check failed) in that order; otherwise returns the job dict.
    """

    def _dependency(job_id: str) -> Dict[str, Any]:
        require_job_store("get_blog_job", *helper_names, detail=store_detail)()
        job = get_job_or_404(job_id)
        if waiting_for is not None:
            flag_name, state_detail = waiting_for
            job = require_job_waiting_for(flag_name, state_detail)(job)
        return job

    return _dependency
