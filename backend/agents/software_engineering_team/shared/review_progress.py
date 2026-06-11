"""Bridge code-review-agent progress reports onto a phase detail callback."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Optional


def call_code_review_agent(
    code_review_agent: Any,
    cr_input: Any,
    detail_callback: Optional[Callable[[str], None]],
) -> Any:
    """Invoke a code review agent, bridging its progress reports to ``detail_callback``.

    The bridge turns ``(step, detail, fraction)`` reports into phase-detail strings
    ("Code review 40%: chunk 2/5 ...") that flow through the existing microtask
    progress channel. Injected fake/external agents may not accept the
    ``progress_callback`` kwarg, so it is only passed when the agent's ``run``
    signature declares it — otherwise the ``except Exception`` around the call
    sites would silently divert a perfectly good agent to the LLM fallback.

    Preconditions:
        - ``code_review_agent`` has a callable ``run`` accepting ``cr_input``.
        - ``detail_callback`` is None or a non-raising single-string callable.

    Postconditions:
        - Returns ``code_review_agent.run(...)``'s result unchanged.
        - When ``detail_callback`` is provided and the agent supports progress
          reporting, each report is forwarded as one formatted string.
    """
    run_kwargs: Dict[str, Any] = {}
    if detail_callback is not None and _accepts_progress_kwarg(code_review_agent.run):
        run_kwargs["progress_callback"] = lambda step, detail, fraction: detail_callback(
            f"Code review {round(fraction * 100)}%: {detail or step}"
        )
    return code_review_agent.run(cr_input, **run_kwargs)


def _accepts_progress_kwarg(run: Any) -> bool:
    """True when ``run`` can accept a ``progress_callback`` keyword argument.

    Accepts both an explicitly declared parameter and a ``**kwargs`` catch-all —
    a forward-compatible wrapper like ``def run(self, inp, **kwargs)`` forwards
    the kwarg and must not silently lose progress reporting.

    Postconditions: returns False (never raises) for un-inspectable callables,
    so an exotic injected fake degrades to a progress-less call instead of being
    diverted to the LLM fallback by the call sites' broad ``except``.
    """
    try:
        params = inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False
    if "progress_callback" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
