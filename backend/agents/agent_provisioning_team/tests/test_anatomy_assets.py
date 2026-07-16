"""Tests for canonical anatomy loading and workspace materialization."""

from pathlib import Path

import pytest

from agent_provisioning_team.anatomy_assets import (
    AGENT_ANATOMY_MD,
    copy_anatomy_bundle_to_directory,
    get_anatomy_prompt_preamble,
    load_agent_anatomy_text,
    try_materialize_anatomy_bundle,
)


def test_load_agent_anatomy_text_non_empty():
    text = load_agent_anatomy_text()
    assert "Input" in text or "input" in text.lower()
    assert AGENT_ANATOMY_MD.is_file()


def test_get_anatomy_prompt_preamble_includes_spec():
    pre = get_anatomy_prompt_preamble()
    assert "AGENT_ANATOMY.md" in pre or "anatomy" in pre.lower()
    assert load_agent_anatomy_text() in pre


def test_copy_anatomy_bundle_to_directory(tmp_path: Path):
    dest = tmp_path / "bundle"
    written = copy_anatomy_bundle_to_directory(dest)
    assert dest.is_dir()
    assert any(p.name == "AGENT_ANATOMY.md" for p in written)
    assert (dest / "AGENT_ANATOMY.md").is_file()


def test_try_materialize_anatomy_bundle_writes_under_docs(tmp_path: Path):
    ws = tmp_path / "ws1"
    ws.mkdir()
    out = try_materialize_anatomy_bundle(str(ws))
    assert out is not None
    assert Path(out).name == "agent_anatomy"
    assert (Path(out) / "AGENT_ANATOMY.md").is_file()


@pytest.mark.parametrize("bad", ["", ".", "/"])
def test_try_materialize_anatomy_bundle_skips_invalid(bad: str):
    assert try_materialize_anatomy_bundle(bad) is None


# -------------------------------------------------------------------------
# anatomy_assets caching, bundle materialisation, and missing-file/dir fallbacks.
# -------------------------------------------------------------------------


def test_load_agent_anatomy_text_cached() -> None:
    from agent_provisioning_team import anatomy_assets

    # Reset cache
    anatomy_assets._anatomy_text_cache = None
    first = anatomy_assets.load_agent_anatomy_text()
    second = anatomy_assets.load_agent_anatomy_text()
    assert first is second


def test_try_materialize_anatomy_bundle_root_skip() -> None:
    from agent_provisioning_team.anatomy_assets import try_materialize_anatomy_bundle

    assert try_materialize_anatomy_bundle(".") is None
    assert try_materialize_anatomy_bundle("/") is None
    assert try_materialize_anatomy_bundle("") is None


def test_try_materialize_anatomy_bundle_writes_files(tmp_path: Path) -> None:
    from agent_provisioning_team.anatomy_assets import try_materialize_anatomy_bundle

    result = try_materialize_anatomy_bundle(str(tmp_path))
    # If the source AGENT_ANATOMY.md exists in the package, result is a path.
    if result is not None:
        assert Path(result).exists()
        assert (Path(result) / "AGENT_ANATOMY.md").exists()


def test_list_design_asset_paths() -> None:
    from agent_provisioning_team.anatomy_assets import list_design_asset_paths

    paths = list_design_asset_paths()
    assert isinstance(paths, list)


def test_get_anatomy_prompt_preamble_includes_diagram_block() -> None:
    from agent_provisioning_team.anatomy_assets import get_anatomy_prompt_preamble

    text = get_anatomy_prompt_preamble()
    assert "AGENT_ANATOMY.md" in text
    assert "diagram" in text.lower()


def test_load_agent_anatomy_text_missing_file(monkeypatch) -> None:
    from agent_provisioning_team import anatomy_assets

    # Force the path attribute to a non-existent file.
    monkeypatch.setattr(anatomy_assets, "AGENT_ANATOMY_MD", anatomy_assets.PACKAGE_DIR / "ghost.md")
    anatomy_assets._anatomy_text_cache = None
    out = anatomy_assets.load_agent_anatomy_text()
    assert "Missing file" in out

    # Reset cache so subsequent tests see the real content again.
    anatomy_assets._anatomy_text_cache = None


def test_list_design_asset_paths_missing_dir(monkeypatch) -> None:
    from agent_provisioning_team import anatomy_assets

    monkeypatch.setattr(
        anatomy_assets, "DESIGN_ASSETS_DIR", anatomy_assets.PACKAGE_DIR / "ghost_dir"
    )
    out = anatomy_assets.list_design_asset_paths()
    assert out == []
