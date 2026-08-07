"""Session store + pure request/mission helpers for the branding team API.

Single owner of the Postgres-backed interactive-review session store and the
small pure helpers (mission parsing, placeholder detection, question building,
answer application) the route modules reuse. Imports nothing from
``api.routes``/``api.background``/``api.conversation``/``api.main``, so it never
participates in an import cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Json

from branding_team.api.models import (
    BrandingQuestion,
    BrandingSession,
    BrandingSessionResponse,
)
from branding_team.models import (
    MISSION_PLACEHOLDERS,
    BrandingMission,
    BrandingMissionFields,
    BrandPhase,
    TeamOutput,
)
from shared.postgres import PostgresHelperMixin
from shared.postgres.metrics import timed_query

# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


class BrandingSessionStore(PostgresHelperMixin):
    """Postgres-backed session store — shared across worker processes."""

    @timed_query(store="branding_sessions", op="create")
    def create(
        self, mission: BrandingMission, latest_output: TeamOutput
    ) -> tuple[str, BrandingSession]:
        """Create a new branding session and persist it to Postgres.

        Preconditions:
            ``mission`` and ``latest_output`` are valid model instances.
        Postconditions:
            Inserts a row into ``branding_sessions`` with a generated UUID,
            the serialized session JSON (including open questions derived from
            ``mission``), and the current UTC timestamp. Returns
            ``(session_id, session)`` where ``session_id`` is the persisted
            UUID and ``session`` is the in-memory session object.
        """
        questions = _build_open_questions(mission)
        session_id = str(uuid4())
        session = BrandingSession(mission=mission, questions=questions, latest_output=latest_output)
        now = datetime.now(tz=timezone.utc)
        self._execute(
            "INSERT INTO branding_sessions (session_id, session_json, updated_at) "
            "VALUES (%s, %s, %s)",
            (session_id, Json(session.model_dump(mode="json")), now),
        )
        return session_id, session

    @timed_query(store="branding_sessions", op="get")
    def get(self, session_id: str) -> Optional[BrandingSession]:
        """Load a branding session from Postgres by its session id.

        Preconditions:
            ``session_id`` is a non-empty string.
        Postconditions:
            Returns the deserialized ``BrandingSession`` if a matching row
            exists; returns ``None`` if no row matches. Raises a Pydantic
            validation error if the stored JSON cannot be parsed as a session.
        """
        row = self._fetch_one(
            "SELECT session_json FROM branding_sessions WHERE session_id = %s",
            (session_id,),
        )
        if row is None:
            return None
        return BrandingSession.model_validate(row["session_json"])

    @timed_query(store="branding_sessions", op="save")
    def save(self, session_id: str, session: BrandingSession) -> None:
        """Persist mutations to an existing session.

        Preconditions:
            ``session_id`` is a non-empty string; ``session`` is a valid
            ``BrandingSession``.
        Postconditions:
            Updates ``session_json`` and ``updated_at`` for the matching row.
            No-op (zero rows affected) when ``session_id`` is unknown.
        """
        now = datetime.now(tz=timezone.utc)
        self._execute(
            "UPDATE branding_sessions SET session_json = %s, updated_at = %s WHERE session_id = %s",
            (Json(session.model_dump(mode="json")), now, session_id),
        )


session_store = BrandingSessionStore()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_target_phase(raw: Optional[str]) -> Optional[BrandPhase]:
    """Parse a target_phase string into a BrandPhase enum, or None.

    Preconditions:
        ``raw`` is a string or None.
    Postconditions:
        Returns ``None`` when ``raw`` is None or empty/falsy (optional phase).
        Returns the matching ``BrandPhase`` when ``raw`` is a valid enum value.
        Raises ``HTTPException(400)`` with detail
        ``Invalid target_phase: {raw}`` when ``raw`` is non-empty but not a
        valid ``BrandPhase`` value.
    """
    if not raw:
        return None
    try:
        return BrandPhase(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid target_phase: {raw}")


def _is_real_value(value: Optional[str]) -> bool:
    """True when *value* is a real (non-placeholder) string.

    Preconditions:
        ``value`` is a string or None.
    Postconditions:
        Returns True iff the stripped value is non-empty and not one of the
        known placeholder sentinels (``MISSION_PLACEHOLDERS``).
    """
    stripped = (value or "").strip()
    return bool(stripped) and stripped not in MISSION_PLACEHOLDERS


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


def _mission_from_payload(payload: BrandingMissionFields) -> BrandingMission:
    """Build a ``BrandingMission`` from a create/run request payload.

    Preconditions:
        ``payload`` is a ``BrandingMissionFields`` instance (satisfied by
        ``CreateBrandRequest`` and ``RunBrandingTeamRequest``).
    Postconditions:
        Returns a ``BrandingMission`` built from ``payload.mission_fields()``;
        visual-identity fields use ``BrandingMission`` defaults; performs no
        I/O and does not mutate ``payload``.
    """
    return BrandingMission(**payload.mission_fields())


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
