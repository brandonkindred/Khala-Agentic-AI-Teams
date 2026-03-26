"""Environment-driven defaults for the blog planning agent."""

from __future__ import annotations

import os
from typing import Optional


def planning_max_iterations() -> int:
    return max(1, min(20, int(os.environ.get("BLOG_PLANNING_MAX_ITERATIONS", "5"))))


def planning_max_parse_retries() -> int:
    return max(1, min(10, int(os.environ.get("BLOG_PLANNING_MAX_PARSE_RETRIES", "3"))))


def planning_model_override() -> Optional[str]:
    """
    When set, the planning phase uses this Ollama model name instead of the pipeline default.

    Same base URL and auth as `LLM_*` / `OLLAMA_API_KEY`. Ignored for non-Ollama test clients.
    """
    raw = (os.environ.get("BLOG_PLANNING_MODEL") or "").strip()
    return raw or None
