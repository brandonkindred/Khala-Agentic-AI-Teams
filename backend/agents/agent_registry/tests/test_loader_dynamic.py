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

    def replace_manifests(self, upserts, delete_ids, *, conn=None) -> None:
        del conn
        self._maybe_raise("replace_manifests")
        for agent_id in delete_ids:
            self.rows.pop(agent_id, None)
        for manifest in upserts:
            self.rows[manifest.id] = manifest


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    store = _FakeStore()
    for name in (
        "_store_active",
        "get",
        "all",
        "upsert",
        "delete",
        "manifests_with_prefix",
        "replace_manifests",
    ):
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
    # Read-your-writes: a manifest registered on this worker whose Postgres
    # write-through FAILED (so it's unconfirmed) still resolves on a store miss —
    # the local copy is the fallback for exactly this case.
    reg = AgentRegistry([], {})
    fake_store.raise_on = {"upsert"}  # write-through fails → id marked unconfirmed
    m = _manifest("agent_studio.local-only-1")
    reg.register(m)
    assert "agent_studio.local-only-1" in reg._unconfirmed
    assert reg.get("agent_studio.local-only-1").id == "agent_studio.local-only-1"


def test_get_does_not_resurrect_confirmed_id_deleted_on_another_worker(
    fake_store: _FakeStore,
) -> None:
    # The resurrection bug: register() confirms the write-through (id NOT unconfirmed),
    # then another worker deletes the row from Postgres. A store miss for a *confirmed*
    # id must return None — not the stale local copy — so cross-worker deletes are seen.
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.shared-1")
    reg.register(m)  # upsert succeeds → confirmed, still in local _by_id
    assert "agent_studio.shared-1" not in reg._unconfirmed
    assert reg.get("agent_studio.shared-1") is not None  # resolves via the store row
    # Simulate another worker's unregister(): the Postgres row is gone.
    fake_store.rows.pop("agent_studio.shared-1")
    assert reg.get("agent_studio.shared-1") is None  # not resurrected from _by_id
    # And it's dropped from the catalog listing too (consistent with get()).
    assert "agent_studio.shared-1" not in {mm.id for mm in reg.all()}


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
    # Default path is best-effort: local install survives a store upsert failure.
    reg.register(_manifest("agent_studio.err-1"))
    assert reg._by_id["agent_studio.err-1"].id == "agent_studio.err-1"


def test_register_require_persist_raises_and_rolls_back_local(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    fake_store.raise_on = {"upsert"}
    m = _manifest("agent_studio.strict-1")
    with pytest.raises(RuntimeError, match="boom:upsert"):
        reg.register(m, require_persist=True)
    assert "agent_studio.strict-1" not in reg._by_id
    assert "agent_studio.strict-1" not in reg._unconfirmed
    assert "agent_studio.strict-1" not in fake_store.rows


def test_register_require_persist_restores_prior_on_upsert_failure(
    fake_store: _FakeStore,
) -> None:
    reg = AgentRegistry([], {})
    prior = _manifest("agent_studio.strict-overwrite", name="Prior")
    reg.register(prior)
    assert "agent_studio.strict-overwrite" in fake_store.rows

    fake_store.raise_on = {"upsert"}
    with pytest.raises(RuntimeError, match="boom:upsert"):
        reg.register(
            _manifest("agent_studio.strict-overwrite", name="New"),
            require_persist=True,
        )
    # Prior local entry restored; store still has the last successful upsert.
    assert reg._by_id["agent_studio.strict-overwrite"].name == "Prior"
    assert fake_store.rows["agent_studio.strict-overwrite"].name == "Prior"


def test_register_require_persist_succeeds_when_store_inactive(
    fake_store: _FakeStore,
) -> None:
    # No dynamic store → local-only is authoritative; require_persist is a no-op.
    fake_store.active = False
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.strict-local")
    reg.register(m, require_persist=True)
    assert reg.get("agent_studio.strict-local") is m
    assert "agent_studio.strict-local" in reg._unconfirmed


def test_register_require_persist_rollback_preserves_concurrent_install(
    fake_store: _FakeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed require_persist must not clobber a concurrent re-register of the same id."""
    reg = AgentRegistry([], {})
    failing = _manifest("agent_studio.race-1", name="Failing")
    concurrent = _manifest("agent_studio.race-1", name="Concurrent")

    def _upsert_then_race(manifest):
        # Simulate another thread installing a newer entry while our store call runs
        # (lock is released around the upsert).
        with reg._lock:
            reg._by_id[manifest.id] = concurrent
            reg._tombstones.pop(manifest.id, None)
            reg._unconfirmed.discard(manifest.id)
        raise RuntimeError("boom:upsert")

    monkeypatch.setattr(ds_mod, "upsert", _upsert_then_race)
    with pytest.raises(RuntimeError, match="boom:upsert"):
        reg.register(failing, require_persist=True)

    assert reg._by_id["agent_studio.race-1"] is concurrent
    assert reg._by_id["agent_studio.race-1"].name == "Concurrent"


def test_replace_dynamic_manifests_rejects_overlap(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    m = _manifest("agentic.team-1.both", team="agentic_team_provisioning")
    with pytest.raises(ValueError, match="disjoint"):
        reg.replace_dynamic_manifests([m], [m.id])
    assert m.id not in reg._by_id
    assert m.id not in fake_store.rows


def test_replace_dynamic_manifests_writes_atomically(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    prior = _manifest("agentic.team-1.old", team="agentic_team_provisioning")
    reg.register(prior)
    replacement = _manifest("agentic.team-1.new", team="agentic_team_provisioning")

    reg.replace_dynamic_manifests([replacement], [prior.id])

    assert prior.id not in fake_store.rows
    assert replacement.id in fake_store.rows
    assert reg.get(prior.id) is None
    assert reg.get(replacement.id) is replacement


def test_replace_dynamic_manifests_leaves_local_unchanged_on_store_failure(
    fake_store: _FakeStore,
) -> None:
    reg = AgentRegistry([], {})
    prior = _manifest("agentic.team-1.old", team="agentic_team_provisioning")
    reg.register(prior)
    fake_store.raise_on = {"replace_manifests"}

    with pytest.raises(RuntimeError, match="boom:replace_manifests"):
        reg.replace_dynamic_manifests(
            [_manifest("agentic.team-1.new", team="agentic_team_provisioning")],
            [prior.id],
        )

    assert prior.id in fake_store.rows
    assert "agentic.team-1.new" not in fake_store.rows
    assert reg.get(prior.id) is not None
    assert reg._by_id[prior.id].id == prior.id


def test_replace_manifests_rejects_empty_id_and_overlap() -> None:
    # Hit the real validators before any Postgres work (no fake_store patch).
    from agent_registry.dynamic_store import replace_manifests

    with pytest.raises(ValueError, match="non-empty id"):
        replace_manifests([_manifest("")], [])
    with pytest.raises(ValueError, match="disjoint"):
        replace_manifests([_manifest("agent_studio.x")], ["agent_studio.x"])


def test_upsert_rejects_empty_id() -> None:
    from agent_registry.dynamic_store import upsert

    with pytest.raises(ValueError, match="non-empty"):
        upsert(_manifest(""))


def test_replace_manifests_shared_conn_avoids_nested_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared-conn path must not call _ensure_schema (nested get_conn deadlock)."""
    from agent_registry import dynamic_store as ds

    monkeypatch.setattr(ds, "_schema_ensured", False)

    def _boom_ensure() -> None:
        raise AssertionError("nested _ensure_schema / pool checkout")

    monkeypatch.setattr(ds, "_ensure_schema", _boom_ensure)

    executed: list[str] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            executed.append(sql if isinstance(sql, str) else str(sql))

    class _Conn:
        def cursor(self, *args, **kwargs):
            return _Cur()

    ds.replace_manifests([_manifest("agent_studio.shared-1")], [], conn=_Conn())
    # DDL + upsert ran on the shared conn; _ensure_schema was never entered.
    assert any("CREATE TABLE" in s or "agent_registry_dynamic" in s for s in executed)
    assert any("INSERT INTO" in s for s in executed)


def test_replace_manifests_shared_conn_propagates_execute_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared-conn write failures must raise without flipping ``_schema_ensured``."""
    from agent_registry import dynamic_store as ds

    monkeypatch.setattr(ds, "_schema_ensured", True)  # skip DDL; hit _execute only

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            raise RuntimeError("shared conn execute boom")

    class _Conn:
        def cursor(self, *args, **kwargs):
            return _Cur()

    with pytest.raises(RuntimeError, match="shared conn execute boom"):
        ds.replace_manifests([_manifest("agent_studio.shared-fail")], [], conn=_Conn())
    # A rolled-back outer txn must be able to retry schema on the same process.
    assert ds._schema_ensured is True  # unchanged from our pre-set True


def test_replace_manifests_shared_conn_propagates_schema_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared-conn DDL failures propagate and must not set ``_schema_ensured``."""
    from agent_registry import dynamic_store as ds

    monkeypatch.setattr(ds, "_schema_ensured", False)

    def _boom_apply(cur) -> None:
        raise RuntimeError("shared conn ddl boom")

    monkeypatch.setattr(ds, "_apply_schema_statements", _boom_apply)

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            raise AssertionError("execute should not run after DDL failure")

    class _Conn:
        def cursor(self, *args, **kwargs):
            return _Cur()

    with pytest.raises(RuntimeError, match="shared conn ddl boom"):
        ds.replace_manifests([_manifest("agent_studio.shared-ddl")], [], conn=_Conn())
    assert ds._schema_ensured is False


def test_manifests_with_id_prefix_require_store_propagates(
    fake_store: _FakeStore,
) -> None:
    reg = AgentRegistry([], {})
    fake_store.raise_on = {"manifests_with_prefix"}
    with pytest.raises(RuntimeError, match="boom:manifests_with_prefix"):
        reg.manifests_with_id_prefix("agentic.", require_store=True)
    # Default still degrades to local.
    assert reg.manifests_with_id_prefix("agentic.") == []


def test_unregister_deletes_from_store(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    m = _manifest("agent_studio.del-1")
    reg.register(m)
    assert reg.unregister("agent_studio.del-1") is True
    assert "agent_studio.del-1" not in fake_store.rows


def test_unregister_refuses_a_static_id(fake_store: _FakeStore) -> None:
    disk = _manifest("blogging.planner", team="blogging")
    reg = AgentRegistry([disk], {})
    assert reg.unregister("blogging.planner") is False
    # Still resolvable — a static id is never actually removed.
    assert reg.get("blogging.planner") is not None
    assert "blogging.planner" not in fake_store.rows


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


def test_all_excludes_tombstoned_id_even_though_store_row_still_there(
    fake_store: _FakeStore,
) -> None:
    # Consistency with get(): within the tombstone window, all()/search()/teams()
    # must not list an id this worker just unregistered, even though the stale
    # store row (failed delete) or this worker's own stale local copy would
    # otherwise resurface it.
    reg = AgentRegistry([], {})
    reg.register(_manifest("agent_studio.tombstoned-listing-1"))
    fake_store.raise_on = {"delete"}
    reg.unregister("agent_studio.tombstoned-listing-1")
    assert "agent_studio.tombstoned-listing-1" in fake_store.rows  # delete failed
    assert reg.get("agent_studio.tombstoned-listing-1") is None  # tombstoned
    ids = {m.id for m in reg.all()}
    assert "agent_studio.tombstoned-listing-1" not in ids


def test_all_excludes_tombstoned_id_when_store_all_fails(fake_store: _FakeStore) -> None:
    # Same guarantee as the happy path above, but when store.all() itself raises
    # (e.g. Postgres outage) and _merged_manifests falls back to the local-only
    # view — that fallback must apply _drop_tombstoned too.
    reg = AgentRegistry([], {})
    reg.register(_manifest("agent_studio.tombstoned-outage-1"))
    reg.unregister("agent_studio.tombstoned-outage-1")
    fake_store.raise_on = {"all"}
    ids = {m.id for m in reg.all()}
    assert "agent_studio.tombstoned-outage-1" not in ids


def test_manifests_with_id_prefix_excludes_tombstoned_id(fake_store: _FakeStore) -> None:
    reg = AgentRegistry([], {})
    reg.register(_manifest("agentic.team-z.tombstoned-1", team="agentic_team_provisioning"))
    fake_store.raise_on = {"delete"}
    reg.unregister("agentic.team-z.tombstoned-1")
    ids = {m.id for m in reg.manifests_with_id_prefix("agentic.team-z.")}
    assert "agentic.team-z.tombstoned-1" not in ids


def test_manifests_with_id_prefix_excludes_tombstoned_id_when_store_scan_fails(
    fake_store: _FakeStore,
) -> None:
    # Same guarantee as the happy path above, but when the store's prefix scan
    # itself raises and the method falls back to the local-only view.
    reg = AgentRegistry([], {})
    reg.register(_manifest("agentic.team-z.tombstoned-2", team="agentic_team_provisioning"))
    reg.unregister("agentic.team-z.tombstoned-2")
    fake_store.raise_on = {"manifests_with_prefix"}
    ids = {m.id for m in reg.manifests_with_id_prefix("agentic.team-z.")}
    assert "agentic.team-z.tombstoned-2" not in ids


def test_tombstones_are_bounded_and_evict_oldest(
    fake_store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = AgentRegistry([], {})
    monkeypatch.setattr(AgentRegistry, "_TOMBSTONE_MAX_ENTRIES", 2)
    fake_store.raise_on = {"delete"}
    for i in range(3):
        reg.register(_manifest(f"agent_studio.bound-{i}"))
        reg.unregister(f"agent_studio.bound-{i}")
    # Cap held at 2; the oldest (bound-0) was evicted, so its tombstone no longer
    # masks a (hypothetical) fresh store row for that id.
    assert len(reg._tombstones) == 2
    assert "agent_studio.bound-0" not in reg._tombstones
    assert "agent_studio.bound-1" in reg._tombstones
    assert "agent_studio.bound-2" in reg._tombstones


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
