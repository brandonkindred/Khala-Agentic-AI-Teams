"""GitHub Reviews-API submission mechanics, with 422-bisection recovery.

GitHub's Reviews API rejects an entire review (422) when a single inline
comment lands off the diff, or when the review requests changes on the bot's
own PR. These helpers implement the recovery ladder the ``/review-pr`` flow
relies on to never silently drop a finding when that happens:

- ``_post_file_comments`` — post file-level review comments (mapped +
  re-anchored leftovers), demoting only 422-rejected anchors to standalone.
- ``_post_review`` — the shared ``create_pull_request_review`` call
  construction, with no error-handling policy of its own.
- ``_try_review`` — submit one PR review, reporting a recoverable 422 rather
  than raising for it.
- ``_post_summary_only`` — post the summary body alone across candidate
  events, for a review with no line-anchored findings.
- ``_submit_review`` — the line-anchored review's full submission strategy:
  try the chosen event, retry as ``COMMENT`` on a 422, then post the summary
  separately and bisect the comments so only the genuinely-bad lines are
  dropped.
- ``_bisect_submit`` — recursively halve a batch of line-anchored comments
  until only the ones GitHub actually rejects are left out.

Only a 422 is ever treated as a recoverable "bad line/anchor"; any other
status (permission, rate-limit, transport, server) propagates so the job
fails loudly instead of silently degrading.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from software_engineering_team.api.coding_team_state import (
    _BISECT_CONTINUATION_BODY,
    _HTTP_UNPROCESSABLE,
)

from .client import GitHubAPIError, GitHubClient, scrub_token_from_text

logger = logging.getLogger(__name__)


def _post_file_comments(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    entries: List[Dict[str, Any]],
) -> tuple[int, List[Dict[str, Any]]]:
    """Post file-level review comments, demoting only 422-rejected anchors to standalone.

    File-level comments (mapped + re-anchored leftovers) and any bisected-out line
    comments (demoted, keeping the file anchor) each go on the dedicated
    review-comments endpoint.

    Preconditions:
        - ``entries`` are comment dicts that may carry ``path``/``body``.
    Postconditions:
        - Returns ``(file_comment_count, standalone)``: the count posted as
          file-level comments, and the entries that must fall back to standalone
          timeline comments (no path, or a 422 bad-anchor rejection). Any non-422
          ``GitHubAPIError`` propagates so the job fails loudly.
    """
    file_comment_count = 0
    standalone: List[Dict[str, Any]] = []
    for comment in entries:
        path = comment.get("path")
        if path:
            try:
                client.create_review_comment(
                    owner=owner,
                    repo=repo,
                    number=pr_number,
                    commit_id=head_sha,
                    path=path,
                    body=scrub_token_from_text(comment.get("body", "")),
                    subject_type="file",
                )
                file_comment_count += 1
                continue
            except GitHubAPIError as e:
                # Only a 422 (bad anchor) is worth demoting to a standalone
                # comment; any other status (permission, rate-limit, transport,
                # server) is a real failure that must propagate so the job fails
                # loudly instead of silently degrading.
                if e.status != _HTTP_UNPROCESSABLE:
                    raise
                # Last resort: fall through to standalone posting (rare).
        standalone.append(comment)
    return file_comment_count, standalone


def _post_review(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    event: str,
    comments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Submit one PR review via the Reviews API, with no error-handling policy of its own.

    The single ``client.create_pull_request_review`` call construction shared by
    ``_try_review``, ``_post_summary_only``, and ``_submit_review``'s bisect
    fallback. Each caller wraps this in its own ``try``/``except`` to apply its
    distinct recovery policy (422-only re-raise, tolerate-until-events-exhausted,
    swallow-and-log); this function does not catch anything.

    Postconditions:
        - Returns whatever ``client.create_pull_request_review`` returns.
          Propagates any ``GitHubAPIError`` unchanged.
    """
    return client.create_pull_request_review(
        owner=owner,
        repo=repo,
        number=pr_number,
        commit_id=head_sha,
        body=body,
        event=event,
        comments=comments,
    )


def _try_review(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    event: str,
    comments: List[Dict[str, Any]],
) -> bool:
    """Submit one PR review, returning False on a recoverable 422 and re-raising otherwise.

    Only a 422 (validation — a bad diff line, or REQUEST_CHANGES on the bot's own
    PR) is recoverable by dropping the event/comments; any other status
    (permission, rate-limit, transport, server) is a real failure re-raised so the
    caller fails loudly instead of silently degrading.

    Preconditions:
        - ``comments`` are already token-scrubbed review-comment dicts.
    Postconditions:
        - Returns True when GitHub accepted the review; False (after logging) on a
          422. Raises ``GitHubAPIError`` for any non-422 status.
    """
    try:
        _post_review(client, owner, repo, pr_number, head_sha, body, event, comments)
        return True
    except GitHubAPIError as e:
        if e.status != _HTTP_UNPROCESSABLE:
            raise
        logger.warning(
            "PR review submit failed (event=%s, comments=%d): %s", event, len(comments), e
        )
        return False


def _post_summary_only(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    events: List[str],
) -> List[Dict[str, Any]]:
    """Post the summary body alone across candidate events; raise if all attempts fail.

    Used when a review carries no line-anchored findings. Unlike the inline-comment
    path, EVERY ``GitHubAPIError`` is tolerated per event (not just 422): the caller
    decides whether a total failure is fatal (a zero-finding review whose only
    output is this summary) or a best-effort courtesy (file-level findings still
    posted separately).

    Preconditions:
        - ``events`` is a non-empty ordered list of candidate review events.
    Postconditions:
        - Returns ``[]`` as soon as one event succeeds; raises the last
          ``GitHubAPIError`` when every event failed.
    """
    last_exc: Optional[GitHubAPIError] = None
    for ev in events:
        try:
            _post_review(client, owner, repo, pr_number, head_sha, body, ev, [])
            return []
        except GitHubAPIError as e:
            logger.warning("PR summary-only review failed (event=%s): %s", ev, e)
            last_exc = e
    assert last_exc is not None
    raise last_exc


def _submit_review(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    body: str,
    event: str,
    comments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Submit the line-anchored review, bisecting out any off-diff comment.

    GitHub rejects the whole review (422) if it requests changes on the bot's own
    PR, or if any single inline comment lands off the diff. So: try the chosen
    event with all comments; on failure retry as COMMENT keeping them (handles the
    self-PR case without losing inline feedback). If the full batch still 422s, a
    stray bad line is poisoning it — post the summary on its own so it is not lost,
    then bisect the comments so only the genuinely-bad lines are dropped while the
    rest stay anchored in (smaller) COMMENT reviews. Only a 422 is treated as a
    bad line; any other status (permission, rate-limit, transport, server) is a
    real failure and is re-raised rather than silently degraded.

    Preconditions:
        - Every entry in ``comments`` is line-anchored (carries ``line``);
          file-level comments are posted by the caller on the dedicated endpoint.
    Postconditions:
        - Every comment GitHub accepts is submitted inline (in one review on the
          happy path, or across bisected COMMENT reviews when a bad line forced a
          split). The review body and every comment body are token-scrubbed before
          submission (LLM output may echo a secret from the reviewed code). Returns
          the original comments GitHub rejected with a 422 even when submitted alone
          (``[]`` when all were posted); the caller demotes those to file-level
          comments. Raises ``GitHubAPIError`` for any non-422 status so the job
          fails loudly instead of masking a real API failure.
        - When ``comments`` is empty this only posts the summary body; it returns
          ``[]`` on success and raises ``GitHubAPIError`` if every attempt fails,
          so the caller can fail a zero-finding review whose only output was the
          (un-postable) summary instead of reporting a hollow success.
    """
    # Scrub before anything leaves for GitHub: the body (LLM summary) and each
    # inline-comment body (LLM description/suggestion) can echo a token from the
    # reviewed code, just like the standalone comments _safe_comment scrubs. Pair
    # each scrubbed comment with its original so the dropped set returned to the
    # caller keeps the original identity (with its ``line``).
    body = scrub_token_from_text(body)
    pairs = [({**c, "body": scrub_token_from_text(c.get("body", ""))}, c) for c in comments]

    events = [event] if event == "COMMENT" else [event, "COMMENT"]

    if not pairs:
        # No line-anchored findings: this call only posts the summary body. If it
        # succeeds, nothing was dropped. If every attempt fails, raise so the
        # caller can decide: when file-level findings still post on the dedicated
        # endpoint the summary is a best-effort courtesy and its failure is
        # tolerated, but a zero-finding review whose only output is this summary
        # must surface as failed rather than report a hollow success.
        return _post_summary_only(client, owner, repo, pr_number, head_sha, body, events)

    # Happy path: one review carrying the summary body + every inline comment.
    # REQUEST_CHANGES degrades to COMMENT for the bot's own PR without losing the
    # comments. Only a 422 is recoverable (retry as COMMENT, then bisect below);
    # _try_review re-raises any other status so the job fails loudly.
    scrubbed = [p[0] for p in pairs]
    for ev in events:
        if _try_review(client, owner, repo, pr_number, head_sha, body, ev, scrubbed):
            return []

    # The full batch was rejected by a bad line. Post the summary on its own so it
    # is not lost, then bisect the comments to drop only the offending ones.
    try:
        _post_review(client, owner, repo, pr_number, head_sha, body, "COMMENT", [])
    except GitHubAPIError as e:
        # Best effort — the bisected comments below still carry the findings.
        logger.warning("PR review summary-only submit failed: %s", e)

    return _bisect_submit(client, owner, repo, pr_number, head_sha, pairs)


def _bisect_submit(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    pairs: List[tuple[Dict[str, Any], Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Post line-anchored comments as COMMENT reviews, bisecting on a 422.

    Used only after the full-batch review failed and the summary was posted
    separately, so each sub-review carries a continuation body rather than
    repeating the summary.

    Preconditions:
        - ``pairs`` is a non-empty list of ``(scrubbed_comment, original_comment)``
          tuples; both bodies are already token-scrubbed.
    Postconditions:
        - Submits one or more COMMENT reviews; every comment GitHub accepts is
          posted inline. Returns the original comments GitHub still rejects when a
          single comment is submitted on its own (``[]`` when all were posted).
    """
    # Only a 422 means a bad diff line worth bisecting out; _try_review re-raises
    # any other status rather than mistaking it for one stray off-diff comment.
    if _try_review(
        client,
        owner,
        repo,
        pr_number,
        head_sha,
        _BISECT_CONTINUATION_BODY,
        "COMMENT",
        [p[0] for p in pairs],
    ):
        return []
    if len(pairs) <= 1:
        return [p[1] for p in pairs]
    mid = len(pairs) // 2
    return _bisect_submit(client, owner, repo, pr_number, head_sha, pairs[:mid]) + _bisect_submit(
        client, owner, repo, pr_number, head_sha, pairs[mid:]
    )
