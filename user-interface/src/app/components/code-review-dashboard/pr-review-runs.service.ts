import { ChangeDetectorRef, Injectable, OnDestroy, inject } from '@angular/core';
import { Subject, Subscription } from 'rxjs';
import { takeUntil } from 'rxjs/operators';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { pollJobStatus } from '../../services/job-status-poller';
import type {
  CodeReviewRunItem,
  GitHubPullRequestItem,
  GitHubRepoItem,
  RunPrReviewResponse,
} from '../../models/integrations.model';
import type { CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { LatestOnly } from '../../shared/latest-only';
import type { PrReviewRecord } from './pr-review-record.model';
import {
  badgeClass as badgeClassFor,
  badgeLabel as badgeLabelFor,
  isLatestRunning as isLatestRunningFor,
  terminalTimestamp,
} from './review-metrics';

/** Shared empty result for `reviewsFor` misses, so callers binding it to a template
 * input (or comparing by reference) don't see a fresh array identity on every call. */
const EMPTY_REVIEWS: readonly PrReviewRecord[] = Object.freeze([]);

/**
 * Owns the Code Review page's review-run domain: hydrating review history from the
 * backend, starting new reviews, live-polling them to completion, and filing GitHub
 * issues from a completed review's proposals — everything `CodeReviewDashboardComponent`
 * used to hold directly (issue: the component mixed this with PR-list management).
 *
 * Provided in `CodeReviewDashboardComponent`'s own `providers` array (not `providedIn: 'root'`)
 * so each dashboard instance gets a fresh service instance whose state resets cleanly on
 * navigation, mirroring `AgentStudioStateService`. Because of that, `inject(ChangeDetectorRef)`
 * below resolves to the *hosting* `CodeReviewDashboardComponent`'s change detector, so this
 * service can call `markForCheck()` itself wherever the component used to.
 *
 * IMPORTANT: this dependency on `ChangeDetectorRef` means this service is ONLY safe to
 * provide inside a component's own `providers` array. Do not change this to
 * `providedIn: 'root'` and do not provide it on a shared/ancestor component — a root
 * provider has no `ChangeDetectorRef` in its injector and construction throws
 * `NullInjectorError`; an ancestor-component provider would silently mark the *ancestor's*
 * view instead of this dashboard's, and live updates would stop rendering with no error.
 *
 * This service is the sole owner of "which repo is current": every method that acts on
 * the expanded repo reads `currentRepo` (set only by {@link reset}) rather than taking a
 * `repo` parameter, so there is exactly one place a caller/service disagreement could
 * happen — call {@link reset} before {@link hydrate}, {@link isStarting}, or
 * {@link startReview} so they act on the intended repo.
 *
 * Invariants: `reviews`/`reviewErrors` only ever hold records for `currentRepo` — callers
 * must call {@link reset} before hydrating a newly-expanded repo so records from a
 * previous repo can never render under another repo's identically-numbered PR (PR numbers
 * collide across repositories). `starting`/`creatingIssues`/`createIssueErrors` are keyed by
 * `owner/repo#number` or `jobId`, which cannot collide across repos, so `reset` does not
 * touch them.
 */
@Injectable()
export class PrReviewRunsService implements OnDestroy {
  private readonly api = inject(CodingTeamApiService);
  private readonly integrationsApi = inject(IntegrationsApiService);
  private readonly cdr = inject(ChangeDetectorRef);

  // All review runs per PR number, newest-first. Hydrated from the backend on
  // load and updated live by the per-job pollers below.
  private _reviews = new Map<number, PrReviewRecord[]>();

  /**
   * Read-only view so external code cannot bypass reset/hydrate/startReview to mutate state directly.
   *
   * Preconditions: none.
   * Postconditions: returns the live per-PR review map typed read-only, including its
   * per-PR arrays (the same references the pollers/hydrate mutate, so bound views stay
   * current); callers must not mutate the map or the arrays it holds. Pure — no side effects.
   */
  get reviews(): ReadonlyMap<number, readonly PrReviewRecord[]> {
    return this._reviews;
  }

  // Per-PR "Start Review" failures, shown inside that PR's expanded panel so a
  // start error and a list-load error never clobber each other. Private — read
  // through `reviewErrorFor`, mutated only by `reset`/`startReview`.
  private readonly reviewErrors = new Map<number, string>();

  // Reviews whose Start Review request is in flight (disables the button). Keyed by
  // `owner/repo#number`, NOT bare PR number: PR numbers collide across repositories, so a
  // bare-number key would let an in-flight start in one repo block the same-numbered PR in
  // another (and is why this set does NOT need clearing on repo switch). Private — read
  // through `isStarting`.
  private readonly starting = new Set<string>();

  // Job ids whose "create issues" request is in flight (disables the button). Private
  // backing field; the template/child bind to the read-only `creatingIssues` view below,
  // and only `createIssuesFor` mutates it.
  private readonly _creatingIssues = new Set<string>();

  /**
   * Read-only view of the in-flight "create issues" job ids (template/child binding).
   *
   * Preconditions: none.
   * Postconditions: returns the live set of job ids with a "create issues" request in
   * flight, typed read-only (same reference `createIssuesFor` mutates); callers must not
   * mutate it. Pure — no side effects.
   */
  get creatingIssues(): ReadonlySet<string> {
    return this._creatingIssues;
  }

  // Per-review "create issues" failure, shown beneath that review's proposals. Private
  // backing field behind the read-only `createIssueErrors` view below; only
  // `createIssuesFor` mutates it.
  private readonly _createIssueErrors = new Map<string, string>();

  /**
   * Read-only view of per-job "create issues" failures (template/child binding).
   *
   * Preconditions: none.
   * Postconditions: returns the live map of job id → last "create issues" error message,
   * typed read-only (same reference `createIssuesFor` mutates); callers must not mutate it.
   * Pure — no side effects.
   */
  get createIssueErrors(): ReadonlyMap<string, string> {
    return this._createIssueErrors;
  }

  // The repo whose reviews/pollers this service currently holds; set by `reset`. The
  // sole source of truth for "which repo is current" — hydrate/isStarting/startReview
  // read this instead of taking a repo parameter (see the class doc).
  private currentRepo: GitHubRepoItem | null = null;

  // "Latest wins" guard so a slow hydrate response from a superseded repo load can't
  // overwrite a newer one (rapid collapse/re-expand of the same repo, or a fast repo switch).
  private readonly reviewsLoad = new LatestOnly();

  // Live status pollers keyed by job id, so `reset`/`ngOnDestroy` can tear them all down.
  private pollers = new Map<string, Subscription>();

  /**
   * Emits a human-readable sentence each time a live-polled review reaches a terminal
   * state or its poller loses the connection — consumed by `CodeReviewDashboardComponent`
   * to drive a visually-hidden `role="status"` live region so screen-reader users hear a
   * review finish without watching the row badge. Fires at most once per `startPolling`
   * call: the terminal and connection-lost callbacks are mutually exclusive and each
   * disposes its own poller immediately (see `startPolling`), so a hydrated
   * already-terminal review (loaded via `hydrate`, never live-polled) never announces.
   * Wording is completed/failed/cancelled, matching the outcome `badgeLabel` shows (a
   * cancelled run is reported as cancelled, not folded into "completed"), so the
   * announcement never disagrees with the row badge it is narrating. Never completed by
   * this service — the subscribing component's own `takeUntil(destroy$)` is the sole
   * teardown mechanism.
   */
  readonly announce$ = new Subject<string>();

  // Completes on destroy; every HTTP subscription is gated on it so a late
  // response can't update a torn-down component.
  private readonly destroy$ = new Subject<void>();

  /**
   * Drop everything keyed by bare PR number and adopt `repo` as the current repo. PR
   * numbers collide across repositories, so records and errors from one repo must never
   * render under another's rows.
   *
   * Pollers are disposed too, not left running: a poller mutates its record object, but
   * this clears the `reviews` map those records live in, so a surviving poller would
   * update an orphan nothing renders while a later hydrate rebuilds a *fresh* record whose
   * `startPolling` would no-op (the jobId still looked "polled"), freezing the row.
   * Disposing here means re-expanding the repo re-fetches history and attaches a fresh
   * poller to the shown record.
   *
   * Preconditions: `repo` is the newly-expanded repo, or `null` when all repo rows are
   * collapsed.
   * Postconditions: `reviews`/`reviewErrors` are empty; no pollers remain running;
   * `currentRepo` is `repo`.
   */
  reset(repo: GitHubRepoItem | null): void {
    this.stopAllPollers();
    this._reviews = new Map();
    this.reviewErrors.clear();
    this.currentRepo = repo;
  }

  /**
   * Reconcile the in-memory review map with the backend for the current repo. The
   * backend is the source of truth, but any review that still has a live poller is
   * preserved as the *same* object its poller mutates — so a review started while this
   * request was on the wire is never dropped and its poller is never killed (closing a
   * hydrate-vs-startReview race). Records from a *different* repository are never folded
   * in: PR numbers collide across repos, so the rebuilt map holds this repo's reviews only.
   *
   * Note: this fetches the repository's recent reviews in one call (the backend `limit`,
   * default 500) rather than per-PR, because the row status badges need the latest review
   * for *every* listed PR up front; a per-PR-on-expand fetch would leave the list without
   * badges. Consequence of the cap: in a repo with more than ~500 recent runs, the oldest
   * runs (and the badges for the least active PRs) may be absent until a future "latest
   * run per PR" backend query lands. Best-effort: a failure leaves the page usable
   * without history.
   *
   * Preconditions: none — no-ops when no repo is current (call {@link reset} first).
   * Postconditions: on success, `reviews` holds the current repo's review history plus
   * any still-live in-flight reviews, in newest-first order per PR; non-terminal runs
   * resume polling. A failure, or a repo switch before the response arrives, leaves
   * `reviews` unchanged.
   */
  hydrate(): void {
    const repo = this.currentRepo;
    if (!repo) return;
    const token = this.reviewsLoad.next();
    this.integrationsApi
      .getGitHubReviewHistory({ owner: repo.owner, repo: repo.name })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (items) => {
          // Drop if superseded by a newer repo load, or if the user is no longer on this
          // repo (a switch that didn't issue a new hydrate) — PR numbers collide across repos.
          if (!this.reviewsLoad.isCurrent(token) || this.currentRepo?.full_name !== repo.full_name) return;
          // Records that still have a live poller must survive the rebuild as the
          // same object their poller writes to, or the UI stops updating.
          const live = new Map<string, PrReviewRecord>();
          for (const list of this._reviews.values()) {
            for (const record of list) {
              if (this.pollers.has(record.jobId)) live.set(record.jobId, record);
            }
          }
          const map = new Map<number, PrReviewRecord[]>();
          const seen = new Set<string>();
          for (const item of items) {
            // Prefer the live record so its poller keeps updating the shown object.
            const record = live.get(item.job_id) ?? this.toRecord(item, repo);
            seen.add(record.jobId);
            const list = map.get(record.prNumber) ?? [];
            list.push(record); // backend returns newest-first; preserve that order
            map.set(record.prNumber, list);
          }
          // Carry over any still-polling review the snapshot didn't include yet
          // (e.g. one started while this request was in flight) — but only when it
          // belongs to this repository, so a switched-away repo's run can't surface
          // under another repo's identical PR number. Collected per PR first, then
          // prepended as one batch: unshifting each live record individually (in
          // `live`'s already newest-first order) would reverse their relative order
          // when more than one is carried over for the same PR.
          const carryOver = new Map<number, PrReviewRecord[]>();
          for (const [jobId, record] of live) {
            if (seen.has(jobId)) continue;
            if (record.owner !== repo.owner || record.repo !== repo.name) continue;
            const list = carryOver.get(record.prNumber) ?? [];
            list.push(record);
            carryOver.set(record.prNumber, list);
          }
          for (const [prNumber, records] of carryOver) {
            map.set(prNumber, [...records, ...(map.get(prNumber) ?? [])]);
          }
          this._reviews = map;
          for (const list of map.values()) {
            for (const record of list) {
              if (!isCodingTeamTerminalStatus(record.status) && !record.error) {
                this.startPolling(record); // guarded against double-start
              }
            }
          }
          this.cdr.markForCheck();
        },
        error: () => {
          // History is a best-effort enhancement; the PR list still works without it.
        },
      });
  }

  /**
   * Map one backend review-run row into a `PrReviewRecord`.
   *
   * Preconditions: `item` is a review-history row for `repo`.
   * Postconditions: returns a record with `owner`/`repo` from `repo`; `startedAt` from
   * `item.created_at` (falling back to the browser clock when absent/unparseable);
   * `completedAt` from `item.completed_at` when present and parseable, else `undefined`.
   * Pure — no side effects.
   */
  private toRecord(item: CodeReviewRunItem, repo: GitHubRepoItem): PrReviewRecord {
    const parsed = Date.parse(item.created_at);
    // completed_at is present only on terminal runs; an unparseable/absent value
    // leaves completedAt undefined so the row shows no duration.
    const completed = item.completed_at ? Date.parse(item.completed_at) : NaN;
    return {
      jobId: item.job_id,
      prNumber: item.pr_number,
      owner: repo.owner,
      repo: repo.name,
      startedAt: Number.isNaN(parsed) ? Date.now() : parsed,
      completedAt: Number.isNaN(completed) ? undefined : completed,
      status: item.status,
      statusText: item.status_text,
      reviewSummary: item.review_summary,
      prUrl: item.pr_url,
      error: item.error,
    };
  }

  /**
   * Repo-scoped key for the in-flight `starting` set (PR numbers collide across repos).
   *
   * Preconditions: `repo` is a repository item; `prNumber` is a PR number.
   * Postconditions: returns a stable `owner/repo#number` key, lowercased so it is
   * case-insensitive (GitHub treats owner/repo case-insensitively). Pure — no side effects.
   */
  private startKey(repo: GitHubRepoItem, prNumber: number): string {
    return `${repo.owner.toLowerCase()}/${repo.name.toLowerCase()}#${prNumber}`;
  }

  /**
   * Whether a Start Review request for this PR in the current repo is in flight.
   *
   * Preconditions: `prNumber` is a PR number from the current repo's list.
   * Postconditions: returns true iff a repo is current and its `owner/repo#prNumber` key
   * is in `starting`. Pure — no side effects.
   */
  isStarting(prNumber: number): boolean {
    return !!this.currentRepo && this.starting.has(this.startKey(this.currentRepo, prNumber));
  }

  /**
   * The "Start Review" error for a PR, if its last attempt failed.
   *
   * Preconditions: `prNumber` is a PR number from the current repo's list.
   * Postconditions: returns the stored start-review error for `prNumber`, or null when the
   * last attempt did not fail. Pure — no side effects.
   */
  reviewErrorFor(prNumber: number): string | null {
    return this.reviewErrors.get(prNumber) ?? null;
  }

  /**
   * Start a code review on `pull` in the current repo, recording it and polling it live.
   *
   * Preconditions: `pull` is one of the current repo's open PRs.
   * Postconditions: no-op when no repo is current, or a start for
   * `owner/repo#pull.number` is already in flight. Otherwise fires the start request; on
   * success, and only while still on the same repo, adds a new `PrReviewRecord` to that
   * PR's list (unless a record for the same job already exists there — e.g. a hydrate
   * that ran while this request was in flight already added it) and begins polling it; on
   * failure, and only while still on the same repo, records the message under
   * `pull.number` in `reviewErrors`. Always calls `markForCheck()`.
   */
  startReview(pull: GitHubPullRequestItem): void {
    const repo = this.currentRepo;
    if (!repo) return;
    const key = this.startKey(repo, pull.number);
    if (this.starting.has(key)) return;
    this.starting.add(key);
    this.reviewErrors.delete(pull.number);
    this.integrationsApi
      .runGitHubReviewPr({ pr_number: pull.number, owner: repo.owner, repo: repo.name })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (resp: RunPrReviewResponse) => {
          this.starting.delete(key);
          // Prefer the server-clock start time so the live duration is computed from
          // server timestamps at both ends (this start + the server completion stamped
          // in startPolling), avoiding a browser-vs-server clock-skew mismatch. Fall
          // back to the browser clock when the server didn't supply one.
          const parsedStart = resp.created_at ? Date.parse(resp.created_at) : NaN;
          const record: PrReviewRecord = {
            jobId: resp.job_id,
            prNumber: pull.number,
            owner: repo.owner,
            repo: repo.name,
            startedAt: Number.isNaN(parsedStart) ? Date.now() : parsedStart,
            status: resp.status,
            prUrl: resp.pr_url,
          };
          // Only record AND poll while the user is still on the repo the review targeted.
          // If they switched away, don't spin an orphan poller for an off-screen record —
          // the hydrate on return re-fetches this run's history and attaches a fresh poller.
          if (this.currentRepo?.full_name === repo.full_name) {
            const list = this._reviews.get(pull.number) ?? [];
            // A concurrent hydrate may have already picked up this job (it was already
            // persisted server-side when this response was still in flight) and attached
            // its own poller; adding a second record here would leave that duplicate
            // stuck, since startPolling below no-ops once a poller for the job exists.
            if (!list.some((r) => r.jobId === record.jobId)) {
              list.unshift(record); // newest-first
              this._reviews.set(pull.number, list);
              this.startPolling(record);
            }
          }
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.starting.delete(key);
          // Only surface the error while the user is still on the repo the review targeted;
          // reviewErrors is keyed by bare PR number, so an unguarded set would render this
          // failure under another repo's identically-numbered PR after a switch.
          if (this.currentRepo?.full_name === repo.full_name) {
            this.reviewErrors.set(pull.number, extractErrorDetail(err, 'Failed to start review.'));
          }
          this.cdr.markForCheck();
        },
      });
  }

  /**
   * Begin polling a review job's status. Mutates `record` in place on each
   * update (and calls `markForCheck()` so the UI refreshes under any change
   * detection strategy). The subscription is registered in `pollers` for
   * teardown and is explicitly unsubscribed — and removed from `pollers` — once
   * the job reaches a terminal state or the connection is lost, so no poller
   * outlives the job. `reset`/`ngOnDestroy` tear down any still-running pollers.
   *
   * Preconditions: `record` is not already being polled.
   * Postconditions: no-op if `record.jobId` is already in `pollers`. Otherwise registers
   * a poller subscription under `record.jobId` that mutates `record` in place on each
   * status update and removes itself from `pollers` once terminal or connection-lost.
   * Exactly one of a terminal status or a connection-lost error causes `announce$` to
   * emit once for this `record`, since both paths dispose the poller immediately.
   */
  private startPolling(record: PrReviewRecord): void {
    if (this.pollers.has(record.jobId)) return;
    const sub = pollJobStatus(
      this.api,
      record.jobId,
      (status: CodingTeamJobStatus) => {
        // Mutate the record in place; markForCheck() makes the live badge/table
        // refresh independent of the change-detection strategy (safe under OnPush).
        record.status = status.status;
        record.statusText = status.status_text;
        record.reviewSummary = status.review_summary ?? record.reviewSummary;
        record.prUrl = status.github_pr_url ?? record.prUrl;
        record.error = status.error;
        if (isCodingTeamTerminalStatus(status.status)) {
          // Stamp the completion time when the run first goes terminal so the row can
          // show a duration without a reload, using the server's terminal timestamp
          // (see terminalTimestamp) rather than the browser clock. `??=` so a value
          // from a prior hydrate is never overwritten.
          record.completedAt ??= terminalTimestamp(status);
          // Same failed-vs-completed test as badgeClass (review-metrics.ts), so this
          // sentence never disagrees with the row badge it narrates. 'cancelled' is
          // checked first and reported as-is: it's a distinct terminal outcome (see
          // CODING_TEAM_TERMINAL_STATUSES), and folding it into "completed" would
          // contradict badgeLabel, which already shows the raw status for it.
          const outcome =
            record.status === 'cancelled'
              ? 'cancelled'
              : record.error || record.status === 'failed'
                ? 'failed'
                : 'completed';
          this.announce$.next(`Review for pull request #${record.prNumber} ${outcome}.`);
          this.disposePoller(record.jobId);
        }
        this.cdr.markForCheck();
      },
      () => {
        record.error = 'Lost connection to the coding team — status polling failed.';
        // Reuse the same error text shown on the row (pr-review-detail.component.html)
        // rather than a second hand-written copy of the sentence.
        this.announce$.next(`Review for pull request #${record.prNumber}: ${record.error}`);
        this.disposePoller(record.jobId);
        this.cdr.markForCheck();
      },
    );
    this.pollers.set(record.jobId, sub);
  }

  /**
   * Unsubscribe a poller and drop it from the registry (idempotent).
   *
   * Preconditions: `jobId` is a job id, possibly not in `pollers`.
   * Postconditions: `pollers` no longer has an entry for `jobId`, and any subscription
   * that was registered there is unsubscribed.
   */
  private disposePoller(jobId: string): void {
    this.pollers.get(jobId)?.unsubscribe();
    this.pollers.delete(jobId);
  }

  /**
   * Preconditions: none.
   * Postconditions: every subscription in `pollers` is unsubscribed and `pollers` is empty.
   */
  private stopAllPollers(): void {
    for (const sub of this.pollers.values()) {
      sub.unsubscribe();
    }
    this.pollers.clear();
  }

  /**
   * All review runs for a PR, newest-first.
   *
   * Preconditions: `prNumber` is a PR number.
   * Postconditions: returns this PR's review list (a shared empty array when it has
   * none — the same reference every time, so callers comparing/binding it by identity
   * see no spurious change). The array is the service's own storage typed read-only —
   * callers must not mutate it, and it stays the *same* reference the live pollers write
   * to (so the detail child sees live updates). Pure — no side effects.
   */
  reviewsFor(prNumber: number): readonly PrReviewRecord[] {
    return this._reviews.get(prNumber) ?? EMPTY_REVIEWS;
  }

  /**
   * The most recent review run for a PR, or null.
   *
   * Preconditions: `prNumber` is a PR number.
   * Postconditions: returns the newest recorded run for `prNumber`, or null when it has
   * none. Pure — no side effects.
   */
  private latestReview(prNumber: number): PrReviewRecord | null {
    return this.reviewsFor(prNumber)[0] ?? null;
  }

  /**
   * True when a PR's latest review is still running (drives the row spinner).
   *
   * Preconditions: `prNumber` is a PR number.
   * Postconditions: returns true iff `prNumber`'s latest run is non-terminal and un-errored.
   * Pure — delegates to `isLatestRunning` in review-metrics.
   */
  isLatestRunning(prNumber: number): boolean {
    return isLatestRunningFor(this.latestReview(prNumber));
  }

  /**
   * Row status badge text derived from the latest review, or null when none.
   *
   * Preconditions: `prNumber` is a PR number.
   * Postconditions: returns the badge label for `prNumber`'s latest run, or null when it
   * has none. Pure — delegates to `badgeLabel` in review-metrics.
   */
  badgeLabel(prNumber: number): string | null {
    return badgeLabelFor(this.latestReview(prNumber));
  }

  /**
   * Row status badge CSS class derived from the latest review.
   *
   * Preconditions: `prNumber` is a PR number.
   * Postconditions: returns the badge CSS class for `prNumber`'s latest run (`''` when it
   * has none). Pure — delegates to `badgeClass` in review-metrics.
   */
  badgeClass(prNumber: number): string {
    return badgeClassFor(this.latestReview(prNumber));
  }

  /**
   * File GitHub issues for the given (child-selected) proposal ids of a
   * review. On success the record's proposal list is replaced with the
   * server's updated copy (filed proposals now carry `issue_url`) — the child
   * component reconciles its own selection against that fresh list.
   *
   * Preconditions: `record` is one of this repo's review runs; `ids` are proposal ids
   * from that run's summary.
   * Postconditions: no-op when `ids` is empty or a request for `record.jobId` is already
   * in flight. Otherwise marks the job in `creatingIssues` for the request's duration; on
   * success, looks up the *current* record for `record.prNumber`/`record.jobId` (a repo
   * collapse/re-expand between the request firing and this response may have rebuilt
   * `reviews` with a fresh record for the same job, orphaning the one passed in here) and
   * replaces its `reviewSummary.pending_issue_proposals` with the server's copy — a no-op
   * if that job no longer has a current record; on failure records the message in
   * `createIssueErrors` under `record.jobId`. Always calls `markForCheck()`.
   */
  createIssuesFor(record: PrReviewRecord, ids: string[]): void {
    const jobId = record.jobId;
    const prNumber = record.prNumber;
    if (ids.length === 0 || this._creatingIssues.has(jobId)) return;
    this._creatingIssues.add(jobId);
    this._createIssueErrors.delete(jobId);
    this.integrationsApi
      .createGitHubReviewIssues(record.owner, record.repo, jobId, ids)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (resp) => {
          this._creatingIssues.delete(jobId);
          const current = this._reviews.get(prNumber)?.find((r) => r.jobId === jobId);
          if (current?.reviewSummary) {
            current.reviewSummary = {
              ...current.reviewSummary,
              pending_issue_proposals: resp.proposals,
            };
          }
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this._creatingIssues.delete(jobId);
          this._createIssueErrors.set(jobId, extractErrorDetail(err, 'Failed to create issue(s).'));
          this.cdr.markForCheck();
        },
      });
  }

  /**
   * Tears down all pollers and completes `destroy$`. Called by Angular when the
   * hosting `CodeReviewDashboardComponent` is destroyed (this service is component-provided).
   *
   * Preconditions: none.
   * Postconditions: `destroy$` is completed (so any subscription still gated on it via
   * `takeUntil` unsubscribes); every poller subscription is unsubscribed and `pollers`
   * is empty.
   */
  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
    this.stopAllPollers();
  }
}
