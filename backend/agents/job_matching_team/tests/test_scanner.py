"""Tests for the job scanner agent (offline: mocked search + fetch + LLM)."""

from __future__ import annotations

from job_matching_team.agents.scanner import JobScannerAgent, _as_bool, _as_int
from job_matching_team.tools.web_fetch import FetchedPage, WebFetchError
from job_matching_team.tools.web_search import SearchResult


class FakeSearcher:
    def __init__(self, by_query):
        self.by_query = by_query

    def search(self, query, *, max_results=10):
        return self.by_query.get(query, [])


class FakeFetcher:
    def __init__(self, pages=None, fail_urls=None):
        self.pages = pages or {}
        self.fail_urls = fail_urls or set()

    def fetch(self, url):
        if url in self.fail_urls:
            raise WebFetchError("boom")
        return self.pages.get(url, FetchedPage(url=url, title="", text="job page text"))


def _posting_llm(by_url):
    def handler(prompt, system):
        for url, payload in by_url.items():
            if url in prompt:
                return payload
        return {"is_job_posting": False}

    from .conftest import ScriptedLLM

    return ScriptedLLM(handler)


def test_scan_extracts_dedups_and_caps():
    hits = [
        SearchResult(title="A", url="http://a.com/1", rank=1),
        SearchResult(title="B", url="http://b.com/2", rank=2),
        SearchResult(title="Dup", url="http://a.com/dup", rank=3),
    ]
    searcher = FakeSearcher({"q": hits})
    fetcher = FakeFetcher()
    llm = _posting_llm(
        {
            "http://a.com/1": {
                "is_job_posting": True,
                "title": "Engineer",
                "company": "Acme",
                "location": "NYC",
                "remote_mode": "remote",
                "salary_min": 100,
                "currency": "USD",
            },
            # Same company/title/location -> same fingerprint -> deduped.
            "http://a.com/dup": {
                "is_job_posting": True,
                "title": "Engineer",
                "company": "Acme",
                "location": "NYC",
            },
            "http://b.com/2": {
                "is_job_posting": True,
                "title": "Manager",
                "company": "Beta",
                "location": "SF",
            },
        }
    )
    agent = JobScannerAgent(llm_client=llm, searcher=searcher, fetcher=fetcher)
    postings = agent.scan(["q"], max_roles=10)
    assert len(postings) == 2
    assert {p.company for p in postings} == {"Acme", "Beta"}
    assert all(p.fingerprint for p in postings)


def test_scan_honours_max_roles():
    hits = [SearchResult(title=str(i), url=f"http://x.com/{i}", rank=i) for i in range(5)]
    llm_payloads = {
        f"http://x.com/{i}": {"is_job_posting": True, "title": f"T{i}", "company": f"C{i}"}
        for i in range(5)
    }
    agent = JobScannerAgent(
        llm_client=_posting_llm(llm_payloads),
        searcher=FakeSearcher({"q": hits}),
        fetcher=FakeFetcher(),
    )
    assert len(agent.scan(["q"], max_roles=2)) == 2


def test_skip_fingerprints_excluded():
    from job_matching_team.models import compute_fingerprint

    fp = compute_fingerprint("Acme", "Engineer", "NYC")
    agent = JobScannerAgent(
        llm_client=_posting_llm(
            {
                "http://a.com/1": {
                    "is_job_posting": True,
                    "title": "Engineer",
                    "company": "Acme",
                    "location": "NYC",
                }
            }
        ),
        searcher=FakeSearcher({"q": [SearchResult(title="A", url="http://a.com/1")]}),
        fetcher=FakeFetcher(),
    )
    out = agent.scan(["q"], max_roles=10, skip_fingerprints={fp})
    assert out == []


def test_non_posting_pages_ignored():
    agent = JobScannerAgent(
        llm_client=_posting_llm({"http://a.com/1": {"is_job_posting": False}}),
        searcher=FakeSearcher({"q": [SearchResult(title="A", url="http://a.com/1")]}),
        fetcher=FakeFetcher(),
    )
    assert agent.scan(["q"], max_roles=10) == []


def test_fetch_failure_falls_back_to_snippet():
    agent = JobScannerAgent(
        llm_client=_posting_llm(
            {"http://a.com/1": {"is_job_posting": True, "title": "Eng", "company": "Acme"}}
        ),
        searcher=FakeSearcher(
            {"q": [SearchResult(title="A", url="http://a.com/1", snippet="snippet text")]}
        ),
        fetcher=FakeFetcher(fail_urls={"http://a.com/1"}),
    )
    out = agent.scan(["q"], max_roles=10)
    assert len(out) == 1


def test_search_error_skips_query():
    class BrokenSearcher:
        def search(self, *a, **k):
            raise RuntimeError("search down")

    agent = JobScannerAgent(
        llm_client=_posting_llm({}),
        searcher=BrokenSearcher(),
        fetcher=FakeFetcher(),
    )
    assert agent.scan(["q"], max_roles=10) == []


def test_extraction_llm_error_skips_posting():
    class BrokenLLM:
        def complete_json(self, *a, **k):
            raise RuntimeError("llm down")

    agent = JobScannerAgent(
        llm_client=BrokenLLM(),
        searcher=FakeSearcher({"q": [SearchResult(title="A", url="http://a.com/1")]}),
        fetcher=FakeFetcher(),
    )
    assert agent.scan(["q"], max_roles=10) == []


def test_invalid_remote_mode_coerced_to_unknown():
    agent = JobScannerAgent(
        llm_client=_posting_llm(
            {
                "http://a.com/1": {
                    "is_job_posting": True,
                    "title": "Eng",
                    "company": "Acme",
                    "remote_mode": "martian",
                }
            }
        ),
        searcher=FakeSearcher({"q": [SearchResult(title="A", url="http://a.com/1")]}),
        fetcher=FakeFetcher(),
    )
    out = agent.scan(["q"], max_roles=10)
    assert out[0].remote_mode == "unknown"


def test_stringified_false_flag_is_not_a_posting():
    # LLM JSON often stringifies booleans; "false" must not create a posting.
    agent = JobScannerAgent(
        llm_client=_posting_llm(
            {"http://a.com/1": {"is_job_posting": "false", "title": "Eng", "company": "Acme"}}
        ),
        searcher=FakeSearcher({"q": [SearchResult(title="A", url="http://a.com/1")]}),
        fetcher=FakeFetcher(),
    )
    assert agent.scan(["q"], max_roles=10) == []


def test_stringified_true_flag_creates_posting():
    agent = JobScannerAgent(
        llm_client=_posting_llm(
            {"http://a.com/1": {"is_job_posting": "true", "title": "Eng", "company": "Acme"}}
        ),
        searcher=FakeSearcher({"q": [SearchResult(title="A", url="http://a.com/1")]}),
        fetcher=FakeFetcher(),
    )
    assert len(agent.scan(["q"], max_roles=10)) == 1


def test_as_bool_helper():
    assert _as_bool(True) is True
    assert _as_bool(False) is False
    assert _as_bool("true") is True
    assert _as_bool("TRUE") is True
    assert _as_bool("yes") is True
    assert _as_bool("1") is True
    assert _as_bool("false") is False
    assert _as_bool("no") is False
    assert _as_bool(1) is True
    assert _as_bool(0) is False
    assert _as_bool(None) is False


def test_as_int_helper():
    assert _as_int(None) is None
    assert _as_int("") is None
    assert _as_int("abc") is None
    assert _as_int("150000") == 150000
    assert _as_int(120.7) == 120
