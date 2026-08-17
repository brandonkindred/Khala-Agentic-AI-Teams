"""
LLM call telemetry: structured recording of every LLM invocation.

Captures token usage, latency, caller identity, and (optionally) prompts/responses
for cost attribution, debugging, and agent performance analysis.

Usage::

    from llm_service.telemetry import record_llm_call, get_recent_calls, get_usage_summary

    record_llm_call(
        team="blogging",
        agent_key="blog_writer",
        model="deepseek-v4-pro:cloud",
        caller_tag="blog_writer_agent.agent.write_draft",
        prompt_tokens=1200,
        completion_tokens=3500,
        total_tokens=4700,
        latency_ms=4200,
        status="success",
    )

    summary = get_usage_summary(team="blogging", window_hours=24)

Observers registered via ``register_call_observer`` are invoked **synchronously**
inside ``record_llm_call``, so they must be cheap and non-blocking — avoid
synchronous DB/network I/O on the call path or it adds latency to every LLM call
(persisters should enqueue/offload rather than write inline).
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from .pricing import estimate_cost_usd

__all__ = [
    "LLMCallRecord",
    "UsageSummary",
    "record_llm_call",
    "register_call_observer",
    "unregister_call_observer",
    "get_recent_calls",
    "get_usage_summary",
    "clear_call_log",
]

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
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    error_type: Optional[str] = None
    job_id: Optional[str] = None
    objective: str = ""
    request_id: str = ""
    task_id: str = ""
    phase: str = ""
    cost_usd: float = 0.0
    outcome: str = ""  # coarse result bucket; defaults to ``status``
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
            "cost_usd": self.cost_usd,
            "outcome": self.outcome,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }
        if self.error_type:
            d["error_type"] = self.error_type
        if self.job_id:
            d["job_id"] = self.job_id
        if self.objective:
            d["objective"] = self.objective
        if self.request_id:
            d["request_id"] = self.request_id
        if self.task_id:
            d["task_id"] = self.task_id
        if self.phase:
            d["phase"] = self.phase
        return d


# ---------------------------------------------------------------------------
# Global call log (thread-safe ring buffer)
# ---------------------------------------------------------------------------

_call_log: Deque[LLMCallRecord] = deque(maxlen=_DEFAULT_BUFFER_SIZE)
_log_lock = threading.Lock()

# Whether to capture prompt/response content (can be large)
_CAPTURE_PROMPTS = os.environ.get("LLM_CAPTURE_PROMPTS", "").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Call observers
# ---------------------------------------------------------------------------
#
# A generic post-record hook so a team (e.g. Software Engineering) can react to
# every LLM call — accumulate per-job cost, persist a trace row — *without*
# llm_service importing team code. Observers are called after the record is
# buffered and the OTel span emitted; an observer raising is swallowed so one
# team's bookkeeping never breaks another team's LLM call.

_observers: List[Callable[["LLMCallRecord"], None]] = []
_observers_lock = threading.Lock()


def register_call_observer(observer: Callable[["LLMCallRecord"], None]) -> None:
    """Register ``observer`` to be invoked with each new :class:`LLMCallRecord`.

    Preconditions: ``observer`` is callable.
    Postconditions: ``observer`` is invoked (best-effort, exceptions swallowed)
        for every subsequent :func:`record_llm_call`. Re-registering the same
        callable is a no-op, so module-import-time registration is idempotent.
    """
    with _observers_lock:
        if observer not in _observers:
            _observers.append(observer)


def unregister_call_observer(observer: Callable[["LLMCallRecord"], None]) -> None:
    """Remove a previously registered observer; no-op if absent (for tests)."""
    with _observers_lock:
        if observer in _observers:
            _observers.remove(observer)


def _notify_observers(record: "LLMCallRecord") -> None:
    with _observers_lock:
        observers = list(_observers)
    for observer in observers:
        try:
            observer(record)
        except Exception:
            logger.debug("LLM call observer failed", exc_info=True)


def _derive_outcome(status: str) -> str:
    """Map a call ``status`` to its coarse outcome bucket.

    Postconditions: returns the status verbatim when non-empty (the usual buckets
        ``success`` / ``error`` / ``rate_limited`` / ``truncated``, plus any
        forward-compatible value), and ``"unknown"`` for an empty status.
    """
    return status or "unknown"


def record_llm_call(
    *,
    team: str = "",
    agent_key: str = "",
    model: str = "",
    caller_tag: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    latency_ms: int = 0,
    status: str = "success",
    error_type: Optional[str] = None,
    job_id: Optional[str] = None,
    objective: str = "",
    request_id: str = "",
    task_id: str = "",
    phase: str = "",
    cost_usd: Optional[float] = None,
    outcome: str = "",
    prompt_text: Optional[str] = None,
    response_text: Optional[str] = None,
) -> LLMCallRecord:
    """Record an LLM call to the in-memory telemetry log.

    ``cost_usd`` defaults to an estimate from :func:`llm_service.pricing.estimate_cost_usd`
    over the token counts when not supplied; ``outcome`` defaults to a coarse
    bucket derived from ``status``. Returns the created record for
    testing/inspection.

    Negative ``prompt_tokens``/``completion_tokens`` are clamped to ``0`` for the
    cost estimate (telemetry recording must never raise into the LLM call path,
    and ``estimate_cost_usd`` rejects negatives); the raw counts are still stored
    on the record as given.
    """
    prompt_preview = None
    response_preview = None
    if _CAPTURE_PROMPTS:
        prompt_preview = (prompt_text or "")[:2000] if prompt_text else None
        response_preview = (response_text or "")[:2000] if response_text else None

    if cost_usd is None:
        try:
            cost_usd = estimate_cost_usd(model, max(0, prompt_tokens), max(0, completion_tokens))
        except Exception:
            # Cost estimation must never break telemetry recording.
            logger.debug("cost estimation failed for model %r", model, exc_info=True)
            cost_usd = 0.0
    else:
        # A caller-supplied cost must not poison spans/counters/job totals with a
        # negative or non-finite value; coerce to a safe non-negative finite float.
        if not math.isfinite(cost_usd) or cost_usd < 0:
            logger.debug("ignoring invalid caller cost_usd=%r for model %r", cost_usd, model)
            cost_usd = 0.0

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
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        error_type=error_type,
        job_id=job_id,
        objective=objective,
        request_id=request_id,
        task_id=task_id,
        phase=phase,
        cost_usd=cost_usd,
        outcome=outcome or _derive_outcome(status),
        prompt_preview=prompt_preview,
        response_preview=response_preview,
    )
    with _log_lock:
        _call_log.append(record)
    _emit_otel_llm_span(record)
    _notify_observers(record)
    return record


# ---------------------------------------------------------------------------
# OpenTelemetry integration
# ---------------------------------------------------------------------------
#
# Every LLM invocation also produces an OpenTelemetry span + metrics so that
# platform-wide traces show which team/agent triggered which model call, how
# long it took, and how many tokens were consumed. The span is added as an
# event to whatever span is currently active, so it nests correctly under
# the server span created by the FastAPI instrumentor.
#
# All OpenTelemetry state is resolved lazily and wrapped in try/except so
# importing llm_service still works when the SDK is absent (e.g. tests).

_otel_initialized: bool = False
_otel_init_lock = threading.Lock()
_otel_tracer: Any = None
_otel_llm_calls: Any = None
_otel_llm_tokens: Any = None
_otel_llm_latency: Any = None
_otel_llm_cost: Any = None


def _ensure_otel_instruments() -> None:
    """Lazily acquire the tracer and metric instruments (exactly once)."""
    global _otel_initialized, _otel_tracer, _otel_llm_calls, _otel_llm_tokens, _otel_llm_latency
    global _otel_llm_cost

    if _otel_initialized:
        return
    # Double-checked locking: concurrent first calls from record_llm_call must
    # not both run the init block and create duplicate instruments.
    with _otel_init_lock:
        if _otel_initialized:
            return
        # Set the flag before creating instruments so this init runs exactly once:
        # if the OTel SDK is unavailable, instrument creation fails and the
        # instruments stay None — but we deliberately do NOT retry on every
        # subsequent LLM call (that would re-enter this lock + re-attempt creation
        # on the hot path). A failed init is treated as "OTel off for this process".
        _otel_initialized = True
        try:
            from shared.observability import get_meter, get_tracer

            _otel_tracer = get_tracer("khala.llm_service")
            meter = get_meter("khala.llm_service")
            _otel_llm_calls = meter.create_counter(
                "khala.llm.calls",
                description="Total LLM calls made by a Khala team/agent",
            )
            _otel_llm_tokens = meter.create_counter(
                "khala.llm.tokens",
                description="Total tokens consumed by LLM calls (prompt + completion)",
            )
            _otel_llm_latency = meter.create_histogram(
                "khala.llm.latency_ms",
                description="LLM call latency in milliseconds",
                unit="ms",
            )
            _otel_llm_cost = meter.create_counter(
                "khala.llm.cost_usd",
                description="Estimated USD cost of LLM calls (prompt + completion)",
                unit="USD",
            )
        except Exception:
            logger.debug("OpenTelemetry instruments unavailable for llm_service", exc_info=True)
            _otel_tracer = None
            _otel_llm_calls = None
            _otel_llm_tokens = None
            _otel_llm_latency = None
            _otel_llm_cost = None


# Call statuses that must NOT mark the span as a tracing error: a successful call,
# plus soft/transient outcomes (provider rate-limiting, an output truncated at the
# token cap) that retry or degrade rather than genuinely fail. Flagging these ERROR
# would create false error signals in distributed tracing and alerting. Any other
# status (e.g. ``error``) — including an unknown future one — is treated as a failure.
_NON_ERROR_LLM_STATUSES = frozenset({"success", "rate_limited", "truncated"})


def _emit_otel_llm_span(record: LLMCallRecord) -> None:
    """Emit an OpenTelemetry span + metrics for a single LLM call record."""
    _ensure_otel_instruments()
    if _otel_tracer is None:
        return
    try:
        attributes = {
            "khala.team": record.team or "unknown",
            "khala.agent_key": record.agent_key or "unknown",
            # ``agent.name`` mirrors ``khala.agent_key`` under the attribute name
            # the SDLC review specified; both are emitted for compatibility.
            "agent.name": record.agent_key or "unknown",
            "khala.caller_tag": record.caller_tag or "",
            "llm.vendor": "ollama",
            "llm.request.model": record.model or "unknown",
            "llm.model": record.model or "unknown",
            "llm.usage.prompt_tokens": record.prompt_tokens,
            "llm.usage.completion_tokens": record.completion_tokens,
            "llm.usage.total_tokens": record.total_tokens,
            "llm.usage.cache_read_tokens": record.cache_read_tokens,
            "llm.usage.cache_creation_tokens": record.cache_creation_tokens,
            # Issue-named aliases for the input/output token counts.
            "llm.input_tokens": record.prompt_tokens,
            "llm.output_tokens": record.completion_tokens,
            "llm.latency_ms": record.latency_ms,
            "llm.status": record.status,
            "cost.usd": record.cost_usd,
            "outcome": record.outcome,
        }
        if record.error_type:
            attributes["llm.error_type"] = record.error_type
        if record.job_id:
            attributes["khala.job_id"] = record.job_id
            attributes["job.id"] = record.job_id
        if record.objective:
            attributes["khala.objective"] = record.objective
        if record.request_id:
            attributes["khala.request_id"] = record.request_id
        if record.task_id:
            attributes["task.id"] = record.task_id
        if record.phase:
            attributes["phase"] = record.phase

        span_name = f"llm.call {record.agent_key or 'agent'}"
        span = _otel_tracer.start_span(span_name, attributes=attributes)
        if record.status not in _NON_ERROR_LLM_STATUSES:
            try:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(
                    Status(StatusCode.ERROR, description=record.error_type or record.status)
                )
            except Exception:
                pass
        span.end()

        metric_attrs = {
            "team": record.team or "unknown",
            "agent_key": record.agent_key or "unknown",
            "model": record.model or "unknown",
            "status": record.status,
        }
        # Record on every call regardless of zero values: a 0 ms latency is a real
        # histogram sample, and a $0 (free local-model) call is real data — gating
        # on truthiness would make "free"/"instant" indistinguishable from "no data".
        if _otel_llm_calls is not None:
            _otel_llm_calls.add(1, metric_attrs)
        if _otel_llm_tokens is not None and record.total_tokens >= 0:
            _otel_llm_tokens.add(record.total_tokens, metric_attrs)
        if _otel_llm_latency is not None and record.latency_ms >= 0:
            _otel_llm_latency.record(record.latency_ms, metric_attrs)
        if _otel_llm_cost is not None and record.cost_usd >= 0:
            _otel_llm_cost.add(record.cost_usd, metric_attrs)
    except Exception:
        logger.debug("Failed to emit OpenTelemetry LLM span", exc_info=True)


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
    total_cache_read_tokens: int = 0
    total_cache_creation_tokens: int = 0
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
            "total_cache_read_tokens": self.total_cache_read_tokens,
            "total_cache_creation_tokens": self.total_cache_creation_tokens,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "error_count": self.error_count,
            "by_agent": self.by_agent,
            "by_model": self.by_model,
        }


def get_usage_summary(
    *,
    team: Optional[str] = None,
    window_hours: Optional[float] = 24.0,
) -> Dict[str, Any]:
    """Aggregate token usage over the given time window.

    Returns a summary dict with totals and per-agent/per-model breakdowns.
    ``window_hours is None`` means all-time (no timestamp cutoff).
    ``window_hours == 0`` is a zero-width window (cutoff is now), matching
    the pre-change route when clients sent numeric ``window=0``.
    """
    with _log_lock:
        if window_hours is None:
            records = list(_call_log)
        else:
            cutoff = time.time() - (window_hours * 3600)
            records = [r for r in _call_log if r.timestamp >= cutoff]
    if team:
        records = [r for r in records if r.team == team]

    summary = UsageSummary(
        team=team or "all",
        window_hours=0.0 if window_hours is None else window_hours,
    )
    total_latency = 0
    for r in records:
        summary.total_calls += 1
        summary.total_prompt_tokens += r.prompt_tokens
        summary.total_completion_tokens += r.completion_tokens
        summary.total_tokens += r.total_tokens
        summary.total_cache_read_tokens += r.cache_read_tokens
        summary.total_cache_creation_tokens += r.cache_creation_tokens
        total_latency += r.latency_ms
        if r.status != "success":
            summary.error_count += 1

        # Per-agent breakdown
        if r.agent_key:
            agent = summary.by_agent.setdefault(r.agent_key, {"calls": 0, "tokens": 0})
            agent["calls"] += 1
            agent["tokens"] += r.total_tokens

        # Per-model breakdown
        model_key = r.model or ""
        model = summary.by_model.setdefault(
            model_key,
            {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "tokens": 0,
            },
        )
        model["calls"] += 1
        model["prompt_tokens"] += r.prompt_tokens
        model["completion_tokens"] += r.completion_tokens
        model["total_tokens"] += r.total_tokens
        model["tokens"] += r.total_tokens

    if summary.total_calls > 0:
        summary.avg_latency_ms = total_latency / summary.total_calls

    return summary.to_dict()


def clear_call_log() -> None:
    """Clear the in-memory call log. For testing only."""
    with _log_lock:
        _call_log.clear()
