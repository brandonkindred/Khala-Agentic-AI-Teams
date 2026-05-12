"""Unit tests for SPEC-007 GuardrailAuditStore (Postgres-backed)."""

import pytest

from nutrition_meal_planning_team.shared.guardrail_audit_store import (
    GuardrailAuditStore,
    get_guardrail_audit_store,
    record_rejection,
)
from shared_postgres import dict_row, get_conn, is_postgres_enabled

pytestmark = pytest.mark.skipif(
    not is_postgres_enabled(),
    reason="GuardrailAuditStore requires Postgres (set POSTGRES_HOST).",
)


class TestGuardrailAuditStore:
    @pytest.fixture
    def store(self):
        return GuardrailAuditStore()

    def test_record_rejection_inserts_row(self, store):
        rejection_id = store.record_rejection(
            "c-reject-1",
            {"name": "Cashew curry"},
            "ingredient violates allergen restriction",
            guardrail_version="1.0.0",
            ingredient_raw="cashews",
            canonical_id="tree_nut",
            tag="allergen",
            detail="tree_nut",
            kb_version="1.0.0",
        )
        assert rejection_id > 0
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT client_id, meal_snapshot, violation_reason, "
                "       ingredient_raw, canonical_id, tag, detail, "
                "       guardrail_version, kb_version, created_at "
                "FROM nutrition_guardrail_rejections WHERE id = %s",
                (rejection_id,),
            )
            row = cur.fetchone()
        assert row is not None
        assert row["client_id"] == "c-reject-1"
        assert row["meal_snapshot"] == {"name": "Cashew curry"}
        assert row["violation_reason"] == "ingredient violates allergen restriction"
        assert row["ingredient_raw"] == "cashews"
        assert row["canonical_id"] == "tree_nut"
        assert row["tag"] == "allergen"
        assert row["detail"] == "tree_nut"
        assert row["guardrail_version"] == "1.0.0"
        assert row["kb_version"] == "1.0.0"
        assert row["created_at"] is not None

    def test_record_rejection_multiple_violations(self, store):
        # SPEC-007: one row per violation, not per recommendation.
        meal = {"name": "Trail mix"}
        first = store.record_rejection(
            "c-reject-multi",
            meal,
            "ingredient violates allergen restriction",
            guardrail_version="1.0.0",
            canonical_id="tree_nut",
        )
        second = store.record_rejection(
            "c-reject-multi",
            meal,
            "ingredient violates allergen restriction",
            guardrail_version="1.0.0",
            canonical_id="peanut",
        )
        assert first != second
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM nutrition_guardrail_rejections WHERE client_id = %s",
                ("c-reject-multi",),
            )
            count = cur.fetchone()[0]
        assert count == 2

    def test_record_rejection_with_minimal_args(self, store):
        # All ingredient-detail kwargs optional; only guardrail_version is required.
        rejection_id = store.record_rejection(
            "c-reject-minimal",
            {"name": "Unknown dish"},
            "policy violation",
            guardrail_version="1.0.0",
        )
        assert rejection_id > 0
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT ingredient_raw, canonical_id, tag, detail, kb_version "
                "FROM nutrition_guardrail_rejections WHERE id = %s",
                (rejection_id,),
            )
            row = cur.fetchone()
        assert row["ingredient_raw"] is None
        assert row["canonical_id"] is None
        assert row["tag"] is None
        assert row["detail"] is None
        assert row["kb_version"] is None

    def test_module_level_function(self):
        rejection_id = record_rejection(
            "c-reject-modlevel",
            {"name": "Soup"},
            "test",
            guardrail_version="1.0.0",
        )
        assert rejection_id > 0

    def test_singleton(self):
        a = get_guardrail_audit_store()
        b = get_guardrail_audit_store()
        assert a is b
