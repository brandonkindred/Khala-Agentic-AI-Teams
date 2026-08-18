"""Tests for the shared unscripted-dummy detection helper.

``is_unscripted_dummy`` must flag only the exact production ``DummyLLMClient``
(so scripted test subclasses still run the real path), and honor its
never-raises contract even when a caller-supplied object misbehaves.
"""

from __future__ import annotations

from typing import Any

from code_review_agent._llm_client_utils import is_unscripted_dummy

from llm_service.clients.dummy import DummyLLMClient


def test_exact_dummy_is_flagged() -> None:
    """Precondition: the exact production ``DummyLLMClient``. Postcondition: True."""
    assert is_unscripted_dummy(DummyLLMClient()) is True


def test_scripted_subclass_is_not_flagged() -> None:
    """Precondition: a ``DummyLLMClient`` subclass (a scripted test double).

    Postcondition: False — the exact-type check lets scripted stubs run the
    real path.
    """

    class _Stub(DummyLLMClient):
        pass

    assert is_unscripted_dummy(_Stub()) is False


def test_wrapper_around_exact_dummy_is_flagged() -> None:
    """Precondition: a wrapper whose ``.client`` is the exact ``DummyLLMClient``.

    Postcondition: True (the ``.client`` fallback is checked).
    """

    class _Wrapper:
        def __init__(self) -> None:
            self.client = DummyLLMClient()

    assert is_unscripted_dummy(_Wrapper()) is True


def test_none_and_plain_object_are_not_flagged() -> None:
    """Precondition: ``None`` or an unrelated object. Postcondition: False, no raise."""
    assert is_unscripted_dummy(None) is False
    assert is_unscripted_dummy(object()) is False


def test_raising_client_descriptor_degrades_to_false() -> None:
    """Precondition: an object whose ``.client`` property raises a non-AttributeError.

    Postcondition: degrades to False rather than propagating — the never-raises
    contract holds unconditionally.
    """

    class _Hostile:
        @property
        def client(self) -> Any:
            raise RuntimeError("boom")

    assert is_unscripted_dummy(_Hostile()) is False
