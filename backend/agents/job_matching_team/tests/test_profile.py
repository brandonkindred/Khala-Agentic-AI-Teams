"""Tests for the job-seeker profile model, loader, and career store."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from job_matching_team.profile import career_store
from job_matching_team.profile.career_store import (
    CAREER_SECTION_KEY,
    CareerProfileUnavailableError,
    load_career_profile,
    save_career_profile,
)
from job_matching_team.profile.loader import (
    EXAMPLE_PROFILE_PATH,
    clear_cache,
    load_job_seeker_profile,
)
from job_matching_team.profile.model import JobSeekerProfile, RankingWeights


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    clear_cache()
    yield
    clear_cache()


def test_bundled_example_loads_and_validates():
    profile = load_job_seeker_profile()
    assert isinstance(profile, JobSeekerProfile)
    assert profile.target_titles  # example has titles
    assert profile.remote_preference in ("remote", "hybrid", "onsite", "any")


def test_explicit_path_round_trip(tmp_path):
    p = tmp_path / "prof.yaml"
    p.write_text("target_titles: [Data Engineer]\nremote_preference: hybrid\nsalary_min: 150000\n")
    profile = load_job_seeker_profile(p)
    assert profile.target_titles == ["Data Engineer"]
    assert profile.remote_preference == "hybrid"
    assert profile.salary_min == 150000


def test_missing_explicit_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_job_seeker_profile(tmp_path / "nope.yaml")


def test_env_path_resolution(monkeypatch, tmp_path):
    p = tmp_path / "env_profile.yaml"
    p.write_text("target_titles: [SRE]\n")
    monkeypatch.setenv("JOB_SEEKER_PROFILE_PATH", str(p))
    profile = load_job_seeker_profile()
    assert profile.target_titles == ["SRE"]


def test_agent_cache_resolution(monkeypatch, tmp_path):
    monkeypatch.delenv("JOB_SEEKER_PROFILE_PATH", raising=False)
    (tmp_path / "job_seeker_profile.yaml").write_text("target_titles: [Platform Eng]\n")
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path))
    profile = load_job_seeker_profile()
    assert profile.target_titles == ["Platform Eng"]


def test_env_path_missing_falls_back_when_not_strict(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_SEEKER_PROFILE_PATH", str(tmp_path / "absent.yaml"))
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    monkeypatch.delenv("JOB_SEEKER_PROFILE_STRICT", raising=False)
    # Missing env path + no cache -> bundled example (no raise).
    profile = load_job_seeker_profile()
    assert isinstance(profile, JobSeekerProfile)


def test_env_path_missing_raises_when_strict(monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_SEEKER_PROFILE_PATH", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("JOB_SEEKER_PROFILE_STRICT", "true")
    with pytest.raises(FileNotFoundError):
        load_job_seeker_profile()


def test_strict_mode_raises_when_unresolved(monkeypatch):
    monkeypatch.delenv("JOB_SEEKER_PROFILE_PATH", raising=False)
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    monkeypatch.setenv("JOB_SEEKER_PROFILE_STRICT", "true")
    with pytest.raises(FileNotFoundError):
        load_job_seeker_profile()


def test_falls_back_to_example_when_unresolved(monkeypatch):
    monkeypatch.delenv("JOB_SEEKER_PROFILE_PATH", raising=False)
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    monkeypatch.delenv("JOB_SEEKER_PROFILE_STRICT", raising=False)
    profile = load_job_seeker_profile()
    # Same content as the bundled example.
    assert profile == JobSeekerProfile.from_yaml_file(EXAMPLE_PROFILE_PATH)


def test_empty_yaml_yields_all_defaults(tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("")
    profile = load_job_seeker_profile(p)
    assert profile == JobSeekerProfile()


def test_merged_with_applies_non_null_overrides():
    base = JobSeekerProfile(target_titles=["A"], salary_min=100)
    merged = base.merged_with({"salary_min": 200, "target_titles": None, "unknown_field": "x"})
    assert merged.salary_min == 200
    # None override is ignored -> standing value preserved.
    assert merged.target_titles == ["A"]
    # Original not mutated.
    assert base.salary_min == 100


def test_merged_with_none_returns_copy():
    base = JobSeekerProfile(target_titles=["A"])
    merged = base.merged_with(None)
    assert merged == base
    assert merged is not base


def test_merged_with_partial_weights_preserves_other_components():
    base = JobSeekerProfile(weights=RankingWeights(title_fit=0.4, skills_fit=0.3, comp_fit=0.05))
    merged = base.merged_with({"weights": {"title_fit": 0.9}})
    # Overridden component takes the new value...
    assert merged.weights.title_fit == 0.9
    # ...while omitted components keep the standing profile's values
    # (not the class defaults).
    assert merged.weights.skills_fit == 0.3
    assert merged.weights.comp_fit == 0.05
    # Original is untouched.
    assert base.weights.title_fit == 0.4


def test_weights_normalize_to_one():
    w = RankingWeights()
    norm = w.normalized()
    assert pytest.approx(sum(norm.values()), abs=1e-9) == 1.0


# ---------------------------------------------------------------------------
# Career store (profile persisted in the central user profile)
# ---------------------------------------------------------------------------


def _enable_postgres(monkeypatch):
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)


def test_load_career_profile_none_when_postgres_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    assert load_career_profile() is None


def test_load_career_profile_returns_validated_section(monkeypatch):
    _enable_postgres(monkeypatch)
    section = {"target_titles": ["Career Eng"], "salary_min": 123000}
    monkeypatch.setattr(
        career_store,
        "get_profile",
        lambda user_id: SimpleNamespace(preferences={CAREER_SECTION_KEY: section}),
    )
    profile = load_career_profile()
    assert profile.target_titles == ["Career Eng"]
    assert profile.salary_min == 123000


def test_load_career_profile_none_when_section_absent(monkeypatch):
    _enable_postgres(monkeypatch)
    monkeypatch.setattr(
        career_store, "get_profile", lambda user_id: SimpleNamespace(preferences={})
    )
    assert load_career_profile() is None


def test_load_career_profile_none_on_operational_failure(monkeypatch):
    _enable_postgres(monkeypatch)

    def boom(user_id):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(career_store, "get_profile", boom)
    assert load_career_profile() is None


def test_load_career_profile_raises_on_malformed_section(monkeypatch):
    _enable_postgres(monkeypatch)
    monkeypatch.setattr(
        career_store,
        "get_profile",
        lambda user_id: SimpleNamespace(
            preferences={CAREER_SECTION_KEY: {"remote_preference": "bogus"}}
        ),
    )
    with pytest.raises(Exception):
        load_career_profile()


def test_save_career_profile_raises_when_postgres_disabled(monkeypatch):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    with pytest.raises(CareerProfileUnavailableError):
        save_career_profile(JobSeekerProfile())


def test_save_career_profile_merges_and_records_association(monkeypatch):
    _enable_postgres(monkeypatch)
    existing = {"other_section": {"keep": True}}
    saved_updates = []
    associations = []

    monkeypatch.setattr(
        career_store, "get_profile", lambda user_id: SimpleNamespace(preferences=dict(existing))
    )

    def fake_upsert(update, user_id):
        saved_updates.append((update, user_id))
        return SimpleNamespace(preferences=update.preferences)

    monkeypatch.setattr(career_store, "upsert_profile", fake_upsert)
    monkeypatch.setattr(
        career_store,
        "record_association_safe",
        lambda *a, **kw: associations.append((a, kw)),
    )

    result = save_career_profile(JobSeekerProfile(target_titles=["Staff Eng"]))

    assert result.target_titles == ["Staff Eng"]
    update, user_id = saved_updates[0]
    # Other profile_json sections are preserved, career section written.
    assert update.preferences["other_section"] == {"keep": True}
    assert update.preferences[CAREER_SECTION_KEY]["target_titles"] == ["Staff Eng"]
    assert user_id == "default"
    # The Career card association is recorded best-effort.
    (artifact_type, team, artifact_id), kwargs = associations[0]
    assert artifact_type == "career"
    assert team == "job_matching"
    assert artifact_id == "career:default"
    assert kwargs["label"] == "Career profile"


def test_save_career_profile_wraps_operational_failure(monkeypatch):
    _enable_postgres(monkeypatch)

    def boom(user_id):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(career_store, "get_profile", boom)
    with pytest.raises(CareerProfileUnavailableError):
        save_career_profile(JobSeekerProfile())


def test_loader_prefers_career_section(monkeypatch):
    monkeypatch.setattr(
        career_store,
        "load_career_profile",
        lambda: JobSeekerProfile(target_titles=["From User Profile"]),
    )
    profile = load_job_seeker_profile()
    assert profile.target_titles == ["From User Profile"]


def test_loader_falls_back_to_yaml_when_career_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(career_store, "load_career_profile", lambda: None)
    p = tmp_path / "env_profile.yaml"
    p.write_text("target_titles: [From YAML]\n")
    monkeypatch.setenv("JOB_SEEKER_PROFILE_PATH", str(p))
    profile = load_job_seeker_profile()
    assert profile.target_titles == ["From YAML"]


def test_loader_explicit_path_skips_career_section(monkeypatch, tmp_path):
    def boom():
        raise AssertionError("career store must not be consulted for explicit paths")

    monkeypatch.setattr(career_store, "load_career_profile", boom)
    p = tmp_path / "prof.yaml"
    p.write_text("target_titles: [Explicit]\n")
    assert load_job_seeker_profile(p).target_titles == ["Explicit"]


def test_zero_weights_fall_back_to_uniform():
    w = RankingWeights(
        title_fit=0,
        seniority_fit=0,
        location_fit=0,
        comp_fit=0,
        company_fit=0,
        skills_fit=0,
    )
    norm = w.normalized()
    assert pytest.approx(sum(norm.values()), abs=1e-9) == 1.0
    assert all(pytest.approx(v) == 1 / 6 for v in norm.values())
