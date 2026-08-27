"""Tests for ``shared.artifacts.read_latest_draft``: the consolidated
final.md -> draft_v2.md -> draft_v1.md fallback-chain lookup.
"""

from __future__ import annotations

from pathlib import Path

from agents.blogging.shared.artifacts import (
    ARTIFACT_PRODUCER,
    load_allowed_claims_for_brief,
    read_latest_draft,
    write_artifact,
)


def test_read_latest_draft_prefers_final(tmp_path: Path) -> None:
    (tmp_path / "final.md").write_text("final content")
    (tmp_path / "draft_v2.md").write_text("v2 content")
    (tmp_path / "draft_v1.md").write_text("v1 content")

    assert read_latest_draft(tmp_path) == "final content"


def test_read_latest_draft_falls_back_to_draft_v2(tmp_path: Path) -> None:
    (tmp_path / "draft_v2.md").write_text("v2 content")
    (tmp_path / "draft_v1.md").write_text("v1 content")

    assert read_latest_draft(tmp_path) == "v2 content"


def test_read_latest_draft_falls_back_to_draft_v1(tmp_path: Path) -> None:
    (tmp_path / "draft_v1.md").write_text("v1 content")

    assert read_latest_draft(tmp_path) == "v1 content"


def test_read_latest_draft_returns_empty_when_nothing_present(tmp_path: Path) -> None:
    assert read_latest_draft(tmp_path) == ""


def test_read_latest_draft_skips_empty_preferred_file(tmp_path: Path) -> None:
    (tmp_path / "final.md").write_text("")
    (tmp_path / "draft_v1.md").write_text("v1 content")

    assert read_latest_draft(tmp_path) == "v1 content"


def test_read_latest_draft_custom_preferred_and_fallback_names(tmp_path: Path) -> None:
    (tmp_path / "custom_fallback.md").write_text("custom fallback content")

    result = read_latest_draft(
        tmp_path,
        "custom_preferred.md",
        fallback_names=("custom_fallback.md",),
    )
    assert result == "custom fallback content"


def test_load_allowed_claims_for_brief_returns_dict_on_topic_match(tmp_path: Path) -> None:
    write_artifact(tmp_path, "allowed_claims.json", {"topic": "AI", "claims": []})

    assert load_allowed_claims_for_brief(tmp_path, "AI") == {"topic": "AI", "claims": []}


def test_load_allowed_claims_for_brief_rejects_stale_topic(tmp_path: Path) -> None:
    """A work_dir reused for a new, unrelated brief must not have its stale
    allowed_claims.json silently applied to the new run."""
    write_artifact(tmp_path, "allowed_claims.json", {"topic": "Old topic", "claims": []})

    assert load_allowed_claims_for_brief(tmp_path, "New topic") is None


def test_load_allowed_claims_for_brief_missing_artifact_is_none(tmp_path: Path) -> None:
    assert load_allowed_claims_for_brief(tmp_path, "AI") is None


def test_load_allowed_claims_for_brief_non_dict_artifact_is_none(tmp_path: Path) -> None:
    (tmp_path / "allowed_claims.json").write_text("[1, 2, 3]")

    assert load_allowed_claims_for_brief(tmp_path, "AI") is None


def test_load_allowed_claims_for_brief_no_work_dir_is_none() -> None:
    assert load_allowed_claims_for_brief(None, "AI") is None
    assert load_allowed_claims_for_brief("", "AI") is None


def test_load_allowed_claims_for_brief_drops_malformed_entries(tmp_path: Path) -> None:
    """Every downstream consumer (writer prompt, fact-check agent) assumes each
    claim is a dict with at least "id" and "text" -- a malformed entry reaching
    them (e.g. a bare string) would crash a .get() call, so the loader must
    filter malformed entries out rather than passing the raw artifact through."""
    write_artifact(
        tmp_path,
        "allowed_claims.json",
        {
            "topic": "AI",
            "claims": [
                {"id": "c1", "text": "Valid.", "citations": ["s1"]},
                "not a dict",
                {"id": "", "text": "No id."},
                {"id": "c2", "text": ""},
                {"text": "No id key at all."},
            ],
        },
    )

    result = load_allowed_claims_for_brief(tmp_path, "AI")
    assert result == {
        "topic": "AI",
        "claims": [{"id": "c1", "text": "Valid.", "citations": ["s1"]}],
    }


def test_load_allowed_claims_for_brief_treats_non_list_claims_as_empty(tmp_path: Path) -> None:
    write_artifact(tmp_path, "allowed_claims.json", {"topic": "AI", "claims": "not a list"})

    assert load_allowed_claims_for_brief(tmp_path, "AI") == {"topic": "AI", "claims": []}


def test_load_allowed_claims_for_brief_treats_missing_claims_key_as_empty(tmp_path: Path) -> None:
    write_artifact(tmp_path, "allowed_claims.json", {"topic": "AI"})

    assert load_allowed_claims_for_brief(tmp_path, "AI") == {"topic": "AI", "claims": []}


def test_allowed_claims_json_producer_matches_planning_stage() -> None:
    """allowed_claims.json is written by the planning stage
    (run_planning/_persist_content_plan_artifacts call extract_allowed_claims());
    the registry must attribute it there since api/routers/artifacts.py surfaces
    producer_phase/producer_agent to callers."""
    entry = ARTIFACT_PRODUCER["allowed_claims.json"]
    assert entry["producer_phase"] == "planning"
    assert entry["producer_agent"] == "BlogPlanningAgent"
