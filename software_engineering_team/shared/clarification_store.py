"""In-memory clarification session store and basic question-generation logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Dict, List
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ClarificationTurn:
    role: str
    message: str
    timestamp: str = field(default_factory=_now_iso)


@dataclass
class ClarificationSession:
    session_id: str
    spec_text: str
    created_at: str = field(default_factory=_now_iso)
    status: str = "active"
    open_questions: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    confidence_score: float = 0.2
    clarification_round: int = 0
    max_rounds: int = 8
    refined_spec: str | None = None
    turns: List[ClarificationTurn] = field(default_factory=list)


class ClarificationStore:
    """Thread-safe in-memory store for clarification sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[str, ClarificationSession] = {}
        self._lock = Lock()

    @staticmethod
    def _extract_open_questions(spec_text: str) -> List[str]:
        lower = spec_text.lower()
        questions: List[str] = []
        if "acceptance criteria" not in lower:
            questions.append("What are the explicit acceptance criteria for this feature?")
        if "non-functional" not in lower and "performance" not in lower:
            questions.append("Are there non-functional requirements (performance, reliability, security)?")
        if "out of scope" not in lower:
            questions.append("What is explicitly out of scope for the first release?")
        if not questions:
            questions.append("What is the highest-risk assumption we should validate first?")
        return questions

    @staticmethod
    def _extract_assumptions(spec_text: str) -> List[str]:
        assumptions = ["Target users and primary user journey are stable."]
        if "api" not in spec_text.lower():
            assumptions.append("An API contract will be needed and can be designed during planning.")
        return assumptions

    def create_session(self, spec_text: str) -> ClarificationSession:
        session_id = str(uuid4())
        open_questions = self._extract_open_questions(spec_text)
        assumptions = self._extract_assumptions(spec_text)
        session = ClarificationSession(
            session_id=session_id,
            spec_text=spec_text,
            open_questions=open_questions,
            assumptions=assumptions,
        )
        summary = "I reviewed your spec. I identified open questions and assumptions to refine it."
        first_question = open_questions[0] if open_questions else "What should we clarify first?"
        session.turns.append(ClarificationTurn(role="assistant", message=f"{summary}\n\nFirst question: {first_question}"))
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> ClarificationSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def add_user_message(self, session_id: str, message: str) -> ClarificationSession | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session:
                return None
            session.turns.append(ClarificationTurn(role="user", message=message))
            session.clarification_round += 1
            # Mark a question as answered per round.
            if session.open_questions:
                session.open_questions.pop(0)
            # Increase confidence each useful turn.
            session.confidence_score = min(1.0, session.confidence_score + 0.2)
            done = session.confidence_score >= 0.85 or session.clarification_round >= session.max_rounds or not session.open_questions
            if done:
                session.status = "completed"
                session.refined_spec = (
                    session.spec_text
                    + "\n\n## Clarification Summary\n"
                    + "\n".join(f"- {t.message}" for t in session.turns if t.role == "user")
                )
                session.turns.append(
                    ClarificationTurn(
                        role="assistant",
                        message="Thanks. Clarification is complete. I generated a refined spec.",
                    )
                )
            else:
                next_q = session.open_questions[0]
                session.turns.append(
                    ClarificationTurn(
                        role="assistant",
                        message=f"Got it. Next clarification question: {next_q}",
                    )
                )
            return session


clarification_store = ClarificationStore()

