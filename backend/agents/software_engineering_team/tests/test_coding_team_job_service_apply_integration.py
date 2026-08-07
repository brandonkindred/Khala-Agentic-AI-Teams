"""Integration tests for atomic job-service apply against the REAL job service.

These exercise the atomic read-after-write the unit tests can only model with the fake client:
``/jobs/{team}/{job_id}/apply`` must return the value written inside its own row-locked transaction,
so a counter increment reports the caller's OWN result, not a value a concurrent patch committed
afterward. Skipped unless run with ``-m integration`` and a real Postgres + job service.
"""

from __future__ import annotations

import pytest

from job_service_client import JobServiceClient


@pytest.mark.integration
def test_apply_returns_callers_own_incremented_value(
    integration_job_service: str, truncate_jobs_table: None
) -> None:
    client = JobServiceClient(team="coding_team")
    client.create_job("j-atomic", status="waiting_for_user")

    r1 = client.apply_and_get("j-atomic", increment={"resume_claim_seq": 1})
    assert r1 is not None and r1["resume_claim_seq"] == 1  # this call's own result

    r2 = client.apply_and_get("j-atomic", increment={"resume_claim_seq": 1})
    assert r2 is not None and r2["resume_claim_seq"] == 2
