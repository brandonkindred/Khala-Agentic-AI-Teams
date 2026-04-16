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


def plan_critic_enabled() -> bool:
    """Master toggle for the independent plan-critic LLM pass.

    Defaults to OFF to allow safe rollout: flip to on once the critic has been
    calibrated against the golden eval set. When disabled, the planner behaves
    exactly as before the critic was introduced.
    """
    raw = (os.environ.get("BLOG_PLAN_CRITIC_ENABLED") or "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def plan_critic_max_iterations() -> int:
    """Cap on critic-driven refine iterations.

    Bounded to [1, 10] to prevent runaway loops when critic and planner oscillate.
    """
    raw = os.environ.get("BLOG_PLAN_CRITIC_MAX_ITERATIONS") or "3"
    try:
        return max(1, min(10, int(raw)))
    except ValueError:
        return 3


def plan_critic_model_override() -> Optional[str]:
    """
    When set, the plan-critic uses this Ollama model instead of the pipeline default.

    Unset by default; stays on qwen3.5 via the shared client. This hook exists so
    per-role model diversification can be enabled later without further code changes.
    """
    raw = (os.environ.get("BLOG_PLAN_CRITIC_MODEL") or "").strip()
    return raw or None
