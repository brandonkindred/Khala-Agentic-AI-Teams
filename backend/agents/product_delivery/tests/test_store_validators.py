"""Unit tests for the store-level validators in :mod:`product_delivery.store`.

These cover the "pre-insert validation" contract — the real store
validates status / scores / estimates **before** the SQL INSERT/UPDATE
runs, so a non-route caller can't bypass the API-level Pydantic guards
and persist invalid rows. Postgres-backed integration tests live in
``test_store.py`` and are skipped when ``POSTGRES_HOST`` is unset; the
checks here run unconditionally because they don't need a live DB.
"""

from __future__ import annotations

import math

import pytest

from product_delivery.store import (
    ProductDeliveryStorageUnavailable,
    ProductDeliveryStore,
    _validate_estimate_points,
    _validate_optional_finite_score,
    _validate_status,
    _validate_title,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_status",
    [
        "",
        "x" * 41,
        "y" * 200,
    ],
)
def test_validate_status_rejects_out_of_bounds(bad_status: str) -> None:
    with pytest.raises(ValueError, match="status must"):
        _validate_status(bad_status)


def test_validate_status_accepts_typical_values() -> None:
    for ok in ("proposed", "in-progress", "x", "x" * 40):
        assert _validate_status(ok) == ok


@pytest.mark.parametrize(
    "bad_value",
    [True, False, math.nan, math.inf, -math.inf],
)
def test_validate_optional_finite_score_rejects_invalid(bad_value: object) -> None:
    with pytest.raises(ValueError):
        _validate_optional_finite_score(bad_value, label="wsjf_score")  # type: ignore[arg-type]


def test_validate_optional_finite_score_accepts_none_and_finite() -> None:
    assert _validate_optional_finite_score(None, label="wsjf_score") is None
    assert _validate_optional_finite_score(0.0, label="wsjf_score") == 0.0
    assert _validate_optional_finite_score(-3.5, label="rice_score") == -3.5


@pytest.mark.parametrize(
    "bad_value",
    [True, False, 0.0, -1.0, math.nan, math.inf, -math.inf],
)
def test_validate_estimate_points_rejects_invalid(bad_value: object) -> None:
    with pytest.raises(ValueError):
        _validate_estimate_points(bad_value)  # type: ignore[arg-type]


def test_validate_estimate_points_accepts_none_and_positive() -> None:
    assert _validate_estimate_points(None) is None
    assert _validate_estimate_points(1.0) == 1.0
    assert _validate_estimate_points(0.5) == 0.5


# ---------------------------------------------------------------------------
# create_*: status must validate BEFORE the INSERT runs.
#
# Without the fix, an out-of-bounds status would reach the SQL INSERT
# and (if Postgres were available) persist before the post-insert
# Pydantic validation raised. We assert that the validator fires first
# by calling the create methods on a real ``ProductDeliveryStore``
# without Postgres configured: a ``ValueError`` from the validator must
# beat the ``ProductDeliveryStorageUnavailable`` from ``_conn``.
# ---------------------------------------------------------------------------


def _store(monkeypatch: pytest.MonkeyPatch) -> ProductDeliveryStore:
    # Belt-and-suspenders: even if the test runner has POSTGRES_HOST
    # set, force the storage-unavailable path so the test isolates the
    # validator's "raises before SQL" guarantee.
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    return ProductDeliveryStore()


def test_create_initiative_validates_status_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    with pytest.raises(ValueError, match="status must"):
        store.create_initiative(
            product_id="p1",
            title="I",
            summary="",
            status="x" * 200,
            author="tester",
        )


def test_create_epic_validates_status_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    with pytest.raises(ValueError, match="status must"):
        store.create_epic(
            initiative_id="i1",
            title="E",
            summary="",
            status="x" * 200,
            author="tester",
        )


def test_create_task_validates_status_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    with pytest.raises(ValueError, match="status must"):
        store.create_task(
            story_id="s1",
            title="T",
            description="",
            status="x" * 200,
            owner=None,
            author="tester",
        )


def test_create_initiative_storage_unavailable_for_valid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: with POSTGRES_HOST unset, a *valid* status must
    surface ``ProductDeliveryStorageUnavailable`` (i.e. the validator
    didn't reject the input — it passed the guard and only the missing
    DB stops the call)."""
    store = _store(monkeypatch)
    with pytest.raises(ProductDeliveryStorageUnavailable):
        store.create_initiative(
            product_id="p1",
            title="I",
            summary="",
            status="proposed",
            author="tester",
        )


# ---------------------------------------------------------------------------
# Whitespace-only status (Codex P3): _validate_status must reject `'   '`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank_status", [" ", "   ", "\t", "\n", " \t \n "])
def test_validate_status_rejects_whitespace_only(blank_status: str) -> None:
    with pytest.raises(ValueError, match="status must"):
        _validate_status(blank_status)


# ---------------------------------------------------------------------------
# Title validator (Codex P3): mirrors API-level min_length=1, max_length=200
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_title",
    [
        "",
        " ",
        "   ",
        "\t",
        "x" * 201,
    ],
)
def test_validate_title_rejects_invalid(bad_title: str) -> None:
    with pytest.raises(ValueError, match="title must"):
        _validate_title(bad_title)


def test_validate_title_accepts_typical_values() -> None:
    for ok in ("A", "A short title", "x" * 200):
        assert _validate_title(ok) == ok


def test_validate_title_uses_label_in_error() -> None:
    # `_validate_title` is reused as `_validate_title(name, label="name")`
    # in `create_product`, so the error must reflect the field name for
    # operators reading the rejection message.
    with pytest.raises(ValueError, match="name must"):
        _validate_title("", label="name")


# ---------------------------------------------------------------------------
# create_*: title (and product name) must validate BEFORE the INSERT runs.
# Mirror of the status guards above.
# ---------------------------------------------------------------------------


def test_create_product_validates_name_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    with pytest.raises(ValueError, match="name must"):
        store.create_product(name="", description="", vision="", author="tester")


def test_create_product_rejects_oversized_name_before_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    with pytest.raises(ValueError, match="name must"):
        store.create_product(name="x" * 201, description="", vision="", author="tester")


@pytest.mark.parametrize("kind", ["initiative", "epic", "story", "task"])
def test_create_methods_validate_title_before_insert(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(monkeypatch)
    common = {"author": "tester"}
    if kind == "initiative":
        call = lambda: store.create_initiative(  # noqa: E731
            product_id="p1", title="", summary="", status="proposed", **common
        )
    elif kind == "epic":
        call = lambda: store.create_epic(  # noqa: E731
            initiative_id="i1", title="", summary="", status="proposed", **common
        )
    elif kind == "story":
        call = lambda: store.create_story(  # noqa: E731
            epic_id="e1",
            title="",
            user_story="",
            status="proposed",
            estimate_points=None,
            **common,
        )
    else:
        call = lambda: store.create_task(  # noqa: E731
            story_id="s1", title="", description="", status="todo", owner=None, **common
        )
    with pytest.raises(ValueError, match="title must"):
        call()
