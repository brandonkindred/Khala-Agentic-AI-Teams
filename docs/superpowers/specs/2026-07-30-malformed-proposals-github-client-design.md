# Design: Enforce no-GitHub-client contract in malformed-proposals test

## Problem

`test_malformed_proposals_field_yields_no_candidates` asserts that a non-list
`pending_issue_proposals` field yields no candidates, and comments that no
GitHub client is needed — but it never stubs `GitHubClient`. If
`create_review_issues` is later refactored to construct the client before
filtering candidates, the test can attempt a real network call.

## Goal

Make the test enforce the “no client constructed” contract so a regression
fails loudly without network access.

## Non-goals

- No production code changes in `pr_review_issues.create_review_issues`
  (it already constructs `GitHubClient` only when `needed` is non-empty).
- No change to the existing empty-result assertions.

## Approach

Raise on construction (same pattern as `test_repo_mismatch_raises_before_any_issue`
in the same file):

1. Monkeypatch `api_main.GitHubClient` to a factory that raises
   `AssertionError` if called.
2. Update the test docstring to state that the client must not be constructed
   when proposals are malformed.

Rejected alternatives:

- Raise only on `create_issue` — weaker; allows construction without failing.
- Reuse `_github_issue_client(on_create=...)` — same weakness and implies a
  client is expected.

## Change site

`backend/agents/software_engineering_team/tests/test_coding_team_review_pr.py`
— method `TestCreateReviewIssuesUnit.test_malformed_proposals_field_yields_no_candidates`.

## Verification

Run:

```bash
pytest agents/software_engineering_team/tests/test_coding_team_review_pr.py::TestCreateReviewIssuesUnit::test_malformed_proposals_field_yields_no_candidates
```
