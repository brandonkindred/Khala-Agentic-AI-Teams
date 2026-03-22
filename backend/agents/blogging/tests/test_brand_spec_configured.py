"""Tests for brand_spec_prompt_configured (API / UI gating)."""

from __future__ import annotations

from pathlib import Path

from shared.brand_spec import brand_spec_prompt_configured


def test_brand_spec_prompt_configured_true_for_repo_docs() -> None:
    root = Path(__file__).resolve().parent.parent
    assert brand_spec_prompt_configured(blogging_root=root) is True


def test_brand_spec_prompt_configured_false_when_file_missing(tmp_path: Path) -> None:
    root = tmp_path / "empty_blog"
    (root / "docs").mkdir(parents=True)
    assert brand_spec_prompt_configured(blogging_root=root) is False


def test_brand_spec_prompt_configured_false_when_too_short(tmp_path: Path) -> None:
    root = tmp_path / "stub_blog"
    docs = root / "docs"
    docs.mkdir(parents=True)
    (docs / "brand_spec_prompt.md").write_text("x" * 100, encoding="utf-8")
    assert brand_spec_prompt_configured(blogging_root=root, min_content_chars=400) is False
