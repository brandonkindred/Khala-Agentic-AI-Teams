"""Root pytest configuration for the backend test suite.

Sets environment variables that keep tests fast and deterministic when a real
LLM is not available (e.g. in CI).  Each variable can still be overridden by
the environment before pytest starts.
"""

import os

# Disable LLM retries so tests that hit an unavailable LLM fail fast and fall
# through to structural fallback paths rather than waiting minutes on backoff.
os.environ.setdefault("LLM_MAX_RETRIES", "0")
