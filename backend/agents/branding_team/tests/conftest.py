"""Shared fixtures for branding_team tests."""

from __future__ import annotations

import os
from typing import Any

import pytest

# Every agent factory resolves a backing Strands model at construction time,
# which raises ``LLMNotConfiguredError`` with no provider configured. CI sets
# this at the job level; the setdefault keeps the suite runnable standalone
# without overriding a provider a caller deliberately chose.
os.environ.setdefault("LLM_PROVIDER", "dummy")

from branding_team.models import BrandingMission  # noqa: E402
from branding_team.shared.phase_output_cache import clear_phase_output_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_phase_output_cache():
    """Clear the process-global phase-output cache around every test.

    ``PhaseOutputCache`` now wraps a namespaced ``shared.cache`` singleton
    (see ``phase_output_cache.py``), shared by every instance in this
    process. Without a reset, one test's cached phase output could be served
    to another test constructing an unrelated ``PhaseOutputCache()``.
    """
    clear_phase_output_cache()
    yield
    clear_phase_output_cache()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_postgres: run against the real shared.postgres connection",
    )
    config.addinivalue_line(
        "markers",
        "real_llm: invoke a live LLM provider; skipped unless LLM_PROVIDER and resolve_provider() are non-dummy",
    )


def make_mission(**overrides: Any) -> BrandingMission:
    """Build a ``BrandingMission`` with sensible defaults for branding_team tests.

    Preconditions:
        Every key in ``overrides`` must be a valid ``BrandingMission`` field name.
    Postconditions:
        Returns a ``BrandingMission`` whose fields equal the defaults below,
        with any provided ``overrides`` taking precedence.
    """
    defaults: dict[str, Any] = {
        "company_name": "Northstar Labs",
        "company_description": "A studio for product teams",
        "target_audience": "enterprise product leaders",
        "values": ["clarity", "trust", "tech"],
        "differentiators": ["hands-on partnership", "execution speed"],
    }
    defaults.update(overrides)
    return BrandingMission(**defaults)
