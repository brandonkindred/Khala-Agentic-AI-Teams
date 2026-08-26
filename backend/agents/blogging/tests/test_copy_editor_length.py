"""Tests for BlogCopyEditorAgent length-feedback injection branches."""

from __future__ import annotations

import json


def _make_agent():
    from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent

    from llm_service import DummyLLMClient

    return BlogCopyEditorAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="Style",
        brand_spec_content="Brand",
    )


def _make_input(**kw):
    from agents.blogging.blog_copy_editor_agent.models import CopyEditorInput

    defaults = {
        "draft": "# Draft\n\n" + " ".join(["word"] * 1500),  # ~1500 words
        "audience": "devs",
        "tone_or_purpose": "inform",
        "target_word_count": 1000,
        "soft_min_words": 700,
        "soft_max_words": 1300,
        "editor_must_fix_over_ratio": 1.4,  # >1400 → must_fix
        "editor_should_fix_over_ratio": 1.15,  # >1150 → should_fix
        "content_profile": "standard_article",
        "length_guidance": "aim for ~1000 words",
        "content_plan_context": "",
    }
    defaults.update(kw)
    return CopyEditorInput(**defaults)


def _patch_agent_response(monkeypatch, response_json: dict) -> None:
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps(response_json)

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)


def test_copy_editor_length_must_fix_injected(monkeypatch) -> None:
    """Draft well over soft_max + must_fix ratio → injects must_fix feedback."""
    a = _make_agent()
    _patch_agent_response(
        monkeypatch,
        {"approved": True, "summary": "ok", "feedback_items": []},
    )
    # 1500 words, target=1000, must_ratio=1.4 → 1500/1000=1.5 > 1.4
    out = a.run(_make_input())
    assert any(f.severity == "must_fix" for f in out.feedback_items)


def test_copy_editor_length_should_fix_injected(monkeypatch) -> None:
    """Draft slightly over soft_max but under must ratio → should_fix."""
    a = _make_agent()
    _patch_agent_response(
        monkeypatch,
        {"approved": True, "summary": "ok", "feedback_items": []},
    )
    # 1200 words, target=1000, soft_max=1100, should_ratio=1.15 → 1200/1000=1.2 → should_fix
    inp = _make_input(
        draft="# Draft\n\n" + " ".join(["word"] * 1200),
        soft_max_words=1100,
    )
    out = a.run(inp)
    # Either should_fix or no fix depending on soft_max
    severities = [f.severity for f in out.feedback_items]
    assert "should_fix" in severities


def test_copy_editor_length_inside_band_no_inject(monkeypatch) -> None:
    """Draft inside band → no injection."""
    a = _make_agent()
    _patch_agent_response(
        monkeypatch,
        {"approved": True, "summary": "ok", "feedback_items": []},
    )
    inp = _make_input(draft="# Draft\n\n" + " ".join(["word"] * 1000))
    out = a.run(inp)
    # No length feedback items
    structure_items = [
        f for f in out.feedback_items if f.category == "structure" and "length" in f.issue.lower()
    ]
    assert not structure_items


def test_copy_editor_technical_deep_dive_thin_draft(monkeypatch) -> None:
    """Technical deep dive with draft below soft_min * 0.88 → 'consider' feedback."""
    a = _make_agent()
    _patch_agent_response(
        monkeypatch,
        {"approved": True, "summary": "ok", "feedback_items": []},
    )
    inp = _make_input(
        draft="# Short\n\n" + " ".join(["word"] * 100),  # 100 words
        content_profile="technical_deep_dive",
        soft_min_words=1500,
        target_word_count=2000,
    )
    out = a.run(inp)
    # The draft is way under soft_min → consider feedback
    assert any(f.severity == "consider" for f in out.feedback_items)


def test_copy_editor_llm_json_parse_failure_uses_fallback(monkeypatch) -> None:
    """Failure to parse JSON → fallback summary returned."""
    from agents.blogging.shared import json_retry as json_retry_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json"

    monkeypatch.setattr(json_retry_mod, "Agent", _Agent)
    a = _make_agent()
    out = a.run(_make_input(draft="# d\n\nshort"))
    assert "could not parse" in out.summary.lower()


def test_copy_editor_feedback_items_filtered(monkeypatch) -> None:
    """Empty issue → item is dropped."""
    a = _make_agent()
    _patch_agent_response(
        monkeypatch,
        {
            "approved": False,
            "summary": "review",
            "feedback_items": [
                {"category": "grammar", "severity": "minor", "issue": "comma"},
                {"category": "x", "severity": "x", "issue": ""},  # dropped
                "not-a-dict",  # dropped
            ],
        },
    )
    out = a.run(_make_input(draft="# d\n\nshort"))
    assert len(out.feedback_items) >= 1
    assert all(f.issue for f in out.feedback_items)


def test_copy_editor_writes_artifact(monkeypatch, tmp_path) -> None:
    a = _make_agent()
    _patch_agent_response(
        monkeypatch,
        {"approved": True, "summary": "ok", "feedback_items": []},
    )
    a.run(_make_input(draft="# d\n\nshort"), feedback_output_path=tmp_path / "fb.json")
    assert (tmp_path / "fb.json").exists()
