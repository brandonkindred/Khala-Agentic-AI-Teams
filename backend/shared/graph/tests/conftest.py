"""Shared fixtures for shared.graph tests."""

from __future__ import annotations

import os

# Agent construction resolves a backing Strands model immediately, which
# raises ``LLMNotConfiguredError`` with no provider configured. The root
# ``backend/conftest.py`` doesn't set this; the setdefault keeps the suite
# runnable standalone without overriding a provider a caller deliberately
# chose (mirrors ``branding_team/tests/conftest.py``).
os.environ.setdefault("LLM_PROVIDER", "dummy")
