"""
Adapter to call the Software Engineering API's Product Requirements Analysis.

Verified paths (software_engineering_team.api.main):
- POST /api/software-engineering/product-analysis/run -> { job_id }
- GET  /api/software-engineering/product-analysis/status/{job_id} -> status, waiting_for_answers, pending_questions, validated_spec_path, ...
- POST /api/software-engineering/product-analysis/{job_id}/answers -> SubmitAnswersRequest { answers: [{ question_id, selected_option_id?, other_text? }] }
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
POLL_INTERVAL = 5.0
MAX_POLL_WAIT = 3600.0


def _se_base_url() -> str:
    return (
        os.environ.get("PLANNING_SOFTWARE_ENGINEERING_URL")
        or os.environ.get("UNIFIED_API_BASE_URL")
        or "http://localhost:8080"
    ).rstrip("/")


def run_product_analysis(
    repo_path: str,
    spec_content: Optional[str] = None,
) -> Optional[str]:
    """
    Start Product Requirements Analysis. Returns job_id or None on failure.
    """
    base = _se_base_url()
    url = f"{base}/api/software-engineering/product-analysis/run"
    payload: Dict[str, Any] = {"repo_path": repo_path}
    if spec_content is not None:
        payload["spec_content"] = spec_content
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("job_id")
    except (httpx.HTTPError, httpx.TimeoutException, ValueError, KeyError) as e:
        logger.warning("Product analysis run failed: %s", e)
        return None


def get_product_analysis_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get status of a product analysis job. Returns None on failure."""
    base = _se_base_url()
    url = f"{base}/api/software-engineering/product-analysis/status/{job_id}"
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        logger.warning("Product analysis status failed for %s: %s", job_id, e)
        return None


def submit_product_analysis_answers(
    job_id: str,
    answers: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Submit answers to open questions. answers: list of {question_id, selected_option_id?, other_text?}.
    Returns updated status dict or None on failure.
    """
    base = _se_base_url()
    url = f"{base}/api/software-engineering/product-analysis/{job_id}/answers"
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            resp = client.post(url, json={"answers": answers})
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, httpx.TimeoutException, ValueError) as e:
        logger.warning("Product analysis submit answers failed for %s: %s", job_id, e)
        return None


def wait_for_product_analysis_completion(
    job_id: str,
    poll_interval: float = POLL_INTERVAL,
    max_wait: float = MAX_POLL_WAIT,
    answer_callback: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Poll status until completed or failed. If waiting_for_answers and answer_callback
    is provided, call answer_callback(pending_questions) and submit answers then resume.
    Returns final status dict; status key is 'completed' or 'failed'.
    """
    start = time.monotonic()
    while (time.monotonic() - start) < max_wait:
        status = get_product_analysis_status(job_id)
        if status is None:
            return {"status": "failed", "error": "Failed to get status"}
        s = status.get("status", "")
        if s == "completed":
            return status
        if s == "failed":
            return status
        if status.get("waiting_for_answers") and answer_callback:
            pending = status.get("pending_questions", [])
            answers = answer_callback(pending)
            if answers:
                submit_product_analysis_answers(job_id, answers)
        time.sleep(poll_interval)
    return {"status": "failed", "error": "Timed out waiting for product analysis"}
