"""Unit tests for shared helpers.

Covers job_store, environment_store, logging_context, phase_state,
provisioner_state edge cases, llm_client, tool_manifest, and
tool_agent_registry helpers that aren't already exercised in the
integration matrix.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_team_studio.agent_provisioning_team.shared.environment_store import (
    EnvironmentInfo as StoreEnvInfo,
)
from agent_team_studio.agent_provisioning_team.shared.environment_store import EnvironmentStore

# ---------------------------------------------------------------------------
# environment_store
# ---------------------------------------------------------------------------


def test_environment_info_from_dict_roundtrip() -> None:
    """Preconditions: none. Postconditions: an ``EnvironmentInfo`` with an
    explicit ``updated_at`` round-trips through ``to_dict``/``from_dict``
    preserving all fields, including ``updated_at``."""
    info = StoreEnvInfo(
        agent_id="a1",
        container_id="c1",
        container_name="agent-a1",
        workspace_path="/w",
        tools_provisioned=["pg", "redis"],
        updated_at="2024-01-02T00:00:00+00:00",
    )
    d = info.to_dict()
    restored = StoreEnvInfo.from_dict(d)
    assert restored.agent_id == "a1"
    assert restored.tools_provisioned == ["pg", "redis"]
    assert restored.updated_at == "2024-01-02T00:00:00+00:00"


def test_environment_info_updated_at_defaults_to_created_at() -> None:
    """Preconditions: none. Postconditions: an ``EnvironmentInfo`` built
    without an explicit ``updated_at`` round-trips through
    ``to_dict``/``from_dict`` with ``updated_at == created_at``."""
    info = StoreEnvInfo(agent_id="a1", container_id="c1", container_name="c1")
    d = info.to_dict()
    assert "updated_at" in d
    assert d["updated_at"] == info.created_at
    restored = StoreEnvInfo.from_dict(d)
    assert restored.updated_at == restored.created_at


def test_environment_info_from_dict_defaults_updated_at_when_key_absent() -> None:
    """Preconditions: ``data`` is a legacy-shaped dict with no ``updated_at``
    key at all (e.g. a record written before this field existed).
    Postconditions: ``from_dict`` defaults ``updated_at`` to ``created_at``
    rather than raising or leaving it unset."""
    legacy_data = {
        "agent_id": "a1",
        "container_id": "c1",
        "container_name": "c1",
        "workspace_path": "/w",
        "created_at": "2024-01-01T00:00:00+00:00",
    }
    restored = StoreEnvInfo.from_dict(legacy_data)
    assert restored.updated_at == "2024-01-01T00:00:00+00:00"


def test_environment_store_defaults_under_agent_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CACHE", str(tmp_path / "agents"))
    from agent_team_studio.agent_provisioning_team.shared.environment_store import (
        EnvironmentStore,
        default_environments_dir,
    )

    expected = tmp_path / "agents" / "agent_provisioning" / "environments"
    assert default_environments_dir() == expected
    store = EnvironmentStore()
    assert store.storage_dir == expected
    assert expected.is_dir()


def test_environment_store_register_get_remove(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    assert store.get("missing") is None

    env = StoreEnvInfo(
        agent_id="a1",
        container_id="c1",
        container_name="c1",
        workspace_path="/w",
    )
    store.register(env)
    assert store.exists("a1")

    fetched = store.get("a1")
    assert fetched.container_id == "c1"

    assert store.remove("a1") is True
    assert store.remove("a1") is False
    assert store.get("a1") is None


def test_environment_store_register_is_atomic_on_write_failure(tmp_path: Path) -> None:
    """A register whose write fails leaves the prior record fully intact.

    Preconditions: ``tmp_path`` is an empty, writable directory.
    Postconditions: after a raising register, ``get`` returns the previous
    record unchanged (never a truncated/partial one) and no temp files remain.
    """
    from agent_team_studio.agent_provisioning_team.shared import environment_store as es_mod

    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="n1", workspace_path="/w")
    )

    with patch.object(es_mod.os, "replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            store.register(
                StoreEnvInfo(
                    agent_id="a1", container_id="c2", container_name="n2", workspace_path="/w2"
                )
            )

    fetched = store.get("a1")
    assert fetched is not None and fetched.container_id == "c1"
    assert [p for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_environment_store_readable_probe(tmp_path: Path) -> None:
    """readable() is True when agent_id's record is absent or readable.

    Preconditions: ``tmp_path`` is an empty, writable directory.
    Postconditions: no record and a readable record both probe True.
    """
    store = EnvironmentStore(storage_dir=tmp_path)
    assert store.readable("missing") is True

    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="n1", workspace_path="/w")
    )
    assert store.readable("a1") is True


def test_environment_store_readable_false_on_unreadable_record_content(tmp_path: Path) -> None:
    """A record whose CONTENT can't be read (not just directory listing) probes False.

    A listable directory is not sufficient evidence: the specific file can be
    individually unreadable while the directory listing (and other files in it)
    are fine.
    """
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="n1", workspace_path="/w")
    )

    with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
        assert store.readable("a1") is False


def test_environment_store_readable_false_when_exists_probe_swallows_permission_error(
    tmp_path: Path,
) -> None:
    """readable() must not rely on Path.exists() to prove absence.

    Path.exists() catches ANY OSError from the underlying stat() call —
    including a transient EACCES on a parent directory — and returns False,
    indistinguishable from "genuinely doesn't exist". A caller that
    pre-checks with .exists() before reading would then skip straight past a
    record it couldn't actually rule out, misreporting a real access failure
    as confirmed absence. readable() must attempt the read directly instead
    so a real access failure surfaces as an OSError from that read, rather
    than being silently swallowed by .exists() first.
    """
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="n1", workspace_path="/w")
    )

    with (
        patch.object(Path, "exists", return_value=False),
        patch.object(Path, "read_text", side_effect=PermissionError("EACCES")),
    ):
        assert store.readable("a1") is False


def test_environment_store_readable_false_on_unreadable_legacy_copy(tmp_path: Path) -> None:
    """An unreadable LEGACY-location record also probes False.

    A primary-directory-only listing check would miss this; readable() must
    probe every candidate path readable() -> `get` would consult, including
    legacy locations.
    """
    from agent_team_studio.agent_provisioning_team.shared import environment_store as es_mod

    store = EnvironmentStore(storage_dir=tmp_path)
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy_path = legacy_dir / "a1.json"
    legacy_path.write_text("{}", encoding="utf-8")

    with (
        patch.object(es_mod, "legacy_environments_dirs", return_value=[legacy_dir]),
        patch.object(Path, "read_text", side_effect=OSError("permission denied")),
    ):
        assert store.readable("a1") is False


def test_environment_store_readable_false_on_malformed_record(tmp_path: Path) -> None:
    """A record that IS readable but malformed/incomplete also probes False.

    ``get`` maps malformed JSON or a JSON object missing a required key to
    ``None`` — correct for lookups, but a destructive caller must not conflate
    that with confirmed absence: the file's mere existence is evidence
    *something* was written there by an unknown prior process.
    """
    store = EnvironmentStore(storage_dir=tmp_path)

    (tmp_path / "a1.json").write_text("not valid json{{{", encoding="utf-8")
    assert store.readable("a1") is False

    (tmp_path / "a2.json").write_text('{"agent_id": "a2"}', encoding="utf-8")
    assert store.readable("a2") is False


def test_environment_store_readable_false_on_invalid_utf8(tmp_path: Path) -> None:
    """Invalid UTF-8 bytes are a readability failure, not a silent pass-through.

    ``Path.read_text`` raises ``UnicodeDecodeError`` on invalid UTF-8, which is
    neither ``OSError`` nor ``json.JSONDecodeError`` — an except clause naming
    only those two would let this exception escape and violate the
    "never raises" contract.
    """
    store = EnvironmentStore(storage_dir=tmp_path)
    (tmp_path / "a1.json").write_bytes(b"\xff\xfe not valid utf-8")
    assert store.readable("a1") is False


def test_environment_store_get_none_on_invalid_utf8(tmp_path: Path) -> None:
    """``get`` also treats invalid UTF-8 bytes as absent rather than raising."""
    store = EnvironmentStore(storage_dir=tmp_path)
    (tmp_path / "a1.json").write_bytes(b"\xff\xfe not valid utf-8")
    assert store.get("a1") is None


def test_environment_store_readable_false_on_invalid_field_value(tmp_path: Path) -> None:
    """A record with all required keys but an invalid value also probes False.

    ``get`` maps this to ``None`` via ``EnvironmentInfo.from_dict`` raising
    ``ValueError`` on the out-of-range ``ssh_port``. Before applying the same
    validation, ``readable`` only checked required-key presence, so it
    disagreed with ``get`` here and reported ``True`` — a destructive rollback
    caller combining ``get() is None`` with ``readable() is True`` would then
    misread this as a confirmed-absent orphan safe to reclaim, when a
    corrupt-but-present record for someone else's container sits right there.
    """
    store = EnvironmentStore(storage_dir=tmp_path)
    (tmp_path / "a1.json").write_text(
        json.dumps(
            {
                "agent_id": "a1",
                "container_id": "c1",
                "container_name": "n1",
                "ssh_port": 99999,
            }
        ),
        encoding="utf-8",
    )
    assert store.get("a1") is None
    assert store.readable("a1") is False


def test_environment_store_get_never_raises_on_unreadable_path(tmp_path: Path) -> None:
    """``get`` honors its never-raises contract even when Path.exists raises.

    Preconditions: ``tmp_path`` is an empty, writable directory.
    Postconditions: an OSError from the filesystem probe (e.g. EACCES) is
    treated as record-absent, not propagated.
    """
    store = EnvironmentStore(storage_dir=tmp_path)
    with patch.object(Path, "exists", side_effect=OSError("permission denied")):
        assert store.get("a1") is None


def test_environment_store_preserves_updated_at_on_get(tmp_path: Path) -> None:
    """Preconditions: ``tmp_path`` is an empty, writable directory.
    Postconditions: an explicit ``updated_at`` passed to ``register`` is
    returned unchanged by a subsequent ``get``."""
    store = EnvironmentStore(storage_dir=tmp_path)
    env = StoreEnvInfo(
        agent_id="a1",
        container_id="c1",
        container_name="c1",
        workspace_path="/w",
        updated_at="2024-06-01T12:00:00+00:00",
    )
    store.register(env)
    fetched = store.get("a1")
    assert fetched.updated_at == "2024-06-01T12:00:00+00:00"


def test_environment_store_register_rejects_none(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    with pytest.raises(ValueError, match="env_info must not be None"):
        store.register(None)


def test_environment_info_construction_rejects_empty_agent_id() -> None:
    """Construction-time validation now fires before ``register`` ever runs."""
    with pytest.raises(ValueError, match="agent_id must be a non-empty string"):
        StoreEnvInfo(
            agent_id="",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
        )


def test_environment_store_register_rejects_empty_agent_id(tmp_path: Path) -> None:
    """``register``'s own guard still fires for an instance mutated after
    construction (a freshly-constructed instance can never have an empty
    ``agent_id``)."""
    store = EnvironmentStore(storage_dir=tmp_path)
    env = StoreEnvInfo(
        agent_id="a1",
        container_id="c1",
        container_name="c1",
        workspace_path="/w",
    )
    env.agent_id = ""
    with pytest.raises(ValueError, match="agent_id must not be empty"):
        store.register(env)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"agent_id": ""},
        {"agent_id": None},
        {"container_id": ""},
        {"container_id": None},
        {"container_name": ""},
        {"container_name": None},
        {"ssh_port": 0},
        {"ssh_port": -1},
        {"ssh_port": 65536},
        {"ssh_port": "22"},
    ],
)
def test_environment_info_rejects_invalid_fields(kwargs: dict) -> None:
    """Preconditions: ``kwargs`` overrides exactly one required field with an
    invalid value. Postconditions: construction raises ``ValueError``."""
    base = {
        "agent_id": "a1",
        "container_id": "c1",
        "container_name": "c1",
        "workspace_path": "/w",
    }
    base.update(kwargs)
    with pytest.raises(ValueError):
        StoreEnvInfo(**base)


def test_environment_store_update_status(tmp_path: Path) -> None:
    """Preconditions: ``tmp_path`` is an empty, writable directory.
    Postconditions: ``update_status`` on a missing agent returns ``False``;
    on an existing agent it updates ``status``, refreshes ``updated_at`` to a
    value distinct from the original ``created_at``, and that refreshed value
    is stable across a subsequent ``get``."""
    store = EnvironmentStore(storage_dir=tmp_path)

    # update on missing returns False
    assert store.update_status("missing", "ready") is False

    store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
        )
    )
    original_created_at = store.get("a1").created_at
    with patch(
        "agent_team_studio.agent_provisioning_team.shared.environment_store.datetime"
    ) as mock_datetime:
        mock_datetime.now.return_value = datetime(2030, 1, 1, tzinfo=timezone.utc)
        assert store.update_status("a1", "ready") is True
    updated = store.get("a1")
    assert updated.status == "ready"
    assert updated.updated_at is not None
    assert updated.updated_at != original_created_at

    # Verify round-trip: a second get preserves the same updated_at
    refetched = store.get("a1")
    assert refetched.updated_at == updated.updated_at


def test_environment_store_update_status_handles_corrupt(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert store.update_status("broken", "ready") is False


def test_environment_store_update_status_handles_invalid_field(tmp_path: Path) -> None:
    """A well-formed JSON record with an invalid field value (e.g. an empty
    ``container_id``) fails ``EnvironmentInfo`` construction inside
    ``from_dict`` — ``update_status`` must return ``False``, not raise."""
    store = EnvironmentStore(storage_dir=tmp_path)
    (tmp_path / "bad-record.json").write_text(
        json.dumps({"agent_id": "bad-record", "container_id": "", "container_name": "c1"}),
        encoding="utf-8",
    )
    assert store.update_status("bad-record", "ready") is False


def test_environment_store_add_tool(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    assert store.add_tool("missing", "pg") is False

    store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
        )
    )
    assert store.add_tool("a1", "pg") is True
    # Idempotent — adding same tool twice doesn't duplicate.
    assert store.add_tool("a1", "pg") is True
    assert store.get("a1").tools_provisioned == ["pg"]


def test_environment_store_add_tools_batch(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
        )
    )
    assert store.add_tools("a1", ["pg", "redis", "pg"]) is True
    assert store.get("a1").tools_provisioned == ["pg", "redis"]


def test_environment_store_add_tool_handles_corrupt(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    bad = tmp_path / "broken.json"
    bad.write_text("not json", encoding="utf-8")
    assert store.add_tool("broken", "pg") is False


def test_environment_store_add_tool_handles_invalid_field(tmp_path: Path) -> None:
    """A well-formed JSON record with an invalid field value (e.g. an
    out-of-range ``ssh_port``) fails ``EnvironmentInfo`` construction inside
    ``from_dict`` — ``add_tool``/``add_tools`` must return ``False``, not
    raise."""
    store = EnvironmentStore(storage_dir=tmp_path)
    (tmp_path / "bad-record.json").write_text(
        json.dumps(
            {
                "agent_id": "bad-record",
                "container_id": "c1",
                "container_name": "c1",
                "ssh_port": 0,
            }
        ),
        encoding="utf-8",
    )
    assert store.add_tool("bad-record", "pg") is False


def test_environment_store_list_all(tmp_path: Path) -> None:
    """Preconditions: ``tmp_path`` is an empty, writable directory.
    Postconditions: ``list_all`` returns every registered environment,
    filters correctly by ``status``, and preserves each entry's
    ``updated_at``."""
    store = EnvironmentStore(storage_dir=tmp_path)

    store.register(
        StoreEnvInfo(
            agent_id="a1",
            container_id="c1",
            container_name="c1",
            workspace_path="/w",
            status="ready",
            updated_at="2024-03-01T00:00:00+00:00",
        )
    )
    store.register(
        StoreEnvInfo(
            agent_id="a2",
            container_id="c2",
            container_name="c2",
            workspace_path="/w",
            status="running",
        )
    )

    all_envs = store.list_all()
    assert len(all_envs) == 2

    ready = store.list_all(status="ready")
    assert len(ready) == 1
    assert ready[0].agent_id == "a1"
    assert ready[0].updated_at == "2024-03-01T00:00:00+00:00"


def test_environment_store_list_all_dedupes_by_agent_id_not_stem(tmp_path: Path) -> None:
    """A legacy file whose stem differs from its agent_id must not produce a duplicate."""
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(
            agent_id="agent_123", container_id="c1", container_name="c1", workspace_path="/w"
        )
    )
    # Legacy file named differently from the agent_id it contains.
    (tmp_path / "backup.json").write_text(
        json.dumps(
            StoreEnvInfo(
                agent_id="agent_123", container_id="c1", container_name="c1", workspace_path="/w"
            ).to_dict()
        ),
        encoding="utf-8",
    )

    out = store.list_all()
    assert len([e for e in out if e.agent_id == "agent_123"]) == 1


def test_environment_store_list_all_skips_corrupt(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="c1", workspace_path="/w")
    )
    # Corrupt JSON
    (tmp_path / "broken.json").write_text("not json", encoding="utf-8")
    # JSON missing required field
    (tmp_path / "incomplete.json").write_text(json.dumps({"agent_id": "x"}), encoding="utf-8")

    out = store.list_all()
    # Only the valid one shows up.
    assert {e.agent_id for e in out} == {"a1"}


def test_environment_store_list_all_skips_non_dict_json(tmp_path: Path) -> None:
    """A file containing a JSON array (not an object) must be skipped, not raise."""
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="c1", workspace_path="/w")
    )
    (tmp_path / "array.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    out = store.list_all()
    # Only the valid one shows up.
    assert {e.agent_id for e in out} == {"a1"}


def test_environment_store_list_all_skips_unsafe_agent_id(tmp_path: Path) -> None:
    """A path-traversal-shaped agent_id from a malicious/malformed file must not surface."""
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="c1", workspace_path="/w")
    )
    # register() would reject this via safe_path_component, so write it directly.
    (tmp_path / "evil.json").write_text(
        json.dumps(
            {
                "agent_id": "../../etc/passwd",
                "container_id": "c2",
                "container_name": "c2",
            }
        ),
        encoding="utf-8",
    )

    out = store.list_all()
    assert {e.agent_id for e in out} == {"a1"}


def test_environment_store_list_all_dedups_by_agent_id_not_filename(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)

    # Two stale/misnamed files, each named after a *different* agent than the
    # one in its own contents: "agent_p.json" actually describes "agent_q",
    # and "agent_q.json" actually describes "agent_r". Glob visits them in
    # filename order (agent_p.json, then agent_q.json).
    #
    # Buggy behavior: after processing agent_p.json, "agent_q" lands in
    # `seen` (from its *contents*). The next file, agent_q.json, is then
    # checked by its *filename stem* ("agent_q"), which now matches `seen`
    # purely by coincidence, so it gets skipped without ever being read —
    # silently dropping the real "agent_r" record.
    (tmp_path / "agent_p.json").write_text(
        json.dumps(
            StoreEnvInfo(
                agent_id="agent_q",
                container_id="p-container",
                container_name="p-name",
                workspace_path="/w",
            ).to_dict()
        ),
        encoding="utf-8",
    )
    (tmp_path / "agent_q.json").write_text(
        json.dumps(
            StoreEnvInfo(
                agent_id="agent_r",
                container_id="q-container",
                container_name="q-name",
                workspace_path="/w",
            ).to_dict()
        ),
        encoding="utf-8",
    )

    out = store.list_all()

    # Both distinct agent_ids ("agent_q" from agent_p.json, "agent_r" from
    # agent_q.json) must be present -- neither is a real duplicate of the
    # other, so neither should be dropped.
    assert {e.agent_id for e in out} == {"agent_q", "agent_r"}


def test_environment_store_list_all_skips_unreadable_file(tmp_path: Path) -> None:
    """An OSError from env_file.read_text() (permissions, deleted mid-scan) must not propagate."""
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(agent_id="a1", container_id="c1", container_name="c1", workspace_path="/w")
    )
    unreadable = tmp_path / "unreadable.json"
    unreadable.write_text(
        json.dumps(
            StoreEnvInfo(
                agent_id="a2", container_id="c2", container_name="c2", workspace_path="/w"
            ).to_dict()
        ),
        encoding="utf-8",
    )

    original_read_text = Path.read_text

    def flaky_read_text(self: Path, *args, **kwargs):
        if self == unreadable:
            raise OSError("simulated permission error")
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", flaky_read_text):
        out = store.list_all()

    assert {e.agent_id for e in out} == {"a1"}


def test_environment_store_get_handles_corrupt(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    (tmp_path / "x.json").write_text("not json", encoding="utf-8")
    assert store.get("x") is None


def test_environment_store_get_treats_incomplete_record_as_absent(tmp_path: Path) -> None:
    """Partial JSON missing required keys must not 500 callers of get()."""
    store = EnvironmentStore(storage_dir=tmp_path)
    (tmp_path / "partial.json").write_text(
        json.dumps({"agent_id": "partial", "status": "running"}),
        encoding="utf-8",
    )
    assert store.get("partial") is None
    assert store.exists("partial") is False


def test_environment_store_reads_legacy_path_and_migrates_on_write(
    tmp_path: Path, monkeypatch
) -> None:
    """Pre-cutover ``.agent_cache/provisioning_environments`` files remain visible."""
    monkeypatch.chdir(tmp_path)
    legacy_dir = tmp_path / ".agent_cache" / "provisioning_environments"
    legacy_dir.mkdir(parents=True)
    legacy_payload = {
        "agent_id": "legacy-a1",
        "container_id": "c-legacy",
        "container_name": "agent-legacy-a1",
        "ssh_host": "localhost",
        "ssh_port": 22,
        "workspace_path": "/workspace",
        "status": "running",
        "tools_provisioned": ["pg"],
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    (legacy_dir / "legacy-a1.json").write_text(json.dumps(legacy_payload), encoding="utf-8")

    primary = tmp_path / "new" / "environments"
    store = EnvironmentStore(storage_dir=primary)

    fetched = store.get("legacy-a1")
    assert fetched is not None
    assert fetched.container_id == "c-legacy"

    assert store.update_status("legacy-a1", "stopped") is True
    assert (primary / "legacy-a1.json").is_file()
    assert not (legacy_dir / "legacy-a1.json").exists()


def test_environment_store_remove_clears_legacy_copy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    legacy_dir = tmp_path / ".agent_cache" / "provisioning_environments"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "legacy-a2.json").write_text(
        json.dumps(
            {
                "agent_id": "legacy-a2",
                "container_id": "c2",
                "container_name": "agent-legacy-a2",
                "workspace_path": "/w",
                "status": "running",
                "tools_provisioned": [],
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    store = EnvironmentStore(storage_dir=tmp_path / "primary")
    assert store.remove("legacy-a2") is True
    assert not (legacy_dir / "legacy-a2.json").exists()


# Malicious identifiers that must never be turned into a filesystem path.
_TRAVERSAL_IDS = ["../../etc/passwd", "a/b", "..\\..\\x", "/etc/passwd", "..", "."]


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_environment_store_rejects_path_traversal_agent_id(tmp_path: Path, bad_id: str) -> None:
    """A traversal agent_id raises on every read/write path and writes nothing."""
    store = EnvironmentStore(storage_dir=tmp_path / "store")

    with pytest.raises(ValueError):
        store.register(
            StoreEnvInfo(agent_id=bad_id, container_id="c", container_name="c", workspace_path="/w")
        )
    with pytest.raises(ValueError):
        store.get(bad_id)
    with pytest.raises(ValueError):
        store.exists(bad_id)
    with pytest.raises(ValueError):
        store.update_status(bad_id, "ready")
    with pytest.raises(ValueError):
        store.add_tool(bad_id, "pg")
    with pytest.raises(ValueError):
        store.remove(bad_id)

    # The guard fires before any filesystem write, so no env file lands inside
    # or outside the store directory.
    assert list(tmp_path.rglob("*.json")) == []


def test_environment_store_allows_dotted_agent_id(tmp_path: Path) -> None:
    """A legitimate id containing a dot (e.g. ``blog.writer``) still round-trips."""
    store = EnvironmentStore(storage_dir=tmp_path)
    store.register(
        StoreEnvInfo(
            agent_id="blog.writer", container_id="c1", container_name="c1", workspace_path="/w"
        )
    )
    assert store.get("blog.writer").container_id == "c1"
    assert store.exists("blog.writer") is True


# ---------------------------------------------------------------------------
# environment_store — fencing token enforcement
# ---------------------------------------------------------------------------


def _register_env(store: EnvironmentStore, agent_id: str = "a1", **kwargs) -> None:
    store.register(
        StoreEnvInfo(
            agent_id=agent_id, container_id="c1", container_name="c1", workspace_path="/w"
        ),
        **kwargs,
    )


def test_environment_store_register_bootstraps_fencing_token(tmp_path: Path) -> None:
    """First write for an agent_id accepts any token (nothing to compare against)."""
    store = EnvironmentStore(storage_dir=tmp_path)
    _register_env(store, fencing_token=5)
    assert store.exists("a1")


def test_environment_store_write_methods_reject_stale_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError

    store = EnvironmentStore(storage_dir=tmp_path)
    _register_env(store, fencing_token=5)

    with pytest.raises(StaleFencingTokenError):
        store.update_status("a1", "ready", fencing_token=4)
    with pytest.raises(StaleFencingTokenError):
        store.add_tool("a1", "postgres", fencing_token=4)
    with pytest.raises(StaleFencingTokenError):
        store.add_tools("a1", ["postgres"], fencing_token=4)
    with pytest.raises(StaleFencingTokenError):
        store.remove("a1", fencing_token=4)
    with pytest.raises(StaleFencingTokenError):
        _register_env(store, fencing_token=4)

    # None of the rejected calls above mutated the record.
    assert store.get("a1").status == "running"


def test_environment_store_write_methods_accept_equal_token(tmp_path: Path) -> None:
    """The concurrent tool fan-out presents the SAME token from many callers;
    a strict '>' comparison would wrongly reject the 2nd..Nth writer."""
    store = EnvironmentStore(storage_dir=tmp_path)
    _register_env(store, fencing_token=5)

    assert store.add_tool("a1", "postgres", fencing_token=5) is True
    assert store.add_tool("a1", "redis", fencing_token=5) is True
    assert store.update_status("a1", "ready", fencing_token=5) is True


def test_environment_store_write_methods_accept_higher_token(tmp_path: Path) -> None:
    store = EnvironmentStore(storage_dir=tmp_path)
    _register_env(store, fencing_token=5)
    assert store.update_status("a1", "ready", fencing_token=6) is True


def test_environment_store_fencing_token_none_is_full_noop(tmp_path: Path) -> None:
    """Omitting fencing_token entirely must behave exactly as before this
    feature existed -- no bootstrap, no check, no stamping."""
    store = EnvironmentStore(storage_dir=tmp_path)
    _register_env(store, fencing_token=5)

    assert store.update_status("a1", "ready") is True
    assert store.add_tool("a1", "postgres") is True
    assert store.remove("a1") is True


# ---------------------------------------------------------------------------
# credential_store — path-traversal guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", _TRAVERSAL_IDS)
def test_credential_store_rejects_path_traversal_agent_id(tmp_path: Path, bad_id: str) -> None:
    """A traversal agent_id can neither read nor overwrite encrypted secrets."""
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path / "store")

    with pytest.raises(ValueError):
        store.store_credentials(bad_id, "pg", {"user": "x"})
    with pytest.raises(ValueError):
        store.get_credentials(bad_id)
    with pytest.raises(ValueError):
        store.delete_credentials(bad_id)

    # No encrypted credential file escaped the store directory.
    assert list(tmp_path.rglob("*.enc")) == []


def test_credential_store_allows_dotted_agent_id(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore

    store = CredentialStore(storage_dir=tmp_path)
    store.store_credentials("blog.writer", "pg", {"user": "x"})
    assert store.get_credentials("blog.writer") == {"pg": {"user": "x"}}


def test_stores_accept_max_length_identifier(tmp_path: Path) -> None:
    """A max-length identifier round-trips: the generated filename (plus the
    provisioner tempfile decoration) stays within the filesystem name limit, so
    no store raises ENAMETOOLONG.
    """
    from agent_team_studio.agent_provisioning_team.shared.credential_store import CredentialStore
    from agent_team_studio.agent_provisioning_team.shared.path_safety import _MAX_COMPONENT_LEN
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    long_id = "a" * _MAX_COMPONENT_LEN

    env = EnvironmentStore(storage_dir=tmp_path / "env")
    env.register(
        StoreEnvInfo(agent_id=long_id, container_id="c", container_name="c", workspace_path="/w")
    )
    assert env.get(long_id).container_id == "c"

    cred = CredentialStore(storage_dir=tmp_path / "cred")
    cred.store_credentials(long_id, "pg", {"u": "x"})
    assert cred.get_credentials(long_id) == {"pg": {"u": "x"}}

    # The provisioner writes a ``.{name}.XXXXXXXX.json`` tempfile — the worst case
    # for name-length overhead — so a successful put proves the reserve is enough.
    ps = ProvisionerStateStore(long_id, storage_dir=tmp_path / "ps")
    ps.put("a1", {"k": 1})
    assert ps.get("a1") == {"k": 1}


# ---------------------------------------------------------------------------
# job_store
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_job_client(monkeypatch):
    """Replace the module-level ``_client`` with a fresh fake."""
    from agent_team_studio.agent_provisioning_team.shared import job_store as js
    from job_service_client_fake import FakeJobServiceClient

    fake = FakeJobServiceClient(team="agent_provisioning_team")
    monkeypatch.setattr(js, "_client", lambda cache_dir=None: fake)
    return fake


def test_job_store_create_and_get(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "agent-1", "default.yaml")
    data = js.get_job("j1")
    assert data["agent_id"] == "agent-1"
    assert data["manifest_path"] == "default.yaml"

    # Missing job → empty dict
    assert js.get_job("missing") == {}


def test_job_store_update_job(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.update_job("j1", progress=42, current_phase="setup")
    data = js.get_job("j1")
    assert data["progress"] == 42
    assert data["current_phase"] == "setup"


def test_job_store_list_jobs(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a1", "m")
    js.create_job("j2", "a2", "m")
    js.update_job("j2", status="completed")

    all_jobs = js.list_jobs(running_only=False)
    assert len(all_jobs) == 2
    active = js.list_jobs(running_only=True)
    assert len(active) == 1
    assert active[0]["job_id"] == "j1"


def test_job_store_mark_running_completed_failed(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.mark_job_running("j1")
    assert js.get_job("j1")["status"] == "running"

    js.mark_job_completed("j1", result={"ok": True})
    data = js.get_job("j1")
    assert data["status"] == "completed"
    assert data["progress"] == 100
    assert data["result"] == {"ok": True}

    js.mark_job_failed("j1", error="kaboom")
    data = js.get_job("j1")
    assert data["status"] == "failed"
    assert data["error"] == "kaboom"


def test_job_store_cancel_job(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    assert js.cancel_job("j1") is True
    assert js.cancel_job("missing") is False


def test_job_store_mark_all_running_jobs_failed(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.create_job("j2", "b", "m")
    js.mark_all_running_jobs_failed("shutdown")
    # Both should now be failed
    assert all(j["status"] == "failed" for j in js.list_jobs(running_only=False))


def test_job_store_mark_all_swallows_exception(monkeypatch, caplog) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    fake = MagicMock()
    fake.mark_all_active_jobs_failed.side_effect = RuntimeError("boom")
    monkeypatch.setattr(js, "_client", lambda cache_dir=None: fake)
    with caplog.at_level(logging.WARNING):
        js.mark_all_running_jobs_failed("shutdown")
    # Exception swallowed (no propagation) AND a warning was logged.
    assert any(
        rec.levelno == logging.WARNING and "mark_all_running_jobs_failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_job_store_update_phase_progress(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.update_phase_progress(
        "j1",
        current_phase="setup",
        progress=20,
        current_tool="pg",
        tools_completed=1,
        tools_total=3,
    )
    data = js.get_job("j1")
    assert data["current_phase"] == "setup"
    assert data["current_tool"] == "pg"
    assert data["tools_completed"] == 1


def test_job_store_add_completed_phase_with_result(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.add_completed_phase("j1", "setup", phase_result={"success": True})
    data = js.get_job("j1")
    assert "setup" in data["completed_phases"]
    assert data["phase_results"]["setup"] == {"success": True}

    # Adding the same phase again must not duplicate it.
    js.add_completed_phase("j1", "setup")
    data = js.get_job("j1")
    assert data["completed_phases"].count("setup") == 1


def test_job_store_add_completed_phase_missing_job(mock_job_client) -> None:
    """When the job doesn't exist add_completed_phase no-ops."""
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.add_completed_phase("missing", "setup")
    assert js.get_job("missing") == {}


def test_job_store_reset_job(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    js.update_job("j1", progress=80, status="failed")
    js.reset_job("j1")
    data = js.get_job("j1")
    assert data["status"] == "pending"
    assert data["progress"] == 0


def test_job_store_delete_job(mock_job_client) -> None:
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    js.create_job("j1", "a", "m")
    assert js.delete_job("j1") is True
    assert js.delete_job("j1") is False


# ---------------------------------------------------------------------------
# logging_context
# ---------------------------------------------------------------------------


def test_logging_context_filter_injects_defaults(caplog) -> None:
    from agent_team_studio.agent_provisioning_team.shared import logging_context as lc
    from agent_team_studio.agent_provisioning_team.shared.logging_context import (
        ProvisioningContextFilter,
    )

    # Reset contextvars in case prior tests leaked into this worker.
    tok_j = lc._job_id_var.set(None)
    tok_a = lc._agent_id_var.set(None)
    tok_p = lc._phase_var.set(None)
    try:
        record = logging.LogRecord(
            name="x",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="m",
            args=(),
            exc_info=None,
        )
        f = ProvisioningContextFilter()
        assert f.filter(record) is True
        assert record.job_id == "-"
        assert record.agent_id == "-"
        assert record.phase == "-"

        # Getter helpers report None when nothing is bound.
        assert lc.get_job_id() is None
        assert lc.get_agent_id() is None
        assert lc.get_phase() is None
    finally:
        lc._job_id_var.reset(tok_j)
        lc._agent_id_var.reset(tok_a)
        lc._phase_var.reset(tok_p)


def test_logging_context_manager_binds_and_unbinds() -> None:
    from agent_team_studio.agent_provisioning_team.shared.logging_context import (
        get_agent_id,
        get_job_id,
        get_phase,
        provisioning_context,
    )

    with provisioning_context(job_id="j1", agent_id="a1", phase="setup"):
        assert get_job_id() == "j1"
        assert get_agent_id() == "a1"
        assert get_phase() == "setup"

    # After exit the values are reset.
    assert get_job_id() is None


def test_logging_context_manager_partial_args() -> None:
    from agent_team_studio.agent_provisioning_team.shared import logging_context as lc

    tok_a = lc._agent_id_var.set(None)
    try:
        with lc.provisioning_context(job_id="j1"):
            assert lc.get_job_id() == "j1"
            assert lc.get_agent_id() is None
    finally:
        lc._agent_id_var.reset(tok_a)


def test_install_filter_is_idempotent() -> None:
    from agent_team_studio.agent_provisioning_team.shared import logging_context as lc

    # First call may or may not install; second is always a no-op.
    lc.install_filter()
    lc.install_filter()


# ---------------------------------------------------------------------------
# phase_state.restore_*
# ---------------------------------------------------------------------------


def test_restore_credentials_validates_shape() -> None:
    from agent_team_studio.agent_provisioning_team.shared.phase_state import restore_credentials

    snap = restore_credentials(
        {
            "success": True,
            "credentials": {"pg": {"tool_name": "pg", "username": "u", "password": "p"}},
        }
    )
    assert snap.success is True
    assert snap.credentials["pg"].username == "u"


def test_restore_account_provisioning_validates_shape() -> None:
    from agent_team_studio.agent_provisioning_team.shared.phase_state import (
        restore_account_provisioning,
    )

    snap = restore_account_provisioning(
        {
            "success": True,
            "tool_results": [
                {"tool_name": "t", "success": True, "provisioner_key": "p", "permissions": []}
            ],
            "tools_completed": 1,
            "tools_total": 1,
        }
    )
    assert snap.success is True
    assert snap.tool_results[0].tool_name == "t"


def test_restore_access_audit_returns_typed() -> None:
    from agent_team_studio.agent_provisioning_team.shared.phase_state import restore_access_audit

    out = restore_access_audit({"passed": True, "verifications": []})
    assert out.passed is True


def test_restore_documentation_validates_shape() -> None:
    from agent_team_studio.agent_provisioning_team.shared.phase_state import restore_documentation

    snap = restore_documentation(
        {
            "success": True,
            "onboarding": {
                "summary": "hi",
                "tools": [],
                "environment_variables": {},
            },
        }
    )
    assert snap.success is True
    assert snap.onboarding.summary == "hi"


def test_restore_documentation_with_none_onboarding() -> None:
    from agent_team_studio.agent_provisioning_team.shared.phase_state import restore_documentation

    snap = restore_documentation({"success": True, "onboarding": None})
    assert snap.success is True
    assert snap.onboarding is None


# ---------------------------------------------------------------------------
# llm_client
# ---------------------------------------------------------------------------


def test_llm_client_is_not_configured_by_default() -> None:
    from agent_team_studio.agent_provisioning_team.shared.llm_client import LLMClient

    assert LLMClient().is_configured is False


def test_llm_client_complete_raises_when_configured(monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.shared.llm_client import LLMClient, LLMRequest

    client = LLMClient()
    # Trick is_configured into True via monkeypatch on the property's getter.
    with patch.object(type(client), "is_configured", property(lambda self: True)):
        with pytest.raises(NotImplementedError):
            client.complete(LLMRequest(system="s", user="u"))


def test_sanitize_prompt_var_default_max() -> None:
    from agent_team_studio.agent_provisioning_team.shared.llm_client import sanitize_prompt_var

    s = sanitize_prompt_var("clean ascii")
    assert s == "clean ascii"


def test_sanitize_prompt_var_strips_emoji() -> None:
    from agent_team_studio.agent_provisioning_team.shared.llm_client import sanitize_prompt_var

    s = sanitize_prompt_var("hi \U0001f600 there")
    # Emoji is disallowed and must be removed, not replaced with underscores.
    assert "\U0001f600" not in s
    assert "_" not in s
    assert s == "hi  there"


def test_sanitize_prompt_var_handles_none() -> None:
    from agent_team_studio.agent_provisioning_team.shared.llm_client import sanitize_prompt_var

    assert sanitize_prompt_var(None) == ""


# ---------------------------------------------------------------------------
# tool_manifest helpers
# ---------------------------------------------------------------------------


def test_load_manifest_success(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import load_manifest

    f = tmp_path / "m.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: postgresql
    provisioner: postgres_provisioner
    config: {database_prefix: "x_"}
""",
        encoding="utf-8",
    )

    m = load_manifest(str(f))
    assert m.version == "1.0"
    assert m.tool_names == ["postgresql"]


def test_load_manifest_missing_file_raises(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import load_manifest

    with pytest.raises(FileNotFoundError):
        load_manifest(str(tmp_path / "ghost.yaml"))


def test_load_manifest_invalid_yaml_raises(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import load_manifest

    bad = tmp_path / "bad.yaml"
    bad.write_text(": this is\n  : not yaml", encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(str(bad))


def test_load_manifest_invalid_structure(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import load_manifest

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        """
tools:
  - name: ""
    provisioner: bogus
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_manifest(str(bad))


def test_load_manifest_empty_file(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import load_manifest

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    m = load_manifest(str(empty))
    assert m.tools == []


def test_validate_manifest_returns_errors(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import validate_manifest

    # Missing file
    out = validate_manifest(str(tmp_path / "ghost.yaml"))
    assert any("not found" in e.lower() for e in out)


def test_validate_manifest_no_tools_warning(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import validate_manifest

    f = tmp_path / "empty_tools.yaml"
    f.write_text("version: '1.0'\ntools: []\n", encoding="utf-8")
    out = validate_manifest(str(f))
    assert any("no tools" in e.lower() for e in out)


def test_validate_manifest_duplicate_names(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import validate_manifest

    f = tmp_path / "dupes.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: pg
    provisioner: postgres_provisioner
  - name: pg
    provisioner: postgres_provisioner
""",
        encoding="utf-8",
    )
    out = validate_manifest(str(f))
    assert any("duplicate" in e.lower() for e in out)


def test_validate_manifest_clean(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import validate_manifest

    f = tmp_path / "good.yaml"
    f.write_text(
        """
version: "1.0"
tools:
  - name: pg
    provisioner: postgres_provisioner
""",
        encoding="utf-8",
    )
    out = validate_manifest(str(f))
    assert out == []


def test_assert_path_within_base_passes(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        assert_path_within_base,
    )

    base = tmp_path
    target = tmp_path / "sub" / "x"
    out = assert_path_within_base(str(target), str(base))
    assert str(out).startswith(str(base.resolve()))


def test_assert_path_within_base_rejects_escape(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        assert_path_within_base,
    )

    with pytest.raises(ValueError, match="escapes"):
        assert_path_within_base("/etc/passwd", str(tmp_path))


def test_tool_definition_invalid_provisioner_rejected() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolDefinition

    with pytest.raises(ValueError):
        ToolDefinition(name="x", provisioner="quantum", config={})


def test_tool_definition_lowercases_name() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolDefinition

    td = ToolDefinition(name="Postgres-XL", provisioner="postgres_provisioner", config={})
    assert td.name == "postgres-xl"


def test_tool_definition_invalid_name_rejected() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolDefinition

    with pytest.raises(ValueError):
        ToolDefinition(name="hi there!", provisioner="postgres_provisioner")


def test_tool_definition_ignores_unknown_top_level_fields() -> None:
    """Older manifests with ``access_level`` should still parse — the field is
    accepted but not surfaced."""
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolDefinition

    td = ToolDefinition(
        name="t",
        provisioner="generic_provisioner",
        config={},
        access_level="full",  # unknown / legacy
    )
    assert not hasattr(td, "access_level")


def test_redis_config_visibility_validator() -> None:
    """Cover the git visibility validator via direct manifest construction."""
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import ToolDefinition

    td = ToolDefinition(name="g", provisioner="git_provisioner", config={"visibility": "public"})
    assert td.config["visibility"] == "public"


def test_validate_provisioner_config_unknown() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        validate_provisioner_config,
    )

    with pytest.raises(ValueError, match="Unknown provisioner"):
        validate_provisioner_config("bogus", {})


def test_validate_manifest_environment_handles_blank_string() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        validate_manifest_environment,
    )

    # None value gets coerced to ""
    out = validate_manifest_environment({"FOO": ""})
    assert out["FOO"] == ""


def test_validate_manifest_environment_rejects_non_scalar() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        validate_manifest_environment,
    )

    with pytest.raises(ValueError, match="scalar"):
        validate_manifest_environment({"FOO": ["a", "b"]})


def test_validate_manifest_environment_rejects_empty_key() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        validate_manifest_environment,
    )

    with pytest.raises(ValueError, match="non-empty"):
        validate_manifest_environment({"": "x"})


def test_validate_manifest_environment_rejects_long_key() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        validate_manifest_environment,
    )

    with pytest.raises(ValueError, match="too long"):
        validate_manifest_environment({"X" * 200: "y"})


def test_validate_manifest_environment_caps_value_length() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        validate_manifest_environment,
    )

    with pytest.raises(ValueError, match="exceeds"):
        validate_manifest_environment({"FOO": "x" * 10_000})


def test_validate_provisioner_config_normalizes() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        validate_provisioner_config,
    )

    out = validate_provisioner_config("postgres_provisioner", {})
    assert out["database_prefix"] == "agent_"


def test_reject_traversal_components_helper() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        _reject_traversal_components,
    )

    out = _reject_traversal_components("/tmp/x", field="p")
    assert out == "/tmp/x"


def test_reject_path_separators_helper_rejects_empty() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        _reject_path_separators,
    )

    with pytest.raises(ValueError, match="non-empty"):
        _reject_path_separators("", field="x")


def test_reject_path_separators_helper_rejects_traversal() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_manifest import (
        _reject_path_separators,
    )

    with pytest.raises(ValueError, match="traverse"):
        _reject_path_separators("..", field="x")


# ---------------------------------------------------------------------------
# tool_agent_registry
# ---------------------------------------------------------------------------


def test_build_default_tool_agents_has_required_keys() -> None:
    from agent_team_studio.agent_provisioning_team.shared.tool_agent_registry import (
        build_default_tool_agents,
    )

    out = build_default_tool_agents()
    for key in (
        "docker_provisioner",
        "postgres_provisioner",
        "redis_provisioner",
        "git_provisioner",
        "generic_provisioner",
    ):
        assert key in out


# ---------------------------------------------------------------------------
# provisioner_state edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", ["../evil", "a/b", "..\\x", "/etc/passwd", "..", "."])
def test_provisioner_state_rejects_path_traversal_name(tmp_path: Path, bad_name: str) -> None:
    """A traversal provisioner_name is rejected before the JSON path is bound."""
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    with pytest.raises(ValueError):
        ProvisionerStateStore(bad_name, storage_dir=tmp_path)
    assert list(tmp_path.rglob("*.json")) == []


def test_provisioner_state_load_corrupt_file(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    # Pre-write a corrupt file
    state = ProvisionerStateStore("xx", storage_dir=tmp_path)
    state.path.write_text("not json", encoding="utf-8")
    # Reload should return {} instead of crashing
    fresh = ProvisionerStateStore("xx", storage_dir=tmp_path)
    assert fresh.get("any") is None


def test_provisioner_state_list_agents(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    store.put("a1", {"x": 1})
    store.put("a2", {"y": 2})
    out = store.list_agents()
    assert out == {"a1": {"x": 1}, "a2": {"y": 2}}


def test_provisioner_state_write_methods_reject_stale_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        CompensationRecord,
        ProvisionerStateStore,
    )

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    store.put("a1", {"x": 1}, fencing_token=5)

    with pytest.raises(StaleFencingTokenError):
        store.put("a1", {"x": 2}, fencing_token=4)
    with pytest.raises(StaleFencingTokenError):
        store.add_compensation("a1", CompensationRecord(kind="k", payload={}), fencing_token=4)
    with pytest.raises(StaleFencingTokenError):
        store.clear_compensations("a1", fencing_token=4)
    with pytest.raises(StaleFencingTokenError):
        store.check_fencing_token("a1", 4)
    with pytest.raises(StaleFencingTokenError):
        store.delete("a1", fencing_token=4)

    # None of the rejected calls above mutated the record.
    assert store.get("a1") == {"x": 1}


def test_provisioner_state_write_methods_accept_equal_and_higher_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    store.put("a1", {"x": 1}, fencing_token=5)

    store.check_fencing_token("a1", 5)  # equal: must not raise
    store.put("a1", {"x": 2}, fencing_token=5)
    store.put("a1", {"x": 3}, fencing_token=6)
    assert store.get("a1") == {"x": 3}
    assert store.delete("a1", fencing_token=6) is True


def test_provisioner_state_check_fencing_token_bootstraps(tmp_path: Path) -> None:
    """No prior record for this agent_id -> any token is accepted (nothing
    to compare against), and the preflight does not persist anything."""
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    store.check_fencing_token("never-seen", 1)
    assert store.get("never-seen") is None


def test_provisioner_state_get_or_create_rejects_stale_token(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.fencing import StaleFencingTokenError
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    store.put("a1", {"x": 1}, fencing_token=5)

    calls: list[int] = []

    def _creator() -> dict:
        calls.append(1)
        return {"x": 99}

    with pytest.raises(StaleFencingTokenError):
        store.get_or_create("a1", _creator, fencing_token=4)
    assert calls == []  # creator must not run when the token is stale


def test_provisioner_state_fencing_token_none_is_full_noop(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        CompensationRecord,
        ProvisionerStateStore,
    )

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    store.put("a1", {"x": 1}, fencing_token=5)

    # Unfenced calls (fencing_token omitted) must behave exactly as before.
    store.put("a1", {"x": 2})
    store.add_compensation("a1", CompensationRecord(kind="k", payload={}))
    store.clear_compensations("a1")
    assert store.delete("a1") is True


def test_compensation_record_serialization() -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        CompensationRecord,
    )

    rec = CompensationRecord(kind="k", payload={"a": 1})
    d = rec.to_json()
    restored = CompensationRecord.from_json(d)
    assert restored.kind == rec.kind
    assert restored.payload == rec.payload


def test_clear_compensations_on_missing_agent(tmp_path: Path) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    store = ProvisionerStateStore("xx", storage_dir=tmp_path)
    # Should be a no-op rather than raising.
    store.clear_compensations("missing")


# -------------------------------------------------------------------------
# ProvisionerStateStore atomic-write failure cleanup.
# -------------------------------------------------------------------------


def test_provisioner_state_save_handles_io_error(tmp_path: Path, monkeypatch) -> None:
    from agent_team_studio.agent_provisioning_team.shared.provisioner_state import (
        ProvisionerStateStore,
    )

    store = ProvisionerStateStore("x", storage_dir=tmp_path)

    # Force os.replace to raise to exercise the cleanup path.
    with patch("os.replace", side_effect=OSError("io")):
        with pytest.raises(OSError):
            store.put("a1", {"x": 1})

    # The mkstemp tempfile is unlinked on failure and the target file was never
    # created (os.replace raised), so the store dir is left clean.
    assert list(tmp_path.iterdir()) == []
