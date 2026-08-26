"""Tests for the blog fact-check agent."""

import json

import pytest
from agents.blogging.blog_fact_check_agent import BlogFactCheckAgent

from llm_service import DummyLLMClient


def test_fact_check_agent_run():
    """BlogFactCheckAgent returns a FactCheckReport with status fields."""
    llm = DummyLLMClient()
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("This is a test draft about software engineering.")
    assert report.claims_status in ("PASS", "FAIL")
    assert report.risk_status in ("PASS", "FAIL")


def test_fact_check_report_has_required_fields():
    """Verify FactCheckReport has all expected fields."""
    llm = DummyLLMClient()
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("Test draft about cloud computing and microservices.")
    assert hasattr(report, "claims_status")
    assert hasattr(report, "risk_status")
    assert hasattr(report, "claims_verified")
    assert hasattr(report, "risk_flags")


def test_fact_check_with_work_dir(tmp_path):
    """Fact-check agent writes report JSON when work_dir provided."""
    llm = DummyLLMClient()
    agent = BlogFactCheckAgent(llm_client=llm)
    report = agent.run("Test draft.", work_dir=tmp_path)
    assert report.claims_status in ("PASS", "FAIL")
    assert (tmp_path / "fact_check_report.json").exists()


def test_fact_check_on_llm_request_callback(monkeypatch) -> None:
    """on_llm_request callback is invoked before the LLM call."""
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"claims_status": "PASS", "risk_status": "PASS"})

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    seen: list[str] = []
    agent = BlogFactCheckAgent(llm_client=object())
    agent.run("Some draft.", on_llm_request=lambda msg: seen.append(msg))
    assert seen == ["Checking facts and claims..."]


@pytest.mark.parametrize("kind", ["rate_limit", "temporary"])
def test_fact_check_transient_error_reraises(monkeypatch, kind) -> None:
    """A transient LLM-transport error propagates unwrapped (delegated to Temporal),
    rather than being masked as a terminal FactCheckError."""
    from agents.blogging.shared import json_retry as jr_mod

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if kind == "rate_limit" else LLMTemporaryError

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise err_cls("transient outage")

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogFactCheckAgent(llm_client=object())
    with pytest.raises(err_cls):
        agent.run("Some draft text.")


def test_fact_check_exhausted_json_fallback(monkeypatch, tmp_path) -> None:
    """Repeated invalid JSON yields a FAIL/FAIL fallback report and writes the artifact."""
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json at all"

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogFactCheckAgent(llm_client=object())
    report = agent.run("draft", work_dir=tmp_path)
    assert report.claims_status == "FAIL"
    assert report.risk_status == "FAIL"
    assert (tmp_path / "fact_check_report.json").exists()


def test_fact_check_unexpected_error_raises_fact_check_error(monkeypatch) -> None:
    """A non-transient, non-JSON error is wrapped as FactCheckError with cause=."""
    from agents.blogging.shared import json_retry as jr_mod
    from agents.blogging.shared.errors import BloggingError, FactCheckError

    root = ValueError("unexpected LLM failure")

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise root

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogFactCheckAgent(llm_client=object())
    with pytest.raises(FactCheckError) as exc_info:
        agent.run("draft")
    err = exc_info.value
    assert isinstance(err, BloggingError)
    assert err.cause is root
    assert "unexpected LLM failure" in str(err)


def test_agent_fact_check_error_accepts_cause_kwarg() -> None:
    """FactCheckError bound in the agent module must accept cause= (and BloggingError).

    Guards against a defensive ImportError fallback that only subclasses Exception
    and rejects the cause= keyword used at the wrap site.
    """
    from agents.blogging.blog_fact_check_agent.agent import FactCheckError
    from agents.blogging.shared.errors import BloggingError

    cause = RuntimeError("root failure")
    err = FactCheckError("Fact-check failed: boom", cause=cause)
    assert isinstance(err, BloggingError)
    assert err.cause is cause
    assert err.phase == "fact_check"


def test_fact_check_normalizes_invalid_status_fail_closed(monkeypatch) -> None:
    """Unrecognized claims/risk status values fail closed as FAIL."""
    from agents.blogging.shared import json_retry as jr_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"claims_status": "UNCLEAR", "risk_status": "REVIEW"})

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    agent = BlogFactCheckAgent(llm_client=object())
    report = agent.run("draft")
    assert report.claims_status == "FAIL"
    assert report.risk_status == "FAIL"


def test_run_fact_check_from_work_dir(monkeypatch, tmp_path) -> None:
    """Public work_dir entrypoint reads draft + allowed_claims and returns a report."""
    from agents.blogging.blog_fact_check_agent import run_fact_check_from_work_dir
    from agents.blogging.shared import json_retry as jr_mod

    (tmp_path / "final.md").write_text("Test draft content.")
    (tmp_path / "allowed_claims.json").write_text('{"claims": []}')

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return json.dumps({"claims_status": "PASS", "risk_status": "PASS"})

    monkeypatch.setattr(jr_mod, "Agent", _Agent)
    report = run_fact_check_from_work_dir(tmp_path, llm_client=object())
    assert report.claims_status in ("PASS", "FAIL")
    assert report.risk_status in ("PASS", "FAIL")
