"""Tests for canonical anatomy loading and workspace materialization."""

from pathlib import Path

import pytest

from agent_team_studio.agent_provisioning_team.anatomy_assets import (
    AGENT_ANATOMY_MD,
    copy_anatomy_bundle_to_directory,
    get_anatomy_prompt_preamble,
    load_agent_anatomy_text,
    try_materialize_anatomy_bundle,
)


def test_load_agent_anatomy_text_cached(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team import anatomy_assets

    # Reset the cache via monkeypatch so it is restored on teardown regardless
    # of outcome (no manual reset that a mid-test failure could skip).
    monkeypatch.setattr(anatomy_assets, "_anatomy_text_cache", None)
    first = load_agent_anatomy_text()
    second = load_agent_anatomy_text()
    assert first is second  # second call returns the cached value
    assert "Input" in first or "input" in first.lower()  # non-empty content
    assert AGENT_ANATOMY_MD.is_file()


def test_load_agent_anatomy_text_missing_file(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team import anatomy_assets

    # Point at a non-existent file and clear the cache so the read actually
    # runs; monkeypatch restores both attributes on teardown.
    monkeypatch.setattr(anatomy_assets, "AGENT_ANATOMY_MD", anatomy_assets.PACKAGE_DIR / "ghost.md")
    monkeypatch.setattr(anatomy_assets, "_anatomy_text_cache", None)
    out = anatomy_assets.load_agent_anatomy_text()
    assert "Missing file" in out


def test_get_anatomy_prompt_preamble_includes_spec():
    pre = get_anatomy_prompt_preamble()
    assert "AGENT_ANATOMY.md" in pre or "anatomy" in pre.lower()
    assert load_agent_anatomy_text() in pre


def test_get_anatomy_prompt_preamble_includes_diagram_block() -> None:
    pre = get_anatomy_prompt_preamble()
    assert "AGENT_ANATOMY.md" in pre
    assert "diagram" in pre.lower()


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


def test_list_design_asset_paths() -> None:
    from agent_team_studio.agent_provisioning_team.anatomy_assets import list_design_asset_paths

    paths = list_design_asset_paths()
    assert isinstance(paths, list)


def test_list_design_asset_paths_missing_dir(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team import anatomy_assets

    monkeypatch.setattr(
        anatomy_assets, "DESIGN_ASSETS_DIR", anatomy_assets.PACKAGE_DIR / "ghost_dir"
    )
    out = anatomy_assets.list_design_asset_paths()
    assert out == []
