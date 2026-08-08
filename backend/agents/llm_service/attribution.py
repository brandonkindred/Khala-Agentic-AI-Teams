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
``contextvars.copy_context()`` (see ``shared/concurrency/heartbeat.py``).

Usage::

    from llm_service import llm_attribution

    with llm_attribution(team="blogging", objective="draft intro section"):
        client.complete(prompt, objective="draft intro section")

The pattern mirrors ``agent_team_studio/agent_provisioning_team/shared/logging_context.py``.
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

    ``task_id`` and ``phase`` locate the call within the Software Engineering
    pipeline (which task, and which of discovery/design/execution/integration)
    so spans and per-job cost can be sliced finely. They default to ``""`` and
    are simply inherited as empty by teams that don't set them.
    """

    agent_key: str = ""
    team: str = ""
    objective: str = ""
    job_id: str = ""
    task_id: str = ""
    phase: str = ""


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
    task_id: Optional[str] = None,
    phase: Optional[str] = None,
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

    Yields:
        The merged :class:`LLMAttribution` active for the block, so callers may
        inspect the effective attribution (``with llm_attribution(...) as attr``).
    """
    base = _attribution.get()
    merged = replace(
        base,
        agent_key=base.agent_key if agent_key is None else agent_key,
        team=base.team if team is None else team,
        objective=base.objective if objective is None else objective,
        job_id=base.job_id if job_id is None else job_id,
        task_id=base.task_id if task_id is None else task_id,
        phase=base.phase if phase is None else phase,
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
    if not request_id:
        # Explicit validation rather than ``assert``: the precondition must hold
        # even under ``python -O`` (which strips asserts), or attribution would
        # silently bind an empty id and break log/telemetry correlation.
        raise ValueError("request_id must be a non-empty string")
    prev = _request_id.get()
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        try:
            _request_id.reset(token)
        except (LookupError, ValueError):  # pragma: no cover - context torn down out of order
            _request_id.set(prev)


def _find_agent_frame() -> Optional[list[str]]:
    """Return the source-path parts after ``/agents/`` for the originating frame.

    Walks the call stack for the innermost frame physically located under
    ``backend/agents/<team>/`` that is *not* owned by ``llm_service`` (skipping
    third-party Strands / asyncio frames and the central client itself), and
    returns the ``"/"``-split path components after the ``/agents/`` marker —
    so ``[0]`` is the team directory and ``[-1]`` is the filename. This is the
    shared basis for both :func:`caller_team` (which wants ``[0]``) and
    :func:`caller_agent` (which wants a finer identity), centralizing the
    frame-walk and the CPython-specific ``sys._getframe`` fallback in one place.

    Source path is used (rather than import-name inspection) because it is a
    reliable identifier regardless of how a team flattens its package onto
    ``sys.path``.

    Must be called *directly* from :func:`caller_team` / :func:`caller_agent`:
    it starts the walk at ``sys._getframe(2)`` to skip its own frame and its
    caller's, landing on the originating code. That code must still be on the
    stack — evaluate before an ``asyncio.to_thread`` hand-off, not inside the
    worker thread (whose stack holds only executor frames).

    Postconditions: returns the path-part list for the innermost matching frame,
        or ``None`` when no such frame exists or the runtime lacks
        ``sys._getframe`` (attribution then degrades to the explicitly-bound
        value and ``caller_tag``, it does not error).
    """
    import sys

    getframe = getattr(sys, "_getframe", None)
    if getframe is None:  # pragma: no cover - non-CPython fallback
        return None
    marker = "/agents/"
    frame = getframe(2)  # skip _find_agent_frame + its caller (caller_team/agent)
    while frame is not None:
        path = (frame.f_code.co_filename or "").replace("\\", "/")
        idx = path.find(marker)
        if idx != -1:
            rest = path[idx + len(marker) :].split("/")
            if rest and rest[0] and rest[0] != "llm_service":
                return rest
        frame = frame.f_back
    return None


def caller_team() -> str:
    """Return the team that owns the calling code, derived from its source path.

    Every team's code lives under ``backend/agents/<team>/``, so the team
    directory name is a reliable identifier regardless of how a team flattens
    its package onto ``sys.path``. Delegates the frame-walk to
    :func:`_find_agent_frame`; see its docstring for the ``sys._getframe`` caveat
    and the "must run before an ``asyncio.to_thread`` hand-off" requirement.

    Postconditions: returns the ``<team>`` directory name of the innermost stack
        frame physically located under ``agents/`` and not owned by
        ``llm_service``; returns ``""`` when no such frame exists.
    """
    rest = _find_agent_frame()
    return rest[0] if rest else ""


# Directory names that are containers rather than an agent's own package — when
# the calling file sits directly in one, its filename stem is the better identity.
#
# Maintenance note: this is a deliberately conservative denylist, not an
# exhaustive one. It must be extended when a new *generic container* directory
# convention is introduced (e.g. a shared ``handlers/`` or ``runners/`` layer);
# the failure mode of omitting one is benign and self-evident — ``caller_agent``
# reports the container directory name (e.g. ``handlers``) instead of the finer
# file stem, which is visibly wrong in logs and points straight back here. It is
# intentionally a module constant rather than env-configurable: the value is a
# property of the repository's directory layout, known at author time, not a
# per-deployment tuning knob.
_GENERIC_AGENT_DIRS = frozenset(
    {"agents", "shared", "tool_agents", "phases", "agent_implementations", "api", "critics"}
)


def caller_agent() -> str:
    """Best-effort agent identity derived from the calling code's source path.

    A fallback for the structured ``agent_key`` field when no explicit key is
    configured (e.g. ``get_strands_model()`` called without one). Mirrors
    :func:`caller_team` — same source-path basis (via :func:`_find_agent_frame`)
    and ``sys._getframe`` caveat — but resolves a finer identity: the package
    directory immediately containing the calling file (e.g. ``ui_design``), or
    the file's stem when that directory is a generic container (e.g.
    ``.../agents/ranker.py`` → ``ranker``).

    Postconditions: returns a non-empty identity for the innermost ``agents/``
        frame not owned by ``llm_service``; returns ``""`` when none exists.
    """
    rest = _find_agent_frame()
    if not rest:
        return ""
    team = rest[0]
    stem = rest[-1].rsplit(".", 1)[0]
    parent = rest[-2] if len(rest) >= 2 else ""
    if parent and parent != team and parent not in _GENERIC_AGENT_DIRS:
        return parent
    if stem and stem not in ("__init__", "agent", "agents", "main"):
        return stem
    return parent or stem


__all__ = [
    "LLMAttribution",
    "current_attribution",
    "current_request_id",
    "new_request_id",
    "llm_attribution",
    "bind_request_id",
    "caller_team",
    "caller_agent",
]
