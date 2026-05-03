"""Root pytest configuration for the backend test suite.

Test layering
-------------

* **Unit tests** (default) must not depend on Postgres or a running job
  service.  Use the :class:`FakeJobServiceClient` helper exposed via the
  ``fake_job_client`` fixture (defined in
  ``backend/agents/job_service_client_fake.py``) and ``monkeypatch`` it
  into the team module under test.

* **Integration tests** are marked ``@pytest.mark.integration``.  They are
  **skipped by default** and only run when pytest is invoked with
  ``-m integration`` (CI does this in the dedicated ``test-integration``
  job, with Postgres + the in-process job service spun up automatically).

The placeholder ``JOB_SERVICE_URL`` set below is just enough for team
modules that build a module-level ``JobServiceClient(team=…)`` to succeed
at import time.  Any actual HTTP call to it will fail loudly — exactly
what we want for unit tests that forgot to monkeypatch.
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
# Globally-applied env defaults — must run before any team module imports.
# ---------------------------------------------------------------------------

os.environ.setdefault("LLM_MAX_RETRIES", "0")
# Placeholder URL: makes JobServiceClient(team=…) construction succeed at
# import time.  Real HTTP calls will fail with a connection error.
os.environ.setdefault("JOB_SERVICE_URL", "http://127.0.0.1:1")


logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent
_JOB_SERVICE_DIR = _BACKEND_ROOT / "job_service"
_AGENTS_DIR = _BACKEND_ROOT / "agents"

# Ensure the agents/ directory is on sys.path so the fixture import below
# works even before pytest applies its own pythonpath additions.
if str(_AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTS_DIR))

# Re-export the shared in-memory fake + fixture.  Teams that override
# pytest's rootdir (e.g. SE) opt in by adding the same import to their own
# tests/conftest.py.
from job_service_client_fake import FakeJobServiceClient, fake_job_client  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Marker registration + default-skip for integration tests.
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: requires real Postgres + the central job service. Skipped unless invoked with `-m integration`.",
    )
    config.addinivalue_line(
        "markers",
        "bench: wall-clock benchmark. Skipped unless invoked with `-m bench`.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration-marked and bench-marked tests unless their marker
    was selected via ``-m``. Mirrors how the integration suite stays out
    of the default run; the bench suite (issue #377) is wall-clock
    sensitive and would slow normal test cycles.
    """
    selected = config.getoption("-m", default="") or ""
    if "integration" not in selected:
        skip = pytest.mark.skip(reason="integration test; run with `pytest -m integration`")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)
    if "bench" not in selected:
        skip_bench = pytest.mark.skip(reason="benchmark; run with `pytest -m bench`")
        for item in items:
            if "bench" in item.keywords:
                item.add_marker(skip_bench)


# ---------------------------------------------------------------------------
# Integration: spin up the real job service in-process, isolate per test.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _postgres_configured() -> bool:
    return bool(os.environ.get("POSTGRES_HOST"))


def _start_in_process_job_service() -> str:
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

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("In-process job service failed to start within 5s")

    @atexit.register
    def _shutdown() -> None:
        server.should_exit = True
        thread.join(timeout=2.0)

    return f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def integration_job_service() -> str:
    """Session-scoped: boot the real job service in-process and return its URL.

    Honours an externally-set ``JOB_SERVICE_URL`` (e.g. CI pointing at a
    sidecar container).  Skips the suite when Postgres is not configured.
    """
    if not _postgres_configured():
        pytest.skip("integration tests require POSTGRES_HOST to be set")

    existing = os.environ.get("JOB_SERVICE_URL", "")
    # Replace placeholder with a real one.
    if existing and existing != "http://127.0.0.1:1":
        return existing
    url = _start_in_process_job_service()
    os.environ["JOB_SERVICE_URL"] = url
    return url


@pytest.fixture
def truncate_jobs_table(integration_job_service: str) -> None:
    """Per-test reset of the shared `jobs` table.  Pull this in when an
    integration test needs a clean slate."""
    sys.path.insert(0, str(_JOB_SERVICE_DIR))
    from db import get_conn  # type: ignore[import-not-found]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE jobs")
