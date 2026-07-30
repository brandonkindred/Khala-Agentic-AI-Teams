# Malformed-Proposals GitHubClient Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `test_malformed_proposals_field_yields_no_candidates` fail if `create_review_issues` constructs a `GitHubClient` when proposals are malformed.

**Architecture:** Strengthen an existing unit test by monkeypatching `api_main.GitHubClient` to a factory that raises `AssertionError` on construction — the same pattern as `test_repo_mismatch_raises_before_any_issue` in the same file. No production code changes.

**Tech Stack:** Python 3.10+, pytest, existing `MonkeyPatch` fixtures in `test_coding_team_review_pr.py`.

## Global Constraints

- Never reference GitHub issues in code, comments, or commit messages (PR body may use `Closes #3320`).
- Design by Contract: update the test docstring to state the no-construction contract.
- Do not modify `pr_review_issues.create_review_issues` production logic.
- Work only in the worktree at `.worktrees/fix-3320-malformed-proposals-github-client` on branch `fix/3320-malformed-proposals-github-client`.

---

## File Structure

| File | Role |
|------|------|
| `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py` | Hosts `TestCreateReviewIssuesUnit.test_malformed_proposals_field_yields_no_candidates` — only file modified for the fix |
| `docs/superpowers/specs/2026-07-30-malformed-proposals-github-client-design.md` | Approved design (already written; include in the same commit if not yet committed) |
| `docs/superpowers/plans/2026-07-30-malformed-proposals-github-client.md` | This plan (optional to commit with the fix) |

No new modules. No production file changes.

---

### Task 1: Strengthen the malformed-proposals unit test

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py` — method `TestCreateReviewIssuesUnit.test_malformed_proposals_field_yields_no_candidates` (currently ~lines 5316–5333)
- Test: same method (the change *is* the test)

**Interfaces:**
- Consumes: `api_main.get_job`, `api_main.GitHubClient`, `pr_review_issues.create_review_issues(job_id, proposal_ids, token)`
- Produces: updated test that asserts empty `created`/`proposals` **and** that `GitHubClient` is never constructed

- [ ] **Step 1: Replace the test method body with the guarded version**

In `backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py`, replace `test_malformed_proposals_field_yields_no_candidates` with:

```python
    def test_malformed_proposals_field_yields_no_candidates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-list pending_issue_proposals field degrades to no candidates and
        never constructs a GitHub client."""
        from software_engineering_team.api import coding_team_main as api_main
        from software_engineering_team.api import pr_review_issues

        job = {
            "github_context": {"owner": "o", "repo": "r", "pr_number": 1, "pr_url": "u"},
            "status": "completed",
            "review_summary": {"pending_issue_proposals": "not-a-list"},
        }
        monkeypatch.setattr(api_main, "get_job", lambda *_a, **_k: job)

        def _fail_client(**_k):
            raise AssertionError(
                "GitHubClient must not be constructed for malformed proposals"
            )

        monkeypatch.setattr(api_main, "GitHubClient", _fail_client)
        out = pr_review_issues.create_review_issues("job1", ["p0"], token="t")
        assert out["created"] == []
        assert out["proposals"] == []
```

Keep surrounding methods unchanged. Match indentation of neighboring methods in `TestCreateReviewIssuesUnit`.

- [ ] **Step 2: Run the target test and confirm it passes**

From the worktree backend directory, using the main-repo venv:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-3320-malformed-proposals-github-client/backend
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestCreateReviewIssuesUnit::test_malformed_proposals_field_yields_no_candidates \
  -v
```

Expected: `1 passed`

- [ ] **Step 3: Smoke-check sibling construction-guard tests still pass**

```bash
PYTHONPATH=. /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestCreateReviewIssuesUnit::test_repo_mismatch_raises_before_any_issue \
  agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestCreateReviewIssuesUnit::test_malformed_proposals_field_yields_no_candidates \
  -v
```

Expected: `2 passed`

- [ ] **Step 4: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-3320-malformed-proposals-github-client
git add \
  backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py \
  docs/superpowers/specs/2026-07-30-malformed-proposals-github-client-design.md \
  docs/superpowers/plans/2026-07-30-malformed-proposals-github-client.md
git commit -m "$(cat <<'EOF'
Guard malformed-proposals review-issue test against GitHub client construction.

EOF
)"
```

Do not commit until the user explicitly asks to commit, unless they already approved committing as part of execution.

---

## Spec Coverage Self-Review

| Spec requirement | Task |
|------------------|------|
| Monkeypatch `GitHubClient` to raise on construction | Task 1 Step 1 |
| Update docstring for no-construction contract | Task 1 Step 1 |
| Keep empty `created`/`proposals` assertions | Task 1 Step 1 |
| No production code changes | Global Constraints + File Structure |
| Verify with the named pytest target | Task 1 Steps 2–3 |

Placeholder scan: none. Type consistency: uses existing `monkeypatch` / `api_main` / `pr_review_issues` symbols only.
