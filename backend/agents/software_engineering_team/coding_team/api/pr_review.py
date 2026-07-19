"""coding_team API — PR-review execution: admission gate, review run/body, submit, bisect, and review-liveness helpers.

Monkeypatched collaborators are dereferenced through the ``main`` module object
at call time so ``monkeypatch.setattr(main, ...)`` keeps taking effect after the
split; models are imported directly.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional

from software_engineering_team.coding_team.activity import ActivityBridge
from software_engineering_team.coding_team.api import main as _main
from software_engineering_team.coding_team.api.models import (
    ReviewPrRequest,
)
from software_engineering_team.coding_team.api.state import (
    _HEARTBEAT_CLOCK_SKEW_TOLERANCE_S,
)
from software_engineering_team.coding_team.github_source import (
    GitHubAPIError,
    GitHubRepoReader,
    anchor_to_first_file,
    annotate_duplicate_proposals,
    build_existing_comments,
    build_review_body,
    choose_event,
    duplicate_check_max_open_issues,
    group_similar_findings,
    inline_comment_to_timeline_body,
    is_within_diff,
    map_issues_to_comments,
    parse_valid_lines,
    partition_issues_by_existing_comments,
    proposal_from_findings,
    render_annotated_hunks,
    scrub_token_from_text,
    split_review_comments,
)
from software_engineering_team.coding_team.github_source.review_submit import (
    _post_file_comments,
    _submit_review,
)
from software_engineering_team.coding_team.job_store import (
    heartbeat_job,
)
from software_engineering_team.coding_team.models import JobStatus

logger = logging.getLogger(__name__)

_REVIEW_GUARD_HEARTBEAT_STALE_S = 300.0
_REVIEW_HEARTBEAT_INTERVAL_S = 30.0


def _review_job_heartbeat_live(job: Dict[str, Any]) -> bool:
    """True when the job's ``last_heartbeat_at`` says a worker is (plausibly) still alive.

    Preconditions: ``job`` is a job record dict (possibly empty).
    Postconditions: returns True iff ``last_heartbeat_at`` parses as an ISO timestamp
        whose age is in ``[-_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S,
        _REVIEW_GUARD_HEARTBEAT_STALE_S)`` — a stamp up to the skew tolerance in the
        future still counts as live (NTP drift), but a stamp further in the future is
        NOT live (implausible skew or corrupt data), mirroring
        ``_answer_wait_heartbeat_fresh``: a dead job with a far-future stamp must not
        block new reviews until that future time passes. A MISSING or unparseable
        stamp returns True (treated as live): the job service stamps
        ``last_heartbeat_at`` on every create/update, so an absent stamp means an
        unfamiliar store, and the guard must fail toward blocking duplicates, not toward
        starting them. Never raises.
    """
    raw = (job or {}).get("last_heartbeat_at")
    if not raw:
        return True
    try:
        beat = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - beat).total_seconds()
    return -_HEARTBEAT_CLOCK_SKEW_TOLERANCE_S <= age < _REVIEW_GUARD_HEARTBEAT_STALE_S


def _running_review_for_pr(owner: str, repo: str, pr_number: int) -> Optional[str]:
    """Return the job_id of a live non-terminal code-review job already reviewing this PR.

    Preconditions: ``owner``/``repo`` are non-empty repository coordinates; ``pr_number``
        is a positive PR number.
    Postconditions: returns the job_id of a non-terminal review job whose
        ``github_context`` matches (owner, repo, pr_number) — owner/repo
        case-insensitively, as GitHub treats them — and whose heartbeat is live per
        :func:`_review_job_heartbeat_live`; ``None`` when no such job exists. A matching
        job whose heartbeat went stale (its worker crashed before terminalizing it) is
        NOT returned — it must not block new reviews of the PR forever — and is
        best-effort marked ``failed`` so it stops surfacing as a zombie running review;
        a failure to mark it never propagates. Only review jobs carry ``pr_number`` in
        ``github_context`` (issue runs carry ``issue_number``), so matching on it never
        collides with an issue run. Raises only if the job-service scan itself fails.

    Cross-worker by construction: the scan reads the shared central job service (the same
    store ``_running_job_for_issue`` uses), so a review already running under a *different*
    uvicorn worker is still seen. Callers needing atomicity against concurrent admission
    must hold :func:`_pr_review_admission` around scan + job creation.

    Performance: O(active-jobs) linear scan over the small non-terminal set (mirrors
    ``_running_job_for_issue``); add an owner/repo/pr filter to ``list_jobs`` if that set
    ever grows materially.
    """
    for j in _main.list_jobs(active_only=True):
        ctx = (j or {}).get("github_context") or {}
        if (
            str(ctx.get("owner") or "").casefold() == owner.casefold()
            and str(ctx.get("repo") or "").casefold() == repo.casefold()
            and ctx.get("pr_number") == pr_number
        ):
            job_id = j.get("job_id")
            if _review_job_heartbeat_live(j):
                return job_id
            # Crash-orphaned: no worker has heartbeated it within the cutoff. Unblock
            # new reviews and terminalize the zombie (best-effort — the unblock matters,
            # the cleanup is cosmetic).
            logger.warning(
                "review job %s for %s/%s#%s has a stale heartbeat; treating as dead and marking failed",
                job_id,
                owner,
                repo,
                pr_number,
            )
            if job_id:
                try:
                    _main.update_job(
                        job_id,
                        status=JobStatus.FAILED.value,
                        error="review worker heartbeat went stale (process died mid-review)",
                    )
                    _main.update_review(
                        job_id,
                        status=JobStatus.FAILED.value,
                        error="review worker heartbeat went stale (process died mid-review)",
                        completed=True,
                    )
                except Exception:  # noqa: BLE001 - unblocking admission must not depend on cleanup
                    logger.warning(
                        "could not mark stale review job %s failed", job_id, exc_info=True
                    )
    return None


def _running_sibling_on_checkout(repo_path: str, own_job_id: str) -> Optional[Dict[str, Any]]:
    """Return another non-terminal job using this checkout, if any.

    Branch prep mutates the working tree (dirty-tree recovery commits files,
    `checkout -B` switches branches). Doing that under a job that is actively
    working would corrupt its run — the pre-recovery code's fail-fast dirty
    guard prevented this by accident, and recovery must not regress it. The
    job store can answer liveness (a deleted job is not running) even though
    it cannot answer leftover attribution — that remains the marker's job.

    Postconditions:
        - Returns the sibling job dict when one exists with a non-terminal
          status and the same checkout; None otherwise. Paths are compared
          canonically (symlinks, ``.``/``..``, trailing slashes resolved), so
          a sibling registered under a different spelling of the same
          checkout still matches. The caller's own job (``own_job_id``) is
          never reported.
    """
    target = os.path.realpath(repo_path)
    for j in _main.list_jobs(active_only=True):
        if not j or j.get("job_id") == own_job_id:
            continue
        sibling_path = j.get("repo_path")
        if sibling_path and os.path.realpath(sibling_path) == target:
            return j
    return None


def _infer_review_language(files: List[Any]) -> str:
    """Pick the dominant language label for the reviewer from the changed filenames.

    Postconditions:
        - Returns "typescript" when TS/JS-family files outnumber Python files,
          else "python" (the agent's two supported language buckets).
    """
    ts = sum(1 for f in files if f.filename.endswith((".ts", ".tsx", ".js", ".jsx")))
    py = sum(1 for f in files if f.filename.endswith(".py"))
    return "typescript" if ts > py else "python"


class ReviewCode(NamedTuple):
    """Result of assembling the reviewer's ``code`` input from a PR's diff."""

    code: str
    files_reviewed: int


# The PullRequestFile.status value GitHub reports for a deleted file. Named so
# the "is this file removed" check isn't a bare string literal at each call site.
_FILE_STATUS_REMOVED = "removed"

# Prefix of the whole-file-mode focus note, exposed so callers/tests can detect
# the note (e.g. in task_requirements) without duplicating its full wording.
WHOLE_FILE_FOCUS_NOTE_PREFIX = "Review focus:"


def _whole_file_focus(body: str) -> str:
    """Append a "tag pre-existing findings" instruction to ``body``.

    Whole-file review shows the reviewer complete files (for context and existing-
    code awareness), which also lets it see unchanged code. Rather than silently
    dropping problems it notices in that unchanged code, the reviewer is told to
    still report them but tag each issue with a ``pre_existing`` boolean, so the
    review flow can route pre-existing findings to GitHub-issue proposals (offered
    to a human) instead of posting them as comments on this PR.

    Preconditions:
        - ``body`` is a string (the PR body or "").

    Postconditions:
        - Returns ``body`` with the focus note appended (or the note alone when
          ``body`` is blank). The note starts with ``WHOLE_FILE_FOCUS_NOTE_PREFIX``
          and instructs the reviewer to emit a ``pre_existing`` field per issue.
    """
    note = (
        f"{WHOLE_FILE_FOCUS_NOTE_PREFIX} evaluate the changes this pull request makes. The complete "
        "file contents are provided for context. For EVERY issue you report, add a boolean field "
        "named `pre_existing` to the issue object:\n"
        "- Set `pre_existing: false` for a defect in the code this pull request ADDS or MODIFIES — "
        "these are the findings that matter for reviewing the PR.\n"
        "- Set `pre_existing: true` for a genuine bug you notice in PRE-EXISTING, UNCHANGED code "
        "that this pull request did not touch (an unrelated defect visible in the surrounding "
        "file). Still report such bugs — do not stay silent about them — but tag them so they are "
        "recorded separately instead of blamed on this change.\n"
        "Do not invent pre-existing issues to pad the review; only tag a finding `pre_existing: "
        "true` when it is a real defect in code outside this PR's change."
    )
    return f"{body}\n\n{note}" if body.strip() else note


# Max concurrent GitHub content fetches when assembling whole-file review input,
# so a large PR's head-file fetches run in a few waves instead of N serial
# round-trips on the review's critical path.
_HEAD_FETCH_PARALLELISM = 8


def _is_whole_file_reviewable(f: Any) -> bool:
    """True for a changed file eligible for whole-file review.

    Preconditions:
        - ``f`` is a ``PullRequestFile`` exposing ``.status`` and ``.patch``.

    Postconditions:
        - Returns True iff ``f`` is not removed and carries a diff ``patch`` (a
          text change, not a binary/oversized file). This is the SAME predicate
          ``_build_review_code`` applies, so the whole-file and hunk paths cover
          exactly the same set of files.
    """
    return f.status != _FILE_STATUS_REMOVED and bool(f.patch)


def _fetch_head_files(
    client: Any, owner: str, repo: str, files: List[Any], head_sha: str
) -> Dict[str, str]:
    """Fetch whole head-commit content for each reviewable changed file.

    Reviewing whole files (instead of only the diff hunks ``_build_review_code``
    renders) removes the "truncated at line N" false positive — a file no longer
    appears to end at its last hunk line — and gives the false-positive filter
    complete files to check findings against.

    Fetches the reviewable files (per :func:`_is_whole_file_reviewable`)
    concurrently (bounded by ``_HEAD_FETCH_PARALLELISM``), since the per-file
    GETs are independent.

    Postconditions:
        - Returns ``{filename: full_content}`` for every reviewable file whose
          head content fetched as non-blank text. A file whose content cannot be
          fetched (404, API error, blank, or a client without the capability) is
          simply omitted. Never raises: whole-file review is an enhancement, so a
          fetch failure degrades (the CALLER decides whole-file vs. hunk mode and
          only uses whole-file mode when EVERY reviewable file fetched, so a
          partial result never silently narrows review scope).
    """
    targets = [f for f in files if _is_whole_file_reviewable(f)]
    if not targets:
        return {}

    def _one(f: Any) -> tuple[str, Optional[str]]:
        try:
            content = client.get_file_contents(owner, repo, f.filename, head_sha)
        except Exception as e:  # noqa: BLE001 - a fetch failure degrades to hunk rendering, never fails review
            logger.warning(
                "PR review: could not fetch head content for %s@%s: %s", f.filename, head_sha, e
            )
            content = None
        return f.filename, content

    workers = min(_HEAD_FETCH_PARALLELISM, len(targets))
    if workers <= 1:
        results = [_one(f) for f in targets]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(_one, targets))
    return {name: content for name, content in results if content and content.strip()}


def _build_review_code(files: List[Any]) -> ReviewCode:
    """Assemble the line-annotated ``code`` input for the reviewer from the diff.

    Renders each changed file's diff hunks (added + context lines, new-file line
    numbers) — not whole files — so the reviewer is scoped to what the PR changed
    and cited line numbers align with the commentable-line map. Each file is wrapped
    in a ``### path ###`` block so the reviewer's coordinator can chunk large PRs.
    Built entirely from the already-fetched ``files`` payload (no extra requests).

    Every reviewable changed file is included — there is no cap on file count.
    The reviewer's coordinator bounds its own per-call prompts, so a large PR is
    chunked rather than truncated.

    Postconditions:
        - Returns ``ReviewCode(code, files_reviewed)`` covering every changed
          file with reviewable rendered content. Binary/removed files and files
          whose diff renders empty are not reviewable and are simply absent.
    """
    blocks: List[str] = []
    reviewed = 0
    for f in files:
        if not f.patch or f.status == _FILE_STATUS_REMOVED:
            continue
        rendered = render_annotated_hunks(f.patch)
        if not rendered:
            continue
        blocks.append(f"### {f.filename} ###\n{rendered}")
        reviewed += 1
    return ReviewCode("\n\n".join(blocks), reviewed)


# Optional dependency: author tagging for persisted review history. Imported once
# at module load behind a try/except so a missing/broken ``agent_console`` (or its
# transitive deps) can never break importing this API; ``_review_author`` falls
# back to "anonymous" when it is unavailable.
try:
    from agent_console.author import resolve_author as _resolve_author  # noqa: E402
except Exception:  # noqa: BLE001 - author tagging is optional, never fatal at import
    _resolve_author = None


def _review_author() -> str:
    """Resolve the author handle for a review row (best-effort, never raises).

    Postconditions:
        - Returns the resolved author handle, or ``"anonymous"`` when the optional
          ``agent_console`` author helper is unavailable or raises.
    """
    if _resolve_author is None:
        return "anonymous"
    try:
        return _resolve_author()
    except Exception:  # noqa: BLE001 - author tagging must never block a review
        return "anonymous"


# Serializes review admission (duplicate-scan + job creation) within this process; the
# Postgres advisory lock in _pr_review_admission extends the same mutual exclusion across
# worker processes when Postgres is configured.

_REVIEW_ADMISSION_LOCK = threading.Lock()


@contextlib.contextmanager
def _pr_review_admission(owner: str, repo: str, pr_number: int):
    """Mutual exclusion for PR-review admission (duplicate scan + job creation).

    Preconditions: ``owner``/``repo``/``pr_number`` identify the PR being admitted.
    Postconditions: while the ``with`` body runs, no other admission for the same PR can
        run — in this process via ``_REVIEW_ADMISSION_LOCK``, and across worker processes
        via a Postgres transaction-scoped advisory lock (``pg_advisory_xact_lock`` keyed
        on the casefolded ``owner/repo#pr``) when Postgres is configured. The advisory
        lock auto-releases when its transaction ends (body exit, exception, or connection
        death — crash-safe). When Postgres is unconfigured or the lock cannot be taken,
        degrades to the process-local lock alone (logged): single-worker admission stays
        fully serialized, and the residual cross-worker window is the pre-lock behavior,
        never worse. Exceptions from the body (e.g. the 409) propagate unchanged; lock
        acquisition itself never raises.

    Invariants: the process lock is always taken before (and released after) the advisory
        lock's transaction, so lock ordering is fixed and deadlock-free.
    """
    with _REVIEW_ADMISSION_LOCK, contextlib.ExitStack() as stack:
        try:
            from shared_postgres import (  # noqa: PLC0415 - optional dep path
                get_conn,
                is_postgres_enabled,
            )

            if is_postgres_enabled():
                conn = stack.enter_context(get_conn())
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    ("coding_team_review_pr", f"{owner}/{repo}#{pr_number}".casefold()),
                )
        except Exception:  # noqa: BLE001 - degrade to process-local admission, never block reviews
            stack.pop_all().close()
            logger.warning(
                "could not take cross-worker review admission lock for %s/%s#%s; "
                "falling back to process-local admission only",
                owner,
                repo,
                pr_number,
                exc_info=True,
            )
        yield


def _start_pr_review_thread(job_id: str, request: ReviewPrRequest, token: str) -> None:
    """Spawn the PR-review hook in a background thread.

    Indirection so tests can monkey-patch this to invoke the hook synchronously
    (mirrors ``_start_hook_thread``).
    """
    t = threading.Thread(
        target=_run_pr_review,
        args=(job_id, request, token),
        daemon=True,
    )
    t.start()


def _run_pr_review(job_id: str, request: ReviewPrRequest, token: str) -> None:
    """Background hook: review the PR, posting exactly one comment per finding.

    Postconditions:
        - On success the job is ``completed`` with ``github_pr_url`` set and one PR
          review submitted (REQUEST_CHANGES on critical/high findings from a PR the
          bot did not author, else COMMENT) whose body carries only the summary.
          Only findings about the code this PR added or modified are posted; a
          finding the reviewer tagged ``pre_existing`` (a bug in unchanged code the
          PR did not touch) is NOT commented — instead it is serialized into
          ``review_summary["pending_issue_proposals"]`` for a human to optionally
          file as a GitHub issue, and it drives neither the review event nor the
          "no issues" reaction.
          Every posted finding produces exactly one comment and no comment lists
          more than one finding: a finding tied to a changed line becomes an individual
          line-anchored inline comment carried in the single review (a stray
          off-diff line is bisected out so the rest stay anchored); a finding whose
          file changed but whose cited line is off-diff becomes an individual
          file-level review comment posted on the dedicated comments endpoint (the
          only one that accepts ``subject_type``); a standalone conversation
          comment is used only as a last resort, when even a file-level post is
          rejected, so no finding is dropped. A finding that cannot be posted at
          all marks the job ``failed`` (via ``comments_failed``); any unhandled
          exception likewise marks it ``failed`` and posts a (token-scrubbed) PR
          comment — never raises. (A best-effort failure to post the summary body
          alone does not fail the job, since the findings still post.)
    """
    owner, repo, pr_number = request.owner, request.repo, request.pr_number
    # Resolve the review engine BEFORE any GitHub call: a mis-wired process (no
    # provider installed) must fail the job immediately instead of burning REST
    # rate budget on PR metadata + diff assembly and then failing anyway.
    provider = _main.get_engine_provider()
    if provider is None:
        logger.error("PR review %s aborted: no engine provider configured", job_id)
        error = (
            "code review failed: no engine provider configured — SE's own "
            "_se_startup() hook must have run, since it installs the engine "
            "provider at startup"
        )
        _main.update_job(job_id, status=JobStatus.FAILED.value, phase="completed", error=error)
        # error=error (not just status_text) so the Code Review page's error column
        # is populated on this path exactly as _record_failure does everywhere else.
        _main.update_review(
            job_id,
            status=JobStatus.FAILED.value,
            status_text="No engine provider configured",
            error=error,
            completed=True,
        )
        # Tell the PR, not just the job store: the reviewer who invoked @khala-review
        # is watching the pull request and would otherwise wait forever on a job that
        # silently failed. The token is already in hand, so post a scrubbed one-liner
        # (best-effort — a GitHub outage must not turn this into an unhandled raise).
        try:
            with _main.GitHubClient(token=token) as client:
                _main._safe_comment(
                    client,
                    owner,
                    repo,
                    pr_number,
                    f"Code review could not run: {scrub_token_from_text(error)}",
                )
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            logger.warning("PR review %s: failed to post abort notice: %s", job_id, exc)
        return
    _main.update_job(
        job_id,
        status=JobStatus.RUNNING.value,
        phase="reviewing",
        status_text="Reviewing pull request",
    )
    _main.update_review(
        job_id, status=JobStatus.RUNNING.value, status_text="Reviewing pull request"
    )
    # Continuous liveness beat for the admission guard: job updates only land at phase
    # transitions, and a single review LLM call can run for minutes — without this, a
    # perfectly healthy review would look heartbeat-stale to _running_review_for_pr.
    # The context manager guarantees the beat stops on every exit path; on_error keeps
    # a job-service blip from killing the beat thread (or the review).
    from shared_concurrency import BackgroundHeartbeat  # noqa: PLC0415 - keep module import light

    review_hb = BackgroundHeartbeat(
        lambda: heartbeat_job(job_id),
        _REVIEW_HEARTBEAT_INTERVAL_S,
        name=f"review-heartbeat-{job_id}",
        beat_first=True,
        on_error=lambda exc: logger.warning("review heartbeat error for job %s: %s", job_id, exc),
    )
    # ``_run_pr_review_body`` already marks the job failed for exceptions raised
    # inside the review itself. This outer guard is the last line of defense for
    # anything that escapes it — heartbeat setup/teardown, or the body's own
    # last-resort finalize failing — so this function honors its "never raises"
    # contract and a hook exception can never leave the job wedged in "running".
    # Fully self-protected: the fallback finalize is best-effort and swallowed.
    try:
        with review_hb:
            _run_pr_review_body(job_id, request, token, owner, repo, pr_number, provider)
    except Exception as exc:  # noqa: BLE001 - the daemon thread must never die with the job left running
        logger.exception("PR review %s: unhandled exception escaped the review body", job_id)
        try:
            # phase="completed" (terminal), matching the success and provider-abort
            # paths — a failed job must not keep a mid-review "reviewing" phase.
            _finalize_review(
                job_id,
                JobStatus.FAILED,
                phase="completed",
                error=scrub_token_from_text(str(exc)),
            )
        except Exception:  # noqa: BLE001 - store unreachable; nothing more we can do, do not re-raise
            logger.exception(
                "PR review %s: last-resort finalize failed after escaped exception", job_id
            )


def _finalize_review(
    job_id: str,
    status: JobStatus,
    status_text: Optional[str] = None,
    *,
    phase: Optional[str] = None,
    github_pr_url: Optional[str] = None,
    review_summary: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Write a terminal review outcome to both the job store and the review row.

    Single source of the paired ``update_job`` + ``update_review`` write that
    every terminal path of the PR-review hook performs. The review row mirrors
    the job minus the job-only fields ``phase``/``github_pr_url``, plus its own
    ``completed=True``. Job is written before review, as at every original site.

    Preconditions:
        - ``status`` is a terminal status (``JobStatus.COMPLETED``/``JobStatus.FAILED``);
          each optional field is supplied only when the originating path set it.
    Postconditions:
        - ``update_job`` then ``update_review`` are called with exactly the
          non-``None`` fields supplied, ``status`` unwrapped to its plain
          ``.value`` for both (a single conversion point for both consumers);
          ``update_review`` additionally receives ``completed=True``.
    """
    job_kwargs: Dict[str, Any] = {"status": status.value}
    review_kwargs: Dict[str, Any] = {"status": status.value, "completed": True}
    if status_text is not None:
        job_kwargs["status_text"] = status_text
        review_kwargs["status_text"] = status_text
    if phase is not None:
        job_kwargs["phase"] = phase
    if github_pr_url is not None:
        job_kwargs["github_pr_url"] = github_pr_url
    if review_summary is not None:
        job_kwargs["review_summary"] = review_summary
        review_kwargs["review_summary"] = review_summary
    if error is not None:
        job_kwargs["error"] = error
        review_kwargs["error"] = error
    _main.update_job(job_id, **job_kwargs)
    _main.update_review(job_id, **review_kwargs)


def _complete_review_noop(
    client: Any,
    job_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr: Any,
    *,
    comment: str,
    status_text: str,
) -> None:
    """Post a no-op review comment and mark the job/review completed.

    The shared shape of the two early exits (no changed files / no reviewable
    content): a courtesy PR comment plus a ``completed`` terminal write.

    Preconditions:
        - ``client`` is an open ``GitHubClient``; ``pr`` carries ``html_url``.
    Postconditions:
        - A best-effort comment is posted and the job/review are finalized
          ``completed`` with ``status_text`` and the PR URL.
    """
    _main._safe_comment(client, owner, repo, pr_number, comment)
    _finalize_review(
        job_id, JobStatus.COMPLETED, status_text, phase="completed", github_pr_url=pr.html_url
    )


def _run_reviewer(
    provider: Any,
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    job_id: str,
    pr: Any,
    files: List[Any],
    code: str,
    head_files: Optional[Dict[str, str]] = None,
    repo_reader: Any = None,
) -> Optional[Any]:
    """Run the injected review engine, recording a failure and returning ``None`` on error.

    The PR reviewer is an injected engine (software_engineering_team owns it);
    coding_team calls it through the CodeEngineProvider so this package imports
    nothing from SE. Progress is coalesced through an ``ActivityBridge`` (shared
    schema, swallow-on-failure, clear-on-exit) whose sub-progress entry is always
    cleared on the way out so it never outlives the review.

    Preconditions:
        - ``provider`` was resolved before the first GitHub call.
        - When ``head_files`` is a non-empty ``{path: whole-file}`` mapping the
          review runs in whole-file mode (``pre_numbered=False``); otherwise it
          falls back to the pre-numbered diff-hunk ``code`` blob.
        - ``repo_reader`` is None or a ``RepoReader`` handed to the false-positive
          verifier so it can confirm existing repository files outside the diff.
    Postconditions:
        - Returns a truthy reviewer output on success. On any reviewer failure —
          an exception, OR a reviewer that returns ``None`` without raising —
          records the failure on the PR/job via ``_record_failure`` and returns
          ``None``. The caller returns on ``None``, so recording the failure here
          is what keeps the daemon-thread job from wedging in ``running`` (the
          pre-decomposition body reached the same terminal-failed state when a
          ``None`` output hit ``output.issues`` and raised into the outer except).
    """
    # last_activity_at is stamped centrally by the job service on every real
    # update, so these writes count as activity for stall detection.
    pr_bridge = ActivityBridge(
        lambda **kw: _main.update_job(job_id, **kw),
        agent="code_review",
        label=f"Reviewing PR #{pr_number}",
    )
    # One call, two modes: only the source shape (files/pre_numbered) and, in
    # whole-file mode, a "focus on the change" requirement differ; everything else
    # is shared, so a new kwarg cannot silently diverge between the branches.
    common = dict(
        repo_reader=repo_reader,
        task_description=f"Review pull request #{pr_number}: {pr.title}",
        language=_infer_review_language(files),
        progress_callback=pr_bridge,
    )
    if head_files:
        # Whole-file review: the reviewer sees complete files (no hunk-end
        # "truncation"), and the false-positive filter (via repo_reader) can
        # confirm existing files a finding claims are missing. Because it now sees
        # unchanged code too, steer it to only raise issues about the change and
        # treat the rest as context — otherwise it would comment on pre-existing,
        # unchanged code the PR never touched.
        mode_kwargs: Dict[str, Any] = dict(
            files=head_files,
            pre_numbered=False,
            task_requirements=_whole_file_focus(pr.body or ""),
        )
    else:
        # _build_review_code renders every line with its original line-number
        # prefix; declaring pre_numbered here (instead of letting the reviewer
        # sniff the format) keeps issue lines verbatim.
        mode_kwargs = dict(code=code, pre_numbered=True, task_requirements=pr.body or "")
    try:
        output = provider.run_pr_code_review(**common, **mode_kwargs)
    except Exception as e:  # noqa: BLE001 - any reviewer failure fails the job cleanly
        logger.exception("PR review agent failed: %s", e)
        # A reviewer-side failure (LLM outage, unrecoverable exhaustion, etc.) is
        # not a code defect: record the detail in the job store but never post the
        # raw exception on the PR — degrade to a quiet, re-runnable outage.
        # str(e) is "" for a bare zero-arg exception (e.g. a durable-review
        # client-side wait timing out with no attached detail); recording just
        # "code review failed: " is useless for later triage, so fall back to
        # naming the exception type when it carries no message of its own.
        detail = str(e) if e.args else f"{type(e).__name__} (no error message)"
        _main._record_review_outage(
            client, owner, repo, pr_number, job_id, f"code review failed: {detail}"
        )
        return None
    finally:
        # Clear so a stale sub-progress entry never outlives the review itself.
        pr_bridge.clear()
    if output is None:
        # A reviewer that returns no output (rather than raising) must not slip
        # through as a silent success — the caller returns on None with no
        # terminal write, which would wedge the job in "running" forever.
        logger.error("PR review agent returned no output for PR #%s", pr_number)
        _main._record_review_outage(
            client,
            owner,
            repo,
            pr_number,
            job_id,
            "code review failed: reviewer returned no output",
        )
        return None
    return output


def _fetch_existing_comments(client: Any, owner: str, repo: str, pr_number: int) -> List[Any]:
    """Best-effort fetch of every comment already on the PR, for de-duplicating findings.

    Preconditions:
        - ``client`` is an open ``GitHubClient``.
    Postconditions:
        - Returns ``build_existing_comments(...)`` over the PR's existing
          review comments, resolved-thread ids, and standalone conversation
          comments. Any failure fetching any of the three (REST error,
          transport error, or a GraphQL-lookup failure already degraded to an
          empty set by ``get_resolved_review_thread_comment_ids`` itself) is
          logged as a warning and degrades to ``[]`` — this lookup must never
          fail an otherwise-working review; a failure here only means findings
          are neither dropped nor cross-referenced on this run, same as a PR
          with no existing comments at all.
    """
    try:
        review_comments = client.list_review_comments(owner, repo, pr_number)
        resolved_ids = client.get_resolved_review_thread_comment_ids(owner, repo, pr_number)
        issue_comments = client.list_issue_comments(owner, repo, pr_number)
        return build_existing_comments(review_comments, resolved_ids, issue_comments)
    except GitHubAPIError as e:
        logger.warning("Could not fetch existing comments for PR #%s: %s", pr_number, e)
        return []


def _detect_duplicate_proposals(
    proposals: List[Dict[str, Any]], client: Any, owner: str, repo: str, pr_number: int
) -> List[Dict[str, Any]]:
    """Annotate pre-existing-finding proposals against the repo's open issues, fail-safe.

    Duplicate-detection is an enhancement to what the human is offered to file, not
    a correctness requirement of the review itself, so no failure here is allowed
    to fail the whole PR review.

    Preconditions:
        - ``client`` is an open ``GitHubClient``. ``proposals`` is fresh from
          ``proposal_from_findings`` — none carry ``matched_existing`` yet.
    Postconditions:
        - Returns ``proposals`` unchanged when empty (no GitHub call made). Otherwise
          fetches up to ``duplicate_check_max_open_issues()`` of the repo's open
          issues and returns ``annotate_duplicate_proposals(proposals, open_issues)``.
        - A failure listing open issues (network, auth, rate-limit) degrades to an
          empty ``open_issues`` list -- mirroring the GitHubAPIError-tolerant
          degrade-and-continue pattern ``_fetch_existing_comments`` already uses --
          so annotation still runs (against no issues, i.e. nothing matches) rather
          than being skipped.
        - A failure in ``annotate_duplicate_proposals`` itself instead falls back to
          marking every proposal ``matched_existing: False`` by hand -- mirroring
          that function's own "no match" branch -- so downstream consumers
          (frontend, ``create_review_issues``) still always see the field.
        - Never raises.
    """
    if not proposals:
        return proposals
    try:
        cap = duplicate_check_max_open_issues()
        open_issues = list(itertools.islice(client.list_open_issues(owner, repo), cap))
        if len(open_issues) >= cap:
            logger.info(
                "PR review #%s: duplicate-detection capped at %d open issues; "
                "some older open issues were not considered",
                pr_number,
                cap,
            )
    except GitHubAPIError as e:
        logger.warning(
            "PR review #%s: could not list open issues for duplicate-detection: %s",
            pr_number,
            e,
        )
        open_issues = []
    except Exception:  # noqa: BLE001 - duplicate-detection must never fail the review
        logger.warning(
            "PR review #%s: unexpected error listing open issues for duplicate-detection",
            pr_number,
            exc_info=True,
        )
        open_issues = []
    try:
        return annotate_duplicate_proposals(proposals, open_issues)
    except Exception:  # noqa: BLE001 - duplicate-detection must never fail the review
        logger.warning(
            "PR review #%s: duplicate annotation failed, proceeding without duplicate detection",
            pr_number,
            exc_info=True,
        )
        # annotate_duplicate_proposals's own "no match" branch does exactly this
        # ({**p, "matched_existing": False}); mirrored here as a safety net so a
        # bug inside that function still leaves every proposal carrying the field.
        return [{**p, "matched_existing": False} for p in proposals]


def _run_pr_review_body(
    job_id: str,
    request: ReviewPrRequest,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    provider: Any,
) -> None:
    """The review hook's body, extracted so the heartbeat wrapper stays trivially correct.

    Preconditions: caller has marked the job/review ``running``, holds a live
        heartbeat for ``job_id``, and passes the already-resolved (non-None)
        engine ``provider`` — the wrapper fails the job before any GitHub call
        when no provider is installed.
    Postconditions: identical to :func:`_run_pr_review` (this IS that contract's
        implementation; see its docstring). Never raises.
    """
    try:
        with _main.GitHubClient(token=token) as client:
            pr = client.get_pull_request(owner, repo, pr_number)
            files = client.get_pull_request_files(owner, repo, pr_number)
            if not files:
                _complete_review_noop(
                    client,
                    job_id,
                    owner,
                    repo,
                    pr_number,
                    pr,
                    comment="Code review: no changed files to review.",
                    status_text="No changed files to review",
                )
                return

            valid_by_path = {f.filename: parse_valid_lines(f.patch) for f in files}
            # Lines the PR actually ADDED — narrower than valid_by_path, which also
            # includes unchanged context lines (so a finding cited on one can still
            # be anchored inline per map_issues_to_comments). Only an added line can
            # override a reviewer's pre_existing tag below: a genuine pre-existing
            # bug on an unchanged context line inside a modified hunk must still
            # route to a proposal, not a PR comment.
            changed_by_path = {
                f.filename: parse_valid_lines(f.patch, added_only=True) for f in files
            }
            code, files_reviewed = _build_review_code(files)
            if not code:
                _complete_review_noop(
                    client,
                    job_id,
                    owner,
                    repo,
                    pr_number,
                    pr,
                    comment="Code review: no reviewable file content.",
                    status_text="No reviewable file content",
                )
                return

            # Prefer whole-file review over diff hunks: complete files remove the
            # hunk-boundary "truncation" false positive, and the repo reader lets
            # the false-positive filter confirm existing (unchanged) repo files a
            # finding claims are missing. Use whole-file mode ONLY when EVERY
            # reviewable file fetched — a partial fetch would silently drop the
            # un-fetched files, so fall back to the hunk ``code`` blob (which
            # covers every changed file from already-fetched patch data).
            reviewable = {f.filename for f in files if _is_whole_file_reviewable(f)}
            head_files = _fetch_head_files(client, owner, repo, files, pr.head_sha)
            whole_file = bool(head_files) and set(head_files) == reviewable
            if not whole_file and head_files:
                logger.info(
                    "PR review #%s: fetched %d/%d whole files; falling back to hunk review "
                    "for full coverage",
                    pr_number,
                    len(head_files),
                    len(reviewable),
                )
            if whole_file:
                files_reviewed = len(head_files)
            repo_reader = GitHubRepoReader(client, owner, repo, pr.head_sha)

            try:
                reviewer_login = client.get_authenticated_login()
            except GitHubAPIError as e:
                # Non-fatal: reviewer_login only feeds choose_event() (the self-PR
                # REQUEST_CHANGES -> COMMENT downgrade). Log so the degradation is
                # not silent, then fall back to "" and let the review proceed — a
                # genuine bad/permission-limited token already surfaces on the PR
                # fetch above and on review submission below.
                logger.warning("Could not resolve reviewer login for PR #%s: %s", pr_number, e)
                reviewer_login = ""

            output = _run_reviewer(
                provider,
                client,
                owner,
                repo,
                pr_number,
                job_id,
                pr,
                files,
                code,
                head_files=head_files if whole_file else None,
                repo_reader=repo_reader,
            )
            if output is None:
                return

            # Split the reviewer's findings by whether they belong to this PR.
            # Defects in the code the PR added or modified drive the review
            # (comments + REQUEST_CHANGES); pre-existing bugs the reviewer noticed
            # in unchanged code are NOT posted on this PR — they become GitHub-issue
            # proposals a human approves later on the Code Review page. A finding
            # without the tag defaults to a PR finding (hunk-mode reviews never
            # tag, so they behave exactly as before). The LLM's self-reported tag
            # is not trusted unconditionally: a finding whose file/line is verified
            # to be a line this PR actually ADDED (per is_within_diff against
            # changed_by_path — deliberately narrower than valid_by_path, which
            # would also match unchanged context lines) cannot legitimately be
            # "pre-existing, unchanged code", so a mistagged pre_existing=true is
            # overridden back to a PR finding rather than silently skipping review.
            pr_issues: List[Any] = []
            preexisting_issues: List[Any] = []
            for i in output.issues:
                if getattr(i, "pre_existing", False) and not is_within_diff(i, changed_by_path):
                    preexisting_issues.append(i)
                else:
                    pr_issues.append(i)
            # Similar findings (same category, near-identical description — e.g. the
            # same "bare import" pattern flagged across several files) are combined
            # into one proposal so a human is offered one issue per kind of problem,
            # not one per occurrence.
            finding_groups = group_similar_findings(preexisting_issues)
            proposals = [proposal_from_findings(g, idx) for idx, g in enumerate(finding_groups)]
            proposals = _detect_duplicate_proposals(proposals, client, owner, repo, pr_number)

            # Recognize findings that duplicate a comment already on the PR (from a
            # prior review run, or a human), so an evolving PR does not accumulate
            # repeat comments every time it is re-reviewed. A match against an
            # already-RESOLVED comment is dropped (requirement: already addressed);
            # a match against a still-open comment is kept but cross-referenced (see
            # map_issues_to_comments/anchor_to_first_file below) instead of posted as
            # an unexplained duplicate. The fetch is best-effort: any failure yields
            # [], so this never turns a working review into a failed one. Skipped
            # entirely on a clean review (no findings): there is nothing to
            # de-duplicate, so the up-to-three API calls the fetch makes would be
            # pure waste.
            if pr_issues:
                existing_comments = _fetch_existing_comments(client, owner, repo, pr_number)
                pr_issues, addressed_issues, existing_by_issue = (
                    partition_issues_by_existing_comments(pr_issues, existing_comments)
                )
            else:
                addressed_issues, existing_by_issue = [], {}

            comments, leftovers = map_issues_to_comments(
                pr_issues, valid_by_path, existing_by_issue
            )

            # Re-anchor leftover findings (file not in diff) as file-level
            # comments on the first changed file in the diff, so they travel as
            # review comments rather than standalone top-level PR conversation
            # comments.  anchor_to_first_file returns None only when valid_by_path
            # is empty — but we already exit early in that case, so the filter is
            # just a safety net.
            anchored_leftovers = [
                anchor_to_first_file(issue, valid_by_path, existing_by_issue.get(id(issue)))
                for issue in leftovers
            ]
            comments = comments + [c for c in anchored_leftovers if c is not None]

            # Two GitHub endpoints, two shapes. Line-anchored comments ride the
            # single review; file-level comments (subject_type="file") go on the
            # dedicated review-comments endpoint, which the reviews array rejects
            # (it does not accept subject_type). Splitting them keeps one bad
            # file-level entry from collapsing the whole review to the fallback.
            line_comments, file_comments = split_review_comments(comments)

            # output.summary/spec_compliance_notes are synthesized by the reviewer
            # engine from its FULL issue list (software_engineering_team's
            # synthesize_review_findings runs before this split), so the narrative
            # can describe a pre-existing finding's theme/location even though its
            # own per-issue comment is suppressed. When any finding was
            # pre-existing, fall back to build_review_body's deterministic "N
            # findings reported" text instead of risking that leak.
            body = build_review_body(
                output.summary if not preexisting_issues else "",
                output.spec_compliance_notes if not preexisting_issues else "",
                issue_count=len(pr_issues),
            )
            event = choose_event(pr_issues, author=pr.author, reviewer=reviewer_login)

            # Submit line-anchored comments in the review, bisecting out any
            # off-diff line so the rest stay anchored. Whatever GitHub still
            # rejects is demoted to a file-level comment below.
            try:
                dropped_lines = _submit_review(
                    client, owner, repo, pr_number, pr.head_sha, body, event, line_comments
                )
            except GitHubAPIError:
                # _submit_review raises only when the whole submission failed
                # (a non-422 on line comments, or every summary-only attempt for
                # a no-line-comment review). Tolerate it ONLY when there are
                # file-level findings still to post — the summary is then a
                # best-effort courtesy and those findings carry the review (and
                # surface any real error themselves). Otherwise nothing reached
                # GitHub, so let the failure mark the job failed rather than
                # report a hollow success.
                if line_comments or not file_comments:
                    raise
                logger.warning("Summary-only review failed; posting file-level findings only")
                dropped_lines = []
            inline_count = len(line_comments) - len(dropped_lines)

            # File-level comments (mapped + re-anchored leftovers) and any
            # bisected-out line comments (demoted, keeping the file anchor) each
            # go on the dedicated endpoint. A rejected line comment falls through
            # as its original entry, so the standalone fallback still names
            # ``path:line``.
            file_comment_count, standalone = _post_file_comments(
                client, owner, repo, pr_number, pr.head_sha, file_comments + dropped_lines
            )

            # Only truly-unpostable findings fall through to standalone comments.
            standalone_bodies = [inline_comment_to_timeline_body(c) for c in standalone]
            comments_failed = sum(
                0 if _main._safe_comment(client, owner, repo, pr_number, body) else 1
                for body in standalone_bodies
            )

            comment_findings = len(standalone)
            # Break the posted PR findings down by severity so the Code Review page can
            # show per-review severity metrics. Aggregated over ``pr_issues`` (the
            # findings actually posted on this PR); pre-existing-bug proposals are
            # excluded. Only the five documented levels are counted and only non-zero
            # levels are emitted, so the map stays compact. Its values sum to
            # ``total_issues`` for findings whose severity is recognized; a finding
            # with an unknown or blank severity is counted in ``total_issues`` but not
            # bucketed here.
            recognized_severities = ("critical", "high", "medium", "low", "info")
            severity_counts: dict[str, int] = {}
            for issue in pr_issues:
                lvl = str(getattr(issue, "severity", "")).lower()
                if lvl in recognized_severities:
                    severity_counts[lvl] = severity_counts.get(lvl, 0) + 1
            review_summary = {
                "total_issues": len(pr_issues),
                "inline_comments": inline_count,
                "file_comments": file_comment_count,
                "comment_findings": comment_findings,
                "comments_failed": comments_failed,
                "event": event,
                "files_reviewed": files_reviewed,
                "severity_counts": severity_counts,
                # Findings that matched an already-RESOLVED existing PR comment and
                # so were dropped rather than re-posted (see
                # partition_issues_by_existing_comments above).
                "addressed_issues_dropped": len(addressed_issues),
                # Pre-existing bugs the reviewer flagged in unchanged code, offered
                # to a human on the Code Review page as GitHub-issue candidates.
                # Not posted on this PR. Each carries a stable ``id``. ``issue_url``/
                # ``issue_number`` start unset, UNLESS annotate_duplicate_proposals
                # already matched the finding to an existing open issue -- in which
                # case they're pre-filled with that issue's identity and
                # ``matched_existing`` is True, so the proposal is never offered as
                # a fresh "create issue" candidate.
                "pending_issue_proposals": proposals,
            }
            if comments_failed:
                # Some findings could not be posted as their own comment; the
                # review (inline comments + body) is already submitted, but the
                # contract "one comment per finding" is broken — surface it as a
                # failure rather than reporting completion.
                err = (
                    f"{comments_failed} of {comment_findings} finding comment(s) "
                    "could not be posted"
                )
                # Notify on the PR itself: the dropped findings no longer live in
                # the review body, so without this the author has no signal on
                # GitHub that part of the review is missing.
                _main._safe_comment(
                    client,
                    owner,
                    repo,
                    pr_number,
                    f"Code review incomplete: {err}. See the coding team job for details.",
                )
                _finalize_review(
                    job_id,
                    JobStatus.FAILED,
                    err,
                    github_pr_url=pr.html_url,
                    review_summary=review_summary,
                    error=err,
                )
                return
            status_text = (
                f"Review posted: {len(pr_issues)} finding(s), "
                f"{inline_count} inline, {file_comment_count} file-level, "
                f"{comment_findings} comment(s), event={event}"
            )
            if proposals:
                noun = "bug" if len(proposals) == 1 else "bugs"
                status_text += f"; {len(proposals)} pre-existing {noun} to review"
            if addressed_issues:
                noun = "finding" if len(addressed_issues) == 1 else "findings"
                status_text += f"; {len(addressed_issues)} already-addressed {noun} skipped"
            # React only when the PR's OWN change is clean. Pre-existing findings
            # are about unchanged code, so they do not withhold the "looks good"
            # signal for the change under review.
            if not pr_issues:
                _react_to_pr(client, owner, repo, pr_number)
            _finalize_review(
                job_id,
                JobStatus.COMPLETED,
                status_text,
                phase="completed",
                github_pr_url=pr.html_url,
                review_summary=review_summary,
            )
    except Exception as review_exc:  # noqa: BLE001 - any failure must mark the job, never wedge it
        # The hook runs in a daemon thread; if we let an exception escape, the thread
        # dies and the job is stuck in "running" forever. Mark it failed (mirroring
        # post_run) and post a best-effort, token-scrubbed PR comment.
        logger.exception("PR review hook failed: %s", review_exc)
        try:
            with _main.GitHubClient(token=token) as client:
                # Same graceful-degradation contract as the reviewer paths: record
                # the detail in the store, keep the raw exception off the PR.
                _main._record_review_outage(
                    client, owner, repo, pr_number, job_id, f"code review failed: {review_exc}"
                )
        except Exception:  # noqa: BLE001 - the status update below is the last resort
            # Safety net: ``_record_review_outage`` above may already have marked
            # the job/review failed, but if it raised (e.g. the GitHub client
            # itself failed) these direct updates ensure the job never wedges in
            # "running". Both writes are idempotent, so a duplicate update here
            # (when _record_review_outage had partly succeeded) is harmless.
            # ``review_exc`` is the original review failure (the inner except has
            # no exception of its own); surface it on both the job and review row.
            # Wrapped so a failing store write (the very reason we reached this
            # last resort) cannot escape and kill the daemon thread — the outer
            # ``_run_pr_review`` guard would catch it, but keeping the body
            # self-consistent means it never depends on that.
            safe_err = scrub_token_from_text(str(review_exc))
            try:
                _finalize_review(job_id, JobStatus.FAILED, phase="completed", error=safe_err)
            except Exception:  # noqa: BLE001 - store unreachable; nothing more we can do
                logger.exception("PR review %s: last-resort finalize failed", job_id)


def _react_to_pr(client: _main.GitHubClient, owner: str, repo: str, pr_number: int) -> None:
    """Best-effort +1 reaction on the PR itself, celebrating a clean review.

    Postconditions: adds a "+1" reaction to PR #``pr_number``. Never raises — a
    failure here (rate limit, missing scope, transport error) must not turn an
    otherwise-successful clean review into a failed job.
    """
    try:
        client.create_issue_reaction(owner, repo, pr_number, content="+1")
    except Exception:  # noqa: BLE001 - reaction is a courtesy signal only
        logger.warning("Could not add +1 reaction to PR #%s", pr_number, exc_info=True)


# ---------------------------------------------------------------------------
# Pre/post hooks for the GitHub flow (no orchestrator changes)
# ---------------------------------------------------------------------------


def _safe_comment(
    client: _main.GitHubClient, owner: str, repo: str, number: int, body: str
) -> bool:
    """Best-effort issue comment; never blocks the job on a failed comment.

    Body is scrubbed to redact tokens that might have leaked from git stderr.

    Postconditions:
        - Returns True when the comment was posted, False when GitHub rejected it.
          Never raises — callers that must not drop a finding inspect the result.
    """
    try:
        client.add_issue_comment(owner, repo, number, scrub_token_from_text(body))
        return True
    except GitHubAPIError as e:
        logger.warning("Failed to comment on issue #%s: %s", number, e)
        return False
