"""
LLM call telemetry: structured recording of every LLM invocation.

Captures token usage, latency, caller identity, and (optionally) prompts/responses
for cost attribution, debugging, and agent performance analysis.

Usage::

    from llm_service.telemetry import record_llm_call, get_recent_calls, get_usage_summary

    record_llm_call(
        team="blogging",
        agent_key="blog_writer",
        model="qwen3.5:397b-cloud",
        caller_tag="blog_writer_agent.agent.write_draft",
        prompt_tokens=1200,
        completion_tokens=3500,
        total_tokens=4700,
        latency_ms=4200,
        status="success",
    )

    summary = get_usage_summary(team="blogging", window_hours=24)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# In-memory ring buffer size. For production, this should be replaced with
# Postgres persistence (see _persist_to_db). The ring buffer provides
# immediate access for dashboards and debugging without DB dependency.
_DEFAULT_BUFFER_SIZE = 10_000


@dataclass
class LLMCallRecord:
    """Structured record of a single LLM invocation."""

    timestamp: float  # time.time()
    team: str
    agent_key: str
    model: str
    caller_tag: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    status: str  # "success", "error", "rate_limited", "truncated"
    error_type: Optional[str] = None
    job_id: Optional[str] = None
    # Opt-in prompt/response capture (when LLM_CAPTURE_PROMPTS=true)
    prompt_preview: Optional[str] = None
    response_preview: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "timestamp": self.timestamp,
            "team": self.team,
            "agent_key": self.agent_key,
            "model": self.model,
            "caller_tag": self.caller_tag,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "status": self.status,
        }
        if self.error_type:
            d["error_type"] = self.error_type
        if self.job_id:
            d["job_id"] = self.job_id
        return d


# ---------------------------------------------------------------------------
# Global call log (thread-safe ring buffer)
# ---------------------------------------------------------------------------

_call_log: Deque[LLMCallRecord] = deque(maxlen=_DEFAULT_BUFFER_SIZE)
_log_lock = threading.Lock()

# Whether to capture prompt/response content (can be large)
_CAPTURE_PROMPTS = os.environ.get("LLM_CAPTURE_PROMPTS", "").lower() in ("true", "1", "yes")


def record_llm_call(
    *,
    team: str = "",
    agent_key: str = "",
    model: str = "",
    caller_tag: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    latency_ms: int = 0,
    status: str = "success",
    error_type: Optional[str] = None,
    job_id: Optional[str] = None,
    prompt_text: Optional[str] = None,
    response_text: Optional[str] = None,
) -> LLMCallRecord:
    """Record an LLM call to the in-memory telemetry log.

    Returns the created record for testing/inspection.
    """
    prompt_preview = None
    response_preview = None
    if _CAPTURE_PROMPTS:
        prompt_preview = (prompt_text or "")[:2000] if prompt_text else None
        response_preview = (response_text or "")[:2000] if response_text else None

    record = LLMCallRecord(
        timestamp=time.time(),
        team=team,
        agent_key=agent_key,
        model=model,
        caller_tag=caller_tag,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        status=status,
        error_type=error_type,
        job_id=job_id,
        prompt_preview=prompt_preview,
        response_preview=response_preview,
    )
    with _log_lock:
        _call_log.append(record)
    return record


def get_recent_calls(
    *,
    team: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return recent LLM call records, optionally filtered by team."""
    with _log_lock:
        records = list(_call_log)
    if team:
        records = [r for r in records if r.team == team]
    return [r.to_dict() for r in records[-limit:]]


@dataclass
class UsageSummary:
    """Aggregated token usage over a time window."""

    team: str
    window_hours: float
    total_calls: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0
    avg_latency_ms: float = 0.0
    error_count: int = 0
    by_agent: Dict[str, Dict[str, int]] = field(default_factory=dict)
    by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "team": self.team,
            "window_hours": self.window_hours,
            "total_calls": self.total_calls,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "error_count": self.error_count,
            "by_agent": self.by_agent,
            "by_model": self.by_model,
        }


def get_usage_summary(
    *,
    team: Optional[str] = None,
    window_hours: float = 24.0,
) -> Dict[str, Any]:
    """Aggregate token usage over the given time window.

    Returns a summary dict with totals and per-agent/per-model breakdowns.
    """
    cutoff = time.time() - (window_hours * 3600)
    with _log_lock:
        records = [r for r in _call_log if r.timestamp >= cutoff]
    if team:
        records = [r for r in records if r.team == team]

    summary = UsageSummary(team=team or "all", window_hours=window_hours)
    total_latency = 0
    for r in records:
        summary.total_calls += 1
        summary.total_prompt_tokens += r.prompt_tokens
        summary.total_completion_tokens += r.completion_tokens
        summary.total_tokens += r.total_tokens
        total_latency += r.latency_ms
        if r.status != "success":
            summary.error_count += 1

        # Per-agent breakdown
        if r.agent_key:
            agent = summary.by_agent.setdefault(r.agent_key, {"calls": 0, "tokens": 0})
            agent["calls"] += 1
            agent["tokens"] += r.total_tokens

        # Per-model breakdown
        if r.model:
            model = summary.by_model.setdefault(r.model, {"calls": 0, "tokens": 0})
            model["calls"] += 1
            model["tokens"] += r.total_tokens

    if summary.total_calls > 0:
        summary.avg_latency_ms = total_latency / summary.total_calls

    return summary.to_dict()


def clear_call_log() -> None:
    """Clear the in-memory call log. For testing only."""
    with _log_lock:
        _call_log.clear()
