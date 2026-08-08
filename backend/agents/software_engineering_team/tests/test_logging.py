"""
Tests that verify agents emit expected log messages, plus TraceIdFilter behavior,
LOG_FORMAT rendering, and the extra={"trace_id": ...} logger call pattern used
across the pipeline.

Run with visible logs: pytest tests/test_logging.py -v --log-cli-level=INFO
"""

import logging

from architecture_expert import ArchitectureExpertAgent, ArchitectureInput

from llm_service import DummyLLMClient
from shared.dev_models.models import ProductRequirements
from shared.observability import bind_trace_id, current_trace_id
from software_engineering_team.shared.logging_config import LOG_FORMAT, TraceIdFilter


def test_architecture_agent_logs_start_and_done(caplog) -> None:
    """Architecture agent logs 'starting' and 'done' at INFO level."""
    caplog.set_level(logging.INFO)
    llm = DummyLLMClient()
    agent = ArchitectureExpertAgent(llm_client=llm)
    reqs = ProductRequirements(
        title="Test",
        description="Desc",
        acceptance_criteria=[],
        constraints=[],
    )
    agent.run(ArchitectureInput(requirements=reqs))

    records = [r.message for r in caplog.records]
    assert any("starting" in r.lower() for r in records)
    assert any("done" in r.lower() for r in records)
    assert any("components" in r.lower() for r in records)


def test_log_format_includes_trace_id_field() -> None:
    """LOG_FORMAT must render the structured trace_id field, not just message text."""
    assert "%(trace_id)s" in LOG_FORMAT


def test_trace_id_filter_defaults_missing_trace_id_to_empty_string() -> None:
    """Records logged without extra={"trace_id": ...} (e.g. third-party library logs)
    must not raise KeyError against LOG_FORMAT's %(trace_id)s field."""
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
    assert not hasattr(record, "trace_id")
    assert TraceIdFilter().filter(record) is True
    assert record.trace_id == ""


def test_trace_id_filter_preserves_explicit_trace_id() -> None:
    """The filter must not clobber a trace_id already attached via extra=."""
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "msg", None, None)
    record.trace_id = "abc123"
    assert TraceIdFilter().filter(record) is True
    assert record.trace_id == "abc123"


def test_logger_call_under_bind_trace_id_carries_trace_id_extra(caplog) -> None:
    """A logger.*(..., extra={"trace_id": current_trace_id()}) call site — the pattern
    used throughout the pipeline's logger calls — attaches the job's bound trace id to
    the emitted record."""
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("software_engineering_team.test_logging_trace_id")
    with bind_trace_id("test-trace-abc123"):
        logger.info("hello", extra={"trace_id": current_trace_id()})
    assert caplog.records[-1].trace_id == "test-trace-abc123"


def test_logger_call_without_bound_trace_id_defaults_to_empty_string(caplog) -> None:
    """Outside any bind_trace_id context, current_trace_id() is "" — the same default
    the pipeline sees for logging that happens before a job's trace id is bound."""
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("software_engineering_team.test_logging_trace_id")
    logger.info("hello unbound", extra={"trace_id": current_trace_id()})
    assert caplog.records[-1].trace_id == ""
