"""Ensure temporal activities can import job_store (remote or file backend)."""


def test_temporal_activities_imports() -> None:
    import software_engineering_team.temporal.activities as activities

    assert activities is not None
