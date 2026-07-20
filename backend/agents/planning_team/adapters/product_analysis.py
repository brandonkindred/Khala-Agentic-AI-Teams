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
from typing import Any, Dict, List, Optional

from shared.http.job_polling import get_json, poll_until_terminal, post_json

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
POLL_INTERVAL = 5.0
MAX_POLL_WAIT = 3600.0

_TERMINAL_STATUSES = frozenset({"completed", "failed"})


def _se_base_url() -> Optional[str]:
    return os.environ.get("PLANNING_SOFTWARE_ENGINEERING_URL") or os.environ.get(
        "UNIFIED_API_BASE_URL"
    )


def run_product_analysis(
    repo_path: str,
    spec_content: Optional[str] = None,
) -> Optional[str]:
    """
    Start Product Requirements Analysis. Returns job_id or None on failure
    (including when the Software Engineering service is unconfigured).
    """
    base = _se_base_url()
    if not base:
        logger.debug("No base URL for product analysis; skipping.")
        return None
    url = f"{base.rstrip('/')}/api/software-engineering/product-analysis/run"
    payload: Dict[str, Any] = {"repo_path": repo_path}
    if spec_content is not None:
        payload["spec_content"] = spec_content
    data = post_json(url, payload, timeout=DEFAULT_TIMEOUT, log_context="Product analysis run")
    return data.get("job_id") if data else None


def get_product_analysis_status(job_id: str) -> Optional[Dict[str, Any]]:
    """Get status of a product analysis job. Returns None on failure."""
    base = _se_base_url()
    if not base:
        logger.debug("No base URL for product analysis; skipping.")
        return None
    url = f"{base.rstrip('/')}/api/software-engineering/product-analysis/status/{job_id}"
    return get_json(
        url, timeout=DEFAULT_TIMEOUT, log_context=f"Product analysis status for {job_id}"
    )


def submit_product_analysis_answers(
    job_id: str,
    answers: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Submit answers to open questions. answers: list of {question_id, selected_option_id?, other_text?}.
    Returns updated status dict or None on failure.
    """
    base = _se_base_url()
    if not base:
        logger.debug("No base URL for product analysis; skipping.")
        return None
    url = f"{base.rstrip('/')}/api/software-engineering/product-analysis/{job_id}/answers"
    return post_json(
        url,
        {"answers": answers},
        timeout=DEFAULT_TIMEOUT,
        log_context=f"Product analysis submit answers for {job_id}",
    )


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

    def _on_poll(status: Dict[str, Any]) -> None:
        if status.get("waiting_for_answers") and answer_callback:
            pending = status.get("pending_questions", [])
            answers = answer_callback(pending)
            if answers:
                submit_product_analysis_answers(job_id, answers)

    return poll_until_terminal(
        lambda: get_product_analysis_status(job_id),
        terminal_statuses=_TERMINAL_STATUSES,
        poll_interval=poll_interval,
        total_timeout=max_wait,
        on_poll=_on_poll,
        log_context="product analysis",
    )
