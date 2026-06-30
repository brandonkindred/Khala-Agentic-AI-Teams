"""Tests for llm_service.provider_store: selection/reset logic, encryption,
CRUD/marking SQL, caching, and the Postgres-disabled no-op contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from llm_service import provider_store as ps


def _entry(
    entry_id: int,
    *,
    provider: str = "ollama",
    limit_exceeded: bool = False,
    reset_at=None,
    sort_order: int = 0,
    api_key: str = "",
) -> ps.ProviderEntry:
    return ps.ProviderEntry(
        id=entry_id,
        label=f"e{entry_id}",
        provider=provider,
        model="m",
        base_url="u",
        api_key=api_key,
        sort_order=sort_order,
        limit_exceeded=limit_exceeded,
        limit_type="rate" if limit_exceeded else "",
        reset_at=reset_at,
    )


NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fake DB plumbing (no live Postgres needed)                                   #
# --------------------------------------------------------------------------- #


class FakeCursor:
    def __init__(
        self, fetchone_rows=None, fetchall_rows=None, rowcount: int = 1, raise_on_execute=False
    ) -> None:
        self.executed: list[tuple] = []
        self._fetchone = list(fetchone_rows or [])
        self._fetchall = fetchall_rows if fetchall_rows is not None else []
        self.rowcount = rowcount
        self._raise = raise_on_execute

    def execute(self, sql, params=None):
        if self._raise:
            raise RuntimeError("boom")
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone.pop(0) if self._fetchone else None

    def fetchall(self):
        return self._fetchall

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    """Install a fake Postgres so provider_store CRUD runs without a live DB."""
    import shared_postgres

    cursor = FakeCursor()
    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(shared_postgres, "get_conn", lambda *a, **k: FakeConn(cursor))
    # Skip the DDL self-heal so `cursor.executed` only holds the statement under test.
    monkeypatch.setattr(ps, "_table_ensured", True)
    ps.clear_cache()
    return cursor


def _row(entry: ps.ProviderEntry, ciphertext: str = ""):
    """A SELECT row tuple in _SELECT_COLUMNS order."""
    return (
        entry.id,
        entry.label,
        entry.provider,
        entry.model,
        entry.base_url,
        ciphertext,
        entry.sort_order,
        entry.limit_exceeded,
        entry.limit_type,
        entry.reset_at,
        None,
        None,
    )


# --------------------------------------------------------------------------- #
# Pure selection / reset logic                                                 #
# --------------------------------------------------------------------------- #


def test_select_first_healthy_wins():
    sel = ps.select_active_entry([_entry(1), _entry(2)], now=NOW)
    assert sel.id == 1


def test_select_skips_limited_within_window():
    e1 = _entry(1, limit_exceeded=True, reset_at=NOW + timedelta(hours=1))
    e2 = _entry(2)
    sel = ps.select_active_entry([e1, e2], now=NOW)
    assert sel.id == 2


def test_select_resets_and_uses_expired_entry(monkeypatch):
    reset_ids: list[int] = []
    monkeypatch.setattr(ps, "reset_entry", lambda i: reset_ids.append(i))
    e1 = _entry(1, limit_exceeded=True, reset_at=NOW - timedelta(seconds=1))
    sel = ps.select_active_entry([e1, _entry(2)], now=NOW)
    assert sel.id == 1
    assert reset_ids == [1]  # the expired entry was reset before reuse


def test_select_expired_without_reset_when_disabled(monkeypatch):
    called = []
    monkeypatch.setattr(ps, "reset_entry", lambda i: called.append(i))
    e1 = _entry(1, limit_exceeded=True, reset_at=NOW - timedelta(seconds=1))
    sel = ps.select_active_entry([e1], now=NOW, reset_expired=False)
    assert sel.id == 1 and called == []


def test_select_all_limited_returns_soonest_reset():
    e1 = _entry(1, limit_exceeded=True, reset_at=NOW + timedelta(hours=5))
    e2 = _entry(2, limit_exceeded=True, reset_at=NOW + timedelta(hours=1))
    sel = ps.select_active_entry([e1, e2], now=NOW)
    assert sel.id == 2  # resets soonest


def test_select_all_limited_none_reset_sorts_last():
    e1 = _entry(1, limit_exceeded=True, reset_at=None)
    e2 = _entry(2, limit_exceeded=True, reset_at=NOW + timedelta(hours=1))
    sel = ps.select_active_entry([e1, e2], now=NOW)
    assert sel.id == 2  # the dated reset is preferred over the undated one


def test_select_empty_returns_none():
    assert ps.select_active_entry([], now=NOW) is None


# --------------------------------------------------------------------------- #
# Encryption helpers                                                           #
# --------------------------------------------------------------------------- #


def test_encrypt_decrypt_round_trip():
    token = ps._encrypt_key("sk-secret")
    assert token and token != "sk-secret"
    assert ps._decrypt_key(token) == "sk-secret"


def test_encrypt_empty_is_empty_and_decrypt_empty_is_empty():
    assert ps._encrypt_key("") == ""
    assert ps._decrypt_key("") == ""


def test_decrypt_corrupt_returns_empty():
    assert ps._decrypt_key("not-a-fernet-token") == ""


def test_row_to_entry_makes_reset_at_tz_aware():
    naive = datetime(2026, 6, 30, 12, 0, 0)  # no tzinfo
    e = _entry(1, limit_exceeded=True)
    row = list(_row(e))
    row[9] = naive
    out = ps._row_to_entry(tuple(row))
    assert out.reset_at.tzinfo is timezone.utc


# --------------------------------------------------------------------------- #
# Postgres-disabled contract                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def no_postgres(monkeypatch):
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: False)
    ps.clear_cache()


def test_disabled_load_is_empty(no_postgres):
    assert ps.load_ordered_entries() == []


def test_disabled_resolve_active_is_none(no_postgres):
    assert ps.resolve_active_provider_config() is None
    assert ps.resolve_active_provider_config("backend") is None


def test_disabled_create_update_delete_reorder_raise(no_postgres):
    with pytest.raises(RuntimeError):
        ps.create_entry(label="x", provider="ollama")
    with pytest.raises(RuntimeError):
        ps.update_entry(1, label="x")
    with pytest.raises(RuntimeError):
        ps.delete_entry(1)
    with pytest.raises(RuntimeError):
        ps.reorder([1, 2])


def test_disabled_mark_and_reset_are_noops(no_postgres):
    # Must not raise; nothing to persist.
    ps.mark_exhausted(1, limit_type="rate", reset_at=NOW)
    ps.reset_entry(1)


# --------------------------------------------------------------------------- #
# CRUD / marking against the fake DB                                           #
# --------------------------------------------------------------------------- #


def test_load_ordered_entries_decrypts_and_orders(fake_db):
    token = ps._encrypt_key("k")
    fake_db._fetchall = [_row(_entry(1, api_key="k"), ciphertext=token)]
    out = ps.load_ordered_entries(use_cache=False)
    assert len(out) == 1 and out[0].api_key == "k"
    assert "ORDER BY sort_order ASC" in fake_db.executed[-1][0]


def test_create_entry_inserts_and_returns(fake_db):
    fake_db._fetchone = [_row(_entry(7, sort_order=3))]
    out = ps.create_entry(label="Anthropic", provider="claude", model="m", api_key="k")
    assert out.id == 7
    sql = fake_db.executed[-1][0]
    assert "INSERT INTO llm_provider_configs" in sql and "MAX(sort_order)" in sql


def test_create_entry_validates_inputs(fake_db):
    with pytest.raises(ValueError):
        ps.create_entry(label="", provider="ollama")
    with pytest.raises(ValueError):
        ps.create_entry(label="x", provider="")


def test_update_entry_config_change_clears_limit_state(fake_db):
    fake_db._fetchone = [_row(_entry(2))]
    ps.update_entry(2, label="new", api_key="k2")  # api_key is a config field
    sql = fake_db.executed[-1][0]
    assert "limit_exceeded = FALSE" in sql and "reset_at = NULL" in sql
    assert "label = %s" in sql


def test_update_label_only_preserves_limit_state(fake_db):
    fake_db._fetchone = [_row(_entry(2))]
    ps.update_entry(2, label="renamed")  # cosmetic edit only
    sql = fake_db.executed[-1][0]
    assert "label = %s" in sql
    # A label-only edit must NOT un-mark a still-rate-limited provider.
    assert "limit_exceeded = FALSE" not in sql and "reset_at = NULL" not in sql


def test_update_model_change_clears_limit_state(fake_db):
    fake_db._fetchone = [_row(_entry(2))]
    ps.update_entry(2, model="other-model")
    sql = fake_db.executed[-1][0]
    assert "model = %s" in sql and "limit_exceeded = FALSE" in sql


def test_update_entry_skips_unset_fields(fake_db):
    fake_db._fetchone = [_row(_entry(2))]
    ps.update_entry(2)  # no fields at all → only updated_at
    sql = fake_db.executed[-1][0]
    assert "label = %s" not in sql
    assert "limit_exceeded = FALSE" not in sql  # nothing config-affecting changed
    assert "updated_at = NOW()" in sql


def test_delete_entry_reports_rowcount(fake_db):
    fake_db.rowcount = 1
    assert ps.delete_entry(5) is True
    fake_db.rowcount = 0
    assert ps.delete_entry(5) is False


def test_reorder_assigns_positions(fake_db):
    fake_db._fetchall = [(1,), (2,), (3,)]  # live id set for the FOR UPDATE check
    ps.reorder([3, 1, 2])
    # A single bulk UPDATE (CASE id WHEN <id> THEN <position>) — one round-trip.
    updates = [c for c in fake_db.executed if "sort_order = CASE id" in c[0]]
    assert len(updates) == 1
    sql, params = updates[0]
    # CASE arms pair each id with its 0-based position, then the WHERE IN id list.
    assert list(params) == [3, 0, 1, 1, 2, 2, 3, 1, 2]
    assert "WHERE id IN (%s, %s, %s)" in sql


def test_reorder_rejects_non_permutation(fake_db):
    fake_db._fetchall = [(1,), (2,), (3,)]
    with pytest.raises(ps.ReorderMismatchError):
        ps.reorder([1, 2])  # missing id 3
    with pytest.raises(ps.ReorderMismatchError):
        ps.reorder([1, 2, 99])  # unknown id
    # No UPDATE was issued for the rejected reorders (only the SELECT).
    assert not any("sort_order = CASE id" in c[0] for c in fake_db.executed)


def test_mark_exhausted_writes_limit_state(fake_db):
    ps.mark_exhausted(4, limit_type="weekly", reset_at=NOW)
    sql, params = fake_db.executed[-1]
    assert "limit_exceeded = TRUE" in sql
    assert params == ("weekly", NOW, 4)


def test_reset_entry_is_conditional(fake_db):
    ps.reset_entry(4)
    sql, params = fake_db.executed[-1]
    assert "limit_exceeded = FALSE" in sql and "AND limit_exceeded = TRUE" in sql
    assert params == (4,)


def test_get_entry_returns_mapped_row(fake_db):
    fake_db._fetchone = [_row(_entry(9))]
    out = ps.get_entry(9)
    assert out.id == 9


def test_get_entry_missing_returns_none(fake_db):
    fake_db._fetchone = []
    assert ps.get_entry(123) is None


def test_resolve_active_provider_config_uses_selection(fake_db):
    fake_db._fetchall = [_row(_entry(1)), _row(_entry(2))]
    out = ps.resolve_active_provider_config()
    assert out.id == 1


def test_update_entry_all_fields(fake_db):
    fake_db._fetchone = [_row(_entry(2))]
    ps.update_entry(2, label="L", provider="claude", model="mm", base_url="http://x", api_key="k")
    sql = fake_db.executed[-1][0]
    for frag in (
        "label = %s",
        "provider = %s",
        "model = %s",
        "base_url = %s",
        "api_key_ciphertext = %s",
    ):
        assert frag in sql


def test_ttl_seconds_env_override(monkeypatch):
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_TTL_S", "5")
    assert ps._ttl_seconds() == 5.0
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_TTL_S", "garbage")
    assert ps._ttl_seconds() == ps._DEFAULT_TTL_S
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_TTL_S", "-1")
    assert ps._ttl_seconds() == 0.0


def test_ensure_table_runs_ddl(monkeypatch):
    import shared_postgres

    cursor = FakeCursor()
    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(shared_postgres, "get_conn", lambda *a, **k: FakeConn(cursor))
    monkeypatch.setattr(ps, "_table_ensured", False)
    ps._ensure_table()
    joined = " ".join(sql for sql, _ in cursor.executed)
    assert "CREATE TABLE IF NOT EXISTS llm_provider_configs" in joined
    assert "CREATE INDEX" in joined


def test_ensure_table_swallows_ddl_error(monkeypatch):
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(
        shared_postgres, "get_conn", lambda *a, **k: FakeConn(FakeCursor(raise_on_execute=True))
    )
    monkeypatch.setattr(ps, "_table_ensured", False)
    ps._ensure_table()  # must not raise


def test_load_uncached_disabled_returns_empty(no_postgres):
    assert ps._load_ordered_uncached() == []


def test_load_uncached_swallows_read_error(monkeypatch):
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(
        shared_postgres, "get_conn", lambda *a, **k: FakeConn(FakeCursor(raise_on_execute=True))
    )
    monkeypatch.setattr(ps, "_table_ensured", True)
    assert ps._load_ordered_uncached() == []


def test_get_entry_disabled_returns_none(no_postgres):
    assert ps.get_entry(1) is None


def test_get_entry_swallows_read_error(monkeypatch):
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(
        shared_postgres, "get_conn", lambda *a, **k: FakeConn(FakeCursor(raise_on_execute=True))
    )
    monkeypatch.setattr(ps, "_table_ensured", True)
    assert ps.get_entry(1) is None


def test_mark_and_reset_swallow_write_errors(monkeypatch):
    import shared_postgres

    monkeypatch.setattr(shared_postgres, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(
        shared_postgres, "get_conn", lambda *a, **k: FakeConn(FakeCursor(raise_on_execute=True))
    )
    monkeypatch.setattr(ps, "_table_ensured", True)
    # Must not raise — marking/reset are best-effort.
    ps.mark_exhausted(1, limit_type="rate", reset_at=NOW)
    ps.reset_entry(1)


def test_list_fingerprint_empty_is_none(no_postgres):
    assert ps.list_fingerprint() == "none"


def test_list_fingerprint_changes_with_structure(fake_db):
    fake_db._fetchall = [_row(_entry(1))]
    fp1 = ps.list_fingerprint()
    assert fp1 != "none"
    ps.clear_cache()
    fake_db._fetchall = [_row(_entry(1)), _row(_entry(2, sort_order=1))]
    fp2 = ps.list_fingerprint()
    assert fp1 != fp2  # adding an entry changes the structural fingerprint


def test_list_fingerprint_stable_across_limit_state(fake_db):
    fake_db._fetchall = [_row(_entry(1))]
    fp1 = ps.list_fingerprint()
    ps.clear_cache()
    # Same structure, now usage-limited: fingerprint must NOT change (so a 429
    # marking never churns the Strands cache).
    fake_db._fetchall = [_row(_entry(1, limit_exceeded=True, reset_at=NOW))]
    fp2 = ps.list_fingerprint()
    assert fp1 == fp2


def test_load_ordered_entries_caches(fake_db):
    fake_db._fetchall = [_row(_entry(1))]
    first = ps.load_ordered_entries()
    n_after_first = len(fake_db.executed)
    second = ps.load_ordered_entries()  # served from cache, no new query
    assert len(fake_db.executed) == n_after_first
    assert [e.id for e in first] == [e.id for e in second]
    ps.clear_cache()
    ps.load_ordered_entries()  # cache cleared → queries again
    assert len(fake_db.executed) > n_after_first
