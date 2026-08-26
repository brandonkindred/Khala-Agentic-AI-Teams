"""Tests for the blog compliance agent."""

from pathlib import Path

import pytest
from agents.blogging.blog_compliance_agent import BlogComplianceAgent
from agents.blogging.shared.brand_spec import load_brand_spec_prompt

from llm_service import DummyLLMClient


@pytest.fixture
def brand_spec_prompt():
    """Load brand spec prompt from docs or use minimal fallback."""
    path = Path(__file__).resolve().parent.parent / "docs" / "brand_spec_prompt.md"
    if path.exists():
        return load_brand_spec_prompt(path)
    return "Brand: Test. Audience: Developers. Purpose: Clarity."


def test_compliance_agent_run(brand_spec_prompt):
    """BlogComplianceAgent returns a ComplianceReport with status."""
    llm = DummyLLMClient()
    agent = BlogComplianceAgent(llm_client=llm)
    draft = """# Hook

This is a good opening. It has two sentences at least.

# Explain the idea

We explain things clearly. Short sentences work best.

# Wrap up

We wrap up nicely. The end."""
    report = agent.run(draft, brand_spec_prompt)
    assert report.status in ("PASS", "FAIL")
    assert hasattr(report, "violations")
    assert hasattr(report, "required_fixes")


def test_compliance_agent_with_work_dir(brand_spec_prompt, tmp_path):
    """Compliance agent writes compliance_report.json when work_dir provided."""
    llm = DummyLLMClient()
    agent = BlogComplianceAgent(llm_client=llm)
    draft = "Short draft."
    agent.run(draft, brand_spec_prompt, work_dir=tmp_path)
    assert (tmp_path / "compliance_report.json").exists()


def test_compliance_fallback_on_dummy(brand_spec_prompt, tmp_path):
    """DummyLLMClient produces a report (may be PASS or FAIL depending on pattern matching)."""
    llm = DummyLLMClient()
    agent = BlogComplianceAgent(llm_client=llm)
    report = agent.run("# Draft\n\nHello.", brand_spec_prompt, work_dir=tmp_path)
    assert report.status in ("PASS", "FAIL")
    assert (tmp_path / "compliance_report.json").exists()


def test_compliance_report_has_required_fields(brand_spec_prompt):
    """Verify ComplianceReport has all expected fields."""
    llm = DummyLLMClient()
    agent = BlogComplianceAgent(llm_client=llm)
    report = agent.run("Test draft content.", brand_spec_prompt)
    assert hasattr(report, "status")
    assert hasattr(report, "violations")
    assert hasattr(report, "required_fixes")
    assert hasattr(report, "notes")


def test_run_compliance_from_work_dir(monkeypatch, tmp_path):
    """Public work_dir entrypoint reads draft + brand spec and returns a report."""
    from agents.blogging.blog_compliance_agent import run_compliance_from_work_dir
    from agents.blogging.shared import json_retry as jr_mod

    (tmp_path / "final.md").write_text("Test draft.")
    (tmp_path / "brand_spec_prompt.md").write_text("Brand spec content.")

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return '{"status": "PASS", "violations": [], "required_fixes": [], "notes": "ok"}'

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    report = run_compliance_from_work_dir(tmp_path, llm_client=object())
    assert report.status in ("PASS", "FAIL")


def test_compliance_on_llm_request_callback(monkeypatch, brand_spec_prompt) -> None:
    """on_llm_request callback is invoked before the LLM call."""
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return '{"status": "PASS", "violations": [], "required_fixes": [], "notes": "ok"}'

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    seen: list[str] = []
    agent = BlogComplianceAgent(llm_client=object())
    agent.run("draft", brand_spec_prompt, on_llm_request=lambda msg: seen.append(msg))
    assert seen == ["Checking compliance with brand guidelines..."]


@pytest.mark.parametrize("kind", ["rate_limit", "temporary"])
def test_compliance_transient_error_reraises(monkeypatch, brand_spec_prompt, kind) -> None:
    """Transient LLM-transport errors re-raise unwrapped (delegated to Temporal)."""
    from agents.blogging.shared import json_retry as jr_mod

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if kind == "rate_limit" else LLMTemporaryError

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise err_cls("transient outage")

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogComplianceAgent(llm_client=object())
    with pytest.raises(err_cls):
        agent.run("draft", brand_spec_prompt)


def test_compliance_exhausted_json_fallback(monkeypatch, brand_spec_prompt, tmp_path) -> None:
    """Repeated invalid JSON yields a FAIL fallback report and writes the artifact."""
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json at all"

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogComplianceAgent(llm_client=object())
    report = agent.run("draft", brand_spec_prompt, work_dir=tmp_path)
    assert report.status == "FAIL"
    assert (tmp_path / "compliance_report.json").exists()


def test_compliance_unexpected_error_fail_closed(monkeypatch, brand_spec_prompt, tmp_path) -> None:
    """Non-transient unexpected errors fail closed via on_unexpected_error (no raise)."""
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise ValueError("unexpected failure")

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogComplianceAgent(llm_client=object())
    report = agent.run("draft", brand_spec_prompt, work_dir=tmp_path)
    assert report.status == "FAIL"
    assert (tmp_path / "compliance_report.json").exists()


def test_compliance_wrapped_transient_error_unwraps(monkeypatch, brand_spec_prompt) -> None:
    """A transient LLM error wrapped in Strands' EventLoopException re-raises unwrapped."""
    from agents.blogging.shared import json_retry as jr_mod
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMTemporaryError

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise EventLoopException(LLMTemporaryError("transient outage"))

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogComplianceAgent(llm_client=object())
    with pytest.raises(LLMTemporaryError):
        agent.run("draft", brand_spec_prompt)
