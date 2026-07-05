"""Bridge code-review-agent progress reports onto a phase detail callback."""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, Optional


def call_code_review_agent(
    code_review_agent: Any,
    cr_input: Any,
    detail_callback: Optional[Callable[[str], None]],
    repo_reader: Any = None,
) -> Any:
    """Invoke a code review agent, bridging its progress reports to ``detail_callback``.

    The bridge turns ``(step, detail, fraction)`` reports into phase-detail strings
    ("Code review 40%: chunk 2/5 ...") that flow through the existing microtask
    progress channel. Injected fake/external agents may not accept the
    ``progress_callback``/``repo_reader`` kwargs, so each is only passed when the
    agent's ``run`` signature declares it (or a ``**kwargs`` catch-all) —
    otherwise the ``except Exception`` around the call sites would silently divert
    a perfectly good agent to the LLM fallback.

    Preconditions:
        - ``code_review_agent`` has a callable ``run`` accepting ``cr_input``.
        - ``detail_callback`` is None or a non-raising single-string callable.
        - ``repo_reader`` is None or a ``RepoReader`` giving the false-positive
          verifier whole-repo read access.

    Postconditions:
        - Returns ``code_review_agent.run(...)``'s result unchanged.
        - When ``detail_callback`` is provided and the agent supports progress
          reporting, each report is forwarded as one formatted string.
        - ``repo_reader`` is forwarded only when non-None and the agent's ``run``
          accepts it, so an agent without the kwarg is never broken by it.
    """
    run_kwargs: Dict[str, Any] = {}
    if detail_callback is not None and _accepts_kwarg(code_review_agent.run, "progress_callback"):
        run_kwargs["progress_callback"] = lambda step, detail, fraction: detail_callback(
            f"Code review {round(fraction * 100)}%: {detail or step}"
        )
    if repo_reader is not None and _accepts_kwarg(code_review_agent.run, "repo_reader"):
        run_kwargs["repo_reader"] = repo_reader
    return code_review_agent.run(cr_input, **run_kwargs)


def _accepts_kwarg(run: Any, name: str) -> bool:
    """True when ``run`` can accept a keyword argument named ``name``.

    Accepts both an explicitly declared parameter and a ``**kwargs`` catch-all —
    a forward-compatible wrapper like ``def run(self, inp, **kwargs)`` forwards
    the kwarg and must not silently lose it.

    Postconditions: returns False (never raises) for un-inspectable callables,
    so an exotic injected fake degrades to a call without ``name`` instead of
    being diverted to the LLM fallback by the call sites' broad ``except``.
    """
    try:
        params = inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False
    if name in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
