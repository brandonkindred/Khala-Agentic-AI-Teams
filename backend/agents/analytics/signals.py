"""
Quality signal definitions and recording for agent performance analytics.

Signals represent discrete quality events that already occur in agent
workflows but aren't currently captured as structured data. This module
provides a standard way to record them for aggregation.

Usage::

    from analytics import record_signal, SignalType

    record_signal(
        signal_type=SignalType.CODE_REVIEW_REJECTED,
        team="software_engineering",
        agent_key="code_review",
        job_id="abc123",
        metadata={"task_id": "task_1", "reason": "missing error handling"},
    )
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)


class SignalType(str, Enum):
    """Standard quality signal types extracted from agent workflows."""

    # Code quality
    CODE_REVIEW_PASSED = "code_review.passed"
    CODE_REVIEW_REJECTED = "code_review.rejected"
    BUILD_SUCCEEDED = "build.succeeded"
    BUILD_FAILED = "build.failed"
    LINT_PASSED = "lint.passed"
    LINT_FAILED = "lint.failed"

    # Acceptance
    ACCEPTANCE_PASSED = "acceptance.passed"
    ACCEPTANCE_FAILED = "acceptance.failed"
    QUALITY_GATE_PASSED = "quality_gate.passed"
    QUALITY_GATE_FAILED = "quality_gate.failed"

    # LLM efficiency
    LLM_RETRY = "llm.retry"
    LLM_TRUNCATED = "llm.truncated"
    LLM_PARSE_ERROR = "llm.parse_error"

    # Task lifecycle
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_RETRIED = "task.retried"

    # Content quality (blogging)
    DRAFT_APPROVED = "draft.approved"
    DRAFT_REVISION_REQUESTED = "draft.revision_requested"


@dataclass
class QualitySignal:
    """A discrete quality event from an agent workflow."""

    signal_type: SignalType
    team: str
    agent_key: str
    timestamp: float = field(default_factory=time.time)
    job_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "signal_type": self.signal_type.value,
            "team": self.team,
            "agent_key": self.agent_key,
            "timestamp": self.timestamp,
        }
        if self.job_id:
            d["job_id"] = self.job_id
        if self.metadata:
            d["metadata"] = self.metadata
        return d


# In-memory signal log
_BUFFER_SIZE = 50_000
_signal_log: Deque[QualitySignal] = deque(maxlen=_BUFFER_SIZE)
_log_lock = threading.Lock()


def record_signal(
    signal_type: SignalType,
    team: str,
    agent_key: str = "",
    job_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> QualitySignal:
    """Record a quality signal event."""
    signal = QualitySignal(
        signal_type=signal_type,
        team=team,
        agent_key=agent_key,
        job_id=job_id,
        metadata=metadata or {},
    )
    with _log_lock:
        _signal_log.append(signal)
    return signal


def get_signals(
    *,
    team: Optional[str] = None,
    signal_type: Optional[SignalType] = None,
    window_hours: float = 24.0,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """Query recorded signals with optional filters."""
    cutoff = time.time() - (window_hours * 3600)
    with _log_lock:
        signals = [s for s in _signal_log if s.timestamp >= cutoff]
    if team:
        signals = [s for s in signals if s.team == team]
    if signal_type:
        signals = [s for s in signals if s.signal_type == signal_type]
    return [s.to_dict() for s in signals[-limit:]]


def get_team_scorecard(
    team: str,
    window_hours: float = 24.0,
) -> Dict[str, Any]:
    """Generate a quality scorecard for a team over a time window.

    Returns metrics like first-pass success rate, retry counts,
    build failure rate, etc.
    """
    cutoff = time.time() - (window_hours * 3600)
    with _log_lock:
        signals = [s for s in _signal_log if s.timestamp >= cutoff and s.team == team]

    scorecard: Dict[str, Any] = {
        "team": team,
        "window_hours": window_hours,
        "total_signals": len(signals),
    }

    # Count by type
    counts: Dict[str, int] = {}
    for s in signals:
        key = s.signal_type.value
        counts[key] = counts.get(key, 0) + 1
    scorecard["counts"] = counts

    # Compute rates
    reviews_total = counts.get("code_review.passed", 0) + counts.get("code_review.rejected", 0)
    if reviews_total > 0:
        scorecard["code_review_pass_rate"] = round(
            counts.get("code_review.passed", 0) / reviews_total, 3
        )

    builds_total = counts.get("build.succeeded", 0) + counts.get("build.failed", 0)
    if builds_total > 0:
        scorecard["build_success_rate"] = round(
            counts.get("build.succeeded", 0) / builds_total, 3
        )

    tasks_total = counts.get("task.completed", 0) + counts.get("task.failed", 0)
    if tasks_total > 0:
        scorecard["task_success_rate"] = round(
            counts.get("task.completed", 0) / tasks_total, 3
        )

    scorecard["llm_retries"] = counts.get("llm.retry", 0)
    scorecard["llm_parse_errors"] = counts.get("llm.parse_error", 0)

    return scorecard
