"""
Agent performance analytics pipeline.

Captures quality signals from agent workflows (code review rejections,
build failures, LLM retries, acceptance rates) and aggregates them
into per-team and per-agent scorecards for data-driven improvement.
"""

from .signals import QualitySignal, SignalType, record_signal

__all__ = ["QualitySignal", "SignalType", "record_signal"]
