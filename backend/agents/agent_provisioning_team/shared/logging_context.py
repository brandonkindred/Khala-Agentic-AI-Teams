"""
Structured logging context for the Agent Provisioning Team.

Adds ``contextvars`` for ``job_id``, ``agent_id`` and ``phase`` so log lines
can be correlated across phases without threading the values through every
function signature. A ``ProvisioningContextFilter`` injects the current
values into every ``LogRecord``.

Usage:

    from .shared.logging_context import provisioning_context, install_filter

    install_filter()  # idempotent; once at process start

    with provisioning_context(job_id=job_id, agent_id=agent_id, phase="setup"):
        logger.info("starting setup")
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

_job_id_var: ContextVar[Optional[str]] = ContextVar("provisioning_job_id", default=None)
_agent_id_var: ContextVar[Optional[str]] = ContextVar("provisioning_agent_id", default=None)
_phase_var: ContextVar[Optional[str]] = ContextVar("provisioning_phase", default=None)


def get_job_id() -> Optional[str]:
    return _job_id_var.get()


def get_agent_id() -> Optional[str]:
    return _agent_id_var.get()


def get_phase() -> Optional[str]:
    return _phase_var.get()


@contextmanager
def provisioning_context(
    job_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    phase: Optional[str] = None,
) -> Iterator[None]:
    """Bind correlation IDs for the duration of a block.

    Any ``None`` argument leaves the existing value untouched.
    """
    tokens = []
    if job_id is not None:
        tokens.append(_job_id_var.set(job_id))
    if agent_id is not None:
        tokens.append(_agent_id_var.set(agent_id))
    if phase is not None:
        tokens.append(_phase_var.set(phase))
    try:
        yield
    finally:
        # Reset in reverse order so nested contexts unwind cleanly.
        for tok in reversed(tokens):
            try:
                tok.var.reset(tok)
            except (LookupError, ValueError):
                pass


class ProvisioningContextFilter(logging.Filter):
    """Inject job_id / agent_id / phase onto every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.job_id = _job_id_var.get() or "-"
        record.agent_id = _agent_id_var.get() or "-"
        record.phase = _phase_var.get() or "-"
        return True


_INSTALLED = False


def install_filter(logger_name: str = "agents.agent_provisioning_team") -> None:
    """Idempotently attach the context filter to the package logger."""
    global _INSTALLED
    if _INSTALLED:
        return
    target = logging.getLogger(logger_name)
    target.addFilter(ProvisioningContextFilter())
    # Also attach to the bare module logger used by the package today.
    logging.getLogger("agent_provisioning_team").addFilter(ProvisioningContextFilter())
    _INSTALLED = True
