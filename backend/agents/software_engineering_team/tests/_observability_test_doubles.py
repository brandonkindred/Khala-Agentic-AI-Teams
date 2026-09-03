"""Shared test double for the trace-store observability tests.

Extracted from ``test_trace_flusher.py`` and ``test_observability_stores.py``,
which each defined field-for-field identical stand-ins for
:class:`llm_service.telemetry.LLMCallRecord` — the only thing the two modules'
copies disagreed on was default values, not shape. Matches the extraction
pattern already used by ``_coding_team_orchestrator_doubles.py`` and
``_review_fallback_test_doubles.py``.

Not a test module itself -- its ``_``-prefixed name prevents pytest from
collecting it (same convention as those two modules).

Further, deliberately unconverged copies of this same field set exist in this
package's own live-Postgres integration sibling (``test_observability_stores_pg.py``,
run only under ``-m integration``) and in ``llm_service/tests/test_usage_store.py``
(a different team's package, so importing it here would reach into another
team's private ``tests/``). Converging those is a larger change than this
extraction and is left for later rather than folded in here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# The full field set _record_to_row reads, including cache_read_tokens/
# cache_creation_tokens — deliberately NOT pre-set by __init__ (see its
# Postconditions), so validation below whitelists by name rather than by
# hasattr(), which would reject exactly the override that field exists for.
_FIELDS = frozenset(
    {
        "timestamp",
        "team",
        "agent_key",
        "job_id",
        "task_id",
        "phase",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cost_usd",
        "latency_ms",
        "status",
        "outcome",
        "objective",
        "request_id",
        "cache_read_tokens",
        "cache_creation_tokens",
    }
)


class TraceCallRecord:
    """Minimal stand-in for :class:`llm_service.telemetry.LLMCallRecord`.

    Preconditions:
        Every key in ``overrides`` is one of :data:`_FIELDS` — an unknown key
        raises ``AttributeError`` rather than silently creating an unused
        attribute (a misspelled override would otherwise surface only as a
        confusing downstream assertion diff).
    Postconditions:
        The constructed instance exposes every field
        :func:`trace_store._record_to_row` reads *except*
        ``cache_read_tokens``/``cache_creation_tokens``, with any
        ``overrides`` values applied on top of those defaults. The two cache
        fields are set only when passed as an override — a bare
        ``TraceCallRecord()`` has no cache-token attributes at all (not
        merely zero), which is what lets it double as the "cache fields
        missing entirely" case without a separate stub.
    """

    def __init__(self, **overrides: Any) -> None:
        self.timestamp = datetime.now(tz=timezone.utc).timestamp()
        self.team = "software_engineering"
        self.agent_key = "backend"
        self.job_id = "j1"
        self.task_id = "t1"
        self.phase = "execution"
        self.model = "m"
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.total_tokens = 15
        self.cost_usd = 0.01
        self.latency_ms = 100
        self.status = "success"
        self.outcome = "success"
        self.objective = "o"
        self.request_id = "r1"
        for k, v in overrides.items():
            if k not in _FIELDS:
                raise AttributeError(f"Unknown TraceCallRecord attribute: {k!r}")
            setattr(self, k, v)


__all__ = ["TraceCallRecord"]
