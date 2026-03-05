"""Integration test for blog_writing_process_v2 (without gates to avoid LLM)."""

import pytest
from pathlib import Path


def test_artifacts_module_import():
    """Verify shared.artifacts can be imported."""
    from shared.artifacts import write_artifact, read_artifact, ARTIFACT_NAMES
    assert "research_packet.md" in ARTIFACT_NAMES
    assert "final.md" in ARTIFACT_NAMES


def test_artifacts_write_read(tmp_path):
    """Verify write_artifact and read_artifact work."""
    from shared.artifacts import write_artifact, read_artifact
    write_artifact(tmp_path, "test.md", "Hello world")
    assert (tmp_path / "test.md").exists()
    content = read_artifact(tmp_path, "test.md")
    assert content == "Hello world"


def test_brand_spec_load():
    """Verify brand_spec can be loaded."""
    from shared.brand_spec import load_brand_spec
    path = Path(__file__).resolve().parent.parent / "docs" / "brand_spec.yaml"
    if not path.exists():
        pytest.skip("brand_spec.yaml not found")
    spec = load_brand_spec(path)
    assert spec.voice.banned_phrases
    assert spec.formatting.min_paragraph_sentences >= 1
