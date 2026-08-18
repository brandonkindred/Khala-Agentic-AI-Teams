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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from shared.concurrency import parallel_map
from shared.temporal.client import get_temporal_client
from software_engineering_team.activity import ActivityBridge
from software_engineering_team.api import coding_team_main as _main
from software_engineering_team.api.advisory_lock import advisory_lock
from software_engineering_team.api.coding_team_models import (
    ReviewPrRequest,
)
from software_engineering_team.api.coding_team_state import (
    _HEARTBEAT_CLOCK_SKEW_TOLERANCE_S,
)
from software_engineering_team.code_review_agent.change_surface import (
    ChangeSurface,
    build_change_surface_from_patches,
)
from software_engineering_team.github_source import (
    GitHubAPIError,
    GitHubRepoReader,
    annotate_duplicate_proposals,
    build_existing_comments,
    build_review_body,
    choose_event,
    duplicate_check_max_open_issues,
    format_issue_comment,
    group_similar_findings,
    inline_comment_to_timeline_body,
    is_within_diff,
    map_issues_to_comments,
    parse_removed_lines,
    parse_valid_lines,
    partition_issues_by_existing_comments,
    proposal_from_findings,
    render_annotated_hunks,
    render_removed_hunks,
    scrub_token_from_text,
    split_review_comments,
)
from software_engineering_team.github_source.review_submit import (
    _post_file_comments,
    _submit_review,
)
from software_engineering_team.job_store import (
    heartbeat_job,
)
from software_engineering_team.models import JobStatus

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
        NOT live (implausible skew or corrupt data): a dead job with a far-future stamp
        must not block new reviews until that future time passes. A MISSING or unparseable
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
        case-insensitively, as GitHub treats them, and ``pr_number`` via ``int()``
        coercion so a string-typed store value still matches — and whose heartbeat
        is live per :func:`_review_job_heartbeat_live`; ``None`` when no such job
        exists. A matching job whose heartbeat went stale (its worker crashed before
        terminalizing it) is NOT returned — it must not block new reviews of the PR
        forever — and is best-effort marked ``failed`` so it stops surfacing as a
        zombie running review; a failure to mark it never propagates. Only review
        jobs carry ``pr_number`` in ``github_context`` (issue runs carry
        ``issue_number``), so matching on it never collides with an issue run.
        Raises only if the job-service scan itself fails.

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
        try:
            stored_pr = int(ctx.get("pr_number"))
        except (TypeError, ValueError, OverflowError):
            continue
        if stored_pr != pr_number:
            continue
        if not (
            str(ctx.get("owner") or "").casefold() == owner.casefold()
            and str(ctx.get("repo") or "").casefold() == repo.casefold()
        ):
            continue
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
            except Exception as exc:  # noqa: BLE001 - unblocking admission must not depend on cleanup
                logger.warning(
                    "could not mark stale review job %s failed: %s",
                    job_id,
                    scrub_token_from_text(str(exc)),
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
        - When ``list_jobs`` itself raises (job service unreachable, etc.),
          logs a scrubbed warning and returns a synthetic sibling so the
          caller fail-closes instead of mutating a checkout whose liveness
          cannot be verified. Never raises.
    """
    target = os.path.realpath(repo_path)
    try:
        jobs = _main.list_jobs(active_only=True)
    except Exception as exc:  # noqa: BLE001 - fail-closed: cannot verify checkout is free
        logger.warning(
            "could not scan jobs for sibling on checkout %s: %s",
            repo_path,
            scrub_token_from_text(str(exc)),
        )
        # Synthetic sibling: callers treat any non-None return as "checkout busy"
        # and mark their own job failed. Prefer that over proceeding blind.
        return {
            "job_id": "<job-scan-unavailable>",
            "repo_path": repo_path,
            "github_context": {},
        }
    for j in jobs:
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
    """Result of assembling the reviewer's ``files`` input from a PR's diff."""

    files: Dict[str, str]
    files_reviewed: int


# The PullRequestFile.status value GitHub reports for a deleted file. Named so
# the "is this file removed" check isn't a bare string literal at each call site.
_FILE_STATUS_REMOVED = "removed"

# Prefix of the scope-tagging focus note, exposed so callers/tests can detect
# the note (e.g. in task_requirements) without duplicating its full wording.
REVIEW_FOCUS_NOTE_PREFIX = "Review focus:"

# Shared "tag pre-existing findings" instruction body, appended after the
# diff-first framing and eight-criteria list. Kept as one constant so the
# tagging contract cannot drift from call-site edits.
_PRE_EXISTING_TAG_INSTRUCTIONS = (
    "For EVERY issue you report, add a boolean field named `pre_existing` to the issue "
    "object:\n"
    "- Set `pre_existing: false` for a defect in the code this pull request ADDS or MODIFIES — "
    "these are the findings that matter for reviewing the PR.\n"
    "- Set `pre_existing: true` for a genuine bug you notice in PRE-EXISTING, UNCHANGED code "
    "that this pull request did not touch (an unrelated defect visible in the surrounding "
    "code). Still report such bugs — do not stay silent about them — but tag them so they are "
    "recorded separately instead of blamed on this change.\n"
    "Do not invent pre-existing issues to pad the review; only tag a finding `pre_existing: "
    "true` when it is a real defect in code outside this PR's change."
)

_DIFF_FIRST_FOCUS_NOTE = (
    f"{REVIEW_FOCUS_NOTE_PREFIX} evaluate what this pull request changes (and enclosing "
    "constructs when shown). Treat surrounding or unchanged code as context, not the primary "
    "target — this is a diff-first review.\n"
    "Judge the change against these eight criteria:\n"
    "1. Logical / syntactic correctness of the change\n"
    "2. Contract changes on touched functions/classes (DbC, signatures, invariants)\n"
    "3. Side effects on callers of those encapsulating constructs\n"
    "4. Architectural standards\n"
    "5. Language / library / framework best practices\n"
    "6. New issues introduced by the change\n"
    "7. Does the change actually implement/fix the ticket/spec?\n"
    "8. Project style preferences\n"
    "Line-number prefixes (`N| `) are a gutter, not source. Ignore them when judging "
    "indentation. A continuation line indented 4 spaces past its opening `(` / `[` / `{` "
    "is standard hanging indent (PEP 8 / ruff), not extra leading whitespace — do not flag it.\n"
    f"{_PRE_EXISTING_TAG_INSTRUCTIONS}"
)


def _diff_first_focus(body: str) -> str:
    """Append the shared diff-first focus note to ``body``.

    Every PR reviewer attempt (change surface, whole-file fallback, or hunk
    files) gets the same note so findings stay change-scoped, the eight
    review criteria are explicit, and ``pre_existing`` tagging stays consistent.

    Preconditions:
        - ``body`` is a string (the PR body or "").

    Postconditions:
        - Returns ``body`` with the focus note appended (or the note alone when
          ``body`` is blank/whitespace). The note starts with
          ``REVIEW_FOCUS_NOTE_PREFIX``, lists the eight criteria, and includes
          ``_PRE_EXISTING_TAG_INSTRUCTIONS``.
    """
    note = _DIFF_FIRST_FOCUS_NOTE
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
    concurrently via :func:`shared.concurrency.parallel_map` (bounded by
    ``_HEAD_FETCH_PARALLELISM``), since the per-file GETs are independent. This
    also propagates the caller's contextvars (LLM attribution, trace_id) into
    each fetch worker — a raw ``ThreadPoolExecutor`` would not.

    Postconditions:
        - Returns ``{filename: full_content}`` for every reviewable file whose
          head content fetched as non-blank text. A file whose content cannot be
          fetched (404, API error, blank, or a client without the capability) is
          simply omitted. Never raises: whole-file review is an enhancement, so a
          fetch failure degrades (the CALLER uses whole-file mode for every
          successfully-fetched file and falls back to hunk rendering only for the
          files whose fetch failed, so a partial result never silently narrows
          review scope).
    """
    targets = [f for f in files if _is_whole_file_reviewable(f)]
    if not targets:
        return {}

    def _one(f: Any) -> tuple[str, Optional[str]]:
        try:
            content = client.get_file_contents(owner, repo, f.filename, head_sha)
        except Exception as e:  # noqa: BLE001 - a fetch failure degrades to hunk rendering, never fails review
            logger.warning(
                "PR review: could not fetch head content for %s@%s: %s",
                f.filename,
                head_sha,
                scrub_token_from_text(str(e)),
            )
            content = None
        return f.filename, content

    results = parallel_map(targets, _one, max_workers=_HEAD_FETCH_PARALLELISM)
    return {name: content for name, content in results if content and content.strip()}


def _build_review_code(files: List[Any]) -> ReviewCode:
    """Assemble the line-annotated ``files`` input for the reviewer from the diff.

    Renders each changed file's diff hunks (added + context lines, new-file line
    numbers) — not whole files — so the reviewer is scoped to what the PR changed
    and cited line numbers align with the commentable-line map. Each file's
    rendered body is keyed by path so the reviewer's coordinator can chunk large
    PRs per file. Built entirely from the already-fetched ``files`` payload (no
    extra requests).

    Every reviewable changed file is included — there is no cap on file count.
    The reviewer's coordinator bounds its own per-call prompts, so a large PR is
    chunked rather than truncated.

    Postconditions:
        - Returns ``ReviewCode(files, files_reviewed)`` covering every changed
          file with reviewable rendered content. Binary/removed files and files
          whose diff renders empty are not reviewable and are simply absent.
    """
    rendered_by_path: Dict[str, str] = {}
    for f in files:
        if not f.patch or f.status == _FILE_STATUS_REMOVED:
            continue
        rendered = render_annotated_hunks(f.patch)
        if not rendered:
            continue
        rendered_by_path[f.filename] = rendered
    return ReviewCode(rendered_by_path, len(rendered_by_path))


def _build_replaced_content(files: List[Any]) -> Dict[str, str]:
    """Derive each changed file's pre-change body from its diff's removed side.

    Built entirely from the already-fetched ``files`` payload (each file's
    ``.patch``) -- no extra GitHub read. Mirrors ``_build_review_code``'s
    eligibility filter (skip files with no patch or status == removed) so the
    returned keyset is a subset of what ``hunk_files``/``head_files`` would
    cover for the same PR.

    Postconditions:
        - Returns ``{path: removed-side body}`` for every changed file whose
          removed-side rendering is non-empty. A file whose patch adds lines
          only (no removed/context rows) is simply absent, not mapped to "".
    """
    replaced_by_path: Dict[str, str] = {}
    for f in files:
        if not f.patch or f.status == _FILE_STATUS_REMOVED:
            continue
        rendered = render_removed_hunks(f.patch)
        if not rendered:
            continue
        replaced_by_path[f.filename] = rendered
    return replaced_by_path


def _build_change_surface_for_reviewable(
    files: List[Any],
    head_files: Dict[str, str],
) -> ChangeSurface:
    """Build a change surface for head-backed reviewable patched files.

    Preconditions:
        - ``files`` is the PR changed-file list (may be empty). Each entry
          exposes ``.filename``, ``.status``, and ``.patch``.
        - ``head_files`` maps path → non-blank head text for successfully
          fetched files.

    Postconditions:
        - Considers only files that pass ``_is_whole_file_reviewable`` and
          whose ``filename`` is present in ``head_files``.
        - Returns ``build_change_surface_from_patches`` for those patches with
          ``new_contents=head_files`` (empty ``ChangeSurface`` when no
          candidates or the builder omits all paths).
        - Never raises for well-typed inputs.
    """
    patches = {
        f.filename: f.patch
        for f in files
        if _is_whole_file_reviewable(f) and f.filename in head_files
    }
    if not patches:
        return ChangeSurface(blocks={})
    return build_change_surface_from_patches(patches, new_contents=head_files)


# Optional dependency: author tagging for persisted review history. Imported once
# at module load behind a try/except so a missing/broken ``agent_platform.console``
# (or its transitive deps) can never break importing this API; ``_review_author``
# falls back to "anonymous" when it is unavailable.
try:
    from agent_platform.console.author import resolve_author as _resolve_author  # noqa: E402
except Exception:  # noqa: BLE001 - author tagging is optional, never fatal at import
    _resolve_author = None


def _review_author() -> str:
    """Resolve the author handle for a review row (best-effort, never raises).

    Postconditions:
        - Returns the resolved author handle, or ``"anonymous"`` when the optional
          ``agent_platform.console`` author helper is unavailable or raises.
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

    Delegates to :func:`advisory_lock` with ``_REVIEW_ADMISSION_LOCK`` as the
    process lock, namespace ``"coding_team_review_pr"``, and key derived from
    the casefolded ``owner/repo#pr`` string. See :func:`advisory_lock` for the
    full locking contract (degradation, invariants, exception behavior).
    """
    with advisory_lock(
        _REVIEW_ADMISSION_LOCK,
        "coding_team_review_pr",
        f"{owner}/{repo}#{pr_number}".casefold(),
    ):
        yield


async def _start_pr_review_temporal(job_id: str, request: ReviewPrRequest, token: str) -> None:
    """
    Dispatches a PR review job to Temporal.
    """
    client = await get_temporal_client()
    
    # Configurable workflow routing per best practices
    workflow_name = os.environ.get("TEMPORAL_PR_REVIEW_WORKFLOW", "CodeReviewWorkflow")
    task_queue = os.environ.get("TEMPORAL_CODE_REVIEW_QUEUE", "code_review-queue")

    await client.start_workflow(
        workflow_name, 
        args=[job_id, request, token],
        id=f"pr-review-{job_id}",
        task_queue=task_queue, 
    )


def _run_pr_review(job_id: str, request: ReviewPrRequest, token: str) -> None:
    """Background hook: review the PR, posting exactly one comment per finding.

    Postconditions:
        - On success the job is ``completed`` with ``github_pr_url`` set and one PR
          review submitted (REQUEST_CHANGES on critical/high findings from a PR the
          bot did not author, else COMMENT) whose body carries only the summary.
          A finding the reviewer tagged ``pre_existing`` (a bug in unchanged code
          the PR did not touch, per its own line/file — see
          :func:`_partition_review_issues`) is NOT commented and is serialized
          into ``review_summary["pending_issue_proposals"]`` for a human to
          optionally file as a GitHub issue, and it drives neither the review
          event nor the "no issues" reaction; however, if
          :func:`_partition_review_issues` proves the finding lies on a line
          the PR actually ADDED, the pre_existing tag is overridden back to a
          PR finding and the finding is commented like any other. Every other finding IS
          posted, including one naming a file this PR never touched at all (see
          below) — such a finding is not necessarily pre-existing (it is often
          "this PR should have added/modified file X but didn't"), so only the
          reviewer's own tag, never file/diff membership alone, withholds a
          finding from the PR.
          Every posted finding produces exactly one comment and no comment lists
          more than one finding: a finding tied to a changed line becomes an individual
          line-anchored inline comment carried in the single review (a stray
          off-diff line is bisected out so the rest stay anchored); a finding whose
          file changed but whose cited line is off-diff becomes an individual
          file-level review comment posted on the dedicated comments endpoint (the
          only one that accepts ``subject_type``); a finding whose file never
          appears in the diff at all (e.g. a module the PR should have added but
          didn't) is posted as its own standalone conversation comment naming its
          own file rather than misattributed to an unrelated changed file — the
          same standalone path also catches, as a last resort, a finding whose
          file-level post GitHub itself rejected, so no finding is dropped. A
          finding that cannot be posted at all marks the job ``failed`` (via
          ``comments_failed``); any unhandled exception likewise marks it
          ``failed`` and posts a (token-scrubbed) PR comment — never raises. (A
          best-effort failure to post the summary body alone does not fail the
          job, since the findings still post.)
    """
    owner, repo, pr_number = request.owner, request.repo, request.pr_number
    # Outer guard covers provider resolution, RUNNING updates, heartbeat setup,
    # and the body: anything that escapes must still mark the job failed (scrubbed)
    # and never raise, so the daemon-thread hook cannot leave a job wedged.
    # Fully self-protected: the fallback finalize is best-effort and swallowed.
    try:
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
                logger.warning(
                    "PR review %s: failed to post abort notice: %s",
                    job_id,
                    scrub_token_from_text(str(exc)),
                )
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
        from shared.concurrency import BackgroundHeartbeat  # noqa: I001, PLC0415 - keep module import light

        review_hb = BackgroundHeartbeat(
            lambda: heartbeat_job(job_id),
            _REVIEW_HEARTBEAT_INTERVAL_S,
            name=f"review-heartbeat-{job_id}",
            beat_first=True,
            on_error=lambda exc: logger.warning(
                "review heartbeat error for job %s: %s",
                job_id,
                scrub_token_from_text(str(exc)),
            ),
        )
        # ``_run_pr_review_body`` already marks the job failed for exceptions raised
        # inside the review itself. This outer guard is the last line of defense for
        # anything that escapes it — heartbeat setup/teardown, or the body's own
        # last-resort finalize failing — so this function honors its "never raises"
        # contract and a hook exception can never leave the job wedged in "running".
        with review_hb:
            _run_pr_review_body(job_id, request, token, owner, repo, pr_number, provider)
    except Exception as exc:  # noqa: BLE001 - the daemon thread must never die with the job left running
        scrubbed_error = scrub_token_from_text(str(exc))
        logger.error(
            "PR review %s: unhandled exception escaped the review body: %s",
            job_id,
            scrubbed_error,
        )
        try:
            # phase="completed" (terminal), matching the success and provider-abort
            # paths — a failed job must not keep a mid-review "reviewing" phase.
            _finalize_review(
                job_id,
                JobStatus.FAILED,
                phase="completed",
                error=scrubbed_error,
            )
        except Exception as finalize_exc:  # noqa: BLE001 - store unreachable; nothing more we can do, do not re-raise
            safe_finalize_error = scrub_token_from_text(str(finalize_exc))
            logger.error(
                "PR review %s: last-resort finalize failed after escaped exception: %s",
                job_id,
                safe_finalize_error,
            )
        # Tell the PR too, same as the no-engine-provider path above: whoever
        # triggered the review is watching it, not the job store, and this is
        # the last line of defense — a GitHub outage here must not raise.
        try:
            with _main.GitHubClient(token=token) as client:
                _main._safe_comment(
                    client,
                    owner,
                    repo,
                    pr_number,
                    f"Code review failed: {scrubbed_error}",
                )
        except Exception:  # noqa: BLE001 - notification is best-effort
            logger.warning(
                "PR review %s: failed to post failure notice after escaped exception", job_id
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
          Raises ``ValueError`` if ``status`` is not terminal (enforced with an
          explicit raise so ``python -O`` cannot strip the check).
    Postconditions:
        - ``update_job`` then ``update_review`` are called with exactly the
          non-``None`` fields supplied, ``status`` unwrapped to its plain
          ``.value`` for both (a single conversion point for both consumers);
          ``update_review`` additionally receives ``completed=True``.
    """
    if status not in (JobStatus.COMPLETED, JobStatus.FAILED):
        raise ValueError(f"_finalize_review requires COMPLETED or FAILED, got {status!r}")
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


def _join_nonblank(parts: List[str]) -> str:
    """Join non-blank strings with a blank-line separator, dropping blanks.

    Postconditions:
        - Returns every non-blank entry of ``parts`` (each ``.strip()``ped)
          joined by ``"\\n\\n"``; ``""`` when every entry is blank. Never raises.
    """
    return "\n\n".join(p.strip() for p in parts if (p or "").strip())


class _MergedReviewerOutput:
    """Duck-typed merge of a PR's whole-file and hunk-fallback reviewer outputs.

    A partial whole-file fetch (see ``_run_reviewer``) reviews the fetched
    subset of files whole and the rest via hunks, as two separate reviewer
    calls. This combines both calls' outputs into one object exposing only
    the three attributes this module reads downstream: ``issues``,
    ``summary``, ``spec_compliance_notes``.

    Preconditions:
        - ``outputs`` has at least one entry; each exposes ``.issues`` (a
          list), ``.summary``/``.spec_compliance_notes`` (str). The outputs
          originate from disjoint file sets (the whole-file subset and the
          hunk-fallback subset), so their issues never describe the same
          finding twice.
    Postconditions:
        - ``.issues`` is the concatenation of every output's ``.issues``, in
          ``outputs`` order (whole-file findings before hunk-fallback ones).
        - ``.summary``/``.spec_compliance_notes`` join each output's non-blank
          text via :func:`_join_nonblank`, so neither call's narrative is
          silently dropped.
    """

    def __init__(self, outputs: List[Any]) -> None:
        self.issues: List[Any] = [i for o in outputs for i in o.issues]
        self.summary = _join_nonblank([o.summary for o in outputs])
        self.spec_compliance_notes = _join_nonblank([o.spec_compliance_notes for o in outputs])


def _run_reviewer(
    provider: Any,
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    job_id: str,
    pr: Any,
    files: List[Any],
    hunk_files: Optional[Dict[str, str]],
    head_files: Optional[Dict[str, str]] = None,
    repo_reader: Any = None,
    change_surface: Optional[ChangeSurface] = None,
) -> Optional[Any]:
    """Run the injected review engine over ``change_surface``, ``head_files``,
    and/or ``hunk_files``, merging their outputs; records a failure and returns
    ``None`` on error.

    The PR reviewer is an injected engine (software_engineering_team owns it);
    coding_team calls it through the CodeEngineProvider so this package imports
    nothing from SE. Progress is coalesced through ONE shared ``ActivityBridge``
    (shared schema, swallow-on-failure) across every attempt, cleared once on
    the way out so it never outlives the review.

    One reviewer call per non-empty source. A non-empty ``change_surface``
    drives the primary pre-numbered attempt (``pre_numbered=True``,
    ``_diff_first_focus``) and replaces a whole-file ``head_files`` attempt;
    when the surface is empty or absent, a truthy ``head_files`` drives
    whole-file review (``pre_numbered=False``, ``_diff_first_focus``). A
    non-empty ``hunk_files`` always drives an additional diff-hunk attempt
    (``pre_numbered=True``, ``_diff_first_focus``). The sources can never be
    mixed into a single call (each attempt's ``files=`` covers disjoint
    paths), which is why partial-fetch PRs may need two calls instead of one.

    Preconditions:
        - ``provider`` was resolved before the first GitHub call.
        - At least one of ``change_surface`` (non-empty), ``head_files``, or
          ``hunk_files`` is truthy — the caller (``_run_pr_review_body``) never
          reaches this function otherwise (its own "nothing reviewable" guard
          returns first). A PR whose whole-file fetch fully succeeds supplies
          only ``head_files`` (``hunk_files`` empty); one whose fetch fully
          failed supplies only ``hunk_files`` (``head_files`` falsy); one whose
          fetch PARTIALLY failed supplies BOTH — ``head_files`` for the fetched
          subset, ``hunk_files`` built ONLY from the files that failed to
          fetch — so both attempts run and are merged. When admission attaches
          a non-empty ``change_surface``, it replaces the whole-file attempt
          while ``hunk_files`` still merges when present.
        - ``repo_reader`` is None or a ``RepoReader`` handed to the false-positive
          verifier so it can confirm existing repository files outside the diff.
        - ``job_id`` is a non-empty string identifying this review run (the
          same id ``record_review_start`` persisted it under). Forwarded to
          every reviewer attempt so the pipeline can bind the transcript
          contextvar (``CodeReviewAgent.run`` -> ``llm_attribution(job_id=...)``)
          and so job progress/outage recording stays keyed to the right job.
    Postconditions:
        - ``replaced_content`` (each changed file's pre-change body, derived
          once from ``files``' diff patches via ``_build_replaced_content``,
          no extra GitHub read) is forwarded to every attempt when non-empty;
          omitted entirely otherwise so ``CodeReviewInput.replaced_content``
          stays at its ``None`` default.
        - On success, returns the single attempt's output unchanged when only
          one ran (identical behavior/kwargs to a single-mode dispatch for
          the all-whole-file, all-surface, and all-hunk cases). When two ran,
          returns a merged duck-typed output — see ``_MergedReviewerOutput``.
        - On ANY attempt's failure — an exception, OR a reviewer that returns
          ``None`` without raising — records the failure on the PR/job via
          ``_record_review_outage`` and returns ``None`` immediately, without
          running any remaining attempt. A successful earlier attempt's output
          is discarded in that case: the review stays all-or-nothing per call.
          The caller returns on ``None``, so recording the failure here is
          what keeps the daemon-thread job from wedging in ``running``.
    """
    # last_activity_at is stamped centrally by the job service on every real
    # update, so these writes count as activity for stall detection.
    pr_bridge = ActivityBridge(
        lambda **kw: _main.update_job(job_id, **kw),
        agent="code_review",
        label=f"Reviewing PR #{pr_number}",
    )
    replaced_content = _build_replaced_content(files)
    common = dict(
        repo_reader=repo_reader,
        task_description=f"Review pull request #{pr_number}: {pr.title}",
        language=_infer_review_language(files),
        progress_callback=pr_bridge,
        job_id=job_id,
    )
    if replaced_content:
        common["replaced_content"] = replaced_content
    # One reviewer call per non-empty source; see the docstring above.
    attempts: List[Dict[str, Any]] = []
    surface = change_surface
    if surface is not None and not surface.is_empty:
        attempts.append(
            dict(
                files=dict(surface.blocks),
                pre_numbered=True,
                task_requirements=_diff_first_focus(pr.body or ""),
            )
        )
    elif head_files:
        # Whole-file review: the reviewer sees complete files (no hunk-end
        # "truncation"), and the false-positive filter (via repo_reader) can
        # confirm existing files a finding claims are missing. Because it now
        # sees unchanged code too, steer it to only raise issues about the
        # change and treat the rest as context — otherwise it would comment on
        # pre-existing, unchanged code the PR never touched.
        attempts.append(
            dict(
                files=head_files,
                pre_numbered=False,
                task_requirements=_diff_first_focus(pr.body or ""),
            )
        )
    if hunk_files:
        # _build_review_code renders every line with its original line-number
        # prefix; declaring pre_numbered here (instead of letting the reviewer
        # sniff the format) keeps issue lines verbatim. Diff hunks still carry
        # unchanged context lines, so steer this attempt the same way the
        # whole-file attempt is steered — otherwise it would comment on
        # pre-existing, unchanged code the PR never touched.
        attempts.append(
            dict(
                files=hunk_files,
                pre_numbered=True,
                task_requirements=_diff_first_focus(pr.body or ""),
            )
        )
    assert attempts, (
        "caller must supply a non-empty change_surface, head_files, and/or non-empty hunk_files"
    )

    outputs: List[Any] = []
    try:
        for mode_kwargs in attempts:
            try:
                output = provider.run_pr_code_review(**common, **mode_kwargs)
            except Exception as e:  # noqa: BLE001 - any reviewer failure fails the job cleanly
                # A reviewer-side failure (LLM outage, unrecoverable exhaustion,
                # etc.) is not a code defect: record the detail in the job
                # store but never post the raw exception on the PR — degrade
                # to a quiet, re-runnable outage. str(e) is "" for a bare
                # zero-arg exception (e.g. a durable-review client-side wait
                # timing out with no attached detail); recording just "code
                # review failed: " is useless for later triage, so fall back
                # to naming the exception type when it carries no message of
                # its own. Scrub once for log + outage so a token that leaked
                # into the exception text never reaches either sink.
                raw_message = str(e)
                detail = (
                    scrub_token_from_text(raw_message)
                    if raw_message.strip()
                    else f"{type(e).__name__} (no error message)"
                )
                logger.error("PR review agent failed: %s", detail)
                _main._record_review_outage(
                    client, owner, repo, pr_number, job_id, f"code review failed: {detail}"
                )
                return None
            if output is None:
                # A reviewer that returns no output (rather than raising) must
                # not slip through as a silent success — the caller returns on
                # None with no terminal write, which would wedge the job in
                # "running" forever.
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
            outputs.append(output)
    finally:
        # Clear so a stale sub-progress entry never outlives the review itself.
        pr_bridge.clear()
    return outputs[0] if len(outputs) == 1 else _MergedReviewerOutput(outputs)


def _run_tasks_draining(tasks: List[Callable[[], Any]]) -> List[Any]:
    """Run every zero-arg *tasks* concurrently; re-raise the first failure after all finish.

    Each task's own exception is caught inside the worker (see ``_capture``
    below) rather than left to propagate through ``parallel_map`` itself — so
    ``parallel_map``'s own fast-fail path (which cancels not-yet-started
    siblings on the first exception) never triggers here. Every task always
    runs to completion, matching the previous hand-rolled
    ``ThreadPoolExecutor`` + drain-every-future contract this replaces.

    Preconditions:
        - ``tasks`` is a list of zero-arg callables, safe to invoke concurrently.
    Postconditions:
        - Every task in ``tasks`` is invoked exactly once, unconditionally.
        - Returns the tasks' results in ``tasks`` order when every task succeeds.
        - Raises the first (by ``tasks`` order, not completion order —
          deterministic, since every exception is captured before
          ``parallel_map`` sees it) exception any task raised, once every task
          has finished.
    """

    def _capture(fn: Callable[[], Any]) -> tuple[Any, Optional[Exception]]:
        try:
            return fn(), None
        except Exception as exc:  # noqa: BLE001 - captured; re-raised below once every task has finished
            return None, exc

    outcomes = parallel_map(tasks, _capture, max_workers=len(tasks), skip_none=False)
    for _, err in outcomes:
        if err is not None:
            raise err
    return [value for value, _ in outcomes]


def _fetch_existing_comments(client: Any, owner: str, repo: str, pr_number: int) -> List[Any]:
    """Best-effort fetch of every comment already on the PR, for de-duplicating findings.

    Preconditions:
        - ``client`` is an open ``GitHubClient``.
    Postconditions:
        - Returns ``build_existing_comments(...)`` over the PR's existing
          review comments, resolved-thread ids, and standalone conversation
          comments, fetched concurrently (independent GitHub reads). Any
          failure fetching any of the three (REST error, transport error, or a
          GraphQL-lookup failure already degraded to an empty set by
          ``get_resolved_review_thread_comment_ids`` itself) is logged as a
          warning and degrades the WHOLE result to ``[]`` — same all-or-nothing
          semantics as the prior serial version — this lookup must never fail
          an otherwise-working review; a failure here only means findings are
          neither dropped nor cross-referenced on this run, same as a PR with
          no existing comments at all. Fanned out via
          :func:`shared.concurrency.parallel_map` (through
          :func:`_run_tasks_draining`), which also propagates the caller's
          contextvars (LLM attribution, trace_id) into each fetch worker — a
          raw ``ThreadPoolExecutor`` would not.
    """

    def _reviews() -> Any:
        return client.list_review_comments(owner, repo, pr_number)

    def _resolved() -> Any:
        return client.get_resolved_review_thread_comment_ids(owner, repo, pr_number)

    def _issues() -> Any:
        return client.list_issue_comments(owner, repo, pr_number)

    try:
        review_comments, resolved_ids, issue_comments = _run_tasks_draining(
            [_reviews, _resolved, _issues]
        )
        return build_existing_comments(review_comments, resolved_ids, issue_comments)
    except Exception as e:  # noqa: BLE001 - this lookup is best-effort, never fails the review
        logger.warning(
            "Could not fetch existing comments for PR #%s: %s",
            pr_number,
            scrub_token_from_text(str(e)),
        )
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
            scrub_token_from_text(str(e)),
        )
        open_issues = []
    except Exception as exc:  # noqa: BLE001 - duplicate-detection must never fail the review
        logger.warning(
            "PR review #%s: unexpected error listing open issues for duplicate-detection: %s",
            pr_number,
            scrub_token_from_text(str(exc)),
        )
        open_issues = []
    try:
        return annotate_duplicate_proposals(proposals, open_issues)
    except Exception as exc:  # noqa: BLE001 - duplicate-detection must never fail the review
        logger.warning(
            "PR review #%s: duplicate annotation failed, proceeding without duplicate detection: %s",
            pr_number,
            scrub_token_from_text(str(exc)),
        )
        # annotate_duplicate_proposals's own "no match" branch does exactly this
        # ({**p, "matched_existing": False}); mirrored here as a safety net so a
        # bug inside that function still leaves every proposal carrying the field.
        return [{**p, "matched_existing": False} for p in proposals]


def _fetch_pr_metadata(
    client: Any, owner: str, repo: str, pr_number: int
) -> tuple[Any, List[Any], str]:
    """Fetch PR detail, PR files, and the authenticated login concurrently.

    Preconditions:
        - ``client`` is an open ``GitHubClient``.
    Postconditions:
        - Returns ``(pr, files, reviewer_login)``. ``get_pull_request`` and
          ``get_pull_request_files`` are independent GitHub reads with no data
          dependency between them, and ``get_authenticated_login`` needs only
          ``client`` too, so all three are dispatched concurrently. A failure
          in ``get_pull_request`` or ``get_pull_request_files`` propagates to
          the caller unchanged (these are NOT best-effort — the review must
          still fail exactly as it did when the calls were serial). A failure
          in ``get_authenticated_login`` (of any exception type) is caught
          internally and degrades ``reviewer_login`` to ``""`` (logged) — it
          only feeds the self-PR event downgrade and must never fail the
          review. Fanned out via :func:`shared.concurrency.parallel_map`
          (through :func:`_run_tasks_draining`), which also propagates the
          caller's contextvars (LLM attribution, trace_id) into each fetch
          worker — a raw ``ThreadPoolExecutor`` would not.
    """

    def _get_pr() -> Any:
        return client.get_pull_request(owner, repo, pr_number)

    def _get_files() -> List[Any]:
        return client.get_pull_request_files(owner, repo, pr_number)

    def _get_login() -> str:
        try:
            return client.get_authenticated_login()
        except Exception as e:  # noqa: BLE001 - reviewer_login is best-effort, never fails the review
            logger.warning(
                "Could not resolve reviewer login for PR #%s: %s",
                pr_number,
                scrub_token_from_text(str(e)),
            )
            return ""

    pr, files, reviewer_login = _run_tasks_draining([_get_pr, _get_files, _get_login])
    return pr, files, reviewer_login


def _files_for_scope(mode: ReviewModeDecision) -> Dict[str, str]:
    """Collect reviewer-visible file bodies for the scope verifier index.

    Postconditions: union of ``head_files``, ``hunk_files``, and non-empty
        change-surface blocks. Never raises.
    """
    files: Dict[str, str] = {}
    files.update(mode.head_files or {})
    files.update(mode.hunk_files or {})
    surface = mode.change_surface
    if surface is not None and not surface.is_empty:
        files.update(dict(surface.blocks))
    return files


def _is_not_reviewed_coverage_finding(issue: Any) -> bool:
    """True when ``issue`` is a blocking unreviewed-range coverage finding.

    Those findings are merged into ``output.issues`` only when
    ``CODE_REVIEW_BLOCK_ON_UNREVIEWED`` is set; they must not go through
    scope verification (an unsure tag would route them to proposals and
    defeat the fail-closed gate).

    Postconditions: True iff the finding description contains
        ``NOT_REVIEWED_FINDING_MARKER``. Never raises.
    """
    from software_engineering_team.code_review_agent.mapping import (
        NOT_REVIEWED_FINDING_MARKER,
    )

    return NOT_REVIEWED_FINDING_MARKER in (getattr(issue, "description", "") or "")


def _tag_review_issues_for_scope(
    output: Any, mode: ReviewModeDecision, pr: Any, files: List[Any]
) -> None:
    """Tag out-of-scope findings before partition so they are not posted.

    Preconditions: ``output`` is a successful reviewer result with ``issues``.
        ``pr`` is the GitHub PR object used for the reviewer task text.
        ``files`` is the PR file list (patches) from admission.
    Postconditions: ``output.issues`` is replaced with scope-tagged copies when
        the verifier runs; blocking "could not be reviewed" coverage findings
        are excluded from the verifier and merged back unchanged so they cannot
        be routed to issue proposals. On Dummy LLM / verifier failure the list
        is left unchanged. Never raises.
    """
    issues = getattr(output, "issues", None) or []
    if not issues:
        return
    try:
        from llm_service import get_client
        from software_engineering_team.code_review_agent.models import CodeReviewInput
        from software_engineering_team.code_review_agent.scope_filter import (
            apply_scope_verification,
        )

        genuine = [i for i in issues if not _is_not_reviewed_coverage_finding(i)]
        if not genuine:
            return
        scope_files = _files_for_scope(mode)
        input_data = None
        if scope_files:
            input_data = CodeReviewInput(
                files=dict(scope_files),
                task_description=(
                    f"Review pull request #{getattr(pr, 'number', '')}: "
                    f"{getattr(pr, 'title', '') or ''}"
                ),
                task_requirements=_diff_first_focus(getattr(pr, "body", None) or ""),
            )
        removed_by_path = {
            f.filename: sorted(parse_removed_lines(getattr(f, "patch", None) or "")) for f in files
        }
        removed_by_path = {path: lines for path, lines in removed_by_path.items() if lines}
        patches_by_path = {
            f.filename: getattr(f, "patch", None) or "" for f in files if getattr(f, "filename", "")
        }
        tagged = apply_scope_verification(
            get_client(),
            issues=genuine,
            changed_by_path=mode.changed_by_path,
            files=scope_files,
            repo_reader=mode.repo_reader,
            input_data=input_data,
            removed_by_path=removed_by_path,
            patches_by_path=patches_by_path,
        )
        # apply_scope_verification preserves finding count and order, so `tagged`
        # aligns 1:1 with `genuine`. Assert that postcondition here so a contract
        # breach surfaces as a clear, caught failure below rather than a
        # StopIteration raised mid-splice from next(tagged_iter).
        assert len(tagged) == len(genuine), (
            f"scope verification must preserve finding count: {len(tagged)} != {len(genuine)}"
        )
        tagged_iter = iter(tagged)
        output.issues = [
            i if _is_not_reviewed_coverage_finding(i) else next(tagged_iter) for i in issues
        ]
    except Exception as exc:  # noqa: BLE001 — tagging must never fail the review
        logger.warning(
            "PR review: scope verification skipped (%s: %s)",
            type(exc).__name__,
            scrub_token_from_text(str(exc)),
        )


class ReviewModeDecision(NamedTuple):
    """Whole-file vs. hunk review-mode decision, plus every input ``_run_reviewer`` needs."""

    valid_by_path: Dict[str, List[int]]
    changed_by_path: Dict[str, List[int]]
    head_files: Dict[str, str]
    change_surface: ChangeSurface
    hunk_files: Dict[str, str]
    files_reviewed: int
    repo_reader: Any


def _decide_review_mode(
    client: Any,
    job_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr: Any,
    files: List[Any],
) -> Optional[ReviewModeDecision]:
    """Decide, PER FILE, whether each reviewable file is reviewed whole or via diff hunks.

    Whole-file vs. hunk review is decided PER FILE, not per PR: every
    reviewable file whose head content fetches successfully is reviewed
    whole; only the files whose fetch fails fall back to hunk rendering (see
    :func:`_run_reviewer`), so one file's failed fetch never discards another
    file's successfully-fetched whole-file body. ``hunk_files`` is
    built only for the files that actually need it, never unconditionally, so
    a PR whose whole-file fetch fully succeeds pays nothing for hunk
    rendering.

    Also runs the review's two "nothing to review" gates itself and completes
    the job as a no-op (via :func:`_complete_review_noop`) when they fire, so
    the caller only has to check for ``None``.

    Preconditions:
        - ``client`` is an open ``GitHubClient``; ``pr`` carries ``head_sha``/
          ``html_url``; ``files`` is the PR's changed-file list from
          :func:`_fetch_pr_metadata` (may be empty).
    Postconditions:
        - Returns ``None`` when ``files`` is empty, when no file passes
          :func:`_is_whole_file_reviewable`, or when the total-hunk-fallback
          branch renders empty ``hunk_files`` — in every case
          :func:`_complete_review_noop` has already posted the courtesy
          comment and finalized the job ``COMPLETED``; the caller must return
          immediately without further GitHub calls.
        - Otherwise returns a :class:`ReviewModeDecision` where: ``head_files``
          is fetched via :func:`_fetch_head_files` for the reviewable files,
          and ``change_surface`` is built from it via
          :func:`_build_change_surface_for_reviewable`. When ``change_surface``
          is non-empty, :func:`_run_reviewer` dispatches it as the PRIMARY
          input and bypasses ``head_files`` entirely, so every reviewable file
          the surface does NOT cover (``reviewable - set(change_surface.blocks)``
          — whether its fetch failed, or fetch succeeded but the builder
          produced no usable body for it, e.g. a patch with no added lines)
          is hunk-rendered into ``hunk_files`` here, and ``files_reviewed``
          sums the surfaced count plus the hunk count: no reviewable file is
          ever covered by neither ``change_surface.blocks`` nor
          ``hunk_files``. When ``change_surface`` is empty, the whole-file/hunk
          decision falls back to ``head_files`` alone: every reviewable file
          fetched whole yields ``hunk_files == {}`` and
          ``files_reviewed == len(head_files)``; a partial fetch renders
          ``hunk_files`` (via :func:`_build_review_code`) ONLY from the files
          that failed to fetch, summing both counts; a total fetch failure
          renders ``hunk_files`` from all ``files``. ``repo_reader`` is always
          constructed for ``pr.head_sha``, whole-file/surface success or not.
        - Never raises for GitHub-fetch failures (:func:`_fetch_head_files`
          degrades internally); any other exception propagates to the
          caller's outer handler.
    """
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
        return None

    # "Nothing reviewable" gate, BEFORE parse_valid_lines, hunk rendering, or
    # whole-file fetch: `reviewable` applies the same non-removed+has-patch
    # predicate _build_review_code applies internally, so an empty set
    # here means _build_review_code(files) would also render "" — skip
    # straight to the noop rather than paying for line-map parsing, a hunk
    # render, or a head-content fetch that could only ever come back empty.
    reviewable = {f.filename for f in files if _is_whole_file_reviewable(f)}
    if not reviewable:
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
        return None

    valid_by_path = {f.filename: parse_valid_lines(f.patch) for f in files}
    # Lines the PR actually ADDED — narrower than valid_by_path, which also
    # includes unchanged context lines (so a finding cited on one can still
    # be anchored inline per map_issues_to_comments). Only an added line can
    # override a reviewer's pre_existing tag below: a genuine pre-existing
    # bug on an unchanged context line inside a modified hunk must still
    # route to a proposal, not a PR comment.
    changed_by_path = {f.filename: parse_valid_lines(f.patch, added_only=True) for f in files}

    # Prefer whole-file review over diff hunks: complete files remove
    # the hunk-boundary "truncation" false positive, and the repo
    # reader lets the false-positive filter confirm existing
    # (unchanged) repo files a finding claims are missing. A file
    # whose whole-file fetch fails falls back to ITS OWN hunk
    # rendering rather than reverting the whole PR to hunk mode — a
    # partial fetch failure must not discard the whole-file bodies
    # that DID come back.
    head_files = _fetch_head_files(client, owner, repo, files, pr.head_sha)
    # Built before the hunk_files/files_reviewed decision below (it is a pure
    # function of files/head_files) since _run_reviewer dispatches a
    # non-empty change_surface as PRIMARY and bypasses head_files entirely —
    # the fallback-hunk_files decision needs to know which files that will
    # leave uncovered, not just which files failed to fetch.
    change_surface = _build_change_surface_for_reviewable(files, head_files)
    missing = reviewable - set(head_files)
    hunk_files: Dict[str, str] = {}
    if not change_surface.is_empty:
        # The surface covers at least one file and will be dispatched as the
        # PRIMARY reviewer input (see _run_reviewer), replacing the
        # whole-file head_files attempt entirely. Any reviewable file the
        # surface does NOT cover — whether its fetch failed, or fetch
        # succeeded but the builder produced no usable body for it (e.g. a
        # patch with no added lines) — must still fall back to hunk
        # rendering here, or it would be silently dropped from review
        # entirely: neither the surface nor the (bypassed) whole-file body
        # would ever reach the reviewer for it.
        surfaced = set(change_surface.blocks)
        uncovered = reviewable - surfaced
        if uncovered:
            fallback_files = [f for f in files if f.filename in uncovered]
            hunk_files, hunk_reviewed = _build_review_code(fallback_files)
            files_reviewed = len(surfaced) + hunk_reviewed
            logger.info(
                "PR review #%s: change surface covers %d/%d reviewable "
                "file(s); the remaining %d fall back to hunk review",
                pr_number,
                len(surfaced),
                len(reviewable),
                len(uncovered),
            )
        else:
            files_reviewed = len(surfaced)
    elif head_files and not missing:
        # Every reviewable file fetched whole but none produced a usable
        # change surface: the hunk blob would be thrown away unread, so skip
        # rendering it entirely.
        files_reviewed = len(head_files)
    elif head_files:
        # Partial fetch, no surface at all: hunk-render ONLY the files that
        # failed to fetch whole; files that DID fetch stay in whole-file mode.
        fallback_files = [f for f in files if f.filename in missing]
        hunk_files, hunk_reviewed = _build_review_code(fallback_files)
        files_reviewed = len(head_files) + hunk_reviewed
        logger.info(
            "PR review #%s: fetched %d/%d whole files; the remaining "
            "%d file(s) fall back to hunk review",
            pr_number,
            len(head_files),
            len(reviewable),
            len(missing),
        )
    else:
        # Total fetch failure: unchanged from before this change —
        # render every changed file's hunks (not just `reviewable`,
        # since _build_review_code applies its own equivalent filter
        # internally).
        hunk_files, files_reviewed = _build_review_code(files)
        if not hunk_files:
            # Belt-and-suspenders: `reviewable` is non-empty (the gate
            # above passed) but every reviewable file's diff hunk
            # happened to render blank (e.g. a hunk that only removes
            # lines, adding no +/context line for
            # render_annotated_hunks to emit). _build_review_code
            # filters on the same non-removed+has-patch predicate as
            # _is_whole_file_reviewable, so this is the only place
            # `hunk_files` can still end up empty once `reviewable` passed.
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
            return None
        logger.info(
            "PR review #%s: whole-file fetch failed for all %d "
            "reviewable file(s); falling back to hunk review",
            pr_number,
            len(reviewable),
        )
    repo_reader = GitHubRepoReader(client, owner, repo, pr.head_sha)
    return ReviewModeDecision(
        valid_by_path=valid_by_path,
        changed_by_path=changed_by_path,
        head_files=head_files,
        change_surface=change_surface,
        hunk_files=hunk_files,
        files_reviewed=files_reviewed,
        repo_reader=repo_reader,
    )


class ReviewIssuePartition(NamedTuple):
    """PR-scoped findings, ready-to-post comments, and pre-existing-bug proposals."""

    pr_issues: List[Any]
    preexisting_issues: List[Any]
    proposals: List[Dict[str, Any]]
    addressed_issues: List[Any]
    line_comments: List[Any]
    file_comments: List[Any]
    standalone_comments: List[str]


def _partition_review_issues(
    output: Any,
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    valid_by_path: Dict[str, List[int]],
    changed_by_path: Dict[str, List[int]],
) -> ReviewIssuePartition:
    """Split the reviewer's raw findings into PR-scoped issues and pre-existing-bug proposals.

    Dedupes the PR-scoped findings against the PR's existing comments, then
    maps/splits the survivors into postable line-, file-level, and standalone
    comments.

    Preconditions:
        - ``output.issues`` is from a successful (non-``None``) call to
          :func:`_run_reviewer`.
        - ``valid_by_path``/``changed_by_path`` are the maps
          :func:`_decide_review_mode` built for the same file set.
        - ``client`` is an open ``GitHubClient``.
    Postconditions:
        - An issue tagged ``pre_existing=True`` is kept in
          ``preexisting_issues`` unless :func:`is_within_diff` against
          ``changed_by_path`` proves it lies on a line this PR actually
          ADDED, in which case it is overridden into ``pr_issues``. An issue
          that omits the tag (or from a caller that never asks for it)
          defaults ``pre_existing=False`` and is treated as a PR finding —
          this deliberately includes a finding naming a file outside the
          diff (e.g. "module X is imported but was never added"): such a
          finding is exactly the kind ``false_positive_filter.py`` already
          keeps rather than treats as noise (a missing file/module the PR
          should have added is a real, in-scope defect, not a pre-existing
          one), so only the reviewer's own tag — never file/diff membership
          alone — routes a finding to proposals.
        - ``proposals`` is :func:`_detect_duplicate_proposals` applied to
          ``proposal_from_findings`` over each :func:`group_similar_findings`
          group of ``preexisting_issues``, with any proposal matched to an
          already-open GitHub issue (``matched_existing: True``) then dropped —
          it is already tracked, so ``proposals`` only ever carries genuinely
          new candidates for a human to consider.
        - When ``pr_issues`` is non-empty, it is first partitioned against
          :func:`_fetch_existing_comments` via
          :func:`partition_issues_by_existing_comments`, producing the
          returned ``addressed_issues`` (findings that matched an
          already-RESOLVED existing comment and were dropped). When
          ``pr_issues`` is empty, ``addressed_issues == []`` and no
          existing-comments fetch happens (nothing to de-duplicate).
        - ``line_comments``/``file_comments`` is :func:`split_review_comments`
          over :func:`map_issues_to_comments` applied to ``pr_issues``.
          ``standalone_comments`` renders (via :func:`format_issue_comment`)
          every leftover ``map_issues_to_comments`` could not resolve to a
          path in the diff at all — such a finding is never misattributed to
          an unrelated changed file (the bug this replaces): it is posted as
          its own standalone conversation comment naming its own
          ``file_path`` instead.
        - The existing-comments fetch and duplicate-detection are both
          best-effort and degrade internally (never raise). Any other
          exception (e.g. from ``map_issues_to_comments``) propagates to the
          caller's outer handler.
    """
    # Split the reviewer's findings by whether they belong to this PR.
    # Defects in the code the PR added or modified drive the review
    # (comments + REQUEST_CHANGES); pre-existing bugs the reviewer noticed
    # in unchanged code are NOT posted on this PR — they become GitHub-issue
    # proposals a human approves later on the Code Review page. A finding
    # without the tag defaults to a PR finding (reviews now tag via
    # _diff_first_focus, but any caller that doesn't ask still
    # behaves exactly as before). The LLM's self-reported tag is not trusted
    # unconditionally: a finding whose file/line is verified to be a line
    # this PR actually ADDED (per is_within_diff against changed_by_path —
    # deliberately narrower than valid_by_path, which would also match
    # unchanged context lines) cannot legitimately be "pre-existing,
    # unchanged code", so a mistagged pre_existing=true is overridden back to
    # a PR finding rather than silently skipping review.
    #
    # Deliberately NOT gated on whether the finding's file is in the diff at
    # all: a finding naming a file outside the diff is very often "this PR
    # should have added/modified file X but didn't" — a genuine, in-scope
    # defect, not a pre-existing one — and false_positive_filter.py already
    # keeps exactly this kind of "unresolved path" finding rather than
    # dropping it as noise. Forcing every off-diff-file finding to
    # preexisting_issues would silently swallow that class of real,
    # PR-blocking finding. The mis-anchoring failure mode that once
    # motivated such a gate (an off-diff finding posted against an unrelated
    # changed file) is fixed below instead, by giving it its own standalone
    # comment rather than a borrowed file anchor.
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
    # Already tracked by an existing open GitHub issue -- nothing new to report.
    proposals = [p for p in proposals if not p.get("matched_existing")]

    # Recognize findings that duplicate a comment already on the PR (from a
    # prior review run, or a human), so an evolving PR does not accumulate
    # repeat comments every time it is re-reviewed. A match against an
    # already-RESOLVED comment is dropped (requirement: already addressed);
    # a match against a still-open comment is kept but cross-referenced (see
    # map_issues_to_comments below) instead of posted as an unexplained
    # duplicate. The fetch is best-effort: any failure yields [], so this
    # never turns a working review into a failed one. Skipped entirely on a
    # clean review (no findings): there is nothing to de-duplicate, so the
    # up-to-three API calls the fetch makes would be pure waste.
    if pr_issues:
        existing_comments = _fetch_existing_comments(client, owner, repo, pr_number)
        pr_issues, addressed_issues, existing_by_issue = partition_issues_by_existing_comments(
            pr_issues, existing_comments
        )
    else:
        addressed_issues, existing_by_issue = [], {}

    comments, leftovers = map_issues_to_comments(pr_issues, valid_by_path, existing_by_issue)

    # A leftover names a file map_issues_to_comments could not resolve to any
    # path in this PR's diff at all (e.g. a module the PR should have added
    # but didn't). Posting it against an unrelated changed file would be
    # misleading and posting nothing at all would silently drop a real
    # finding, so it gets its own standalone conversation comment instead —
    # naming its own file_path, tied to no diff line or anchor.
    standalone_comments = [
        format_issue_comment(issue, existing_by_issue.get(id(issue))) for issue in leftovers
    ]

    # Two GitHub endpoints, two shapes. Line-anchored comments ride the
    # single review; file-level comments (subject_type="file") go on the
    # dedicated review-comments endpoint, which the reviews array rejects
    # (it does not accept subject_type). Splitting them keeps one bad
    # file-level entry from collapsing the whole review to the fallback.
    line_comments, file_comments = split_review_comments(comments)

    return ReviewIssuePartition(
        pr_issues=pr_issues,
        preexisting_issues=preexisting_issues,
        proposals=proposals,
        addressed_issues=addressed_issues,
        line_comments=line_comments,
        file_comments=file_comments,
        standalone_comments=standalone_comments,
    )


class CommentPostingResult(NamedTuple):
    """Outcome of submitting one PR review's comments to GitHub."""

    event: str
    inline_count: int
    file_comment_count: int
    comment_findings: int
    comments_failed: int


def _post_review_comments(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    pr: Any,
    reviewer_login: str,
    output: Any,
    partition: ReviewIssuePartition,
) -> CommentPostingResult:
    """Build and submit the review body/event, then post every finding as its own comment.

    Preconditions:
        - ``partition`` was produced by :func:`_partition_review_issues` for
          this same ``output``/PR.
        - ``client`` is an open ``GitHubClient``; ``pr`` carries ``head_sha``,
          ``author``, ``html_url``.
    Postconditions:
        - The review body is built via ``build_review_body`` from
          ``output.summary``/``output.spec_compliance_notes``, forced to
          ``""`` when ``partition.proposals`` is non-empty (the reviewer's
          narrative can otherwise leak a pre-existing finding's theme/location
          even though its own comment is suppressed; ``proposals`` is the
          signal a human will still see something withheld from this PR). The
          returned ``event`` is ``choose_event(partition.pr_issues,
          author=pr.author, reviewer=reviewer_login)``.
        - ``partition.line_comments`` are submitted via :func:`_submit_review`,
          bisecting out any off-diff line so the rest stay anchored. A
          ``GitHubAPIError`` from that submission is swallowed ONLY when there
          are no line comments AND there is a file-level comment or a
          standalone comment still to post (the summary submission failed but
          another finding still carries the review); in every other case it
          is re-raised UNCHANGED to the caller's outer handler.
        - File-level comments plus any bisected-out line comments are posted
          via :func:`_post_file_comments`; whatever that still cannot post,
          together with every entry in ``partition.standalone_comments``
          (findings whose file was never in the diff at all — see
          :func:`_partition_review_issues`), is posted as a standalone
          conversation comment via ``_main._safe_comment`` (module-qualified
          so ``monkeypatch.setattr(main, ...)`` keeps taking effect).
        - Returns a :class:`CommentPostingResult` with ``inline_count =
          len(line_comments) - len(dropped_lines)``, ``file_comment_count``
          from :func:`_post_file_comments`, ``comment_findings`` counting
          every standalone comment attempted (both sources), and
          ``comments_failed`` counting the standalone posts that returned
          falsy.
        - Never raises except the untolerated ``GitHubAPIError`` re-raise
          above; ``_post_file_comments``/``_main._safe_comment`` are
          best-effort by their own contract already.
    """
    # output.summary/spec_compliance_notes are synthesized by the reviewer
    # engine from its FULL issue list (software_engineering_team's
    # synthesize_review_findings runs before this split), so the narrative
    # can describe a pre-existing finding's theme/location even though its
    # own per-issue comment is suppressed. Suppress on partition.proposals
    # rather than partition.preexisting_issues directly: proposals is the
    # actual signal a human will see something withheld from this PR (the
    # two are equivalent today -- group_similar_findings never returns zero
    # groups for a non-empty input, and annotate_duplicate_proposals never
    # drops a proposal, only tags it -- but proposals is the one that would
    # still be correct if that grouping/dedup contract ever changed).
    suppress_narrative = bool(partition.proposals)
    body = build_review_body(
        output.summary if not suppress_narrative else "",
        output.spec_compliance_notes if not suppress_narrative else "",
        issue_count=len(partition.pr_issues),
    )
    event = choose_event(partition.pr_issues, author=pr.author, reviewer=reviewer_login)

    line_comments = partition.line_comments
    file_comments = partition.file_comments

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
        # a no-line-comment review). Tolerate it ONLY when there is a
        # file-level or standalone finding still to post — the summary is
        # then a best-effort courtesy and that finding carries the review
        # (and surfaces any real error itself). Otherwise nothing reached
        # GitHub, so let the failure mark the job failed rather than
        # report a hollow success. A non-empty line_comments is deliberately
        # NEVER tolerated even when file/standalone comments also exist: a
        # rejected line-comment submission is a real, unexplained GitHub
        # error (not the routine 422 _submit_review already retries around),
        # so it must fail the job loudly rather than be silently masked by
        # whatever else happens to post successfully.
        if line_comments or not (file_comments or partition.standalone_comments):
            raise
        logger.warning("Summary-only review failed; posting remaining findings only")
        dropped_lines = []
    inline_count = len(line_comments) - len(dropped_lines)

    # File-level comments and any bisected-out line comments (demoted,
    # keeping the file anchor) each go on the dedicated endpoint. A rejected
    # line comment falls through as its original entry, so the standalone
    # fallback still names ``path:line``.
    file_comment_count, standalone = _post_file_comments(
        client, owner, repo, pr_number, pr.head_sha, file_comments + dropped_lines
    )

    # Two sources feed standalone conversation comments: findings GitHub
    # still rejected as file-level comments (truly unpostable), and findings
    # whose file was never in the diff at all (already rendered by
    # _partition_review_issues, since it has the existing-comment
    # cross-reference those bodies need).
    standalone_bodies = [inline_comment_to_timeline_body(c) for c in standalone] + list(
        partition.standalone_comments
    )
    comments_failed = sum(
        0 if _main._safe_comment(client, owner, repo, pr_number, body) else 1
        for body in standalone_bodies
    )

    return CommentPostingResult(
        event=event,
        inline_count=inline_count,
        file_comment_count=file_comment_count,
        comment_findings=len(standalone_bodies),
        comments_failed=comments_failed,
    )


def _finalize_review_outcome(
    client: Any,
    job_id: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr: Any,
    files_reviewed: int,
    partition: ReviewIssuePartition,
    posting: CommentPostingResult,
) -> None:
    """Compute severity metrics, assemble ``review_summary``, and write the terminal outcome.

    Preconditions:
        - ``partition``/``posting`` come from the same review run, produced in
          ``_partition_review_issues`` → ``_post_review_comments`` order.
        - ``client`` is an open ``GitHubClient``; ``pr`` carries ``html_url``.
    Postconditions:
        - ``severity_counts`` buckets ``partition.pr_issues`` over the five
          recognized levels (critical/high/medium/low/info); an issue with an
          unrecognized or blank severity counts toward ``total_issues`` but
          not into ``severity_counts``.
        - ``review_summary`` is built with the same keys/semantics as before
          the split: ``total_issues``, ``inline_comments``, ``file_comments``,
          ``comment_findings``, ``comments_failed``, ``event``,
          ``files_reviewed``, ``severity_counts``, ``addressed_issues_dropped``,
          ``pending_issue_proposals``.
        - When ``posting.comments_failed`` is truthy: posts an "incomplete"
          notice via ``_main._safe_comment``, calls :func:`_finalize_review`
          with ``JobStatus.FAILED``, and returns — no further writes happen.
        - Otherwise: builds ``status_text`` (finding counts plus optional
          "pre-existing bugs"/"already-addressed findings" clauses), reacts
          ``+1`` via :func:`_react_to_pr` only when ``partition.pr_issues`` is
          empty, and calls :func:`_finalize_review` with
          ``JobStatus.COMPLETED``.
        - Exactly one terminal :func:`_finalize_review` call happens per
          invocation. Does not itself catch exceptions from
          ``_finalize_review``/``_main._safe_comment``/``_react_to_pr`` — a
          failure there propagates to the caller's outer handler exactly as
          before the split.
    """
    pr_issues = partition.pr_issues
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
        "inline_comments": posting.inline_count,
        "file_comments": posting.file_comment_count,
        "comment_findings": posting.comment_findings,
        "comments_failed": posting.comments_failed,
        "event": posting.event,
        "files_reviewed": files_reviewed,
        "severity_counts": severity_counts,
        # Findings that matched an already-RESOLVED existing PR comment and
        # so were dropped rather than re-posted (see
        # partition_issues_by_existing_comments above).
        "addressed_issues_dropped": len(partition.addressed_issues),
        # Pre-existing bugs the reviewer flagged in unchanged code, offered
        # to a human on the Code Review page as GitHub-issue candidates.
        # Not posted on this PR. Each carries a stable ``id`` and unset
        # ``issue_url``/``issue_number``. A finding annotate_duplicate_proposals
        # matched to an existing open issue is already tracked, so
        # _partition_review_issues drops it before it ever reaches here --
        # every entry below is a genuinely new candidate.
        "pending_issue_proposals": partition.proposals,
    }
    if posting.comments_failed:
        # Some findings could not be posted as their own comment; the
        # review (inline comments + body) is already submitted, but the
        # contract "one comment per finding" is broken — surface it as a
        # failure rather than reporting completion.
        err = (
            f"{posting.comments_failed} of {posting.comment_findings} finding comment(s) "
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
        f"{posting.inline_count} inline, {posting.file_comment_count} file-level, "
        f"{posting.comment_findings} comment(s), event={posting.event}"
    )
    if partition.proposals:
        noun = "bug" if len(partition.proposals) == 1 else "bugs"
        status_text += f"; {len(partition.proposals)} pre-existing {noun} to review"
    if partition.addressed_issues:
        noun = "finding" if len(partition.addressed_issues) == 1 else "findings"
        status_text += f"; {len(partition.addressed_issues)} already-addressed {noun} skipped"
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

        Orchestrates the review as a straight-line pipeline of named helpers,
        each owning one concern: :func:`_fetch_pr_metadata` (metadata),
        :func:`_decide_review_mode` (whole-file vs. hunk decision, including
        the "nothing to review" no-op exits), :func:`_run_reviewer` (engine
        invocation), :func:`_partition_review_issues` (issue partitioning),
        :func:`_post_review_comments` (comment posting), and
        :func:`_finalize_review_outcome` (finalization). All of it runs inside
        the same GitHub client and the same outer ``try/except`` below, so a
        failure at any step still reaches the one exception handler that
        marks the job failed.
    """
    try:
        with _main.GitHubClient(token=token) as client:
            pr, files, reviewer_login = _fetch_pr_metadata(client, owner, repo, pr_number)

            mode = _decide_review_mode(client, job_id, owner, repo, pr_number, pr, files)
            if mode is None:
                return

            output = _run_reviewer(
                provider,
                client,
                owner,
                repo,
                pr_number,
                job_id,
                pr,
                files,
                mode.hunk_files,
                head_files=mode.head_files or None,
                change_surface=mode.change_surface,
                repo_reader=mode.repo_reader,
            )
            if output is None:
                return

            _tag_review_issues_for_scope(output, mode, pr, files)

            partition = _partition_review_issues(
                output,
                client,
                owner,
                repo,
                pr_number,
                mode.valid_by_path,
                mode.changed_by_path,
            )
            posting = _post_review_comments(
                client, owner, repo, pr_number, pr, reviewer_login, output, partition
            )
            _finalize_review_outcome(
                client, job_id, owner, repo, pr_number, pr, mode.files_reviewed, partition, posting
            )
    except Exception as review_exc:  # noqa: BLE001 - any failure must mark the job, never wedge it
        # The hook runs in a daemon thread; if we let an exception escape, the thread
        # dies and the job is stuck in "running" forever. Mark it failed (mirroring
        # post_run) and post a best-effort, token-scrubbed PR comment.
        safe_err = scrub_token_from_text(str(review_exc))
        logger.error("PR review hook failed: %s", safe_err)
        try:
            with _main.GitHubClient(token=token) as client:
                # Same graceful-degradation contract as the reviewer paths: record
                # the detail in the store, keep the raw exception off the PR.
                _main._record_review_outage(
                    client, owner, repo, pr_number, job_id, f"code review failed: {safe_err}"
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
            try:
                _finalize_review(job_id, JobStatus.FAILED, phase="completed", error=safe_err)
            except Exception as finalize_exc:  # noqa: BLE001 - store unreachable; nothing more we can do
                safe_finalize_err = scrub_token_from_text(str(finalize_exc))
                logger.error(
                    "PR review %s: last-resort finalize failed: %s",
                    job_id,
                    safe_finalize_err,
                )


def _react_to_pr(client: _main.GitHubClient, owner: str, repo: str, pr_number: int) -> None:
    """Best-effort +1 reaction on the PR itself, celebrating a clean review.

    Postconditions: adds a "+1" reaction to PR #``pr_number``. Never raises — a
    failure here (rate limit, missing scope, transport error) must not turn an
    otherwise-successful clean review into a failed job.
    """
    try:
        client.create_issue_reaction(owner, repo, pr_number, content="+1")
    except Exception as exc:  # noqa: BLE001 - reaction is a courtesy signal only
        logger.warning(
            "Could not add +1 reaction to PR #%s: %s",
            pr_number,
            scrub_token_from_text(str(exc)),
        )


# ---------------------------------------------------------------------------
# Pre/post hooks for the GitHub flow (no orchestrator changes)
# ---------------------------------------------------------------------------


def _safe_comment(
    client: _main.GitHubClient, owner: str, repo: str, number: int, body: str
) -> bool:
    """Best-effort issue comment; never blocks the job on a failed comment.

    Body is scrubbed to redact tokens that might have leaked from any source
    (e.g., git stderr, engine output).

    Postconditions:
        - Returns True when the comment was posted, False on any failure (GitHub
          API rejection or any other exception from ``add_issue_comment``).
          Never raises — callers that must not drop a finding inspect the result.
    """
    try:
        client.add_issue_comment(owner, repo, number, scrub_token_from_text(body))
        return True
    except Exception as e:  # noqa: BLE001 - comment is best-effort, never fails the job
        logger.warning("Failed to comment on issue #%s: %s", number, scrub_token_from_text(str(e)))
        return False
