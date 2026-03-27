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
    assert load_agent_anatomy_text()[:200] in pre


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
