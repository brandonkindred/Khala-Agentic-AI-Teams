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
    NotAnIssueError,
    PullRequest,
    Repo,
    SubIssue,
    is_ready,
    issue_to_plan_input,
    pick_ready_issue,
    scrub_token_from_text,
)
from coding_team.github_source.client import _is_safe_ref, _parse_next_link
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


def _expected_basic_header(token: str) -> str:
    """Expected git auth header for a fake token, built at runtime so a
    credential-shaped Base64 literal never appears in source — secret
    scanners (GitGuardian etc.) flag the pattern regardless of how fake
    the values are (same convention as TestScrubTokenFromText)."""
    import base64

    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Authorization: Basic {encoded}"


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
        with pytest.raises(NotAnIssueError) as exc_info:
            client.get_issue("o", "r", 5)
        # NotAnIssueError remains a GitHubAPIError so existing handlers catch it.
        assert isinstance(exc_info.value, GitHubAPIError)
        assert exc_info.value.number == 5

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


class TestScrubTokenFromText:
    def test_redacts_user_at_url(self) -> None:
        # Build the credentialed URL at runtime so the literal `user:pwd@host`
        # pattern never appears contiguously in source — secret scanners
        # (GitGuardian etc.) flag that pattern regardless of how fake the
        # values look.
        scheme = "https://"
        user = "u" + "ser"
        pwd = "fa" + "ke"
        msg = f"fatal: unable to push to {scheme}{user}:{pwd}@example.com/repo.git"

        out = scrub_token_from_text(msg)
        assert pwd not in out
        assert user not in out
        assert "https://***@example.com/repo.git" in out

    def test_idempotent_on_clean_text(self) -> None:
        assert scrub_token_from_text("nothing sensitive here") == "nothing sensitive here"

    def test_handles_empty(self) -> None:
        assert scrub_token_from_text("") == ""


class TestIsSafeRef:
    def test_accepts_normal_branch_names(self) -> None:
        assert _is_safe_ref("main")
        assert _is_safe_ref("feature/foo-bar")
        assert _is_safe_ref("release_2.1.0")

    def test_rejects_leading_dash(self) -> None:
        assert not _is_safe_ref("-evil")

    def test_rejects_shell_metacharacters(self) -> None:
        assert not _is_safe_ref("foo;rm -rf /")
        assert not _is_safe_ref("foo bar")
        assert not _is_safe_ref("$(echo)")

    def test_rejects_empty(self) -> None:
        assert not _is_safe_ref("")


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

    # Context-manager protocol so the production code's `with` blocks work.
    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

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

    gu_mod = sys.modules["software_engineering_team.shared.git_utils"]
    if not hasattr(gu_mod, "commit_working_tree"):
        # Functional stand-in: api.main imports commit_working_tree for dirty-tree
        # recovery, and TestPrepareIssueBranch exercises that path against real
        # repos — a (True, "...") no-op stub would leave the tree dirty and make
        # those tests order-dependent on whether the real module loaded first.
        def _stub_commit_working_tree(repo_path, message):
            import subprocess as sp

            sp.run(["git", "-C", str(repo_path), "add", "-A"], capture_output=True)
            r = sp.run(
                [
                    "git",
                    "-C",
                    str(repo_path),
                    "-c",
                    "user.name=Stub",
                    "-c",
                    "user.email=stub@example.com",
                    "commit",
                    "--no-gpg-sign",
                    "-m",
                    message,
                ],
                capture_output=True,
                text=True,
            )
            ok = r.returncode == 0 or "nothing to commit" in (r.stdout + r.stderr)
            return ok, (r.stdout + r.stderr).strip()[:200]

        gu_mod.commit_working_tree = _stub_commit_working_tree  # type: ignore[attr-defined]


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
    monkeypatch.setattr(api_main, "_prepare_issue_branch", lambda *a, **kw: (True, None, []))
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
        # Token is validated *before* the start-comment fires, so when get_repo
        # fails we should see exactly the failure comment.
        assert any("failed: " in body for _, body in gh.comments)
        assert not any("started job" in body for _, body in gh.comments)

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
        # Two comments expected: "started" + failure.
        bodies = [b for _, b in gh.comments]
        assert any("started job" in b for b in bodies)
        assert any("orchestrator exploded" in b for b in bodies)

    def test_no_merged_tasks_marks_failed(self, patched_app, monkeypatch) -> None:
        """Orchestrator returns successfully but with no merged task."""
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)

        def _no_merge(job_id: str, _rp, _plan, **kw):
            kw["update_job_fn"](
                status="completed",
                phase="completed",
                task_graph_snapshot=[{"id": "t1", "status": "to_do", "feature_branch": None}],
            )

        monkeypatch.setattr(patched_app["api"], "run_coding_team_orchestrator", _no_merge)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "no merged tasks" in job["error"]
        assert gh.created_pulls == []
        assert any("produced no merged tasks" in b for _, b in gh.comments)

    def test_fast_forward_failure_sets_status_failed(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(patched_app["api"], "_fast_forward", lambda *a, **kw: (False, "ff err"))
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        # Regression: previously left status="completed" with an error field.
        assert job["status"] == "failed"
        assert "fast-forward failed" in job["error"]

    def test_push_failure_sets_status_failed(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(patched_app["api"], "_push_branch", lambda *a, **kw: (False, "auth"))
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"

    def test_pr_lookup_failure_sets_status_failed(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})

        def _raise_lookup(*_a, **_kw):
            raise GitHubAPIError(500, "lookup boom")

        gh.find_existing_pr = _raise_lookup  # type: ignore[assignment]
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "find_existing_pr" in job["error"]

    def test_pr_creation_failure_sets_status_failed(self, patched_app) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})

        def _raise_create(**_kw):
            raise GitHubAPIError(422, "validation")

        gh.create_pull_request = _raise_create  # type: ignore[assignment]
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(1, repo_path=patched_app["repo_path"]),
        )
        job = patched_app["jobs"].get_job(resp.json()["job_id"])
        assert job["status"] == "failed"
        assert "create_pull_request" in job["error"]

    def test_pr_number_pointing_at_pr_returns_400(self, patched_app) -> None:
        """Operator passed a PR number, not an issue number → 400, not 502."""
        gh = _FakeClient()

        def _raise(_o, _r, _n):
            raise NotAnIssueError(7)

        gh.get_issue = _raise  # type: ignore[assignment]
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(7, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 400
        assert "pull request" in resp.json()["detail"]

    def test_branch_prep_failure(self, patched_app, monkeypatch) -> None:
        gh = _FakeClient(issues=[_issue(1)], sub_map={1: []})
        patched_app["set_github"](gh)
        monkeypatch.setattr(
            patched_app["api"],
            "_prepare_issue_branch",
            lambda *a, **kw: (False, "no remote", []),
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

    def test_terminal_job_for_same_issue_does_not_block(self, patched_app) -> None:
        """A previously-failed job must not block a retry on the same issue."""
        patched_app["jobs"].create_job(
            "old-failed-job",
            status="failed",
            github_context={
                "owner": "o",
                "repo": "r",
                "issue_number": 5,
                "issue_url": "x",
            },
            error="prior push failed",
        )
        gh = _FakeClient(issues=[_issue(5)], sub_map={5: []})
        patched_app["set_github"](gh)
        resp = patched_app["client"].post(
            "/run-from-github",
            json=_body(5, repo_path=patched_app["repo_path"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["job_id"] != "old-failed-job"


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

    def test_strips_trailing_whitespace_in_head(self, patched_app) -> None:
        api = patched_app["api"]
        # Title that hits the boundary exactly with a trailing space.
        out = api._truncate_title("a " * 130, 7)  # 260 chars, trims to fit
        assert " (closes #7)" in out
        assert "  (closes" not in out  # no double-space at the boundary

    def test_empty_title_falls_back_to_issue_number(self, patched_app) -> None:
        api = patched_app["api"]
        # No leading-space-only PR title; we substitute a placeholder instead.
        assert api._truncate_title("", 42) == "Issue #42 (closes #42)"


class TestPrepareIssueBranch:
    """Exercise _prepare_issue_branch against a real on-disk git repo.

    These tests deliberately avoid the ``patched_app`` fixture because that
    fixture monkey-patches the git helpers to no-op stubs for the endpoint
    tests; we want the real implementations here.
    """

    @staticmethod
    def _git(repo: str, *args: str) -> None:
        import subprocess

        subprocess.run(
            ["git", "-C", repo, *args],
            check=True,
            capture_output=True,
            text=True,
        )

    def _init_repo(self, path) -> str:
        repo = str(path / "repo")
        import os

        os.makedirs(repo, exist_ok=True)
        self._git(repo, "init", "-q")
        # Disable commit signing in case the host environment forces it.
        self._git(repo, "config", "commit.gpgsign", "false")
        self._git(repo, "config", "tag.gpgsign", "false")
        self._git(repo, "config", "user.email", "test@example.com")
        self._git(repo, "config", "user.name", "test")
        # Older git defaults to "master"; rename to "main" explicitly.
        self._git(repo, "checkout", "-q", "-b", "main")
        with open(f"{repo}/README.md", "w") as fh:
            fh.write("seed\n")
        self._git(repo, "add", "README.md")
        self._git(repo, "commit", "-q", "--no-gpg-sign", "-m", "seed")
        # Self-alias as origin so fetch works without a real remote.
        self._git(repo, "remote", "add", "origin", repo)
        return repo

    @pytest.fixture
    def api(self):
        """Import the api module fresh, without the patched_app fixture's stubs."""
        _stub_heavy_modules()
        from coding_team.api import main as api_main

        return api_main

    def test_dirty_tree_recovered_to_rescue_branch(self, api, tmp_path) -> None:
        """Uncommitted unattributed changes are preserved, then prep proceeds."""
        repo = self._init_repo(tmp_path)
        with open(f"{repo}/README.md", "a") as fh:
            fh.write("dirty\n")

        ok, msg, notes = api._prepare_issue_branch(repo, "origin", "main", "khala/issue-9")
        assert ok is True, msg
        assert any("khala/rescue/" in n for n in notes)
        import subprocess

        status = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
        assert status == ""

    def test_clean_tree_succeeds(self, api, tmp_path) -> None:
        repo = self._init_repo(tmp_path)
        self._git(repo, "fetch", "origin", "main")
        ok, msg, _notes = api._prepare_issue_branch(repo, "origin", "main", "khala/issue-9")
        assert ok is True, msg
        import subprocess

        head = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert head == "khala/issue-9"

    def test_unsafe_default_branch_rejected(self, api, tmp_path) -> None:
        repo = self._init_repo(tmp_path)
        ok, msg, _notes = api._prepare_issue_branch(repo, "origin", "--exec=evil", "khala/issue-9")
        assert ok is False
        assert "unsafe" in (msg or "")

    def test_unsafe_integration_branch_rejected(self, api, tmp_path) -> None:
        repo = self._init_repo(tmp_path)
        ok, msg, _notes = api._prepare_issue_branch(repo, "origin", "main", "-evil-name")
        assert ok is False
        assert "unsafe" in (msg or "")


class TestGitCredentialThreading:
    """The token must reach the network git ops (fetch/push) transiently.

    The unified API clones with a credential that is never persisted to
    ``.git/config``; the coding-team service runs later on the same shared
    checkout, so it must re-supply the token on every fetch/push or a default
    checkout has no auth — private repos (and the final push for public repos)
    would otherwise fail or hang until the git timeout after the job started.
    """

    @pytest.fixture
    def api(self):
        """Import the api module fresh, without the patched_app fixture's stubs."""
        _stub_heavy_modules()
        from coding_team.api import main as api_main

        return api_main

    def test_git_auth_env_injects_transient_basic_header(self, api) -> None:
        env = api._git_auth_env("secret-tok")
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        # GitHub's git smart-HTTP endpoint rejects `Bearer` (401) even for a
        # valid token — only Basic with the x-access-token username works.
        assert env["GIT_CONFIG_VALUE_0"] == _expected_basic_header("secret-tok")
        # Disable interactive prompts so a bad credential fails fast.
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        # Inherits the parent environment (PATH etc. survive).
        assert "PATH" in env

    def test_prepare_issue_branch_passes_auth_env_to_fetch(self, api, monkeypatch) -> None:
        calls = []

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            calls.append((args, env))
            return 0, ""

        monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))
        monkeypatch.setattr(api, "_git", fake_git)
        ok, msg, _notes = api._prepare_issue_branch(
            "/repo", "origin", "main", "khala/issue-1", "tok-123"
        )
        assert ok is True, msg
        # Both fetches (base branch + issue-branch continuation candidate)
        # must carry the auth env.
        fetches = [(args, env) for args, env in calls if args[0] == "fetch"]
        assert len(fetches) == 2
        for _args, env in fetches:
            assert env is not None
            assert env["GIT_CONFIG_VALUE_0"] == _expected_basic_header("tok-123")
        # Local-only git ops never carry the credential.
        assert all(env is None for args, env in calls if args[0] != "fetch")

    def test_prepare_issue_branch_without_token_uses_no_auth_env(self, api, monkeypatch) -> None:
        calls = []

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            calls.append((args, env))
            return 0, ""

        monkeypatch.setattr(api, "_working_tree_dirty", lambda p: (True, False, None))
        monkeypatch.setattr(api, "_git", fake_git)
        ok, _msg, _notes = api._prepare_issue_branch("/repo", "origin", "main", "khala/issue-1")
        assert ok is True
        assert all(env is None for _, env in calls)

    def test_push_branch_passes_auth_env(self, api, monkeypatch) -> None:
        captured = {}

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            captured["args"] = args
            captured["env"] = env
            return 0, ""

        monkeypatch.setattr(api, "_git", fake_git)
        ok, msg = api._push_branch("/repo", "origin", "khala/issue-1", "tok-xyz")
        assert ok is True, msg
        assert captured["args"][0] == "push"
        assert captured["env"]["GIT_CONFIG_VALUE_0"] == _expected_basic_header("tok-xyz")

    def test_push_branch_without_token_uses_no_auth_env(self, api, monkeypatch) -> None:
        captured = {}

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            captured["env"] = env
            return 0, ""

        monkeypatch.setattr(api, "_git", fake_git)
        ok, _ = api._push_branch("/repo", "origin", "khala/issue-1")
        assert ok is True
        assert captured["env"] is None

    def test_push_branch_rejects_unsafe_branch_before_running_git(self, api, monkeypatch) -> None:
        called = {"ran": False}

        def fake_git(*a, **kw):
            called["ran"] = True
            return 0, ""

        monkeypatch.setattr(api, "_git", fake_git)
        ok, msg = api._push_branch("/repo", "origin", "-evil", "tok")
        assert ok is False
        assert "unsafe" in (msg or "")
        assert called["ran"] is False


class TestActiveIssueMarkerLifecycle:
    """The marker means "this checkout holds unpublished work for issue N":
    it is cleared only once the work is published (PR recorded). Every
    unpublished terminal path — orchestrator exception, no merged tasks,
    fast-forward/push/PR failure — must retain it so a retry continues from
    `development` instead of rescuing the finished work and starting over."""

    def _run(self, patched_app, monkeypatch, github_client, orchestrator=None):
        api = patched_app["api"]
        cleared: list[str] = []
        monkeypatch.setattr(api, "_clear_active_issue", lambda p: cleared.append(p))
        if orchestrator is not None:
            monkeypatch.setattr(api, "run_coding_team_orchestrator", orchestrator)
        patched_app["set_github"](github_client)
        resp = patched_app["client"].post(
            "/run-from-github", json=_body(3, repo_path=patched_app["repo_path"])
        )
        assert resp.status_code == 200
        return cleared

    def test_cleared_on_publish_success(self, patched_app, monkeypatch) -> None:
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == [patched_app["repo_path"]]

    def test_retained_when_orchestrator_raises(self, patched_app, monkeypatch) -> None:
        def boom(*_a, **_kw):
            raise RuntimeError("orchestrator died")

        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client, orchestrator=boom)
        assert cleared == []

    def test_retained_when_no_merged_tasks(self, patched_app, monkeypatch) -> None:
        def no_merge(_job_id, _repo, _plan, **kw):
            kw["update_job_fn"](status="completed", task_graph_snapshot=[])

        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client, orchestrator=no_merge)
        assert cleared == []

    def test_retained_when_push_fails(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        monkeypatch.setattr(api, "_push_branch", lambda *a, **kw: (False, "remote hung up"))
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == []

    def test_retained_when_fast_forward_fails(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        monkeypatch.setattr(api, "_fast_forward", lambda *a, **kw: (False, "not possible"))
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == []

    def test_retained_when_pr_creation_fails(self, patched_app, monkeypatch) -> None:
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})

        def _raise_create(**_kw):
            raise GitHubAPIError(422, "validation")

        client.create_pull_request = _raise_create  # type: ignore[assignment]
        cleared = self._run(patched_app, monkeypatch, client)
        assert cleared == []

    def test_prep_notes_posted_as_issue_comments(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        monkeypatch.setattr(api, "_clear_active_issue", lambda p: None)
        monkeypatch.setattr(
            api,
            "_prepare_issue_branch",
            lambda *a, **kw: (True, None, ["♻️ recovered", "▶️ continuing"]),
        )
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        patched_app["set_github"](client)
        resp = patched_app["client"].post(
            "/run-from-github", json=_body(3, repo_path=patched_app["repo_path"])
        )
        assert resp.status_code == 200
        bodies = [body for _n, body in client.comments]
        assert "♻️ recovered" in bodies
        assert "▶️ continuing" in bodies

    def test_prep_receives_issue_number(self, patched_app, monkeypatch) -> None:
        api = patched_app["api"]
        seen: dict = {}

        def fake_prep(*args, **kwargs):
            seen["issue_number"] = kwargs.get("issue_number")
            return True, None, []

        monkeypatch.setattr(api, "_clear_active_issue", lambda p: None)
        monkeypatch.setattr(api, "_prepare_issue_branch", fake_prep)
        client = _FakeClient(issues=[_issue(3)], sub_map={3: []})
        patched_app["set_github"](client)
        resp = patched_app["client"].post(
            "/run-from-github", json=_body(3, repo_path=patched_app["repo_path"])
        )
        assert resp.status_code == 200
        assert seen["issue_number"] == 3


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
