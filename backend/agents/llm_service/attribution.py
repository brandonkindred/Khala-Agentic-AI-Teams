"""Per-call LLM attribution context.

Carries *who* made an LLM call (``agent_key``/``team``), *why* (``objective``),
and the owning ``job_id`` — plus a per-call ``request_id`` — through the call
stack via :mod:`contextvars` so the central client can stamp every log line and
telemetry record without each of the ~hundreds of call sites threading the data
through by hand.

Why contextvars (and not instance attributes on the client): the Ollama client
is a process-wide cached singleton shared by every concurrent agent, so storing
per-request attribution on ``self`` is racy. ContextVars are per-thread / per
asyncio-task, so two agents sharing one client each see their own attribution.
``asyncio.to_thread`` (used by the Strands adapter) copies the active context
into the worker thread automatically; raw ``ThreadPoolExecutor`` fan-out does
not — callers that fan out across raw threads should propagate context with
``contextvars.copy_context()`` (see ``shared_concurrency/heartbeat.py``).

Usage::

    from llm_service import llm_attribution

    with llm_attribution(team="blogging", objective="draft intro section"):
        client.complete(prompt, objective="draft intro section")

The pattern mirrors ``agent_provisioning_team/shared/logging_context.py``.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator, Optional


@dataclass(frozen=True)
class LLMAttribution:
    """Immutable attribution for an LLM call.

    Invariants:
        - All fields are plain strings; an unset field is ``""`` (never ``None``),
          so log/telemetry formatting never has to guard for ``None``.
    """

    agent_key: str = ""
    team: str = ""
    objective: str = ""
    job_id: str = ""


_EMPTY = LLMAttribution()

_attribution: ContextVar[LLMAttribution] = ContextVar("llm_attribution", default=_EMPTY)
_request_id: ContextVar[str] = ContextVar("llm_request_id", default="")


def current_attribution() -> LLMAttribution:
    """Return the attribution active on the current thread/task.

    Postconditions: returns the most recently entered :class:`LLMAttribution`,
        or the empty default when no :func:`llm_attribution` block is active.
    """
    return _attribution.get()


def current_request_id() -> str:
    """Return the request id bound for the in-flight call, or ``""`` when none.

    Postconditions: non-empty only while a :func:`bind_request_id` block is
        active on this thread/task.
    """
    return _request_id.get()


def new_request_id() -> str:
    """Return a short, unique, log-friendly request id.

    Postconditions: returns a 12-char lowercase hex string; successive calls
        return distinct values with overwhelming probability (uuid4-derived).
    """
    return uuid.uuid4().hex[:12]


@contextmanager
def llm_attribution(
    *,
    agent_key: Optional[str] = None,
    team: Optional[str] = None,
    objective: Optional[str] = None,
    job_id: Optional[str] = None,
) -> Iterator[LLMAttribution]:
    """Bind LLM attribution for the duration of the ``with`` block.

    Each argument is *merged* onto the currently active attribution: ``None``
    inherits the existing value, a non-``None`` value (including ``""``)
    overrides it. Blocks nest — an inner block may refine only the fields it
    passes while inheriting the rest from the enclosing block.

    Preconditions: each provided argument is a ``str`` or ``None``.
    Postconditions: inside the block, :func:`current_attribution` reflects the
        merged value; on exit (including via exception) the exact attribution
        that was active on entry is restored.
    """
    base = _attribution.get()
    merged = replace(
        base,
        agent_key=base.agent_key if agent_key is None else agent_key,
        team=base.team if team is None else team,
        objective=base.objective if objective is None else objective,
        job_id=base.job_id if job_id is None else job_id,
    )
    token = _attribution.set(merged)
    try:
        yield merged
    finally:
        try:
            _attribution.reset(token)
        except (LookupError, ValueError):  # pragma: no cover - context torn down out of order
            _attribution.set(base)


@contextmanager
def bind_request_id(request_id: str) -> Iterator[str]:
    """Bind ``request_id`` for the duration of the ``with`` block.

    Preconditions: ``request_id`` is a non-empty string.
    Postconditions: inside the block, :func:`current_request_id` returns
        ``request_id``; on exit (including via exception) the previous value is
        restored.
    """
    assert request_id, "request_id must be non-empty"
    prev = _request_id.get()
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        try:
            _request_id.reset(token)
        except (LookupError, ValueError):  # pragma: no cover - context torn down out of order
            _request_id.set(prev)


def caller_team() -> str:
    """Return the team that owns the calling code, derived from its source path.

    Every team's code lives under ``backend/agents/<team>/``, so the team
    directory name is a reliable identifier regardless of how a team flattens
    its package onto ``sys.path`` (so it succeeds where import-name inspection
    does not). The walk starts at the immediate caller and skips ``llm_service``
    and any non-team frames (e.g. third-party Strands / asyncio frames), so it
    finds the originating agent even when the call is dispatched through an
    intermediary.

    Must be evaluated on a thread/task whose stack still contains the agent
    frame — e.g. before an ``asyncio.to_thread`` hand-off, not inside the
    worker thread (whose stack holds only executor frames).

    Implementation note: uses ``sys._getframe`` (CPython-specific, chosen over
    ``inspect.stack()`` to avoid reading source context on every LLM call). On a
    runtime without ``sys._getframe`` this returns ``""`` — attribution degrades
    to the explicitly-bound team and ``caller_tag``, it does not error.

    Postconditions: returns the ``<team>`` directory name of the innermost stack
        frame physically located under ``agents/`` and not owned by
        ``llm_service``; returns ``""`` when no such frame exists.
    """
    import sys

    getframe = getattr(sys, "_getframe", None)
    if getframe is None:  # pragma: no cover - non-CPython fallback
        return ""
    marker = "/agents/"
    frame = getframe(1)  # start at the immediate caller
    while frame is not None:
        path = (frame.f_code.co_filename or "").replace("\\", "/")
        idx = path.find(marker)
        if idx != -1:
            top = path[idx + len(marker) :].split("/", 1)[0]
            if top and top != "llm_service":
                return top
        frame = frame.f_back
    return ""


__all__ = [
    "LLMAttribution",
    "current_attribution",
    "current_request_id",
    "new_request_id",
    "llm_attribution",
    "bind_request_id",
    "caller_team",
]
