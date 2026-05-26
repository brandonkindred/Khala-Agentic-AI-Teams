"""Tests for ``sales_team.dossier_store.DossierStore``.

The store is Postgres-only in production. These tests swap the module's
``get_conn`` for a dict-backed fake cursor so we exercise every code path
(insert, upsert, batch lookup, missing rows, ts parsing) without needing
a live database.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

from sales_team import dossier_store
from sales_team.models import DeepResearchResult, Prospect, ProspectDossier, ProspectListEntry

# ---------------------------------------------------------------------------
# Fake Postgres
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db
        self._last_fetch_one: dict | None = None
        self._last_fetch_all: list[dict] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params: tuple | list = ()) -> None:
        sql_l = " ".join(sql.split()).lower()
        params = tuple(params)

        if sql_l.startswith("insert into sales_dossiers"):
            (id_, prospect_id, company_name, full_name, data, generated_at) = params
            payload = data.obj if hasattr(data, "obj") else data
            self._db["dossiers"][id_] = {
                "id": id_,
                "prospect_id": prospect_id,
                "company_name": company_name,
                "full_name": full_name,
                "data": payload,
                "generated_at": generated_at,
            }
            return

        if sql_l.startswith("select data from sales_dossiers where id"):
            (id_,) = params
            row = self._db["dossiers"].get(id_)
            self._last_fetch_one = {"data": row["data"]} if row else None
            return

        if "select prospect_id, data from sales_dossiers where prospect_id" in sql_l:
            (ids,) = params
            wanted = set(ids)
            self._last_fetch_all = [
                {"prospect_id": row["prospect_id"], "data": row["data"]}
                for row in self._db["dossiers"].values()
                if row["prospect_id"] in wanted
            ]
            return

        if sql_l.startswith("insert into sales_prospect_lists"):
            (
                id_,
                product_name,
                total_prospects,
                companies_represented,
                data,
                generated_at,
            ) = params
            payload = data.obj if hasattr(data, "obj") else data
            self._db["lists"][id_] = {
                "id": id_,
                "product_name": product_name,
                "total_prospects": total_prospects,
                "companies_represented": companies_represented,
                "data": payload,
                "generated_at": generated_at,
            }
            return

        if sql_l.startswith("select data from sales_prospect_lists where id"):
            (id_,) = params
            row = self._db["lists"].get(id_)
            self._last_fetch_one = {"data": row["data"]} if row else None
            return

        if "from sales_prospect_lists" in sql_l and "order by generated_at desc" in sql_l:
            (limit,) = params
            ordered = sorted(
                self._db["lists"].values(),
                key=lambda r: r["generated_at"],
                reverse=True,
            )[:limit]
            self._last_fetch_all = [
                {
                    "id": r["id"],
                    "product_name": r["product_name"],
                    "total_prospects": r["total_prospects"],
                    "companies_represented": r["companies_represented"],
                    "generated_at": r["generated_at"],
                }
                for r in ordered
            ]
            return

        raise AssertionError(f"unexpected SQL in fake cursor: {sql!r}")

    def fetchone(self):
        return self._last_fetch_one

    def fetchall(self):
        return self._last_fetch_all


class _FakeConn:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db

    def cursor(self, row_factory=None):  # noqa: ANN001
        return _FakeCursor(self._db)


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch):
    """Install the fake ``get_conn`` on the dossier_store module."""
    db: dict[str, Any] = {"dossiers": {}, "lists": {}}

    @contextmanager
    def _fake_get_conn(*args, **kwargs):
        yield _FakeConn(db)

    monkeypatch.setattr(dossier_store, "get_conn", _fake_get_conn)
    return db


# ---------------------------------------------------------------------------
# Helpers / module-level
# ---------------------------------------------------------------------------


def test_now_iso_returns_iso_with_offset() -> None:
    out = dossier_store._now_iso()
    assert "T" in out
    assert out.endswith("+00:00") or out.endswith("Z")


def test_parse_ts_returns_now_on_empty_string() -> None:
    out = dossier_store._parse_ts("")
    assert isinstance(out, datetime)
    assert out.tzinfo is timezone.utc


def test_parse_ts_returns_now_on_invalid_string() -> None:
    out = dossier_store._parse_ts("not-a-timestamp")
    assert isinstance(out, datetime)


def test_parse_ts_parses_valid_iso8601() -> None:
    out = dossier_store._parse_ts("2026-05-21T01:02:03+00:00")
    assert out.year == 2026
    assert out.month == 5
    assert out.day == 21


# ---------------------------------------------------------------------------
# DossierStore.save_dossier / get_dossier
# ---------------------------------------------------------------------------


def _make_dossier(**overrides) -> ProspectDossier:
    fields = dict(
        prospect_id="prs_1",
        full_name="Jane Smith",
        current_title="VP Sales",
        current_company="Acme Corp",
        executive_summary="Runs the SDR team at Acme.",
        sources=["https://news.example.com/acme-series-b"],
        confidence=0.7,
    )
    fields.update(overrides)
    return ProspectDossier(**fields)


def test_save_dossier_assigns_id_and_timestamp_when_missing(fake_pg) -> None:
    store = dossier_store.DossierStore()
    saved = store.save_dossier(_make_dossier())
    assert saved.dossier_id.startswith("dsr_")
    assert saved.generated_at
    # And it landed in the fake DB.
    assert saved.dossier_id in fake_pg["dossiers"]


def test_save_dossier_keeps_caller_id_and_timestamp(fake_pg) -> None:
    store = dossier_store.DossierStore()
    dossier = _make_dossier(dossier_id="dsr_existing", generated_at="2026-05-21T00:00:00+00:00")
    saved = store.save_dossier(dossier)
    assert saved.dossier_id == "dsr_existing"
    assert saved.generated_at == "2026-05-21T00:00:00+00:00"
    # The data column carries the full model dump.
    row = fake_pg["dossiers"]["dsr_existing"]
    assert row["data"]["prospect_id"] == "prs_1"


def test_get_dossier_returns_none_when_missing(fake_pg) -> None:
    store = dossier_store.DossierStore()
    assert store.get_dossier("dsr_missing") is None


def test_get_dossier_round_trip(fake_pg) -> None:
    store = dossier_store.DossierStore()
    saved = store.save_dossier(_make_dossier())
    loaded = store.get_dossier(saved.dossier_id)
    assert loaded is not None
    assert loaded.full_name == "Jane Smith"
    assert loaded.prospect_id == "prs_1"


# ---------------------------------------------------------------------------
# DossierStore.get_dossiers_by_prospect_ids
# ---------------------------------------------------------------------------


def test_get_dossiers_by_prospect_ids_returns_empty_when_no_ids(fake_pg) -> None:
    store = dossier_store.DossierStore()
    # Early-return branch — must NOT touch Postgres.
    assert store.get_dossiers_by_prospect_ids([]) == {}


def test_get_dossiers_by_prospect_ids_returns_keyed_map(fake_pg) -> None:
    store = dossier_store.DossierStore()
    a = store.save_dossier(_make_dossier(prospect_id="prs_a", full_name="A"))
    b = store.save_dossier(_make_dossier(prospect_id="prs_b", full_name="B"))
    out = store.get_dossiers_by_prospect_ids(["prs_a", "prs_b", "prs_missing"])
    assert set(out.keys()) == {"prs_a", "prs_b"}
    assert out["prs_a"].dossier_id == a.dossier_id
    assert out["prs_b"].dossier_id == b.dossier_id


def test_get_dossiers_by_prospect_ids_keeps_newest_when_duplicate(fake_pg) -> None:
    store = dossier_store.DossierStore()
    # Save older.
    store.save_dossier(
        _make_dossier(
            dossier_id="dsr_old",
            prospect_id="prs_dup",
            generated_at="2025-01-01T00:00:00+00:00",
        )
    )
    # Save newer.
    store.save_dossier(
        _make_dossier(
            dossier_id="dsr_new",
            prospect_id="prs_dup",
            generated_at="2026-01-01T00:00:00+00:00",
        )
    )
    out = store.get_dossiers_by_prospect_ids(["prs_dup"])
    assert out["prs_dup"].dossier_id == "dsr_new"


# ---------------------------------------------------------------------------
# DossierStore.save_prospect_list / get / list
# ---------------------------------------------------------------------------


def _make_list(**overrides) -> DeepResearchResult:
    p = Prospect(id="prs_a", company_name="Acme")
    entry = ProspectListEntry(
        rank=1, prospect=p, dossier_id="dsr_x", dossier_url="/api/sales/dossiers/dsr_x"
    )
    fields = dict(
        list_id="",
        product_name="ProductX",
        generated_at="",
        total_prospects=1,
        companies_represented=1,
        entries=[entry],
    )
    fields.update(overrides)
    return DeepResearchResult(**fields)


def test_save_prospect_list_assigns_id_and_timestamp(fake_pg) -> None:
    store = dossier_store.DossierStore()
    saved = store.save_prospect_list(_make_list())
    assert saved.list_id.startswith("plst_")
    assert saved.generated_at
    assert saved.list_id in fake_pg["lists"]


def test_save_prospect_list_keeps_caller_provided_id(fake_pg) -> None:
    store = dossier_store.DossierStore()
    saved = store.save_prospect_list(
        _make_list(list_id="plst_explicit", generated_at="2026-05-21T01:01:01+00:00")
    )
    assert saved.list_id == "plst_explicit"


def test_get_prospect_list_returns_none_when_missing(fake_pg) -> None:
    store = dossier_store.DossierStore()
    assert store.get_prospect_list("plst_nope") is None


def test_get_prospect_list_round_trip(fake_pg) -> None:
    store = dossier_store.DossierStore()
    saved = store.save_prospect_list(_make_list())
    loaded = store.get_prospect_list(saved.list_id)
    assert loaded is not None
    assert loaded.product_name == "ProductX"
    assert loaded.entries[0].prospect.company_name == "Acme"


def test_list_prospect_lists_returns_summaries_sorted_newest_first(fake_pg) -> None:
    store = dossier_store.DossierStore()
    store.save_prospect_list(_make_list(product_name="A", generated_at="2025-01-01T00:00:00+00:00"))
    store.save_prospect_list(_make_list(product_name="B", generated_at="2026-05-21T00:00:00+00:00"))
    summaries = store.list_prospect_lists(limit=50)
    names = [s["product_name"] for s in summaries]
    assert names == ["B", "A"]
    # Summary fields are the minimal ones, not the full data payload.
    expected_keys = {
        "list_id",
        "product_name",
        "total_prospects",
        "companies_represented",
        "generated_at",
    }
    assert set(summaries[0].keys()) == expected_keys


def test_list_prospect_lists_respects_limit(fake_pg) -> None:
    store = dossier_store.DossierStore()
    for i in range(5):
        store.save_prospect_list(
            _make_list(product_name=f"P{i}", generated_at=f"2025-01-0{i + 1}T00:00:00+00:00")
        )
    assert len(store.list_prospect_lists(limit=2)) == 2


def test_list_prospect_lists_normalizes_non_datetime_generated_at(fake_pg) -> None:
    """If the DB ever returns a string for generated_at (unusual but possible
    if the column type drifted), the summary serialiser must still produce
    a string without raising."""
    store = dossier_store.DossierStore()
    store.save_prospect_list(_make_list(generated_at="2026-05-21T00:00:00+00:00"))
    # Mutate the fake row to use a plain string for generated_at (mimics
    # type drift). The store's branch ``isinstance(...)`` falls through to
    # ``str(...)`` in that case.
    list_id = next(iter(fake_pg["lists"]))
    fake_pg["lists"][list_id]["generated_at"] = "not-a-datetime"
    summaries = store.list_prospect_lists()
    assert summaries[0]["generated_at"] == "not-a-datetime"
