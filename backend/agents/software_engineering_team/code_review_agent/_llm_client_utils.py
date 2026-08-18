"""Shared LLM-client detection helpers for the code-review verification passes.

Lives in a common location — not inside any single pass — so passes that need
to detect the production dummy harness (``scope_filter``, ``scope_classifier``)
depend on this stable helper rather than on each other's private internals.

This is distinct from :func:`llm_service.clients.dummy.is_dummy_llm_client_wrapped`,
which uses ``isinstance`` and therefore also matches scripted ``DummyLLMClient``
*subclasses* used as test doubles. :func:`is_unscripted_dummy` deliberately uses
an *exact* type check so those scripted subclasses are NOT treated as the no-op
harness and still exercise the real code path in tests.
"""

from __future__ import annotations

from typing import Any


def is_unscripted_dummy(llm: Any) -> bool:
    """True for the production dummy harness, not scripted test subclasses.

    Preconditions: ``llm`` may be any object (including ``None``).
    Postconditions: ``True`` iff ``llm`` or ``llm.client`` is exactly
        ``DummyLLMClient`` (not a subclass used as a test stub), so the no-LLM
        harness short-circuits while scripted stubs still run the real path.
        Pure; **never raises unconditionally** — any failure (a misbehaving
        ``.client`` descriptor, or the lazy ``DummyLLMClient`` import itself
        failing) degrades to ``False`` rather than propagating. A ``False`` on a
        failed import is safe: the object cannot be a ``DummyLLMClient`` the
        importer couldn't even load.
    """
    try:
        from llm_service.clients.dummy import DummyLLMClient

        if type(llm) is DummyLLMClient:
            return True
        inner = getattr(llm, "client", None)
        return type(inner) is DummyLLMClient
    except Exception:  # noqa: BLE001 — import / descriptor failure must not break the never-raises contract
        return False
