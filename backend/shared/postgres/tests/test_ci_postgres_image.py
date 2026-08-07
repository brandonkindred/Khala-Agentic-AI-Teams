"""Regression guards for the Postgres image pinned in CI workflows.

The ``test-shared-postgres`` job (and sibling Postgres-backed CI jobs)
must use a supported ``postgres:*-alpine`` tag. Pinning to an
unavailable major (historically ``postgres:18-alpine`` before that tag
existed) prevents the service container from starting and blocks the
shared.postgres suite.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# backend/shared/postgres/tests/ → repo root is parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Supported pin shared with sandbox-stack.yml and the provisioning README.
_EXPECTED_CI_POSTGRES_IMAGE = "postgres:16-alpine"


def _postgres_service_images(workflow: dict) -> dict[str, str]:
    """Return ``{job_id: image}`` for every job that declares a postgres service.

    Preconditions:
        ``workflow`` is a parsed GitHub Actions workflow document with a
        ``jobs`` mapping (may be empty).
    Postconditions:
        Returns a dict whose keys are job ids and whose values are the
        ``services.postgres.image`` strings. Jobs without a postgres
        service are omitted. Raises ``AssertionError`` if a postgres
        service is declared without an ``image`` string.
    """
    assert isinstance(workflow, dict), "workflow must be a mapping"
    jobs = workflow.get("jobs") or {}
    assert isinstance(jobs, dict), "workflow.jobs must be a mapping"

    images: dict[str, str] = {}
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        services = job.get("services") or {}
        if not isinstance(services, dict):
            continue
        postgres = services.get("postgres")
        if postgres is None:
            continue
        assert isinstance(postgres, dict), f"{job_id}.services.postgres must be a mapping"
        image = postgres.get("image")
        assert isinstance(image, str) and image.strip(), (
            f"{job_id}.services.postgres.image must be a non-empty string"
        )
        images[job_id] = image.strip()
    return images


def test_ci_workflow_exists():
    """Preconditions: none. Postconditions: the CI workflow file is present."""
    assert _CI_WORKFLOW.is_file(), f"missing CI workflow at {_CI_WORKFLOW}"


def test_ci_postgres_services_pin_supported_image():
    """Every CI postgres service must use the supported alpine pin.

    Preconditions:
        ``.github/workflows/ci.yml`` exists and is valid YAML with at least
        one job that declares ``services.postgres``.
    Postconditions:
        Every such service uses ``postgres:16-alpine``, and the workflow
        text does not contain ``postgres:18-alpine``.
    """
    text = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "postgres:18-alpine" not in text, (
        "ci.yml must not pin postgres:18-alpine; use postgres:16-alpine "
        "(aligned with sandbox-stack.yml)"
    )

    workflow = yaml.safe_load(text)
    images = _postgres_service_images(workflow)
    assert images, "expected at least one job with services.postgres"
    assert "test-shared-postgres" in images, (
        "test-shared-postgres must declare a postgres service"
    )

    unexpected = {job: img for job, img in images.items() if img != _EXPECTED_CI_POSTGRES_IMAGE}
    assert not unexpected, (
        f"CI postgres services must use {_EXPECTED_CI_POSTGRES_IMAGE}; "
        f"found {unexpected}"
    )
