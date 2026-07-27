"""Tests for ``shared.artifacts.read_latest_draft``: the consolidated
final.md -> draft_v2.md -> draft_v1.md fallback-chain lookup.
"""

from __future__ import annotations

from pathlib import Path

from agents.blogging.shared.artifacts import read_latest_draft


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
