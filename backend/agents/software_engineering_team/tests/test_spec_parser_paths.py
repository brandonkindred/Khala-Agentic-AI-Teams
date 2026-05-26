"""Tests for spec_parser path-precedence and versioning logic."""

from __future__ import annotations

from pathlib import Path

import pytest
from spec_parser import (
    SPEC_FILENAME,
    get_latest_spec_content,
    get_latest_spec_path,
    get_newest_spec_content,
    get_newest_spec_path,
    get_next_updated_spec_version,
)


def test_get_latest_spec_path_product_analysis_validated(tmp_path: Path):
    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "validated_spec.md").write_text("validated content")
    (pa / "updated_spec.md").write_text("updated content")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "validated_spec.md"


def test_get_latest_spec_path_product_analysis_updated(tmp_path: Path):
    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "updated_spec.md").write_text("c")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "updated_spec.md"


def test_get_latest_spec_path_product_analysis_versioned(tmp_path: Path):
    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "updated_spec_v1.md").write_text("v1")
    (pa / "updated_spec_v3.md").write_text("v3")
    (pa / "updated_spec_v2.md").write_text("v2")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "updated_spec_v3.md"


def test_get_latest_spec_path_product_analysis_malformed_version(tmp_path: Path):
    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "updated_spec_vabc.md").write_text("malformed")
    (pa / "updated_spec_v2.md").write_text("v2")
    p = get_latest_spec_path(tmp_path)
    # Picks the highest numeric (v2)
    assert p.name == "updated_spec_v2.md"


def test_get_latest_spec_path_plan_validated(tmp_path: Path):
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "validated_spec.md").write_text("c")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "validated_spec.md"


def test_get_latest_spec_path_plan_versioned(tmp_path: Path):
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "updated_spec_v5.md").write_text("v5")
    (plan / "updated_spec_v1.md").write_text("v1")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "updated_spec_v5.md"


def test_get_latest_spec_path_initial_spec(tmp_path: Path):
    (tmp_path / SPEC_FILENAME).write_text("initial")
    p = get_latest_spec_path(tmp_path)
    assert p.name == SPEC_FILENAME


def test_get_latest_spec_path_spec_md(tmp_path: Path):
    (tmp_path / "spec.md").write_text("c")
    p = get_latest_spec_path(tmp_path)
    assert p.name == "spec.md"


def test_get_latest_spec_path_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        get_latest_spec_path(tmp_path)


def test_get_latest_spec_path_invalid_dir(tmp_path: Path):
    bad = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        get_latest_spec_path(bad)


def test_get_latest_spec_content_returns_text(tmp_path: Path):
    (tmp_path / SPEC_FILENAME).write_text("the content")
    assert get_latest_spec_content(tmp_path) == "the content"


def test_get_latest_spec_content_pa_versioned(tmp_path: Path):
    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "updated_spec_v2.md").write_text("v2 content")
    assert get_latest_spec_content(tmp_path) == "v2 content"


def test_get_latest_spec_content_plan_versioned(tmp_path: Path):
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "updated_spec_v2.md").write_text("plan v2")
    assert get_latest_spec_content(tmp_path) == "plan v2"


def test_get_newest_spec_path_uses_mtime(tmp_path: Path, monkeypatch):
    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    older = pa / "validated_spec.md"
    newer = pa / "updated_spec_v2.md"
    older.write_text("old")
    newer.write_text("new")
    # Set newer file's mtime in the future
    import os
    import time as _time

    old_t = _time.time() - 1000
    new_t = _time.time()
    os.utime(older, (old_t, old_t))
    os.utime(newer, (new_t, new_t))
    p = get_newest_spec_path(tmp_path)
    assert p == newer


def test_get_newest_spec_path_falls_back_to_latest(tmp_path: Path):
    """When no '*spec*.md' files exist anywhere, falls back to get_latest_spec_path."""
    # No spec at all -> raises
    with pytest.raises(FileNotFoundError):
        get_newest_spec_path(tmp_path)


def test_get_newest_spec_path_invalid_dir(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        get_newest_spec_path(tmp_path / "missing")


def test_get_newest_spec_content(tmp_path: Path):
    (tmp_path / SPEC_FILENAME).write_text("hello")
    assert get_newest_spec_content(tmp_path) == "hello"


def test_get_next_updated_spec_version_no_dir(tmp_path: Path):
    assert get_next_updated_spec_version(tmp_path) == 1


def test_get_next_updated_spec_version_returns_max_plus_one(tmp_path: Path):
    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "updated_spec_v1.md").write_text("v1")
    (pa / "updated_spec_v3.md").write_text("v3")
    assert get_next_updated_spec_version(tmp_path) == 4


def test_get_next_updated_spec_version_skips_malformed(tmp_path: Path):
    pa = tmp_path / "plan" / "product_analysis"
    pa.mkdir(parents=True)
    (pa / "updated_spec_v2.md").write_text("v2")
    (pa / "updated_spec_vbad.md").write_text("malformed")
    assert get_next_updated_spec_version(tmp_path) == 3


def test_get_newest_spec_path_with_only_initial(tmp_path: Path):
    """When only initial_spec.md exists at root, it is selected."""
    (tmp_path / SPEC_FILENAME).write_text("initial")
    p = get_newest_spec_path(tmp_path)
    assert p.name == SPEC_FILENAME


def test_get_newest_spec_path_picks_plan(tmp_path: Path):
    """plan/ matches *spec*.md glob."""
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "validated_spec.md").write_text("c")
    p = get_newest_spec_path(tmp_path)
    assert p.name == "validated_spec.md"
