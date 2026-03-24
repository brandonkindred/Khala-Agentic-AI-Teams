"""Integration test for blog_writing_process_v2 (without gates to avoid LLM)."""

from pathlib import Path

import pytest


def test_artifacts_module_import():
    """Verify shared.artifacts can be imported."""
    from shared.artifacts import ARTIFACT_NAMES

    assert "research_packet.md" in ARTIFACT_NAMES
    assert "final.md" in ARTIFACT_NAMES


def test_artifacts_write_read(tmp_path):
    """Verify write_artifact and read_artifact work."""
    from shared.artifacts import read_artifact, write_artifact

    write_artifact(tmp_path, "test.md", "Hello world")
    assert (tmp_path / "test.md").exists()
    content = read_artifact(tmp_path, "test.md")
    assert content == "Hello world"


def test_brand_spec_load():
    """Verify brand_spec_prompt.md can be loaded as full prompt text."""
    from shared.brand_spec import load_brand_spec_prompt

    path = Path(__file__).resolve().parent.parent / "docs" / "brand_spec_prompt.md"
    if not path.exists():
        pytest.skip("brand_spec_prompt.md not found")
    content = load_brand_spec_prompt(path)
    assert isinstance(content, str)
    assert len(content.strip()) > 0
