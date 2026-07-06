"""Hermetic tests for the dynamic-manifest Postgres overlay in ``AgentRegistry``.

The dynamic store's CRUD is exercised against live Postgres in
``test_dynamic_store.py``; here we monkeypatch the ``dynamic_store`` module with an
in-memory fake so we can assert the *registry's* static/dynamic split, merge
precedence, write-through gating, and degrade-on-error behavior without a database.
"""

from __future__ import annotations

import pytest

from agent_registry import dynamic_store as ds_mod
from agent_registry.loader import AgentRegistry
from agent_registry.models import AgentManifest, IOSchema, SourceInfo


def _manifest(agent_id: str, team: str = "agent_studio", name: str = "N") -> AgentManifest:
    return AgentManifest(
        id=agent_id,
        team=team,
        name=name,
        summary="s",
        source=SourceInfo(entrypoint="m:f"),
    )


class _FakeStore:
    """In-memory stand-in for the ``dynamic_store`` module functions."""

    def __init__(self) -> None:
        self.rows: dict[str, AgentManifest] = {}
        self.active = True
        self.raise_on: set[str] = set()  # op names that should blow up

    def _maybe_raise(self, op: str) -> None:
        if op in self.raise_on:
            raise RuntimeError(f"boom:{op}")

    def _store_active(self) -> bool:
        return self.active

    def get(self, agent_id: str):
        self._maybe_raise("get")
        return self.rows.get(agent_id)

    def all(self):
        self._maybe_raise("all")
        return list(self.rows.values())

    def upsert(self, manifest: AgentManifest) -> None:
        self._maybe_raise("upsert")
        self.rows[manifest.id] = manifest

    def delete(self, agent_id: str) -> None:
        self._maybe_raise("delete")
        self.rows.pop(agent_id, None)

    def manifests_with_prefix(self, prefix: str):
        self._maybe_raise("manifests_with_prefix")
        return [m for m in self.rows.values() if m.id.startswith(prefix)]


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    store = _FakeStore()
    for name in ("_store_active", "get", "all", "upsert", "delete", "manifests_with_prefix"):
        monkeypatch.setattr(ds_mod, name, getattr(store, name))
    return store


def test_static_id_resolution_never_touches_the_store(fake_store: _FakeStore) -> None:
    disk = _manifest("blogging.planner", team="blogging")
    reg = AgentRegistry([disk], {})
    # Make any store access explode; a static id must not reach it.
    fake_store.raise_on = {"get", "all", "manifests_with_prefix"}
    assert reg.get("blogging.planner") is disk


def test_get_resolves_dynamic_id_from_store(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    saved = _manifest("agent_studio.mine-abc")
    fake_store.upsert(saved)
    got = reg.get("agent_studio.mine-abc")
    assert got is not None and got.id == "agent_studio.mine-abc"


def test_get_reads_your_writes_local_copy_on_store_miss(fake_store: _FakeStore) -> None:
    # Read-your-writes: a manifest present locally (e.g. registered on this worker
    # whose Postgres write-through failed) still resolves even when the store row
    # is absent — the store row is preferred, the local copy is the fallback.
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.local-only-1")
    reg._by_id[m.id] = m  # local copy, not in the store
    assert reg.get("agent_studio.local-only-1") is m


def test_get_unknown_dynamic_id_returns_none(fake_store: _FakeStore) -> None:
    # No static, store, or local entry → None.
    reg = AgentRegistry([], {})
    assert reg.get("agent_studio.nowhere-1") is None


def test_get_degrades_to_local_on_store_error(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.local-1")
    reg._by_id[m.id] = m
    fake_store.raise_on = {"get"}
    assert reg.get("agent_studio.local-1") is m


def test_register_writes_through_for_dynamic_ids(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.new-1")
    reg.register(m)
    assert "agent_studio.new-1" in fake_store.rows


def test_register_never_persists_static_ids(fake_store: _FakeStore) -> None:
    disk = _manifest("blogging.planner", team="blogging")
    reg = AgentRegistry([disk], {})
    # Re-registering a static id (e.g. sandbox injection overwrite) must not persist.
    reg.register(disk)
    assert fake_store.rows == {}


def test_register_swallows_store_error(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    fake_store.raise_on = {"upsert"}
    # Must not raise — the generated path holds a lock and swallows registry errors.
    reg.register(_manifest("agent_studio.err-1"))
    assert reg._by_id["agent_studio.err-1"].id == "agent_studio.err-1"


def test_unregister_deletes_from_store(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.del-1")
    reg.register(m)
    assert reg.unregister("agent_studio.del-1") is True
    assert "agent_studio.del-1" not in fake_store.rows


def test_all_merges_static_over_dynamic(fake_store: _FakeStore) -> None:
    disk = _manifest("blogging.planner", team="blogging", name="Disk")
    reg = AgentRegistry([disk], {})
    fake_store.upsert(_manifest("agent_studio.saved-1"))
    # A dynamic row colliding with a static id must not shadow the disk manifest.
    fake_store.rows["blogging.planner"] = _manifest(
        "blogging.planner", team="blogging", name="Shadow"
    )
    by_id = {m.id: m for m in reg.all()}
    assert set(by_id) == {"blogging.planner", "agent_studio.saved-1"}
    assert by_id["blogging.planner"].name == "Disk"  # static wins


def test_all_degrades_to_local_on_store_error(fake_store: _FakeStore) -> None:
    disk = _manifest("blogging.planner", team="blogging")
    reg = AgentRegistry([disk], {})
    fake_store.raise_on = {"all"}
    assert {m.id for m in reg.all()} == {"blogging.planner"}


def test_all_includes_dynamic_entry_whose_write_through_failed(fake_store: _FakeStore) -> None:
    # register() persists locally even when the best-effort Postgres upsert fails;
    # all()/search()/teams() must not hide that entry from the catalog just because
    # the store never received it (read-your-writes parity with get()).
    reg = AgentRegistry([], {})
    fake_store.raise_on = {"upsert"}
    reg.register(_manifest("agent_studio.write-through-failed-1"))
    ids = {m.id for m in reg.all()}
    assert "agent_studio.write-through-failed-1" in ids


def test_manifests_with_id_prefix_includes_dynamic_entry_whose_write_through_failed(
    fake_store: _FakeStore,
) -> None:
    reg = AgentRegistry([], {})
    fake_store.raise_on = {"upsert"}
    reg.register(
        _manifest("agentic.team-y.write-through-failed-1", team="agentic_team_provisioning")
    )
    ids = {m.id for m in reg.manifests_with_id_prefix("agentic.team-y.")}
    assert "agentic.team-y.write-through-failed-1" in ids


def test_search_and_teams_see_dynamic_entries(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    fake_store.upsert(_manifest("agent_studio.searchme-1", name="Searchable"))
    assert any(s.id == "agent_studio.searchme-1" for s in reg.search())
    assert any(g.team == "agent_studio" and g.agent_count == 1 for g in reg.teams())


def test_manifests_with_id_prefix_unions_store(fake_store: _FakeStore) -> None:
    # A generated agent registered on "another worker" (store-only) is visible to
    # the stale-roster cleanup scan on this worker.
    reg = AgentRegistry([], {})
    fake_store.upsert(_manifest("agentic.team-x.gen-1", team="agentic_team_provisioning"))
    ids = {m.id for m in reg.manifests_with_id_prefix("agentic.team-x.")}
    assert ids == {"agentic.team-x.gen-1"}


def test_manifests_with_id_prefix_static_wins(fake_store: _FakeStore) -> None:
    # A static disk id in the prefix range wins over a colliding dynamic row.
    disk = _manifest("blogging.planner", team="blogging", name="Disk")
    reg = AgentRegistry([disk], {})
    fake_store.rows["blogging.planner"] = _manifest(
        "blogging.planner", team="blogging", name="Shadow"
    )
    got = {m.id: m for m in reg.manifests_with_id_prefix("blogging.")}
    assert got["blogging.planner"].name == "Disk"


def test_manifests_with_id_prefix_degrades_on_store_error(fake_store: _FakeStore) -> None:
    disk = _manifest("blogging.planner", team="blogging")
    reg = AgentRegistry([disk], {})
    fake_store.raise_on = {"manifests_with_prefix"}
    ids = {m.id for m in reg.manifests_with_id_prefix("blogging.")}
    assert ids == {"blogging.planner"}


def test_unregister_swallows_store_error(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.del-err")
    reg._by_id[m.id] = m  # local only
    fake_store.raise_on = {"delete"}
    # Must not raise even though the store delete blows up; local removal still happens.
    assert reg.unregister("agent_studio.del-err") is True
    assert "agent_studio.del-err" not in reg._by_id


def test_get_does_not_resurrect_after_unregister_delete_fails(fake_store: _FakeStore) -> None:
    # A failed best-effort Postgres delete must not let get() resurrect the stale
    # row on the worker that just unregistered it (tombstone window).
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.resurrect-1")
    reg.register(m)
    assert "agent_studio.resurrect-1" in fake_store.rows  # upsert succeeded
    fake_store.raise_on = {"delete"}
    assert reg.unregister("agent_studio.resurrect-1") is True
    # The store row is still there (delete failed) but get() must not return it.
    assert "agent_studio.resurrect-1" in fake_store.rows
    assert reg.get("agent_studio.resurrect-1") is None


def test_register_after_failed_unregister_clears_the_tombstone(fake_store: _FakeStore) -> None:
    # Re-registering the same id supersedes an earlier failed-delete tombstone.
    reg = AgentRegistry([], {})
    reg.register(_manifest("agent_studio.re-reg-1"))
    fake_store.raise_on = {"delete"}
    reg.unregister("agent_studio.re-reg-1")
    assert reg.get("agent_studio.re-reg-1") is None  # tombstoned
    fake_store.raise_on = set()
    fresh = _manifest("agent_studio.re-reg-1", name="Fresh")
    reg.register(fresh)
    assert reg.get("agent_studio.re-reg-1") is fresh


def test_pg_off_behaves_exactly_as_before(fake_store: _FakeStore) -> None:
    fake_store.active = False  # POSTGRES_HOST unset / in sandbox
    disk = _manifest("blogging.planner", team="blogging")
    reg = AgentRegistry([disk], {})
    reg.register(_manifest("agent_studio.local-only"))
    assert reg.get("agent_studio.local-only") is not None  # local dict only
    assert fake_store.rows == {}  # nothing persisted
    assert {m.id for m in reg.all()} == {"blogging.planner", "agent_studio.local-only"}


def test_inline_schema_summary_flag(fake_store: _FakeStore) -> None:
    m = _manifest("agent_studio.inline-1")
    m.inputs = IOSchema(inline_schema={"type": "object"})
    reg = AgentRegistry([m], {})
    summary = reg.search(team="agent_studio")[0]
    assert summary.has_input_schema is True
    assert summary.has_output_schema is False


def test_inline_schema_empty_dict_still_counts_as_present(fake_store: _FakeStore) -> None:
    # has_input_schema must key off presence (matching GET /schema/input's
    # `is not None` check), not truthiness — an empty-but-present inline_schema is
    # still "has a schema" on both sides, so the catalog and the schema endpoint agree.
    m = _manifest("agent_studio.inline-empty-1")
    m.inputs = IOSchema(inline_schema={})
    reg = AgentRegistry([m], {})
    summary = reg.search(team="agent_studio")[0]
    assert summary.has_input_schema is True
