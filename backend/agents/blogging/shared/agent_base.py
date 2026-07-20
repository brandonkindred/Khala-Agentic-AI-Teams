"""
Shared constructor boilerplate for blogging agents.
"""

from __future__ import annotations

from typing import Any


class _BlogAgentBase:
    """
    Shared constructor boilerplate for blogging agents.

    Preconditions:
        - llm_client is not None.
    Postconditions:
        - self._model is llm_client.
    """

    def __init__(self, llm_client: Any) -> None:
        assert llm_client is not None, "llm_client is required"
        self._model = llm_client
