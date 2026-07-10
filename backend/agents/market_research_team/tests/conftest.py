import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Sample JSON responses that mock LLM output for each agent
# ---------------------------------------------------------------------------

SAMPLE_INSIGHT_JSON = json.dumps(
    {
        "user_jobs": ["Speed up onboarding for new hires"],
        "pain_points": ["Manual handoffs across teams cause delays"],
        "desired_outcomes": ["Reduce onboarding time by 50%"],
        "direct_quotes": ["I spend half my day just chasing people for status updates."],
    }
)

SAMPLE_SIGNALS_JSON = json.dumps(
    [
        {
            "signal": "User pain urgency",
            "confidence": 0.72,
            "evidence": ["Users report spending 50% of time on manual handoffs"],
        },
        {
            "signal": "Adoption motivation clarity",
            "confidence": 0.65,
            "evidence": ["Clear desired outcome: reduce onboarding time by 50%"],
        },
    ]
)

SAMPLE_VIABILITY_JSON = json.dumps(
    {
        "verdict": "promising_with_risks",
        "confidence": 0.68,
        "rationale": [
            "Strong pain signal from multiple interviewees.",
            "Clear willingness to adopt new tools.",
        ],
        "suggested_next_experiments": [
            "Run a concierge MVP with 3 target users.",
            "Test value prop with a landing page experiment.",
        ],
    }
)

SAMPLE_VIABILITY_LOW_JSON = json.dumps(
    {
        "verdict": "needs_more_validation",
        "confidence": 0.45,
        "rationale": ["Limited evidence from interviews.", "Need more data points."],
        "suggested_next_experiments": ["Conduct 5 more problem interviews."],
    }
)

SAMPLE_SCRIPTS_JSON = json.dumps(
    [
        "Interview script:\n1) Tell me about your onboarding process.\n2) What frustrates you most?\n3) What have you tried?\n4) What would ideal look like?",
        "Transcript tagging guide:\n- job_to_be_done\n- pain_point\n- desired_outcome\n- workaround\n- trigger_event",
        "Decision checkpoint:\n- What evidence improved confidence?\n- What assumptions remain?\n- What experiment next?",
    ]
)

SAMPLE_CONSISTENCY_JSON = json.dumps(
    {
        "signal": "Cross-interview theme consistency",
        "confidence": 0.7,
        "evidence": [
            "Manual handoffs mentioned in 3/4 interviews",
            "Onboarding delay is recurring theme",
        ],
    }
)


@pytest.fixture(autouse=True)
def _patched_market_research_job_client(monkeypatch, fake_job_client):
    """Route the team's job_store ``_client`` factory through the in-memory fake.

    Lets ``test_api.py`` exercise the FastAPI app end-to-end without requiring
    the real job service or Postgres. Also clears the module-level singleton
    cache so a real client cached at import time can't leak in.
    """
    from market_research_team.shared import job_store as js

    monkeypatch.setattr(js, "_client_instance", None, raising=False)
    monkeypatch.setattr(js, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client


@pytest.fixture(autouse=True)
def _mock_strands(monkeypatch):
    """Patch Strands agent construction + calls for all tests.

    The orchestrator and Temporal activities drive the specialist ``agents.py``
    dataclass agents directly (the Strands graph was retired), so mocking the
    two agent-level seams — ``_build_strands_agent`` (construction) and
    ``_call_agent`` (the LLM round-trip) — fully decouples the suite from any
    real LLM provider. ``_fake_call_agent`` routes to canned JSON by prompt
    keyword so each stage gets schema-valid output.
    """
    monkeypatch.setattr(
        "market_research_team.agents._build_strands_agent",
        lambda *args, **kwargs: MagicMock(),
    )

    def _fake_call_agent(agent, prompt):
        """Return mock JSON based on prompt keywords."""
        prompt_lower = prompt.lower()
        # Consistency must be matched before the generic transcript/psychology
        # checks — its prompt mentions "insights"/"analyze" but is the
        # cross-interview stage.
        if "consistency" in prompt_lower or "cross-interview" in prompt_lower:
            return SAMPLE_CONSISTENCY_JSON
        if "transcript" in prompt_lower and "analyze" in prompt_lower:
            return SAMPLE_INSIGHT_JSON
        if (
            "psychology" in prompt_lower
            or "adoption" in prompt_lower
            or "market signals" in prompt_lower
        ):
            return SAMPLE_SIGNALS_JSON
        if "viability" in prompt_lower or "verdict" in prompt_lower:
            return SAMPLE_VIABILITY_JSON
        if "research artifacts" in prompt_lower or "interview script" in prompt_lower:
            return SAMPLE_SCRIPTS_JSON
        # Fallback
        return SAMPLE_INSIGHT_JSON

    monkeypatch.setattr("market_research_team.agents._call_agent", _fake_call_agent)
