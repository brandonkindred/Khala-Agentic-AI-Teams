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
    if detail_callback is not None:
        try:
            accepts_progress = (
                "progress_callback" in inspect.signature(code_review_agent.run).parameters
            )
        except (TypeError, ValueError):
            accepts_progress = False
        if accepts_progress:
            run_kwargs["progress_callback"] = lambda step, detail, fraction: detail_callback(
                f"Code review {int(fraction * 100)}%: {detail or step}"
            )
    return code_review_agent.run(cr_input, **run_kwargs)
