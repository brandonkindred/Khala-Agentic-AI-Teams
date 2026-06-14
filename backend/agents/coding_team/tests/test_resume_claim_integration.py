"""Integration tests for the cross-worker resume claim against the REAL job service.

These exercise the atomic read-after-write the unit tests can only model with the fake client:
``/jobs/{team}/{job_id}/apply`` must return the value written inside its own row-locked transaction,
so a counter increment reports the caller's OWN result, not a value a concurrent patch committed
afterward. Skipped unless run with ``-m integration`` and a real Postgres + job service.
"""

from __future__ import annotations

import pytest

from coding_team import job_store
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


@pytest.mark.integration
def test_claim_resume_single_winner_against_real_service(
    integration_job_service: str, truncate_jobs_table: None
) -> None:
    client = JobServiceClient(team="coding_team")
    client.create_job("j-claim", status="waiting_for_user")

    assert job_store.claim_resume("j-claim") is True  # acquires the free lease
    assert job_store.claim_resume("j-claim") is False  # the fresh lease now blocks

    # Releasing the stamp makes it immediately re-claimable (seq stays monotonic).
    job_store.release_resume_claim("j-claim")
    assert job_store.claim_resume("j-claim") is True
