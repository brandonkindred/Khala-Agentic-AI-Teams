"""Tests for accessibility_audit_team artifact store."""

import pytest

from accessibility_audit_team.artifact_store import (
    ArtifactMetadata,
    ArtifactStore,
    ArtifactType,
    FileSystemBackend,
    RetentionPolicy,
    get_artifact_store,
)


@pytest.fixture()
def fs_backend(tmp_path):
    return FileSystemBackend(base_path=str(tmp_path / "artifacts"))


@pytest.fixture()
def artifact_store(tmp_path):
    backend = FileSystemBackend(base_path=str(tmp_path / "artifacts"))
    return ArtifactStore(backend=backend)


def test_artifact_type_enum_values():
    assert ArtifactType.EVIDENCE_PACK == "evidence_pack"
    assert ArtifactType.SCREENSHOT == "screenshot"
    assert ArtifactType.REPORT == "report"


def test_retention_policy_values():
    assert RetentionPolicy.EPHEMERAL == "ephemeral"
    assert RetentionPolicy.SHORT == "short"
    assert RetentionPolicy.STANDARD == "standard"
    assert RetentionPolicy.LONG == "long"
    assert RetentionPolicy.PERMANENT == "permanent"


@pytest.mark.anyio
async def test_file_system_backend_store_and_retrieve(fs_backend):
    content = b"test content"
    meta = ArtifactMetadata(
        artifact_ref="test-ref-1",
        artifact_type=ArtifactType.SCREENSHOT,
        audit_id="audit-1",
    )
    ref = await fs_backend.store("test-ref-1", content, meta)
    assert ref == "test-ref-1"

    retrieved = await fs_backend.retrieve("test-ref-1")
    assert retrieved == content


@pytest.mark.anyio
async def test_artifact_store_get_nonexistent(fs_backend):
    result = await fs_backend.retrieve("nonexistent-ref")
    assert result is None


@pytest.mark.anyio
async def test_artifact_store_delete(fs_backend):
    content = b"deletable"
    meta = ArtifactMetadata(
        artifact_ref="del-ref",
        artifact_type=ArtifactType.VIDEO,
        audit_id="audit-2",
    )
    await fs_backend.store("del-ref", content, meta)
    deleted = await fs_backend.delete("del-ref")
    assert deleted is True

    retrieved = await fs_backend.retrieve("del-ref")
    assert retrieved is None


@pytest.mark.anyio
async def test_artifact_store_metadata(fs_backend):
    content = b"meta content"
    meta = ArtifactMetadata(
        artifact_ref="meta-ref",
        artifact_type=ArtifactType.DOM_SNAPSHOT,
        audit_id="audit-3",
    )
    await fs_backend.store("meta-ref", content, meta)
    retrieved_meta = await fs_backend.get_metadata("meta-ref")
    assert retrieved_meta is not None
    assert retrieved_meta.artifact_ref == "meta-ref"
    assert retrieved_meta.audit_id == "audit-3"
    assert retrieved_meta.content_hash != ""


@pytest.mark.anyio
async def test_artifact_store_store_evidence_pack(artifact_store):
    ref = await artifact_store.store_evidence_pack(
        audit_id="audit-1",
        finding_id="F-001",
        content=b'{"evidence": "data"}',
    )
    assert "evidence_pack" in ref
    assert "audit-1" in ref


@pytest.mark.anyio
async def test_artifact_store_store_screenshot(artifact_store):
    ref = await artifact_store.store_screenshot(
        audit_id="audit-1",
        content=b"\x89PNG...",
        description="screenshot of homepage",
    )
    assert "screenshot" in ref


@pytest.mark.anyio
async def test_artifact_store_list_artifacts(artifact_store):
    await artifact_store.store_screenshot("audit-99", b"img1")
    await artifact_store.store_screenshot("audit-99", b"img2")
    artifacts = await artifact_store.list_audit_artifacts("audit-99")
    assert len(artifacts) == 2


def test_get_artifact_store_returns_instance():
    import accessibility_audit_team.artifact_store as mod

    # Reset singleton
    mod._artifact_store = None
    store = get_artifact_store()
    assert isinstance(store, ArtifactStore)
    # Second call returns same instance
    store2 = get_artifact_store()
    assert store is store2
