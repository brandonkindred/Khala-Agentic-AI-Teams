"""
Shared logging configuration for software engineering team agents.

Use this to get consistent, readable logs when running agents or the API.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Optional


class HttpxErrorLevelFilter(logging.Filter):
    """Elevate httpx 5xx responses from INFO to ERROR."""

    _5XX_PATTERN = re.compile(r'"HTTP/[\d.]+ (5\d{2})')

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno == logging.INFO:
            match = self._5XX_PATTERN.search(record.getMessage())
            if match:
                record.levelno = logging.ERROR
                record.levelname = "ERROR"
        return True


LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%H:%M:%S"

# Agent and infrastructure logger names (enable DEBUG for verbose step-by-step visibility)
AGENT_LOGGERS = [
    # Orchestrator and infrastructure
    "orchestrator",
    "shared.git_utils",
    "shared.repo_writer",
    "shared.job_store",
    "shared.command_runner",
    # Agent loggers
    "architecture_expert.agent",
    "tech_lead_agent.agent",
    "backend_agent.agent",
    "frontend_team.feature_agent.agent",
    "devops_agent.agent",
    "security_agent.agent",
    "qa_agent.agent",
    "code_review_agent.agent",
    "dbc_comments_agent.agent",
    "documentation_agent.agent",
    "spec_parser",
    "api.main",
]


def setup_logging(
    level: int = logging.INFO,
    *,
    agent_level: Optional[int] = None,
    log_file: Optional[Path] = None,
    verbose: bool = False,
) -> None:
    """
    Configure logging for the software engineering team.

    Args:
        level: Root log level (default INFO).
        agent_level: Override level for agent loggers. If None, uses `level`.
        log_file: Optional path to write logs to a file.
        verbose: If True, set agent loggers to DEBUG for step-by-step visibility.
    """
    agent_level = agent_level or (logging.DEBUG if verbose else level)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers to avoid duplicates when called multiple times
    for h in root.handlers[:]:
        root.removeHandler(h)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    for name in AGENT_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(agent_level)

    # Apply filter to httpx logger to elevate 5xx errors to ERROR level
    # Also set to WARNING to suppress routine request INFO logs
    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addFilter(HttpxErrorLevelFilter())
    httpx_logger.setLevel(logging.WARNING)

    # Suppress verbose LLM request INFO logs (only show warnings/errors)
    llm_logger = logging.getLogger("shared.llm")
    llm_logger.setLevel(logging.WARNING)

    # Suppress uvicorn access logs (HTTP request logs) - only show warnings/errors
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.setLevel(logging.WARNING)
