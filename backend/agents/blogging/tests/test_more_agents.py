"""Targeted tests for under-covered agent helpers:

* ``blog_compliance_agent.agent._fallback_compliance_report``
* ``blog_compliance_agent.agent.run_compliance_from_work_dir``
* ``blog_medium_stats_agent.scraper.parse_number``, ``parse_metrics_from_text``,
  ``extract_posts_from_html``, ``collect_medium_stats`` (with full mock).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# compliance agent
# ---------------------------------------------------------------------------


def test_compliance_fallback_report() -> None:
    from agents.blogging.blog_compliance_agent.agent import _fallback_compliance_report

    out = _fallback_compliance_report(RuntimeError("rate limit"))
    assert out.status == "FAIL"
    assert out.required_fixes
    assert "rate limit" in out.notes


def test_compliance_run_with_no_validator(monkeypatch, tmp_path) -> None:
    """No validator_report → 'No validator report available.' branch."""
    from agents.blogging.blog_compliance_agent.agent import BlogComplianceAgent
    from agents.blogging.shared import json_retry as agent_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return '{"status": "PASS", "violations": [], "required_fixes": [], "notes": "ok"}'

    monkeypatch.setattr(agent_mod, "Agent", _Agent)

    a = BlogComplianceAgent(llm_client=object())
    report = a.run("draft text", brand_spec_prompt="brand", work_dir=tmp_path)
    assert report.status == "PASS"
    assert (tmp_path / "compliance_report.json").exists()


def test_compliance_run_with_validator_summary(monkeypatch, tmp_path) -> None:
    from agents.blogging.blog_compliance_agent.agent import BlogComplianceAgent
    from agents.blogging.shared import json_retry as agent_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return (
                '{"status": "FAIL", '
                '"violations": [{"rule_id": "r1", "description": "vague"}], '
                '"required_fixes": ["be specific"], "notes": null}'
            )

    monkeypatch.setattr(agent_mod, "Agent", _Agent)

    a = BlogComplianceAgent(llm_client=object())
    validator_report = {
        "status": "PASS",
        "checks": [
            {"name": "word_count", "status": "PASS"},
            {"name": "headings", "status": "FAIL"},
        ],
    }
    report = a.run(
        "draft", brand_spec_prompt="brand", validator_report=validator_report, work_dir=tmp_path
    )
    assert report.status == "FAIL"
    assert len(report.violations) == 1


def test_compliance_run_fallback_on_persistent_parse_failure(monkeypatch, tmp_path) -> None:
    """When JSON parse fails twice in a round, fallback report is returned."""
    from agents.blogging.blog_compliance_agent.agent import BlogComplianceAgent
    from agents.blogging.shared import json_retry as agent_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json at all"

    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    a = BlogComplianceAgent(llm_client=object())
    report = a.run("draft", brand_spec_prompt="brand", work_dir=tmp_path)
    assert report.status == "FAIL"
    assert (tmp_path / "compliance_report.json").exists()


def test_compliance_run_with_exception_fallback(monkeypatch, tmp_path) -> None:
    """A non-transient, non-JSON exception falls back to a fail-closed report (no retry)."""
    from agents.blogging.blog_compliance_agent.agent import BlogComplianceAgent
    from agents.blogging.shared import json_retry as agent_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("LLM totally down")

    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    a = BlogComplianceAgent(llm_client=object())
    report = a.run("draft", brand_spec_prompt="brand", work_dir=tmp_path)
    assert report.status == "FAIL"


@pytest.mark.parametrize("kind", ["rate_limit", "temporary"])
def test_compliance_run_transient_error_reraises(monkeypatch, tmp_path, kind) -> None:
    """A transient LLM-transport error re-raises (delegated to Temporal), never fallback."""
    from agents.blogging.blog_compliance_agent.agent import BlogComplianceAgent
    from agents.blogging.shared import json_retry as agent_mod

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if kind == "rate_limit" else LLMTemporaryError

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise err_cls("transient outage")

    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    a = BlogComplianceAgent(llm_client=object())
    with pytest.raises(err_cls):
        a.run("draft", brand_spec_prompt="brand", work_dir=tmp_path)
    # Fail-closed report must NOT have been written for a transient error.
    assert not (tmp_path / "compliance_report.json").exists()


def test_compliance_status_normalization(monkeypatch, tmp_path) -> None:
    """Unknown status string → coerced to FAIL."""
    from agents.blogging.blog_compliance_agent.agent import BlogComplianceAgent
    from agents.blogging.shared import json_retry as agent_mod

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return (
                '{"status": "weird", "violations": [], '
                '"required_fixes": "single string", "notes": "ok"}'
            )

    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    a = BlogComplianceAgent(llm_client=object())
    report = a.run("draft", brand_spec_prompt="brand", work_dir=tmp_path)
    assert report.status == "FAIL"
    # required_fixes coerced to list
    assert isinstance(report.required_fixes, list)


def test_run_compliance_from_work_dir(monkeypatch, tmp_path) -> None:
    """Pull draft + validator_report from disk and run agent."""
    from agents.blogging.blog_compliance_agent.agent import run_compliance_from_work_dir
    from agents.blogging.shared import json_retry as agent_mod

    (tmp_path / "final.md").write_text("# Draft\nBody.")
    import json as json_mod

    (tmp_path / "validator_report.json").write_text(
        json_mod.dumps({"status": "PASS", "checks": []})
    )
    (tmp_path / "brand_spec_prompt.md").write_text("Brand spec")

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return '{"status": "PASS", "violations": [], "required_fixes": [], "notes": ""}'

    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    out = run_compliance_from_work_dir(
        tmp_path, llm_client=object(), brand_spec_path=tmp_path / "brand_spec_prompt.md"
    )
    assert out.status == "PASS"


def test_run_compliance_from_work_dir_falls_back_to_default_brand(monkeypatch, tmp_path) -> None:
    """When brand_spec_path doesn't exist on disk, falls back to docs/brand_spec_prompt.md."""
    from agents.blogging.blog_compliance_agent.agent import run_compliance_from_work_dir
    from agents.blogging.shared import json_retry as agent_mod

    (tmp_path / "final.md").write_text("# Draft\nBody.")

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return '{"status": "PASS", "violations": [], "required_fixes": [], "notes": null}'

    monkeypatch.setattr(agent_mod, "Agent", _Agent)
    # Call without brand_spec_path → triggers fallback path
    out = run_compliance_from_work_dir(tmp_path, llm_client=object())
    assert out.status == "PASS"


# ---------------------------------------------------------------------------
# medium stats scraper helpers
# ---------------------------------------------------------------------------


def test_scraper_parse_number_variants() -> None:
    from agents.blogging.blog_medium_stats_agent.scraper import parse_number

    assert parse_number("1,234") == 1234.0
    assert parse_number("1.5K") == 1500.0
    assert parse_number("2.3M") == 2_300_000.0
    assert parse_number("42") == 42.0
    assert parse_number("") is None
    assert parse_number("not a number") is None


def test_scraper_parse_metrics_from_text() -> None:
    from agents.blogging.blog_medium_stats_agent.scraper import parse_metrics_from_text

    text = "Story | 1,234 views | 567 reads | 89 fans | 45 claps"
    out = parse_metrics_from_text(text)
    assert out["views"] == 1234
    assert out["reads"] == 567
    assert out["fans"] == 89
    assert out["claps"] == 45

    # Empty input
    assert parse_metrics_from_text("") == {}
    # Unmatched text
    assert parse_metrics_from_text("nothing relevant here") == {}


def test_scraper_extract_posts_from_html() -> None:
    from agents.blogging.blog_medium_stats_agent.scraper import extract_posts_from_html

    html = """
    <html>
      <body>
        <table>
          <tr>
            <a href="/@user/my-post-1">My First Post</a> 1,234 views
          </tr>
          <tr>
            <a href="/@user/my-post-2">My Second Post</a> 5K views
          </tr>
          <tr>
            <a href="/@user/followers">Followers</a>
          </tr>
        </table>
      </body>
    </html>
    """
    out = extract_posts_from_html(html)
    assert len(out) == 2
    titles = [p["title"] for p in out]
    assert "My First Post" in titles
    assert "My Second Post" in titles


def test_scraper_extract_posts_from_html_empty() -> None:
    from agents.blogging.blog_medium_stats_agent.scraper import extract_posts_from_html

    assert extract_posts_from_html("") == []
    assert extract_posts_from_html("<html></html>") == []


def test_collect_medium_stats_requires_integration(monkeypatch) -> None:
    """When integration helper returns an error, collect_medium_stats raises."""
    from agents.blogging.blog_medium_stats_agent import scraper as sc
    from agents.blogging.blog_medium_stats_agent.models import MediumStatsRunConfig

    monkeypatch.setattr(sc, "resolve_medium_stats_storage_state", lambda: (None, "", "no creds"))
    with pytest.raises(RuntimeError, match="no creds"):
        sc.collect_medium_stats(MediumStatsRunConfig(headless=True))


def test_collect_medium_stats_no_session(monkeypatch) -> None:
    from agents.blogging.blog_medium_stats_agent import scraper as sc
    from agents.blogging.blog_medium_stats_agent.models import MediumStatsRunConfig

    monkeypatch.setattr(sc, "resolve_medium_stats_storage_state", lambda: (None, "host.com", ""))
    with pytest.raises(RuntimeError, match="empty"):
        sc.collect_medium_stats(MediumStatsRunConfig(headless=True))


def test_collect_medium_stats_storage_override(monkeypatch) -> None:
    """When storage_state_override is provided, integration resolver is skipped."""
    # Make playwright unavailable to short-circuit before browser launch
    import builtins

    from agents.blogging.blog_medium_stats_agent import scraper as sc
    from agents.blogging.blog_medium_stats_agent.models import MediumStatsRunConfig

    real_import = builtins.__import__

    def deny(name, *a, **kw):
        if "playwright" in name:
            raise ImportError("no playwright")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", deny)

    cfg = MediumStatsRunConfig(
        headless=True,
        storage_state_override={"cookies": []},
        account_hint_override="example.com",
    )
    with pytest.raises(RuntimeError, match="playwright is not installed"):
        sc.collect_medium_stats(cfg)
