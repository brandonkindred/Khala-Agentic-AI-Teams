"""
Tests for the GitHub-issue-driven coding-team flow.

Covers the github_source module (client / resolver / mapper) and the
POST /run-from-github endpoint in api/main.py. No real network — every test
either uses httpx.MockTransport for the low-level client or monkey-patches
the GitHubClient and helper functions on the api module.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import httpx
import pytest

from coding_team.github_source import (
    GitHubAPIError,
    GitHubClient,
    Issue,
    PullRequest,
    Repo,
    SubIssue,
    is_ready,
    issue_to_plan_input,
    pick_ready_issue,
)
from coding_team.github_source.client import _parse_next_link
from coding_team.models import CodingTeamPlanInput

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> GitHubClient:
    """Build a GitHubClient whose underlying httpx.Client uses a mock transport."""
    transport = httpx.MockTransport(handler)
    client = GitHubClient(token="t", sleep=lambda _s: None)
    client._client.close()  # type: ignore[attr-defined]
    client._client = httpx.Client(transport=transport, timeout=client._timeout)  # type: ignore[attr-defined]
    return client


def _issue_payload(number: int, **overrides: Any) -> dict[str, Any]:
    return {
        "number": number,
        "title": overrides.get("title", f"Issue {number}"),
        "body": overrides.get("body"),
        "state": overrides.get("state", "open"),
        "html_url": overrides.get("html_url", f"https://example/issues/{number}"),
        "labels": overrides.get("labels", []),
        **({"pull_request": {}} if overrides.get("is_pr") else {}),
    }


def _sub_payload(number: int, state: str = "open") -> dict[str, Any]:
    return {"number": number, "state": state, "title": f"Sub {number}"}


# ---------------------------------------------------------------------------
# Client: pagination & PR filtering
# ---------------------------------------------------------------------------


class TestClientListOpenIssues:
    def test_paginates_via_link_header(self) -> None:
        page1 = [_issue_payload(1), _issue_payload(2)]
        page2 = [_issue_payload(3)]

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.params.get("page") == "2":
                return httpx.Response(200, json=page2)
            return httpx.Response(
                200,
                json=page1,
                headers={
                    "Link": '<https://api.github.com/x?page=2>; rel="next"',
                },
            )

        client = _client_with(handler)
        numbers = [i.number for i in client.list_open_issues("o", "r")]
        assert numbers == [1, 2, 3]

    def test_filters_out_pull_requests(self) -> None:
        payload = [
            _issue_payload(1),
            _issue_payload(2, is_pr=True),
            _issue_payload(3),
        ]

        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = _client_with(handler)
        numbers = [i.number for i in client.list_open_issues("o", "r")]
        assert numbers == [1, 3]

    def test_passes_label_filter(self) -> None:
        seen: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.url.params.get("labels") or "")
            return httpx.Response(200, json=[])

        client = _client_with(handler)
        list(client.list_open_issues("o", "r", label="ready"))
        assert seen == ["ready"]


class TestClientSubIssues:
    def test_404_returns_empty_list(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        client = _client_with(handler)
        assert client.list_sub_issues("o", "r", 7) == []

    def test_paginates(self) -> None:
        page1 = [_sub_payload(10), _sub_payload(11)]
        page2 = [_sub_payload(12)]

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.params.get("page") == "2":
                return httpx.Response(200, json=page2)
            return httpx.Response(
                200,
                json=page1,
                headers={"Link": '<https://api.github.com/x?page=2>; rel="next"'},
            )

        client = _client_with(handler)
        subs = client.list_sub_issues("o", "r", 1)
        assert [s.number for s in subs] == [10, 11, 12]


class TestClientGetIssue:
    def test_rejects_pr(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_issue_payload(5, is_pr=True))

        client = _client_with(handler)
        with pytest.raises(GitHubAPIError):
            client.get_issue("o", "r", 5)

    def test_coerces_null_body(self) -> None:
        def handler(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_issue_payload(5, body=None))

        client = _client_with(handler)
        assert client.get_issue("o", "r", 5).body == ""


class TestClientRetries:
    def test_retries_on_502_then_raises(self) -> None:
        calls = {"n": 0}

        def handler(_req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(502, json={"message": "bad gateway"})

        client = _client_with(handler)
        with pytest.raises(GitHubAPIError):
            client.get_repo("o", "r")
        # max_retries default = 3
        assert calls["n"] == 3

    def test_rate_limit_sleeps_and_retries(self) -> None:
        slept: list[float] = []

        def handler(_req: httpx.Request) -> httpx.Response:
            if not slept:  # first call: rate-limited
                return httpx.Response(
                    403,
                    json={"message": "rate limited"},
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "0",
                    },
                )
            return httpx.Response(200, json={"default_branch": "main"})

        transport = httpx.MockTransport(handler)
        client = GitHubClient(token="t", sleep=lambda s: slept.append(s))
        client._client.close()  # type: ignore[attr-defined]
        client._client = httpx.Client(transport=transport, timeout=10)  # type: ignore[attr-defined]

        repo = client.get_repo("o", "r")
        assert repo.default_branch == "main"
        assert len(slept) == 1


class TestParseNextLink:
    def test_extracts_next(self) -> None:
        header = (
            '<https://api.github.com/r?page=2>; rel="next", '
            '<https://api.github.com/r?page=5>; rel="last"'
        )
        assert _parse_next_link(header) == "https://api.github.com/r?page=2"

    def test_no_next(self) -> None:
        assert _parse_next_link(None) is None
        assert _parse_next_link('<x>; rel="last"') is None


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class _FakeClient:
    """Just the surface dependency_resolver / endpoint code touches."""

    def __init__(
        self,
        issues: Optional[list[Issue]] = None,
        sub_map: Optional[dict[int, list[SubIssue]]] = None,
        repo: Optional[Repo] = None,
        existing_pr: Optional[PullRequest] = None,
    ) -> None:
        self._issues = issues or []
        self._sub_map = sub_map or {}
        self._repo = repo or Repo(default_branch="main")
        self._existing_pr = existing_pr
        self.created_pulls: list[dict[str, Any]] = []
        self.comments: list[tuple[int, str]] = []
        self.fail_comments = False
        self.fail_get_repo = False

    def list_open_issues(self, _o: str, _r: str, label: Optional[str] = None):
        for i in self._issues:
            if label and label not in i.labels:
                continue
            yield i

    def get_issue(self, _o: str, _r: str, n: int) -> Issue:
        for i in self._issues:
            if i.number == n:
                return i
        raise GitHubAPIError(404, f"missing #{n}")

    def list_sub_issues(self, _o: str, _r: str, n: int) -> list[SubIssue]:
        return list(self._sub_map.get(n, []))

    def get_repo(self, _o: str, _r: str) -> Repo:
        if self.fail_get_repo:
            raise GitHubAPIError(500, "boom")
        return self._repo

    def add_issue_comment(self, _o: str, _r: str, n: int, body: str) -> None:
        if self.fail_comments:
            raise GitHubAPIError(403, "no scope")
        self.comments.append((n, body))

    def find_existing_pr(self, _o: str, _r: str, _h: str):
        return self._existing_pr

    def create_pull_request(self, **kwargs: Any) -> PullRequest:
        self.created_pulls.append(kwargs)
        return PullRequest(
            number=42,
            html_url="https://example/pr/42",
            head=kwargs["head"],
            base=kwargs["base"],
        )


def _issue(num: int, title: str = "T", body: str = "B", labels: tuple[str, ...] = ()) -> Issue:
    return Issue(
        number=num,
        title=title,
        body=body,
        state="open",
        html_url=f"https://example/issues/{num}",
        labels=labels,
    )


class TestIsReady:
    def test_no_subs_is_ready(self) -> None:
        c = _FakeClient(sub_map={})
        r = is_ready(c, "o", "r", _issue(1))
        assert r.ready is True
        assert r.blocking == ()

    def test_all_closed_is_ready(self) -> None:
        c = _FakeClient(sub_map={1: [SubIssue(2, "closed", "x"), SubIssue(3, "closed", "y")]})
        r = is_ready(c, "o", "r", _issue(1))
        assert r.ready is True

    def test_any_open_blocks(self) -> None:
        c = _FakeClient(sub_map={1: [SubIssue(2, "closed", "x"), SubIssue(3, "open", "y")]})
        r = is_ready(c, "o", "r", _issue(1))
        assert r.ready is False
        assert r.blocking == (3,)


class TestPickReady:
    def test_skips_blocked_returns_first_ready(self) -> None:
        c = _FakeClient(
            issues=[_issue(1), _issue(2), _issue(3)],
            sub_map={
                1: [SubIssue(99, "open", "x")],
                2: [],
                3: [],
            },
        )
        picked = pick_ready_issue(c, "o", "r")
        assert picked is not None
        issue, ready = picked
        assert issue.number == 2
        assert ready.ready is True

    def test_returns_none_when_all_blocked(self) -> None:
        c = _FakeClient(
            issues=[_issue(1), _issue(2)],
            sub_map={
                1: [SubIssue(10, "open", "x")],
                2: [SubIssue(11, "open", "y")],
            },
        )
        assert pick_ready_issue(c, "o", "r") is None

    def test_label_filter_passes_through(self) -> None:
        c = _FakeClient(
            issues=[
                _issue(1, labels=("other",)),
                _issue(2, labels=("ready",)),
            ],
            sub_map={1: [], 2: []},
        )
        picked = pick_ready_issue(c, "o", "r", label="ready")
        assert picked is not None
        assert picked[0].number == 2


# ---------------------------------------------------------------------------
# issue_to_plan_input
# ---------------------------------------------------------------------------


class TestIssueToPlanInput:
    def test_maps_basic_fields(self) -> None:
        plan = issue_to_plan_input(
            _issue(7, title="Add login", body="Sign in with email."),
            "/tmp/repo",
            sub_issues=[],
            owner="o",
            repo="r",
        )
        assert isinstance(plan, CodingTeamPlanInput)
        assert plan.requirements_title == "Add login"
        assert plan.requirements_description == "Sign in with email."
        assert plan.repo_path == "/tmp/repo"
        gh = plan.project_overview["github_issue"]
        assert gh["owner"] == "o"
        assert gh["repo"] == "r"
        assert gh["number"] == 7
        assert plan.existing_code_summary is None

    def test_summarizes_closed_sub_issues(self) -> None:
        plan = issue_to_plan_input(
            _issue(7),
            "/tmp/repo",
            sub_issues=[
                SubIssue(8, "closed", "Schema"),
                SubIssue(9, "closed", "Migrations"),
            ],
            owner="o",
            repo="r",
        )
        assert plan.existing_code_summary is not None
        assert "#8 Schema" in plan.existing_code_summary
        assert "#9 Migrations" in plan.existing_code_summary

    def test_skips_open_sub_issues_in_summary(self) -> None:
        plan = issue_to_plan_input(
            _issue(7),
            "/tmp/repo",
            sub_issues=[SubIssue(8, "open", "WIP")],
            owner="o",
            repo="r",
        )
        assert plan.existing_code_summary is None


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------


def _stub_heavy_modules() -> None:
    """
    Pre-load lightweight stand-ins for the LLM/agent modules that api.main
    transitively imports. This keeps the endpoint tests insulated from the
    full agent stack (strands, llm_service, software_engineering_team, ...).
    """
    import sys
    import types

    # Replace coding_team.orchestrator with a stub exposing only the symbol
    # api.main imports. Tests monkey-patch the function on api.main itself.
    if "coding_team.orchestrator" not in sys.modules or not hasattr(
        sys.modules["coding_team.orchestrator"], "_stubbed"
    ):
        stub = types.ModuleType("coding_team.orchestrator")
        stub._stubbed = True  # type: ignore[attr-defined]

        def _noop(*_a: Any, **_kw: Any) -> None:
            return None

        stub.run_coding_team_orchestrator = _noop  # type: ignore[attr-defined]
        sys.modules["coding_team.orchestrator"] = stub

    # software_engineering_team.shared.git_utils.DEVELOPMENT_BRANCH only.
    if "software_engineering_team" not in sys.modules:
        sys.modules["software_engineering_team"] = types.ModuleType("software_engineering_team")
    if "software_engineering_team.shared" not in sys.modules:
        sys.modules["software_engineering_team.shared"] = types.ModuleType(
            "software_engineering_team.shared"
        )
    if "software_engineering_team.shared.git_utils" not in sys.modules:
        gu = types.ModuleType("software_engineering_team.shared.git_utils")
        gu.DEVELOPMENT_BRANCH = "development"  # type: ignore[attr-defined]
        sys.modules["software_engineering_team.shared.git_utils"] = gu


@pytest.fixture
def patched_app(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """
    Wire the coding_team API with:
      * a FakeJobServiceClient backing job_store
      * GitHubClient replaced with a stub (per test, via the returned setter)
      * _start_hook_thread invoked synchronously
      * git helpers that succeed by default
      * orchestrator no-op that records a merged task
    """
    _stub_heavy_modules()

    from job_service_client_fake import FakeJobServiceClient

    fake_jobs = FakeJobServiceClient(team="coding_team")

    from coding_team import job_store as job_store_mod

    monkeypatch.setattr(job_store_mod, "_client", lambda *a, **kw: fake_jobs)

    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    monkeypatch.setenv("GITHUB_TOKEN", "fake-token")

    from coding_team.api import main as api_main

    holder: dict[str, Any] = {"client": _FakeClient()}

    def _make_client(**_kw: Any) -> _FakeClient:
        return holder["client"]

    monkeypatch.setattr(api_main, "GitHubClient", _make_client)

    # Run the hook synchronously inside the request.
    monkeypatch.setattr(
        api_main,
        "_start_hook_thread",
        lambda *a, **kw: api_main._run_with_github_hooks(*a, **kw),
    )

    # Git helpers: success by default.
    monkeypatch.setattr(api_main, "_prepare_issue_branch", lambda *a, **kw: (True, None))
    monkeypatch.setattr(api_main, "_fast_forward", lambda *a, **kw: (True, None))
    monkeypatch.setattr(api_main, "_push_branch", lambda *a, **kw: (True, None))

    # Orchestrator no-op: mark a merged task on the job.
    def _fake_orchestrator(job_id: str, _repo_path, _plan, **kw):
        update_fn = kw["update_job_fn"]
        update_fn(
            status="completed",
            phase="completed",
            task_graph_snapshot=[
                {
                    "id": "t1",
                    "status": "merged",
                    "feature_branch": "feature/t1",
                    "merged_at": "2026-05-10T00:00:00Z",
                }
            ],
        )

    monkeypatch.setattr(api_main, "run_coding_team_orchestrator", _fake_orchestrator)

    from fastapi.testclient import TestClient

    return {
        "client": TestClient(api_main.app),
        "api": api_main,
        "repo_path": str(repo_path),
        "set_github": lambda fc: holder.__setitem__("client", fc),
        "github": lambda: holder["client"],
        "jobs": fake_jobs,
    }


def _body(issue_number: int = 1, **overrides: Any) -> dict[str, Any]:
    return {
        "owner": "o",
        "repo": "r",
        "repo_path": overrides.pop("repo_path"),
        "issue_number": issue_number,
        **overrides,
    }


class TestEndpointHappyPath:
    def test_picks_ready_issue_and_opens_pr(self, patched_app) -> None:
        gh = _FakeClient(
            issues=[_issue(11, title="Add feature")],
            sub_map={11: []},
        )
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json={
                "owner": "o",
                "repo": "r",
                "repo_path": patched_app["repo_path"],
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["issue_number"] == 11
        # Two comments: start + draft-PR-opened
        assert len(gh.comments) == 2
        assert "started job" in gh.comments[0][1]
        assert "Draft PR opened" in gh.comments[1][1]
        # PR was created
        assert len(gh.created_pulls) == 1
        assert gh.created_pulls[0]["draft"] is True
        assert gh.created_pulls[0]["head"] == "khala/issue-11"
        assert gh.created_pulls[0]["base"] == "main"
        # Job persisted with PR url
        job = patched_app["jobs"].get_job(data["job_id"])
        assert job["github_pr_url"] == "https://example/pr/42"
        assert job["integration_branch"] == "khala/issue-11"

    def test_specific_issue_number(self, patched_app) -> None:
        gh = _FakeClient(issues=[_issue(7)], sub_map={7: []})
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(7, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        assert resp.json()["issue_number"] == 7


class TestEndpointFailures:
    def test_no_token_returns_400(self, patched_app, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        resp = patched_app["client"].post(
            "/run-from-github",
            json={"owner": "o", "repo": "r", "repo_path": patched_app["repo_path"]},
        )
        assert resp.status_code == 400
        assert "GITHUB_TOKEN" in resp.json()["detail"]

    def test_bad_repo_path_returns_400(self, patched_app) -> None:
        resp = patched_app["client"].post(
            "/run-from-github",
            json={"owner": "o", "repo": "r", "repo_path": "/nope/does-not-exist"},
        )
        assert resp.status_code == 400
        assert "repo_path" in resp.json()["detail"]

    def test_no_ready_issue_returns_404(self, patched_app) -> None:
        gh = _FakeClient(
            issues=[_issue(1)],
            sub_map={1: [SubIssue(2, "open", "blocker")]},
        )
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json={"owner": "o", "repo": "r", "repo_path": patched_app["repo_path"]},
        )
        assert resp.status_code == 404
        assert gh.created_pulls == []
        assert gh.comments == []

    def test_specific_issue_blocked_returns_409(self, patched_app) -> None:
        gh = _FakeClient(
            issues=[_issue(1)],
            sub_map={1: [SubIssue(2, "open", "blocker")]},
        )
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 409
        assert "blocked by sub-issues [2]" in resp.json()["detail"]

    def test_get_repo_failure(self, patched_app) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        gh.fail_get_repo = True
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        # Hook ran synchronously, so 200 is returned but the job is failed.
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "get_repo" in job["error"]
        assert gh.created_pulls == []

    def test_orchestrator_raises(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)

        def _boom(*_a, **_kw) -> None:
            raise RuntimeError("orchestrator exploded")

        monkeypatch.setattr(patched_app["api"], "run_coding_team_orchestrator", _boom)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "orchestrator exploded" in job["error"]
        assert gh.created_pulls == []

    def test_branch_prep_failure(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(
            patched_app["api"],
            "_prepare_issue_branch",
            lambda *a, **kw: (False, "no remote"),
        )
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "branch prep failed" in job["error"]
        assert gh.created_pulls == []

    def test_fast_forward_failure(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(patched_app["api"], "_fast_forward", lambda *a, **kw: (False, "ff err"))
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert "fast-forward failed" in job["error"]
        assert gh.created_pulls == []

    def test_push_failure(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(patched_app["api"], "_push_branch", lambda *a, **kw: (False, "auth"))
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert "git push failed" in job["error"]
        assert gh.created_pulls == []


class TestEndpointReuse:
    def test_reuses_existing_pr(self, patched_app) -> None:
        gh = _FakeClient(
            issues=[_issue(1)],
            sub_map={1: []},
            existing_pr=PullRequest(
                number=99,
                html_url="https://example/pr/99",
                head="khala/issue-1",
                base="main",
            ),
        )
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        # No new PR created, but job records the existing PR url.
        assert gh.created_pulls == []
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["github_pr_url"] == "https://example/pr/99"
        assert any("Reusing existing draft PR" in c[1] for c in gh.comments)


class TestEndpointDuplicateGuard:
    def test_rejects_concurrent_run_for_same_issue(self, patched_app) -> None:
        # Seed a running job tagged with the same issue.
        patched_app["jobs"].create_job(
            "running-job",
            status="running",
            github_context={
                "owner": "o",
                "repo": "r",
                "issue_number": 5,
                "issue_url": "x",
            },
        )
        gh = _FakeClient(issues=[_issue(5)], sub_map={5: []})
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(5, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"]


class TestTruncateTitle:
    def test_unicode_long_title_caps_at_256(self, patched_app) -> None:
        api = patched_app["api"]
        title = "✦" * 300
        out = api._truncate_title(title, 42)
        assert len(out) == 256
        assert out.endswith(" (closes #42)")

    def test_short_title_unchanged(self, patched_app) -> None:
        api = patched_app["api"]
        out = api._truncate_title("Add login", 7)
        assert out == "Add login (closes #7)"


class TestStatusResponseSurfacing:
    def test_status_returns_github_fields(self, patched_app) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        post = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        job_id = post.json()["job_id"]
        status = patched_app["client"].get(f"/status/{job_id}")
        body = status.json()
        assert body["github_context"]["issue_number"] == 1
        assert body["github_pr_url"] == "https://example/pr/42"
