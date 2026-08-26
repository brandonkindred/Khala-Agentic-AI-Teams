"""Targeted tests for medium-size coverage gaps:

* ``blog_fact_check_agent.agent`` — JSON retry, fallback, run_from_work_dir.
* ``validators/runner`` — run_validators, claims policy, run_from_work_dir.
* ``shared.content_plan`` — content_plan_to_content_brief_markdown deep branches.
* ``shared.medium_integration_access`` — full happy path with stubbed unified_api modules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# blog_fact_check_agent
# ---------------------------------------------------------------------------


def test_fact_check_run_happy(monkeypatch, tmp_path: Path) -> None:
    from agents.blogging.blog_fact_check_agent import BlogFactCheckAgent
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps(
                {
                    "claims_status": "PASS",
                    "risk_status": "PASS",
                    "claims_verified": ["c1"],
                    "risk_flags": [],
                    "required_disclaimers": [],
                    "notes": "ok",
                }
            )

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    a = BlogFactCheckAgent(llm_client=object())
    report = a.run(
        "draft text",
        allowed_claims={"claims": [{"id": "c1", "text": "X is Y", "citations": []}]},
        work_dir=tmp_path,
    )
    assert report.claims_status == "PASS"
    assert (tmp_path / "fact_check_report.json").exists()


def test_fact_check_run_normalizes_invalid_status(monkeypatch, tmp_path: Path) -> None:
    from agents.blogging.blog_fact_check_agent import BlogFactCheckAgent
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"claims_status": "weird", "risk_status": "weird", "notes": None})

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    a = BlogFactCheckAgent(llm_client=object())
    report = a.run("draft", work_dir=tmp_path)
    assert report.claims_status == "FAIL"
    assert report.risk_status == "FAIL"


def test_fact_check_run_json_retry_then_fallback(monkeypatch, tmp_path: Path) -> None:
    from agents.blogging.blog_fact_check_agent import BlogFactCheckAgent
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json"

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    a = BlogFactCheckAgent(llm_client=object())
    report = a.run("draft", work_dir=tmp_path)
    assert report.claims_status == "FAIL"
    assert (tmp_path / "fact_check_report.json").exists()


def test_fact_check_run_llm_exception_raises(monkeypatch) -> None:
    from agents.blogging.blog_fact_check_agent import BlogFactCheckAgent
    from agents.blogging.shared import json_retry as jr_mod
    from agents.blogging.shared.errors import FactCheckError

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    a = BlogFactCheckAgent(llm_client=object())
    with pytest.raises(FactCheckError):
        a.run("draft")


def test_fact_check_run_from_work_dir(monkeypatch, tmp_path: Path) -> None:
    from agents.blogging.blog_fact_check_agent.agent import run_fact_check_from_work_dir
    from agents.blogging.shared import json_retry as jr_mod

    (tmp_path / "final.md").write_text("# Draft\nbody")
    (tmp_path / "allowed_claims.json").write_text(
        '{"claims": [{"id": "c1", "text": "X", "citations": []}]}'
    )

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"claims_status": "PASS", "risk_status": "PASS", "notes": "ok"})

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    out = run_fact_check_from_work_dir(tmp_path, llm_client=object())
    assert out.claims_status == "PASS"


def test_fact_check_run_from_work_dir_fallback_draft(monkeypatch, tmp_path: Path) -> None:
    """When final.md doesn't exist, try draft_v2.md then draft_v1.md."""
    from agents.blogging.blog_fact_check_agent.agent import run_fact_check_from_work_dir
    from agents.blogging.shared import json_retry as jr_mod

    (tmp_path / "draft_v1.md").write_text("# Draft v1\nbody")

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"claims_status": "PASS", "risk_status": "PASS"})

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    out = run_fact_check_from_work_dir(tmp_path, llm_client=object())
    assert out.claims_status == "PASS"


# ---------------------------------------------------------------------------
# validators/runner
# ---------------------------------------------------------------------------


def test_run_validators_happy(tmp_path: Path) -> None:
    from agents.blogging.shared.brand_spec import BrandSpec
    from agents.blogging.validators.runner import run_validators

    spec = BrandSpec()
    out = run_validators(
        "# Draft\n\nA paragraph.\n",
        spec,
        work_dir=tmp_path,
    )
    assert out.status in ("PASS", "FAIL")
    assert (tmp_path / "validator_report.json").exists()


def test_check_claims_policy_disabled() -> None:
    from agents.blogging.validators.runner import check_claims_policy

    assert check_claims_policy("draft", None, False) is None
    assert check_claims_policy("draft", {"claims": []}, False) is None
    # require=True but no allowed_claims → None
    assert check_claims_policy("draft", None, True) is None


def test_check_claims_policy_unknown_id() -> None:
    from agents.blogging.validators.runner import check_claims_policy

    result = check_claims_policy(
        "Body text [CLAIM:unknown] more.",
        {"claims": [{"id": "known", "text": "X"}]},
        True,
    )
    assert result.status == "FAIL"
    assert "unknown" in result.details["unknown_claim_ids"]


def test_check_claims_policy_all_known() -> None:
    from agents.blogging.validators.runner import check_claims_policy

    result = check_claims_policy(
        "Body [CLAIM:c1] more.",
        {"claims": [{"id": "c1", "text": "X"}]},
        True,
    )
    assert result.status == "PASS"


def test_run_validators_from_work_dir(tmp_path: Path) -> None:
    from agents.blogging.validators.runner import run_validators_from_work_dir

    (tmp_path / "final.md").write_text("# Draft\nBody.")
    (tmp_path / "brand_spec_prompt.md").write_text("Brand spec")
    (tmp_path / "allowed_claims.json").write_text('{"claims": [{"id": "c1", "text": "X"}]}')

    out = run_validators_from_work_dir(tmp_path)
    assert out.status in ("PASS", "FAIL")


def test_run_validators_from_work_dir_fallback_draft(tmp_path: Path) -> None:
    """Falls back from final.md to draft_v2/draft_v1."""
    from agents.blogging.validators.runner import run_validators_from_work_dir

    (tmp_path / "draft_v1.md").write_text("# Draft v1\nBody.")
    (tmp_path / "brand_spec_prompt.md").write_text("Brand spec")

    out = run_validators_from_work_dir(tmp_path)
    assert out is not None


def test_run_validators_from_work_dir_missing_brand_spec(tmp_path: Path, monkeypatch) -> None:
    """When neither work_dir nor default brand_spec_prompt.md exist, raise."""
    from agents.blogging.validators.runner import run_validators_from_work_dir

    (tmp_path / "final.md").write_text("# Draft\nBody.")

    # Override the default path to a non-existent one so we hit the FileNotFoundError
    monkeypatch.setattr(
        Path,
        "exists",
        lambda self: "final.md" in str(self) or "validator_report" in str(self),
    )
    with pytest.raises(FileNotFoundError):
        run_validators_from_work_dir(tmp_path)


# ---------------------------------------------------------------------------
# content_plan markdown helpers — exercise all section fields
# ---------------------------------------------------------------------------


def test_content_plan_to_content_brief_markdown_all_fields() -> None:
    from agents.blogging.shared.content_plan import (
        ContentPlanSection,
        TitleCandidate,
        TitleScoring,
        content_plan_to_content_brief_markdown,
        content_plan_to_markdown_doc,
    )

    from ._content_plan_test_utils import make_content_plan

    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="Flow",
        target_reader="Devs",
        opening_strategy="Start with a hook",
        conclusion_guidance="End with a next step",
        sections=[
            ContentPlanSection(
                title="A",
                coverage_description="cov",
                order=0,
                key_points=["point 1", "point 2"],
                what_to_avoid=["jargon"],
                reader_takeaway="They learn X",
                strongest_point="Stat about X",
                story_opportunity="A bug story",
                opening_hook="Once upon a time",
                transition_to_next="Now consider",
                research_support_note="Source A",
                gap_flag=True,
            )
        ],
        title_candidates=[
            TitleCandidate(
                title="My Title",
                probability_of_success=0.7,
                scoring=TitleScoring(
                    curiosity_gap=0.8,
                    specificity=0.7,
                    audience_fit=0.9,
                    seo_potential=0.6,
                    emotional_pull=0.7,
                    rationale="Strong hook",
                ),
            )
        ],
    )

    md = content_plan_to_content_brief_markdown(plan)
    assert "Devs" in md
    assert "Once upon a time" in md
    assert "Strong hook" in md
    assert "jargon" in md

    full_doc = content_plan_to_markdown_doc(plan)
    assert "Requirements analysis" in full_doc


def test_content_plan_build_research_digest(monkeypatch) -> None:
    from agents.blogging.shared.content_plan import build_research_digest

    # Within budget — returns as-is
    short = "short doc"
    assert build_research_digest(short, max_chars=100) == short

    # Empty
    assert build_research_digest("", max_chars=100) == ""

    # Over budget without llm — returns as-is
    big = "x" * 1000
    assert build_research_digest(big, max_chars=50) == big

    # Over budget with llm
    import llm_service as ls

    def fake_compact(text, max_chars, llm, label):
        return text[:max_chars]

    monkeypatch.setattr(ls, "compact_text", fake_compact)
    out = build_research_digest("x" * 1000, max_chars=50, llm=object())
    assert len(out) == 50


# ---------------------------------------------------------------------------
# shared.medium_integration_access — happy path with stubbed modules
# ---------------------------------------------------------------------------


def test_medium_integration_happy_path(monkeypatch) -> None:
    """When unified_api modules are importable, resolve_medium_stats_storage_state returns state."""
    import sys
    import types

    # Create fake unified_api modules
    fake_integrations_store = types.ModuleType("unified_api.integrations_store")
    fake_integrations_store.get_medium_config = lambda: {
        "enabled": True,
        "linked_email": "user@example.com",
    }
    fake_integrations_store.get_medium_session_storage_state_json = lambda: '{"cookies": []}'
    fake_integrations_store.set_medium_session_storage_state_json = lambda x: None

    fake_unified_api = sys.modules.get("unified_api") or types.ModuleType("unified_api")
    fake_unified_api.integrations_store = fake_integrations_store

    monkeypatch.setitem(sys.modules, "unified_api", fake_unified_api)
    monkeypatch.setitem(sys.modules, "unified_api.integrations_store", fake_integrations_store)

    from agents.blogging.shared.medium_integration_access import resolve_medium_stats_storage_state

    state, hint, err = resolve_medium_stats_storage_state()
    assert state == {"cookies": []}
    assert hint == "example.com"
    assert err == ""


def test_medium_integration_disabled(monkeypatch) -> None:
    import sys
    import types

    fake_integrations_store = types.ModuleType("unified_api.integrations_store")
    fake_integrations_store.get_medium_config = lambda: {"enabled": False}
    fake_integrations_store.get_medium_session_storage_state_json = lambda: ""
    fake_integrations_store.set_medium_session_storage_state_json = lambda x: None

    fake_unified_api = sys.modules.get("unified_api") or types.ModuleType("unified_api")
    fake_unified_api.integrations_store = fake_integrations_store
    monkeypatch.setitem(sys.modules, "unified_api", fake_unified_api)
    monkeypatch.setitem(sys.modules, "unified_api.integrations_store", fake_integrations_store)

    from agents.blogging.shared.medium_integration_access import resolve_medium_stats_storage_state

    state, hint, err = resolve_medium_stats_storage_state()
    assert state is None
    assert "disabled" in err.lower()


def test_medium_integration_invalid_json(monkeypatch) -> None:
    import sys
    import types

    fake_integrations_store = types.ModuleType("unified_api.integrations_store")
    fake_integrations_store.get_medium_config = lambda: {
        "enabled": True,
        "linked_email": "x@y.com",
    }
    fake_integrations_store.get_medium_session_storage_state_json = lambda: "not json"
    fake_integrations_store.set_medium_session_storage_state_json = lambda x: None

    fake_unified_api = sys.modules.get("unified_api") or types.ModuleType("unified_api")
    fake_unified_api.integrations_store = fake_integrations_store
    monkeypatch.setitem(sys.modules, "unified_api", fake_unified_api)
    monkeypatch.setitem(sys.modules, "unified_api.integrations_store", fake_integrations_store)

    from agents.blogging.shared.medium_integration_access import resolve_medium_stats_storage_state

    state, hint, err = resolve_medium_stats_storage_state()
    assert state is None
    assert "invalid JSON" in err


def test_medium_integration_non_dict_state(monkeypatch) -> None:
    import sys
    import types

    fake_integrations_store = types.ModuleType("unified_api.integrations_store")
    fake_integrations_store.get_medium_config = lambda: {
        "enabled": True,
        "linked_email": "x@y.com",
    }
    fake_integrations_store.get_medium_session_storage_state_json = lambda: '["not", "a", "dict"]'
    fake_integrations_store.set_medium_session_storage_state_json = lambda x: None

    fake_unified_api = sys.modules.get("unified_api") or types.ModuleType("unified_api")
    fake_unified_api.integrations_store = fake_integrations_store
    monkeypatch.setitem(sys.modules, "unified_api", fake_unified_api)
    monkeypatch.setitem(sys.modules, "unified_api.integrations_store", fake_integrations_store)

    from agents.blogging.shared.medium_integration_access import resolve_medium_stats_storage_state

    state, hint, err = resolve_medium_stats_storage_state()
    assert state is None
    assert "JSON object" in err
