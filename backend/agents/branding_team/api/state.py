"""Session store + pure request/mission helpers for the branding team API.

Single owner of the Postgres-backed interactive-review session store and the
small pure helpers (mission parsing, placeholder detection, question building,
answer application) the route modules reuse. Imports nothing from
``api.routes``/``api.background``/``api.conversation``/``api.main``, so it never
participates in an import cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional
from uuid import uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

from branding_team.api.models import (
    BrandingQuestion,
    BrandingSession,
    BrandingSessionResponse,
)
from branding_team.models import BrandingMission, BrandPhase, TeamOutput
from shared_postgres import get_conn
from shared_postgres.metrics import timed_query

# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


class BrandingSessionStore:
    """Postgres-backed session store — shared across worker processes."""

    @timed_query(store="branding_sessions", op="create")
    def create(
        self, mission: BrandingMission, latest_output: TeamOutput
    ) -> tuple[str, BrandingSession]:
        questions = _build_open_questions(mission)
        session_id = str(uuid4())
        session = BrandingSession(mission=mission, questions=questions, latest_output=latest_output)
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
                "VALUES (%s, %s, %s)",
                (session_id, Json(session.model_dump(mode="json")), now),
            )
        return session_id, session

    @timed_query(store="branding_sessions", op="get")
    def get(self, session_id: str) -> Optional[BrandingSession]:
        with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT session_json FROM branding_sessions WHERE session_id = %s",
                (session_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return BrandingSession.model_validate(row["session_json"])

    @timed_query(store="branding_sessions", op="save")
    def save(self, session_id: str, session: BrandingSession) -> None:
        """Persist mutations to an existing session."""
        now = datetime.now(tz=timezone.utc)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE branding_sessions SET session_json = %s, updated_at = %s "
                "WHERE session_id = %s",
                (Json(session.model_dump(mode="json")), now, session_id),
            )


session_store = BrandingSessionStore()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_target_phase(raw: Optional[str]) -> Optional[BrandPhase]:
    """Parse a target_phase string into a BrandPhase enum, or None."""
    if not raw:
        return None
    try:
        return BrandPhase(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid target_phase: {raw}")


# Sentinel strings the assistant/UI use for a field that has no real value yet.
_MISSION_PLACEHOLDERS = ("TBD", "To be discussed.", "—", "")


def _is_real_value(value: Optional[str]) -> bool:
    """True when *value* is a real (non-placeholder) string.

    Preconditions:
        ``value`` is a string or None.
    Postconditions:
        Returns True iff the stripped value is non-empty and not one of the
        known placeholder sentinels (``_MISSION_PLACEHOLDERS``).
    """
    return (value or "").strip() not in _MISSION_PLACEHOLDERS


def _mission_has_brand_name(mission: BrandingMission) -> bool:
    """True if company_name is a real value (not a placeholder)."""
    return _is_real_value(mission.company_name)


def _mission_has_minimal_required_fields(mission: BrandingMission) -> bool:
    """True if we have real company name, description, and target audience (not placeholders)."""
    return (
        _is_real_value(mission.company_name)
        and _is_real_value(mission.company_description)
        and _is_real_value(mission.target_audience)
    )


def _mission_from_payload(payload: Any) -> BrandingMission:
    """Build a ``BrandingMission`` from a create/run request payload.

    Preconditions:
        ``payload`` exposes the eight mission fields (``company_name``,
        ``company_description``, ``target_audience``, ``values``,
        ``differentiators``, ``desired_voice``, ``existing_brand_material``,
        ``wiki_path``) — satisfied by ``CreateBrandRequest`` and
        ``RunBrandingTeamRequest``.
    Postconditions:
        Returns a ``BrandingMission`` populated from those fields; performs no
        I/O and does not mutate ``payload``.
    """
    return BrandingMission(
        company_name=payload.company_name,
        company_description=payload.company_description,
        target_audience=payload.target_audience,
        values=payload.values,
        differentiators=payload.differentiators,
        desired_voice=payload.desired_voice,
        existing_brand_material=payload.existing_brand_material,
        wiki_path=payload.wiki_path,
    )


def _build_open_questions(mission: BrandingMission) -> List[BrandingQuestion]:
    questions: List[BrandingQuestion] = []
    if not mission.values:
        questions.append(
            BrandingQuestion(
                id="core-values",
                question="What are the 3-5 core brand values we should optimize for?",
                context="These values are the foundation of Phase 1 (Strategic Core). They define behavioral expectations and drive all downstream brand decisions.",
                target_field="values",
            )
        )
    if not mission.differentiators:
        questions.append(
            BrandingQuestion(
                id="differentiators",
                question="What differentiators should the team emphasize against competitors?",
                context="Differentiation pillars are critical to Phase 1 (Strategic Core). They shape positioning, narrative, and competitive strategy.",
                target_field="differentiators",
            )
        )
    questions.append(
        BrandingQuestion(
            id="voice-approval",
            question="Do you approve the proposed brand voice, or what adjustment should be made?",
            context="Voice decisions bridge Phase 1 (Strategic Core) and Phase 2 (Narrative & Messaging). They must be locked before messaging work begins.",
            target_field="desired_voice",
        )
    )
    return questions


def _session_response(session_id: str, session: BrandingSession) -> BrandingSessionResponse:
    open_questions = [q for q in session.questions if q.status == "open"]
    answered_questions = [q for q in session.questions if q.status == "answered"]
    status = "awaiting_user_answers" if open_questions else "ready_for_rollout"
    current_phase = (
        session.latest_output.current_phase.value if session.latest_output else "strategic_core"
    )
    return BrandingSessionResponse(
        session_id=session_id,
        status=status,
        current_phase=current_phase,
        mission=session.mission,
        latest_output=session.latest_output,
        open_questions=open_questions,
        answered_questions=answered_questions,
    )


def _apply_answer(
    mission: BrandingMission, question: BrandingQuestion, answer: str
) -> BrandingMission:
    normalized = answer.strip()
    if question.target_field in {"values", "differentiators"}:
        entries = [item.strip() for item in normalized.split(",") if item.strip()]
        if question.target_field == "values":
            return mission.model_copy(update={"values": entries})
        return mission.model_copy(update={"differentiators": entries})
    if question.target_field == "desired_voice":
        return mission.model_copy(update={"desired_voice": normalized})
    return mission
