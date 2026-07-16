"""Unit tests for Medium stats parsing helpers (no Playwright)."""

from agents.blogging.blog_medium_stats_agent.models import MediumPostStats, MediumStatsReport
from agents.blogging.blog_medium_stats_agent.scraper import (
    extract_posts_from_html,
    parse_metrics_from_text,
    parse_number,
)


def test_parse_number_plain() -> None:
    assert parse_number("1,234") == 1234.0
    assert parse_number("42") == 42.0


def test_parse_number_suffix() -> None:
    assert parse_number("1.2k") == 1200.0
    assert parse_number("3M") == 3_000_000.0


def test_parse_metrics_from_text() -> None:
    text = "My story 1,240 views 89 reads 12 fans"
    m = parse_metrics_from_text(text)
    assert m.get("views") == 1240
    assert m.get("reads") == 89
    assert m.get("fans") == 12


def test_extract_posts_from_html_fixture() -> None:
    html = """
    <html><body><table><tr>
    <td><a href="/@writer/my-story-slug">Great Article Title</a></td>
    <td>500 views 40 reads</td>
    </tr></table></body></html>
    """
    rows = extract_posts_from_html(html)
    assert len(rows) == 1
    assert rows[0]["url"] == "https://medium.com/@writer/my-story-slug"
    assert "Great Article" in rows[0]["title"]
    assert "500 views" in rows[0]["raw_row_text"]


def test_medium_stats_report_model_dump_json_safe() -> None:
    r = MediumStatsReport(
        account_hint="example.com",
        posts=[
            MediumPostStats(
                title="Hello",
                url="https://medium.com/@u/hello",
                stats={"views": 10},
            ),
        ],
    )
    d = r.model_dump(mode="json")
    assert d["posts"][0]["title"] == "Hello"
    assert d["posts"][0]["stats"]["views"] == 10
