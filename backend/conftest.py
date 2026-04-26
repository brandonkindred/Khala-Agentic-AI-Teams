"""Root pytest configuration for the backend test suite.

In addition to fast-fail LLM defaults, this conftest spins up the central job
service (``backend/job_service/main.py``) in-process for the duration of the
test session.  Every team's ``JobServiceClient(...)`` then talks to the
in-process server, with no file-backed fallback.

When ``POSTGRES_HOST`` is unset we fall back to setting a placeholder URL so
that *importing* team modules (which build module-level ``JobServiceClient``
instances) does not crash at collection time.  Any test that actually issues
a request will then fail loudly with a connection error — matching the
project policy that all migrated teams require Postgres for local dev/tests.
"""

from __future__ import annotations

import atexit
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fast-fail LLM defaults
# ---------------------------------------------------------------------------

os.environ.setdefault("LLM_MAX_RETRIES", "0")


# ---------------------------------------------------------------------------
# Job service in-process spin-up
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent
_JOB_SERVICE_DIR = _BACKEND_ROOT / "job_service"
_AGENTS_DIR = _BACKEND_ROOT / "agents"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _postgres_configured() -> bool:
    return bool(os.environ.get("POSTGRES_HOST"))


def _start_job_service() -> str | None:
    """Start the job service on a free port and return its base URL.

    Returns ``None`` (and emits a warning) if Postgres is not configured —
    in that case we leave a placeholder ``JOB_SERVICE_URL`` set so that
    module imports succeed.
    """
    if not _postgres_configured():
        logger.warning(
            "POSTGRES_HOST not set — skipping in-process job service spin-up. Tests that exercise job state will fail."
        )
        return None

    # The job service module imports use ``from db import …`` etc. so its
    # directory must be on sys.path before we import its app.
    if str(_JOB_SERVICE_DIR) not in sys.path:
        sys.path.insert(0, str(_JOB_SERVICE_DIR))
    if str(_AGENTS_DIR) not in sys.path:
        sys.path.insert(0, str(_AGENTS_DIR))

    import uvicorn
    from job_service.main import app  # type: ignore[import-not-found]

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, name="job-service-test", daemon=True)
    thread.start()

    # Wait for the server to be ready (max 5s)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("In-process job service failed to start within 5s")

    base_url = f"http://127.0.0.1:{port}"

    @atexit.register
    def _shutdown() -> None:
        server.should_exit = True
        thread.join(timeout=2.0)

    return base_url


# Set JOB_SERVICE_URL *before* any team module imports.  If the user already
# exported one (e.g. pointing at a docker-compose job-service), respect it.
if not os.environ.get("JOB_SERVICE_URL"):
    _resolved = _start_job_service()
    os.environ["JOB_SERVICE_URL"] = _resolved or "http://127.0.0.1:1"


# ---------------------------------------------------------------------------
# Per-test isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _truncate_jobs_table() -> None:
    """Wipe the shared ``jobs`` table before each test for isolation."""
    if not _postgres_configured():
        return
    try:
        sys.path.insert(0, str(_JOB_SERVICE_DIR))
        from db import get_conn  # type: ignore[import-not-found]

        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE jobs")
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not truncate jobs table between tests: %s", exc)
