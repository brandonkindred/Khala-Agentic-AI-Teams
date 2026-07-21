"""End-to-end learning-loop integration test (requires Postgres).

Acceptance criterion: a simulated agent crash produces a post-mortem, which
becomes a ``se_learnings`` row retrievable through the public
``learnings_store`` seam.

Skipped unless run with ``-m integration`` against a live Postgres (CI's
``test-integration`` job provides one).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def _se_schema():
    from shared.postgres import is_postgres_enabled, register_team_schemas

    if not is_postgres_enabled():
        pytest.skip("Postgres not configured")
    from software_engineering_team.postgres import SCHEMA

    register_team_schemas(SCHEMA)
    # Clean slate for learnings.
    from shared.postgres import get_conn

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE se_learnings")
    yield
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE se_learnings")


def test_crash_to_post_mortem_to_learning(_se_schema, tmp_path) -> None:
    """A simulated crash flows through post-mortem to a retrievable learning row."""
    from software_engineering_team.shared.learnings_store import count_learnings, retrieve_learnings
    from software_engineering_team.shared.post_mortem import write_post_mortem

    # 1. A simulated agent crash writes a post-mortem (which ingests a learning).
    unique_error = "DatabaseConnectionTimeout while seeding fixtures"
    write_post_mortem(
        agent_name="backend_dev",
        task_description="Seed the database for integration tests",
        original_prompt="...",
        partial_responses=[],
        continuation_attempts=5,
        decomposition_depth=20,
        error=RuntimeError(unique_error),
        project_root=tmp_path,
    )

    # 2. The learning row exists and is retrievable through the public seam.
    assert count_learnings() >= 1
    hits = retrieve_learnings("database connection timeout seeding")
    assert any(unique_error in h.trigger for h in hits)
