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
