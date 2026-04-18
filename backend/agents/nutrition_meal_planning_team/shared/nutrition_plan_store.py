"""Postgres-backed storage for nutrition plans.

SPEC-004 §4.5 cache key. Until SPEC-004, the plan cache invalidated on
a hash of the whole ``ClientProfile`` (excluding ``client_id`` and
``updated_at``). That is overly aggressive: editing a cuisine
preference or a household description wipes the cache even though
those fields do not affect calculator output.

Post-SPEC-004 the cache key is:

    (client_id, calculator_version, profile_cache_vector)

where ``profile_cache_vector`` is a hash of **only** the fields that
drive ``nutrition_calc.compute_daily_targets``. Editing preferences
no longer invalidates; bumping the calculator version alone does.

Backwards compatibility: the ``profile_hash`` column was the legacy
key. The new columns (``calculator_version`` / ``profile_cache_vector``)
are additive via migration 003. A row written by the legacy code path
will have NULLs in the new columns and is treated as a cache miss by
the new reader, forcing regeneration on first read post-migration.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from shared_postgres import Json, dict_row, get_conn
from shared_postgres.metrics import timed_query

from ..models import ClientProfile, NutritionPlan

logger = logging.getLogger(__name__)

_STORE = "nutrition_meal_planning"


def _profile_hash(profile: ClientProfile) -> str:
    """Legacy whole-profile hash (pre-SPEC-004).

    Kept for backward compatibility with rows written before the
    migration. Not used on the new read path.
    """
    data = profile.model_dump(exclude={"client_id", "updated_at"})
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def profile_cache_vector(profile: ClientProfile) -> str:
    """Hash of the fields that affect ``compute_daily_targets`` only.

    Per SPEC-004 §4.5, we include:

    - Every ``BiometricInfo`` field except ``preferred_units`` (cosmetic)
      and ``measured_at`` (metadata).
    - ``dietary_needs`` — drives macros (keto raises fat share, etc.).
    - ``GoalsInfo`` numeric / enum fields (goal_type, target_weight_kg,
      rate_kg_per_week). ``notes`` is excluded (narrative only).
    - ``ClinicalInfo`` — conditions, medications, reproductive_state,
      ed_history_flag, clinician_overrides.

    We exclude:

    - ``household``, ``preferences``, ``lifestyle.lunch_context`` /
      ``equipment_constraints`` — these drive meal planning (SPEC-010),
      not targets.
    - ``allergies_and_intolerances`` — enforced at recipe level by
      SPEC-007's guardrail, not relevant to numeric targets.
    - ``updated_at``, ``client_id``, ``schema_version``,
      ``profile_version`` — bookkeeping.

    Deterministic: the returned string is byte-identical for
    byte-identical input.
    """
    bio = profile.biometrics.model_dump(exclude={"preferred_units", "measured_at"})
    goals = profile.goals.model_dump(exclude={"notes"})
    clin = profile.clinical.model_dump() if profile.clinical is not None else {}
    payload = {
        "biometrics": bio,
        "dietary_needs": sorted(profile.dietary_needs or []),
        "goals": goals,
        "clinical": clin,
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class NutritionPlanStore:
    """Postgres-backed store for nutrition plans with calculator-version-aware cache."""

    def __init__(self) -> None:
        # Stateless; the connection pool lives inside shared_postgres.
        pass

    @timed_query(store=_STORE, op="get_cached_plan")
    def get_cached_plan(
        self,
        client_id: str,
        profile: ClientProfile,
        calculator_version: Optional[str] = None,
    ) -> Optional[NutritionPlan]:
        """Return cached plan if the key matches, else None.

        When ``calculator_version`` is provided (SPEC-004 flow), the
        row must match on all three of ``(client_id,
        calculator_version, profile_cache_vector)``. Legacy rows
        with NULL columns are treated as misses.

        When ``calculator_version`` is None (legacy flow, still
        supported during rollout), falls back to the whole-profile
        hash comparison.
        """
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT profile_hash, plan, calculator_version, profile_cache_vector "
                "FROM nutrition_plans WHERE client_id = %s",
                (client_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None

        if calculator_version is not None:
            # New cache key path.
            if row.get("calculator_version") != calculator_version:
                return None
            if row.get("profile_cache_vector") != profile_cache_vector(profile):
                return None
        else:
            # Legacy path: whole-profile hash.
            if row["profile_hash"] != _profile_hash(profile):
                return None

        try:
            return NutritionPlan.model_validate(row["plan"])
        except Exception:
            logger.warning("Failed to parse cached nutrition plan for %s", client_id, exc_info=True)
            return None

    @timed_query(store=_STORE, op="save_plan")
    def save_plan(
        self,
        client_id: str,
        profile: ClientProfile,
        plan: NutritionPlan,
        calculator_version: Optional[str] = None,
    ) -> None:
        """Save (upsert) a nutrition plan along with both legacy and new keys.

        We always write the legacy ``profile_hash`` too so rollback of
        the SPEC-004 flag produces correct cache behavior on the
        legacy read path. The incremental storage cost is 16 bytes per
        cached plan.
        """
        ts = datetime.now(tz=timezone.utc)
        legacy_hash = _profile_hash(profile)
        vector = profile_cache_vector(profile) if calculator_version is not None else None
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO nutrition_plans "
                "(client_id, profile_hash, plan, generated_at, "
                " calculator_version, profile_cache_vector) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (client_id) DO UPDATE "
                "SET profile_hash = EXCLUDED.profile_hash, "
                "    plan = EXCLUDED.plan, "
                "    generated_at = EXCLUDED.generated_at, "
                "    calculator_version = EXCLUDED.calculator_version, "
                "    profile_cache_vector = EXCLUDED.profile_cache_vector",
                (
                    client_id,
                    legacy_hash,
                    Json(plan.model_dump()),
                    ts,
                    calculator_version,
                    vector,
                ),
            )

    @timed_query(store=_STORE, op="invalidate_plan")
    def invalidate_plan(self, client_id: str) -> None:
        """Force-miss the cache on next read (used by /regenerate endpoint)."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM nutrition_plans WHERE client_id = %s", (client_id,))


# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------

_default_store: Optional[NutritionPlanStore] = None


def get_nutrition_plan_store() -> NutritionPlanStore:
    """Return the process-wide store, instantiating on first call."""
    global _default_store
    if _default_store is None:
        _default_store = NutritionPlanStore()
    return _default_store


# Silence unused-import for Any; kept for future type hints.
_ = Any
