"""Shared fixtures for Planning tests."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))


def make_llm(complete_text_return, *, max_ctx: int = 16384, complete_return: str = "CONDENSED"):
    """Build a fake LLMClient for the digestion path.

    The map step calls ``complete_text``; ``compact_text`` (oversized-section
    fallback) calls ``complete`` and ``get_max_context_tokens``. ``get_max_context_tokens``
    MUST return an int (the budget math does ``int(ctx * 3.5)``), so a bare
    ``MagicMock`` is insufficient — that is exactly what this helper guards against.

    Args:
        complete_text_return: value (or callable) returned by ``complete_text``; if a
            callable, it is used as the mock's ``side_effect`` so per-section prompts
            can return different payloads.
        max_ctx: model context size; use a small value to force multi-section splits.
        complete_return: value returned by ``complete`` (used by ``compact_text``).
    """
    llm = MagicMock()
    llm.get_max_context_tokens.return_value = max_ctx
    if callable(complete_text_return):
        llm.complete_text.side_effect = complete_text_return
    else:
        llm.complete_text.return_value = complete_text_return
    llm.complete.return_value = complete_return
    return llm


@pytest.fixture
def llm_factory():
    """Expose ``make_llm`` as a fixture for tests that prefer fixture injection."""
    return make_llm


def multi_heading_doc(n: int, body_chars: int) -> str:
    """Build a markdown doc with ``n`` heading-blocks of ~``body_chars`` each.

    Shared test helper for forcing multi-section splits (used by the spec_digest
    and phase tests).
    """
    return "".join(f"# Heading {i}\n" + ("b" * body_chars) + "\n" for i in range(n))
