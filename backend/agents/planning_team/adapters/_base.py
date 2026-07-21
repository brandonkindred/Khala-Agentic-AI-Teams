"""Shared base-URL resolution and URL building for planning_team adapters.

The three adapter modules (ai_systems, market_research, product_analysis)
each independently reimplemented the same shape: resolve a service base URL
from a team-specific env var (falling back to UNIFIED_API_BASE_URL), and
guard every call site with "no base URL -> debug-log -> return None" before
building the request URL. BaseAdapter is the single home for that shape.

It intentionally does NOT wrap the HTTP call or polling primitives
(post_json/get_json/poll_until_terminal from shared_http.job_polling) —
those stay imported and invoked directly inside each adapter module so
existing unit tests can keep patching them by module-qualified name
(e.g. planning_team.adapters.market_research.poll_until_terminal).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class BaseAdapter:
    """Resolves a downstream service's base URL and builds request URLs.

    Invariants:
        - env_var, path_prefix, and unconfigured_log are non-empty strings
          for the lifetime of the instance.
    """

    def __init__(self, *, env_var: str, path_prefix: str, unconfigured_log: str) -> None:
        """
        Preconditions:
            - env_var is a non-empty environment variable name.
            - path_prefix is a non-empty URL path segment (e.g. "/api/ai-systems").
            - unconfigured_log is a non-empty human-readable label used in the
              "no base URL" debug log line.
        """
        assert env_var, "env_var must be non-empty"
        assert path_prefix, "path_prefix must be non-empty"
        assert unconfigured_log, "unconfigured_log must be non-empty"
        self.env_var = env_var
        self.path_prefix = path_prefix
        self.unconfigured_log = unconfigured_log

    def base_url(self) -> Optional[str]:
        """
        Postconditions:
            - Returns os.environ[env_var] if set and non-empty, else
              os.environ["UNIFIED_API_BASE_URL"] if set and non-empty, else None.
        """
        return os.environ.get(self.env_var) or os.environ.get("UNIFIED_API_BASE_URL")

    def build_url(self, path: str) -> Optional[str]:
        """
        Preconditions:
            - path is a URL path segment beginning with "/" (e.g. "/build").
        Postconditions:
            - Returns None and logs a DEBUG "No base URL for {unconfigured_log};
              skipping." line when base_url() is None.
            - Otherwise returns f"{base_url}{path_prefix}{path}" with any
              trailing slash on base_url stripped before joining.
        """
        assert path.startswith("/"), f"path must start with '/', got {path!r}"
        base = self.base_url()
        if not base:
            logger.debug("No base URL for %s; skipping.", self.unconfigured_log)
            return None
        return f"{base.rstrip('/')}{self.path_prefix}{path}"
