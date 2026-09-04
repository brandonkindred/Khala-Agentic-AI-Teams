"""Pins ``branding_team.store.now_iso``'s output contract.

Its own module rather than beside the store tests: ``test_store.py`` carries
``pytestmark = [pytest.mark.integration, pytest.mark.real_postgres]``, and a
pure-format contract needs no database — parked there it would simply skip.
"""

from __future__ import annotations

import re


def test_now_iso_is_utc_with_an_explicit_offset() -> None:
    """``now_iso`` is public API precisely so ``coordination.py`` can share it, and
    its shape is load-bearing beyond "it parses".

    Every branding write path stamps ``updated_at`` with it, so a silent change of
    format -- a ``Z`` suffix, a coarser ``timespec`` -- would break lexicographic
    ordering of that column against rows already written, without breaking any
    existing test: those only exercise that a write succeeds. This pins the
    postcondition the docstring states.
    """
    from datetime import datetime, timedelta

    from branding_team.store import now_iso

    value = now_iso()
    parsed = datetime.fromisoformat(value)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert value.endswith("+00:00")
    # Microsecond precision, pinned via timespec in now_iso itself: without it
    # isoformat() omits the fraction whenever microsecond == 0, so this assertion
    # would fail against a correct implementation roughly once in a million runs.
    assert re.fullmatch(r".*T\d{2}:\d{2}:\d{2}\.\d{6}", value.split("+")[0])


def test_create_client_stamps_through_the_shared_formatter(monkeypatch) -> None:
    """The store's OWN write paths must go through ``now_iso`` too.

    ``test_now_iso_is_utc_...`` pins the formatter's output but would keep
    passing if a call site reverted to a hand-rolled
    ``datetime.now(...).isoformat()`` -- re-introducing exactly the drift the
    shared formatter exists to prevent, including the fraction-dropping hazard
    at ``microsecond == 0``. The store tests would not catch it either: they
    assert a write succeeds, not what it stamped. ``coordination.py``'s call
    site already has this test; this is the same argument applied in-module.

    Stubs ``_execute`` so no database is needed -- this module is deliberately
    ungated, unlike the ``real_postgres`` store suite.
    """
    from branding_team import store as store_module

    monkeypatch.setattr(store_module, "now_iso", lambda: "STAMP-FROM-SHARED-FORMATTER")

    executed: list = []
    store = store_module.BrandingStore()
    monkeypatch.setattr(
        type(store), "_execute", lambda _self, sql, params=(): executed.append((sql, params)) or 1
    )

    client = store.create_client("acme")

    assert client.created_at == "STAMP-FROM-SHARED-FORMATTER"
    assert client.updated_at == "STAMP-FROM-SHARED-FORMATTER"
    (_sql, params) = executed[0]
    # The stamp must reach the row, not just the returned model.
    assert params[1].obj["created_at"] == "STAMP-FROM-SHARED-FORMATTER"
    assert params[1].obj["updated_at"] == "STAMP-FROM-SHARED-FORMATTER"


def _stub_transaction(monkeypatch, store_module, cursor):
    """Point ``BrandingStore._transaction`` at *cursor*, so no database is needed.

    The three brand write paths run inside a transaction rather than through
    ``_execute``, which is why they need this instead of the simpler stub
    ``create_client``'s test uses.
    """
    import contextlib

    @contextlib.contextmanager
    def _fake_transaction(_self):
        yield cursor

    monkeypatch.setattr(store_module.BrandingStore, "_transaction", _fake_transaction)


class _FakeCursor:
    """Records executed SQL and serves canned rows."""

    def __init__(self, rows=None) -> None:
        self.executed: list = []
        self._rows = list(rows or [])

    def execute(self, sql, params=()) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


def test_create_brand_stamps_through_the_shared_formatter(monkeypatch) -> None:
    """``create_brand`` must stamp through ``now_iso`` like every other path.

    Same argument as ``create_client``'s test, applied to the second of the four
    write sites: reverting this one to a hand-rolled ``isoformat()`` would drift
    the column's format with nothing failing.
    """
    from branding_team import store as store_module
    from branding_team.models import BrandingMission

    monkeypatch.setattr(store_module, "now_iso", lambda: "STAMP-FROM-SHARED-FORMATTER")

    # The first fetchone answers the "does this client exist" probe.
    cursor = _FakeCursor(rows=[{"exists": 1}])
    _stub_transaction(monkeypatch, store_module, cursor)

    store = store_module.BrandingStore()
    brand = store.create_brand(
        "client_x",
        BrandingMission(
            company_name="Acme",
            company_description="A company that makes things.",
            target_audience="everyone",
        ),
    )

    assert brand is not None
    assert brand.created_at == "STAMP-FROM-SHARED-FORMATTER"
    assert brand.updated_at == "STAMP-FROM-SHARED-FORMATTER"


def test_update_brand_stamps_through_the_shared_formatter(monkeypatch) -> None:
    """``update_brand``'s ``updated_at`` restamp must come from ``now_iso``.

    Asserts the patch handed to ``_apply_brand_patch`` -- the row is what
    matters, and this path returns whatever that helper returns rather than a
    model it built itself.
    """
    from branding_team import store as store_module

    monkeypatch.setattr(store_module, "now_iso", lambda: "STAMP-FROM-SHARED-FORMATTER")

    patches: list = []
    monkeypatch.setattr(
        store_module,
        "_apply_brand_patch",
        lambda _cur, _bid, _cid, patch: patches.append(patch) or None,
    )
    _stub_transaction(monkeypatch, store_module, _FakeCursor())

    store = store_module.BrandingStore()
    store.update_brand("client_x", "brand_x", name="Renamed")

    assert patches[0]["updated_at"] == "STAMP-FROM-SHARED-FORMATTER"


def test_append_brand_version_stamps_through_the_shared_formatter(monkeypatch) -> None:
    """``append_brand_version`` stamps TWO fields from one ``now_iso`` call.

    The row's ``updated_at`` and the appended history entry's ``created_at``
    both come from it, so both are asserted -- a revert could get one right and
    leave the other drifting.
    """
    from branding_team import store as store_module
    from branding_team.models import BrandPhase, TeamOutput, WorkflowStatus

    monkeypatch.setattr(store_module, "now_iso", lambda: "STAMP-FROM-SHARED-FORMATTER")

    patches: list = []
    monkeypatch.setattr(
        store_module,
        "_apply_brand_patch",
        lambda _cur, _bid, _cid, patch: patches.append(patch) or None,
    )
    _stub_transaction(
        monkeypatch, store_module, _FakeCursor(rows=[{"version": "2", "history": []}])
    )

    store = store_module.BrandingStore()
    store.append_brand_version(
        "client_x",
        "brand_x",
        TeamOutput(
            status=WorkflowStatus.READY_FOR_ROLLOUT,
            mission_summary="summary",
            current_phase=BrandPhase.STRATEGIC_CORE,
        ),
    )

    patch = patches[0]
    assert patch["updated_at"] == "STAMP-FROM-SHARED-FORMATTER"
    assert patch["history"][-1]["created_at"] == "STAMP-FROM-SHARED-FORMATTER"
