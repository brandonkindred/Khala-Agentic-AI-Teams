"""Tests for the web search and fetch tools (HTTP transport mocked)."""

from __future__ import annotations

import httpx
import pytest

from job_matching_team.tools import web_fetch, web_search
from job_matching_team.tools.web_fetch import WebFetcher, WebFetchError
from job_matching_team.tools.web_search import OllamaWebSearch, WebSearchError


def _patch_client(monkeypatch, module, handler):
    """Patch ``module.httpx.Client`` to use a MockTransport running ``handler``."""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("follow_redirects", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(module.httpx, "Client", factory)


# --- web_search ------------------------------------------------------------


def test_search_requires_api_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    s = OllamaWebSearch(api_key=None)
    with pytest.raises(WebSearchError, match="OLLAMA_API_KEY"):
        s.search("python jobs")


def test_search_parses_results(monkeypatch):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer key123"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Job A", "url": "http://a.com", "content": "snippet"},
                    {"url": "http://b.com"},  # missing title -> uses url
                    {"title": "no url"},  # skipped (no url)
                ]
            },
        )

    _patch_client(monkeypatch, web_search, handler)
    s = OllamaWebSearch(api_key="key123")
    results = s.search("python jobs", max_results=5)
    assert [r.url for r in results] == ["http://a.com", "http://b.com"]
    assert results[0].snippet == "snippet"
    assert results[1].title == "http://b.com"


def test_search_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(web_search.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("transient")
        return httpx.Response(200, json={"results": [{"title": "A", "url": "http://a.com"}]})

    _patch_client(monkeypatch, web_search, handler)
    results = OllamaWebSearch(api_key="k").search("q")
    assert calls["n"] == 2
    assert results[0].url == "http://a.com"


def test_search_exhausts_retries(monkeypatch):
    monkeypatch.setattr(web_search.time, "sleep", lambda *_: None)

    def handler(request):
        raise httpx.ConnectError("down")

    _patch_client(monkeypatch, web_search, handler)
    with pytest.raises(WebSearchError, match="after"):
        OllamaWebSearch(api_key="k").search("q")


def test_search_non_200_raises(monkeypatch):
    _patch_client(monkeypatch, web_search, lambda req: httpx.Response(500, text="boom"))
    with pytest.raises(WebSearchError, match="status 500"):
        OllamaWebSearch(api_key="k").search("q")


def test_search_validates_args():
    s = OllamaWebSearch(api_key="k")
    with pytest.raises(AssertionError):
        s.search("")
    with pytest.raises(AssertionError):
        s.search("q", max_results=0)


# --- web_fetch -------------------------------------------------------------


def test_extract_strips_markup_and_title():
    f = WebFetcher()
    html = "<html><head><title> My  Job </title></head><body><script>x=1</script><p>Hello  world</p></body></html>"
    title, text = f._extract(html)
    assert title == "My Job"
    assert "Hello world" in text
    assert "x=1" not in text


def test_fetch_truncates_content(monkeypatch):
    big = "<body>" + ("a " * 5000) + "</body>"
    _patch_client(monkeypatch, web_fetch, lambda req: httpx.Response(200, text=big))
    page = WebFetcher(max_content=100).fetch("http://x.com")
    assert len(page.text) <= 100


def test_fetch_error_status_raises(monkeypatch):
    _patch_client(monkeypatch, web_fetch, lambda req: httpx.Response(404, text="nope"))
    with pytest.raises(WebFetchError, match="status 404"):
        WebFetcher().fetch("http://x.com")


def test_fetch_transport_error_raises(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("refused")

    _patch_client(monkeypatch, web_fetch, handler)
    with pytest.raises(WebFetchError):
        WebFetcher().fetch("http://x.com")


def test_fetch_requires_absolute_url():
    with pytest.raises(AssertionError):
        WebFetcher().fetch("not-a-url")
