"""Resolve a manifest's ``source.entrypoint`` and call it with a request body.

Handles three shapes:
  1. Pydantic / plain class with a ``run`` method.
  2. Pydantic / plain class callable (``__call__``).
  3. Plain function (or factory whose name starts with ``make_`` — call it to get
     the agent object, then invoke its ``run``/``__call__``).

All user-space exceptions propagate; the shim turns them into HTTP 422 with a
trace. Import errors / missing symbols are AgentNotRunnableError → HTTP 500.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AgentNotRunnableError(RuntimeError):
    """The manifest's entrypoint cannot be loaded or invoked."""


async def invoke_entrypoint(entrypoint: str, body: Any) -> Any:
    """Import ``module:Symbol`` and call it with ``body``. Awaits if coroutine."""
    target = _resolve_entrypoint(entrypoint)
    callable_obj = _materialise(target)
    logger.info("dispatching to %s (body keys: %s)", entrypoint, _sample_keys(body))
    result = callable_obj(body)
    if inspect.iscoroutine(result):
        result = await result
    return result


def _resolve_entrypoint(entrypoint: str) -> Any:
    if ":" not in entrypoint:
        raise AgentNotRunnableError(
            f"Malformed entrypoint {entrypoint!r}; expected 'module.path:Symbol'."
        )
    module_path, symbol = entrypoint.split(":", 1)
    try:
        module = importlib.import_module(module_path)
    except Exception as exc:
        raise AgentNotRunnableError(f"Cannot import module {module_path!r}: {exc}") from exc
    if not hasattr(module, symbol):
        raise AgentNotRunnableError(f"Module {module_path!r} has no attribute {symbol!r}.")
    return getattr(module, symbol)


def _materialise(target: Any) -> Any:
    """Turn a class / factory / callable into a callable that accepts a request body."""
    # Factory convention: name starts with 'make_' → call with no args to get the agent.
    if callable(target) and getattr(target, "__name__", "").startswith("make_"):
        agent = target()
        return _bind_invocation(agent)

    # Class → instantiate with no args; invoke run() or __call__.
    if inspect.isclass(target):
        try:
            agent = target()
        except TypeError as exc:
            raise AgentNotRunnableError(
                f"Cannot instantiate {target.__name__} with no args: {exc}. "
                "Phase 2 invoke shim expects zero-arg constructors."
            ) from exc
        return _bind_invocation(agent)

    # Plain function → call directly.
    if callable(target):
        return target

    raise AgentNotRunnableError(f"Entrypoint {target!r} is not callable.")


def _bind_invocation(agent: Any):
    """Pick the right callable method on an agent instance."""
    for method_name in ("run", "invoke", "execute", "__call__"):
        method = getattr(agent, method_name, None)
        if callable(method):
            return method
    raise AgentNotRunnableError(
        f"Agent {type(agent).__name__} has no run()/invoke()/execute()/__call__ method."
    )


def _sample_keys(body: Any) -> list[str]:
    if isinstance(body, dict):
        return sorted(body.keys())[:8]
    return []


async def maybe_await(result: Any) -> Any:
    if inspect.iscoroutine(result):
        return await result
    if asyncio.isfuture(result):
        return await result
    return result
