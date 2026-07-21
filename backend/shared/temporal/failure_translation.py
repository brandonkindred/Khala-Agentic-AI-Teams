"""Shared Temporal workflow-failure → native-exception translation.

An activity re-shapes a domain exception as a typed ``ApplicationError``
(``raise SomeDomainError(...)`` inside the activity, tagged by Temporal with
``type=SomeDomainError.__name__``); Temporal then surfaces that at the
dispatch boundary as a ``WorkflowFailureError`` whose cause chain carries the
marker. :func:`translate_workflow_failure` walks that chain and re-raises the
*native* exception so a route's untouched ``except SomeDomainError:`` mapping
still applies through Temporal exactly as it would in a non-Temporal path.

This is the single, shared implementation of the "walk the cause chain, match
an ``ApplicationError.type`` marker, re-raise the native exception" pattern —
teams should call this rather than hand-rolling their own bounded walk, so the
walk's cycle-safety and chain-attribute conventions can't drift between call
sites.
"""

from __future__ import annotations

# Bounded walk so a cyclic/adversarial cause chain can never loop forever.
DEFAULT_MAX_CAUSE_DEPTH = 12


def translate_workflow_failure(
    exc: BaseException,
    marker_exceptions: dict[str, type[Exception]],
    *,
    max_depth: int = DEFAULT_MAX_CAUSE_DEPTH,
) -> None:
    """Re-raise the native exception a workflow failure's cause chain carries.

    Walks the standard exception chain (``__cause__`` / ``__context__``) for
    an ``ApplicationError``-shaped node whose ``type`` marker is a key in
    ``marker_exceptions`` and re-raises the mapped native exception. Temporal
    surfaces the marker either at the top of the chain (the activity raised it
    directly) or nested under an ``ActivityError``; the walk handles both.
    (Temporal's ``FailureError.cause`` is defined as an alias of
    ``__cause__``, so the standard attributes cover both temporalio and plain
    exceptions.)

    Preconditions:
        * ``exc`` is the exception caught at the dispatch boundary (typically
          a ``temporalio.client.WorkflowFailureError``).
        * ``marker_exceptions`` maps marker strings (matched against each
          node's ``type`` attribute) to the native exception class to raise.
    Postconditions:
        * Raises ``marker_exceptions[marker](message)`` (chained ``from exc``)
          the first time a matching marker is found in the chain, preferring
          the node's ``message`` attribute for the new exception's text and
          falling back to ``str(node)``. Returns normally (raising nothing) if
          no match is found within ``max_depth`` hops — the caller re-raises
          ``exc`` itself. Never raises anything other than the one mapped
          native exception; bounded and cycle-safe (id-based visited set).
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    depth = 0
    while node is not None and id(node) not in seen and depth < max_depth:
        seen.add(id(node))
        depth += 1
        marker = getattr(node, "type", None)
        native = marker_exceptions.get(marker) if isinstance(marker, str) else None
        if native is not None:
            message = getattr(node, "message", None) or str(node)
            raise native(message) from exc
        node = node.__cause__ or node.__context__
