import { Component, DestroyRef, ElementRef, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { Subject, Subscription, timer, of } from 'rxjs';
import { catchError, switchMap, tap } from 'rxjs/operators';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CodingTeamApiService } from '../../services/coding-team-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { pollJobStatus } from '../../services/job-status-poller';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { SettleAnnouncer } from '../../shared/settle-announcer';
import { EmptyStateComponent } from '../../shared/empty-state/empty-state.component';
import { CodingTeamMonitorComponent } from '../coding-team-monitor/coding-team-monitor.component';
import { TeamAssistantChatComponent } from '../team-assistant-chat/team-assistant-chat.component';
import { OutOfScopeIssuesComponent } from './out-of-scope-issues/out-of-scope-issues.component';
import {
  PendingQuestionsComponent,
  type AnswersSubmittedStatus,
} from '../pending-questions/pending-questions.component';
import type {
  AddressPrCommentsResponse,
  GitHubIssueItem,
  GitHubPullRequestItem,
  GitHubRepoItem,
  OutOfScopeProposalItem,
  RunGitHubIssueResponse,
} from '../../models/integrations.model';
import type {
  CodingTeamGitHubContext,
  CodingTeamJobListItem,
  CodingTeamJobStatus,
} from '../../models/coding-team.model';
import type { TeamAssistantFieldSpec } from '../../models/team-assistant.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { deferFocus } from '../../shared/defer-focus';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { LatestOnly } from '../../shared/latest-only';
import { resultCountAnnouncement } from '../../shared/result-count-announcement';
import { NotificationService } from '../../core/notification.service';
import {
  appendActivityNarrative,
  emptyActivityNarrative,
  thoughtStreamPanelTitle,
  type ActivityNarrativeState,
} from './activity-narrative';

/** How often the Runs list is re-fetched while the page is open. */
const RUNS_POLL_MS = 15000;

/** Quiet period after the last "thinking" token before announcing a settled line count — tuned for
 * a raw LLM token stream (many updates per second), not this page's HTTP poll cadence. Local to
 * this component; NOT shared with coding-team-monitor.component.ts's summary announcer, which polls
 * at a much slower, fixed cadence and needs its own differently-sized window (see
 * SUMMARY_ANNOUNCE_SETTLE_MS there) — only the SettleAnnouncer timer mechanism is shared. */
const THINKING_ANNOUNCE_SETTLE_MS = 1500;

/** localStorage key for the last repo the user expanded, so it can be pre-expanded on return. */
const LAST_REPO_STORAGE_KEY = 'coding-team-last-repo-v1';

/**
 * Precomputed view-model for one run row, so the Jobs accordion template binds plain properties
 * instead of calling helper methods per change-detection cycle. Rebuilt from `runs` in `applyRuns`
 * (every poll), so the relative `timeAgo` refreshes on the poll cadence.
 */
interface RunRowVm {
  run: CodingTeamJobListItem;
  issueNumber?: number;
  prNumber?: number;
  /** "#42" for an issue-driven run, "#42 (PR)" for a PR-remediation run; '' when neither is known. */
  displayLabel: string;
  /** "owner/repo" of the run's repository, so rows from different repos are tellable apart. */
  repoLabel: string;
  status: string;
  badgeClass: string;
  waiting: boolean;
  /** Live status/phase line for active runs; null when there's nothing to show (no empty tooltip). */
  detail: string | null;
  timeAgo: string;
}

/**
 * The Runs panel's identity model: a coding-team job is driven either by a GitHub issue (the
 * "run an issue" flow) or by a pull request (the address-comments remediation flow, whose parent
 * and per-comment child jobs carry `pr_number` instead of `issue_number`). Widening the panel to a
 * discriminated union over the two, rather than assuming `issue_number` everywhere, is what lets a
 * PR-driven job be listed, selected, and reached for answering paused HITL questions.
 */
type RunIdentity = { type: 'issue'; number: number } | { type: 'pr'; number: number };

/** The coding-team page's available sub-views. */
type CodingTeamPageView = 'chat' | 'github' | 'pulls' | 'jobs' | 'issues';

/**
 * The run identity carried by a job's GitHub context, or null when it carries neither.
 * If both `issue_number` and `pr_number` are present, the issue identity wins so the Runs
 * panel never treats one job as two distinct identities.
 */
function runIdentity(ctx: CodingTeamGitHubContext | undefined): RunIdentity | null {
  if (ctx?.issue_number != null) return { type: 'issue', number: ctx.issue_number };
  if (ctx?.pr_number != null) return { type: 'pr', number: ctx.pr_number };
  return null;
}

/**
 * Stable identity of one issue/PR across repositories ("owner/repo#number" for an issue,
 * "owner/repo#pr-number" for a PR, lowercased) so "In progress" chips from one repo can never
 * light up the same number in another repo — or an issue and a PR that happen to share a number.
 */
function runKey(owner: string | undefined, repo: string | undefined, identity: RunIdentity): string {
  const suffix = identity.type === 'pr' ? `pr-${identity.number}` : `${identity.number}`;
  return `${(owner ?? '').toLowerCase()}/${(repo ?? '').toLowerCase()}#${suffix}`;
}

/** Stable identity for an in-flight PR-comment remediation request. */
function prAddressKey(owner: string, repo: string, prNumber: number): string {
  return `${owner.toLowerCase()}/${repo.toLowerCase()}#${prNumber}`;
}

/**
 * Precomputed view-model for one issue row, so the GitHub list template binds plain properties
 * instead of calling helper methods per change-detection cycle. Rebuilt whenever the visible page or
 * the "In progress" chip set changes.
 */
interface IssueRowVm {
  issue: GitHubIssueItem;
  number: number;
  title: string;
  labels: string[];
  inProgress: boolean;
  hasDeps: boolean;
  blocked: boolean;
  openDepsCount: number;
  depsTooltip: string;
}

/**
 * Main page for the Coding Team feature, hosting three single-select views: the job Runs
 * panel (default), a GitHub issue browser, and the assistant chat.
 *
 * The GitHub view lists every repository the configured PAT can access — repository access
 * is defined by the PAT's own authorization, not by per-repo Khala configuration. Expanding
 * a repo loads its open issues; starting a run targets that repo via per-request owner/repo
 * parameters. The Runs panel shows runs from every repository, and both the "In progress"
 * chips and the run rows are keyed by `owner/repo#number` (see {@link runKey}) so
 * identical issue numbers across repositories can never collide.
 */
@Component({
  selector: 'app-coding-team-page',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatIconModule,
    MatButtonModule,
    MatButtonToggleModule,
    MatProgressSpinnerModule,
    MatChipsModule,
    MatTooltipModule,
    MatExpansionModule,
    MatFormFieldModule,
    MatInputModule,
    MatPaginatorModule,
    RouterLink,
    HealthIndicatorComponent,
    LoadingSpinnerComponent,
    EmptyStateComponent,
    CodingTeamMonitorComponent,
    TeamAssistantChatComponent,
    OutOfScopeIssuesComponent,
    PendingQuestionsComponent,
    InlineBannerComponent,
  ],
  templateUrl: './coding-team-page.component.html',
  styleUrl: './coding-team-page.component.scss',
})
export class CodingTeamPageComponent implements OnInit, OnDestroy {
  private readonly api = inject(CodingTeamApiService);
  private readonly integrationsApi = inject(IntegrationsApiService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly notifications = inject(NotificationService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  /** Which single view is visible. The page opens on the job Runs panel. */
  private _activeView: CodingTeamPageView = 'jobs';

  get activeView(): CodingTeamPageView {
    return this._activeView;
  }

  set activeView(view: CodingTeamPageView) {
    this._activeView = view;
    // Auto-load out-of-scope issues when switching to the Issues tab with a repo selected.
    if (view === 'issues' && this.selectedRepo && this.oosProposals.length === 0 && !this.oosLoading) {
      this.loadOutOfScopeIssues();
    }
    // Auto-load open PRs when switching to the Pull Requests tab with a repo selected.
    // Gated on `pullsLoaded`, NOT on `pulls.length === 0`: a repo with no open PRs
    // is a successful load that legitimately leaves the array empty, and a
    // length-based guard would re-fetch it on every single tab switch. The flag
    // still permits a retry after a failure — `loadPulls` sets it only on success
    // — and `selectRepo` clears it so a repo switch always re-fetches.
    if (view === 'pulls' && this.selectedRepo && !this.pullsLoaded && !this.loadingPulls) {
      this.loadPulls();
    }
  }

  // Embedded assistant chat configuration — named properties rather than template literals, so the
  // chat panel's wiring lives in one place.
  /** Assistant workflow endpoint for the embedded chat. */
  readonly teamApiUrl = '/api/coding-team/assistant';
  readonly chatTeamName = 'Coding Team';
  readonly chatTeamDescription = 'Software Engineering sub-team — task graph and implementation';
  readonly chatFields: TeamAssistantFieldSpec[] = [
    { key: 'repo_path', label: 'Repository path', placeholder: 'Path to project repository', required: true },
    { key: 'plan_input', label: 'Plan / tasks (optional)', placeholder: 'Feature descriptions or structured plan hints' },
  ];

  healthCheck = (): ReturnType<CodingTeamApiService['health']> => this.api.health();

  // GitHub integration state
  githubConfigured = false;
  isLoadingConfig = true;
  // The operator's optional "Default label filter" from the integration config. It is a
  // GLOBAL filter: applied (explicitly and visibly, see the issue-list header) to every
  // repo's issue listing, so the dashboard setting actually takes effect rather than only
  // ever reaching the backend's no-target fallback path (which the repo-scoped UI never uses).
  defaultLabel = '';
  // Whether the configured `defaultLabel` filter is currently applied to the expanded repo.
  // Per-repo and transient: reset to on whenever a repo is (re)expanded (see `resetIssueState`),
  // so the operator can toggle it off from the issue-list header to browse/run on unlabelled
  // issues in this repo without editing the dashboard or affecting other repos.
  labelFilterActive = true;

  // Repository list — every repo the configured PAT can access. The PAT's own
  // authorization configuration is the source of truth; nothing is configured in Khala.
  repos: GitHubRepoItem[] = [];
  loadingRepos = false;
  reposLoaded = false;
  repoError: string | null = null;
  /** The expanded repo whose issues are shown; null when every repo row is collapsed. */
  selectedRepo: GitHubRepoItem | null = null;
  /** Free-text filter over `repos`, matched against `full_name`. */
  repoSearch = '';
  /** Restoring the remembered repo happens at most once per component instance, on the first
   * successful `loadRepos()` — never on a later manual "Refresh", so a user who collapsed it
   * isn't forced back to it. Only cleared on success (see `loadRepos`), so a failed first load
   * still gets a restore attempt on the next successful one. */
  private initialReposLoad = true;

  // Issue list (scoped to the expanded repo)
  issues: GitHubIssueItem[] = [];
  loadingIssues = false;
  issuesLoaded = false;
  issueError: string | null = null;
  /** Free-text filter over `issues`, matched against `title`. Reset per-repo in `resetIssueState`. */
  issueSearch = '';
  // "Latest wins" guard so a slow issue load superseded by a newer one (rapid
  // collapse/re-expand of the same repo, or a repo switch) is discarded, and the
  // loading flag is always cleared by the current handler rather than getting stuck.
  private readonly issuesLoad = new LatestOnly();
  /** View-models for the visible issue page; rebuilt in `recomputeIssueVms`. */
  pagedIssueVms: IssueRowVm[] = [];

  // Issue list pagination (client-side over the fully-fetched issue array)
  readonly PAGE_SIZE_OPTIONS = [10, 25, 50];
  /** Smallest configured page size; the paginator is shown only when the list exceeds it. */
  readonly paginatorThreshold = Math.min(...this.PAGE_SIZE_OPTIONS);
  pageSize = 10;
  pageIndex = 0;

  // Issue selection & confirmation
  private _selectedIssue: GitHubIssueItem | null = null;
  /** Open-dependency refs ("#3, #5") for the selected issue, precomputed so the confirm panel binds a
   * plain string instead of calling `openDepRefs` each change-detection cycle. */
  selectedIssueOpenDepsText = '';
  runningIssue = false;

  get selectedIssue(): GitHubIssueItem | null {
    return this._selectedIssue;
  }

  /** Setting the selected issue refreshes its precomputed open-dependency refs. */
  set selectedIssue(issue: GitHubIssueItem | null) {
    this._selectedIssue = issue;
    this.selectedIssueOpenDepsText = issue ? this.openDepRefs(issue) : '';
  }

  // Pull Requests tab — open PRs for the expanded repo, each with an "address
  // unresolved comments" action.
  /** Open pull requests for the expanded repo (GET /github/pulls). */
  pulls: GitHubPullRequestItem[] = [];
  loadingPulls = false;
  pullsLoaded = false;
  pullError: string | null = null;
  /** PRs whose "address comments" job is currently being started, keyed by `owner/repo#number`
   * (see {@link prAddressKey}) so a double-click can't submit the same PR twice and identical PR
   * numbers across repos never collide. */
  private readonly addressingPrs = new Set<string>();
  // "Latest wins" guard so a slow PR load superseded by a newer one (repo switch) is discarded.
  private readonly pullsLoad = new LatestOnly();

  // Runs panel — the persistent, non-dismissable status panel.
  /** Every coding-team run for this repo (running + recent terminal), newest snapshot from /jobs. */
  runs: CodingTeamJobListItem[] = [];
  /** Non-terminal runs, for the "Running" section. Derived from `runs` in `applyRuns`. */
  runningRuns: CodingTeamJobListItem[] = [];
  /** Terminal runs, for the "Recent" section. Derived from `runs` in `applyRuns`. */
  recentRuns: CodingTeamJobListItem[] = [];
  /** View-models for the Running / Recent run rows; rebuilt from `runs` in `applyRuns`. */
  runningRunVms: RunRowVm[] = [];
  recentRunVms: RunRowVm[] = [];
  runsError: string | null = null;
  /** The run whose live detail is shown; null when nothing is selected. */
  selectedRunId: string | null = null;
  /** Issue/PR number of the selected run — kept so the chip survives a list snapshot that lags a just-started run. */
  selectedRunNumber: number | null = null;
  /**
   * Whether `selectedRunNumber` identifies an issue or a PR. `null` means "not yet known" and is
   * treated as `'issue'` everywhere the number is used, matching the panel's original issue-only
   * behavior for runs selected before this field existed.
   */
  selectedRunKind: 'issue' | 'pr' | null = null;
  /** Repository of the selected run, paired with `selectedRunNumber` for cross-repo-safe chips and retries. */
  selectedRunOwner = '';
  selectedRunRepo = '';
  /** Latest polled status of `selectedRunId`; null until the first poll lands. */
  private _jobStatus: CodingTeamJobStatus | null = null;
  /** Badge modifier for the selected run's status, precomputed from `jobStatus` so the detail panel
   * binds a plain field instead of calling `badgeClass` each change-detection cycle. */
  jobStatusBadgeClass = 'neutral';
  /** Whether the selected run has reached a terminal state, precomputed from `jobStatus` (the detail
   * panel binds this instead of calling `isJobTerminal()`). */
  jobStatusTerminal = false;
  /** Whether the selected run is paused on questions, precomputed from `jobStatus` (the detail panel
   * binds this instead of calling `hasPendingQuestions()` each change-detection cycle). */
  jobStatusHasPendingQuestions = false;
  /** aria-live text for the "Agent thinking" panel: a streaming cue while output is still arriving,
   * then a settled line count once it's been quiet for `THINKING_ANNOUNCE_SETTLE_MS`. Empty when
   * there's no thinking output to announce. Driven by `thinkingAnnouncer`, disposed in
   * `ngOnDestroy`. */
  thinkingAnnouncement = '';
  /** Owns the settle-timer bookkeeping for `thinkingAnnouncement` — see `shared/settle-announce.ts`.
   * `onChange` flips to an immediate streaming cue on any differing, non-empty update; `onSettle`
   * replaces it with a line count once the input has been quiet for `THINKING_ANNOUNCE_SETTLE_MS`. */
  private readonly thinkingAnnouncer = new SettleAnnouncer(
    THINKING_ANNOUNCE_SETTLE_MS,
    (thinking) => {
      if (!thinking.trim()) {
        this.thinkingAnnouncement = '';
        return;
      }
      const lineCount = thinking.split('\n').filter((line) => line.trim().length > 0).length;
      this.thinkingAnnouncement = `${lineCount} line${lineCount === 1 ? '' : 's'} of reasoning`;
    },
    () => {
      this.thinkingAnnouncement = 'Agent is thinking…';
    },
  );
  /** True while `thinkingAnnouncer` has a pending settle timer — exposed so callers/tests can check
   * debounce state without reaching into the private field. */
  get thinkingAnnouncementPending(): boolean {
    return this.thinkingAnnouncer.isPending;
  }
  /** In-memory activity narrative for the selected run (Jobs thought-stream panel). */
  activityNarrative: ActivityNarrativeState = emptyActivityNarrative();
  /** Polite live-region cue for activity-only updates; never contains raw narrative lines. */
  activityAnnouncement = '';
  /** Monotonic counter so each activity cue mutates the live-region text (identical strings are not re-announced). */
  private activityAnnounceSeq = 0;

  // Out-of-scope issues state (Issues tab)
  oosProposals: OutOfScopeProposalItem[] = [];
  oosLoading = false;
  oosFiling = false;
  oosError: string | null = null;

  /**
   * Preconditions: none.
   * Postconditions: true when reasoning text or at least one narrative line is present.
   */
  get showThoughtStreamPanel(): boolean {
    return !!(this._jobStatus?.thinking || this.activityNarrative.lines.length > 0);
  }

  /**
   * Preconditions: none.
   * Postconditions: panel title preferring thinking when reasoning is present; null when hidden.
   */
  get thoughtStreamTitle(): 'Agent thinking' | 'Agent activity' | null {
    return thoughtStreamPanelTitle(!!this._jobStatus?.thinking, this.activityNarrative.lines.length > 0);
  }

  /**
   * Preconditions: none.
   * Postconditions: newline-joined `at  text` rows for the Activity stream `<pre>`.
   */
  get activityStreamText(): string {
    return this.activityNarrative.lines.map((l) => `${l.at}  ${l.text}`).join('\n');
  }

  get jobStatus(): CodingTeamJobStatus | null {
    return this._jobStatus;
  }

  /** Setting the polled status refreshes the precomputed badge class, terminal flag, pending-questions
   * flag, thinking-panel announcement, and activity narrative. */
  set jobStatus(status: CodingTeamJobStatus | null) {
    this._jobStatus = status;
    this.jobStatusBadgeClass = this.badgeClass(status?.status);
    this.jobStatusTerminal = isCodingTeamTerminalStatus(status?.status);
    this.jobStatusHasPendingQuestions =
      !!status?.waiting_for_answers && (status?.pending_questions?.length ?? 0) > 0;
    this.thinkingAnnouncer.update(status?.thinking ?? '');
    if (!status) {
      this.activityNarrative = emptyActivityNarrative();
      this.activityAnnouncement = '';
      this.activityAnnounceSeq = 0;
      return;
    }
    const prev = this.activityNarrative;
    this.activityNarrative = appendActivityNarrative(prev, status, new Date().toISOString());
    // Compare `lines` identity (not length): a capped append keeps length constant but replaces the array.
    if (this.activityNarrative.lines !== prev.lines && !status.thinking) {
      this.activityAnnounceSeq += 1;
      this.activityAnnouncement = `Agent activity updated (${this.activityAnnounceSeq})`;
    }
  }

  /**
   * Set when the selected run's status poll gives up, so the detail panel shows an error instead of
   * an indefinite "Starting…" spinner when the very first status never arrives. Cleared whenever a
   * fresh poll starts (new selection or resubmitted answers).
   */
  jobStatusError: string | null = null;

  /** True while a manual resume POST is in flight (drives a spinner on the Resume button). */
  resumingJob = false;
  /** True for a short window after the job id is copied, to flip the copy icon to a check. */
  jobIdCopied = false;

  /** Issue/PR keys ("owner/repo#number" or "owner/repo#pr-number") with a non-terminal coding-team run, for "In progress" chips. */
  activeRunKeys = new Set<string>();

  private pollSub: Subscription | null = null;
  private runsSub: Subscription | null = null;
  private readonly refreshTrigger$ = new Subject<void>();
  /** Auto-select a run only on the first list load, so later polls never steal the user's selection. */
  private initialRunsLoad = true;
  /** Handle for the copy-confirmation reset, cleared on destroy so it never fires on a dead view. */
  private copyResetTimer: ReturnType<typeof setTimeout> | null = null;
  /** Handle for the pending confirm-panel/row focus move, cleared on destroy so it never fires on a dead view. */
  private focusTimer: ReturnType<typeof setTimeout> | null = null;

  ngOnInit(): void {
    this.checkGitHubConfig();
  }

  ngOnDestroy(): void {
    this.stopPolling();
    this.runsSub?.unsubscribe();
    this.runsSub = null;
    // Drop the selected-run bookkeeping so nothing pairs with a dead view.
    this.selectedRunId = null;
    this.selectedRunNumber = null;
    this.selectedRunKind = null;
    this.selectedRunOwner = '';
    this.selectedRunRepo = '';
    if (this.copyResetTimer) {
      clearTimeout(this.copyResetTimer);
      this.copyResetTimer = null;
    }
    this.thinkingAnnouncer.dispose();
    this.clearFocusTimer();
    this.refreshTrigger$.complete();
  }

  /**
   * Load the GitHub integration status (enabled flag + token) and gate the page on it. Repository
   * access is defined by the PAT itself, so no owner/repo configuration is required: on success the
   * page is configured when the integration is enabled and a token is stored, the Runs poll starts,
   * and the accessible-repository list loads; on error, leaves the page in the unconfigured state.
   * Sets `isLoadingConfig` for the duration.
   */
  checkGitHubConfig(): void {
    this.isLoadingConfig = true;
    this.integrationsApi
      .getGitHubConfig()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (cfg) => {
        this.githubConfigured = cfg.enabled && cfg.token_configured;
        this.defaultLabel = cfg.default_label ?? '';
        this.isLoadingConfig = false;
        if (this.githubConfigured) {
          this.startRunsPolling();
          this.refreshTrigger$.next();
          this.loadRepos();
        }
      },
      error: () => {
        this.githubConfigured = false;
        this.isLoadingConfig = false;
      },
    });
  }

  /**
   * Load every repository the PAT can access into the repo accordion.
   *
   * Preconditions: none.
   * Postconditions: a no-op while a load is already in flight (`loadingRepos`). On success `repos`
   * holds the accessible repositories (most recently pushed first, as the API returns them) and any
   * expanded repo/issue state is reset; on the very first successful load for this component
   * instance, a remembered repo (see `loadLastRepo`) present in the result is re-expanded via
   * `toggleRepo` — a later manual refresh never repeats this, so collapsing it sticks. On error
   * `repoError` is surfaced and `initialReposLoad` is left untouched, so the next successful load
   * still attempts the restore.
   */
  loadRepos(): void {
    if (this.loadingRepos) return;
    this.loadingRepos = true;
    this.repoError = null;
    this.selectedRepo = null;
    this.issues = [];
    this.issuesLoaded = false;
    this.selectedIssue = null;
    this.integrationsApi
      .getGitHubRepos()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (repos) => {
        this.repos = repos;
        this.reposLoaded = true;
        this.loadingRepos = false;
        // A runs poll that already applied an unfiltered snapshot before this list arrived
        // re-filters on its next tick (repos auto-loads on init, so the window is brief); we
        // deliberately do NOT re-apply here to avoid racing the first poll's auto-select.
        if (this.initialReposLoad) {
          this.initialReposLoad = false;
          this.restoreLastRepo();
        }
      },
      error: (err: unknown) => {
        this.repoError = extractErrorDetail(err, 'Failed to load repositories.');
        this.loadingRepos = false;
      },
    });
  }

  /**
   * Re-expand the remembered last-used repo, if it is still accessible.
   *
   * Preconditions: called once, right after `repos` is populated by the first successful
   * `loadRepos()`.
   * Postconditions: a no-op when nothing is remembered or the remembered repo is no longer in
   * `repos` (PAT lost access, repo renamed/deleted) — no error is surfaced either way. Otherwise
   * expands it via `toggleRepo`, which also loads its issues.
   */
  private restoreLastRepo(): void {
    const fullName = this.loadLastRepo();
    if (!fullName) return;
    const repo = this.repos.find((r) => r.full_name === fullName);
    if (repo) {
      this.toggleRepo(repo);
    }
  }

  /** Read the remembered last-used repo's `full_name`, or null if none/unreadable. */
  private loadLastRepo(): string | null {
    try {
      return localStorage.getItem(LAST_REPO_STORAGE_KEY);
    } catch {
      // Storage unavailable (e.g. private browsing) — behave as if nothing were remembered.
      return null;
    }
  }

  /** Remember a repo as the last one used, so it can be pre-expanded on a future visit. */
  private persistLastRepo(fullName: string): void {
    try {
      localStorage.setItem(LAST_REPO_STORAGE_KEY, fullName);
    } catch {
      // Storage full or unavailable — silently ignore; the page still works without memory.
    }
  }

  /**
   * Toggle a repository row: expand it (and load its issues) when collapsed, collapse it when it is
   * the open one. Only one repo is expanded at a time, and the issue list is always scoped to it.
   * Expanding also remembers the repo (see `persistLastRepo`) for next visit; collapsing does not
   * forget it — the memory tracks "last repo worked on," not "currently expanded repo."
   */
  toggleRepo(repo: GitHubRepoItem): void {
    const collapse = this.selectedRepo?.full_name === repo.full_name;
    this.selectedRepo = collapse ? null : repo;
    this.resetIssueState();
    if (!collapse) {
      this.loadIssues();
      this.persistLastRepo(repo.full_name);
    }
  }

  /** Clear all issue-scoped state (list, selection, search, pagination, error) when the expanded repo changes. */
  private resetIssueState(): void {
    this.issues = [];
    this.issuesLoaded = false;
    this.selectedIssue = null;
    this.issueError = null;
    this.issueSearch = '';
    this.pageIndex = 0;
    // The label filter is per-repo: each newly-expanded repo starts with the operator's
    // configured default applied, and the toggle is a transient override for that repo only
    // (so turning it off for one repo doesn't silently unfilter every other repo).
    this.labelFilterActive = true;
    // PR-tab state is repo-scoped too: clear it so switching repos re-fetches the new repo's
    // open PRs (the Pull Requests tab auto-loads while `pullsLoaded` is false). The in-flight guard
    // set is not cleared — a job already being started for the previous repo's PR must still
    // clear its own key when its request settles.
    this.pulls = [];
    this.pullsLoaded = false;
    this.pullError = null;
    // Invalidate any in-flight loadPulls() for the PREVIOUS repo and reset the loading flag:
    // without this, switching away mid-load leaves `loadingPulls` stuck true until that stale
    // request settles, blocking the Pulls-tab auto-load guard (`!loadingPulls`) for the newly
    // selected repo — its panel would stay blank until a manual Refresh. Advancing the token
    // here (rather than only in loadPulls) makes the stale response's own `isCurrent` check
    // fail too, so it can never overwrite the new repo's state even if it lands afterward.
    this.pullsLoad.next();
    this.loadingPulls = false;
  }

  /** Repos matching `repoSearch` (case-insensitive substring of `full_name`); all repos when empty. */
  get filteredRepos(): GitHubRepoItem[] {
    const query = this.repoSearch.trim().toLowerCase();
    if (!query) return this.repos;
    return this.repos.filter((repo) => repo.full_name.toLowerCase().includes(query));
  }

  /**
   * Polite live-region text for how many repos `filteredRepos` currently shows.
   *
   * Preconditions: none.
   * Postconditions: returns `''` before `repos` has loaded or when the token has no repo access
   * at all (`repos.length === 0`) — in both cases the search field isn't rendered, so there's
   * nothing to announce and announcing "0 repositories shown" would contradict the no-access
   * empty state. Otherwise returns
   * `resultCountAnnouncement(filteredRepos.length, 'repository', 'repositories')`.
   */
  get repoCountAnnouncement(): string {
    if (!this.reposLoaded || this.repos.length === 0) return '';
    return resultCountAnnouncement(this.filteredRepos.length, 'repository', 'repositories');
  }

  /** True when there are repos to show but the search excludes every one. */
  get hasFilteredOutRepos(): boolean {
    return this.repos.length > 0 && this.filteredRepos.length === 0;
  }

  /** Clear the repo search, restoring the full repo list. */
  clearRepoSearch(): void {
    this.repoSearch = '';
  }

  /**
   * The label to filter the issue listing by, or `undefined` for no filter.
   *
   * Preconditions: none.
   * Postconditions: returns the configured `defaultLabel` when a non-empty label is configured
   * AND the filter is active; `undefined` otherwise (so a blank/toggled-off filter is unfiltered).
   */
  activeLabel(): string | undefined {
    return this.labelFilterActive && this.defaultLabel ? this.defaultLabel : undefined;
  }

  /**
   * Toggle the configured default-label filter on/off for this session and reload the issues.
   *
   * Preconditions: a `defaultLabel` is configured (the toggle only renders then) and a repo is
   * expanded (else the reload is a no-op).
   * Postconditions: `labelFilterActive` is flipped and `loadIssues` re-fetches with/without the
   * filter, letting the operator browse unlabelled issues without editing the dashboard setting.
   */
  toggleLabelFilter(): void {
    this.labelFilterActive = !this.labelFilterActive;
    // Reload the issue list only — the Runs list is unaffected by which issues are shown.
    this.loadIssues(false);
  }

  /**
   * Refresh the expanded repo's open-issue list. Resets the selection/pagination and fetches
   * issues; sets `issueError` on failure.
   *
   * Preconditions: none.
   * Postconditions: a no-op when no repo is expanded. A response that lands after the user switched
   * to a different repo is discarded, so rapid repo switches can never show one repo's issues under
   * another repo's row. When `refreshRuns` is true (the default, used on repo expand/refresh) the
   * Runs snapshot is refetched too so the two lists never drift; a label-filter toggle passes false
   * to avoid a needless Runs refetch (the runs don't depend on which issues are listed — the "In
   * progress" chips are recomputed from existing run state either way).
   */
  loadIssues(refreshRuns = true): void {
    const repo = this.selectedRepo;
    if (!repo) return;
    // Claim a token so a slow response superseded by a newer load (collapse/re-expand of the
    // same repo, or a repo switch) is discarded — and the loading flag is always cleared by
    // the current handler, so it can't get stuck true after a switch-away.
    const token = this.issuesLoad.next();
    this.loadingIssues = true;
    this.issueError = null;
    this.selectedIssue = null;
    if (refreshRuns) {
      // Refreshing the issue list also refreshes the Runs list and the "In progress" chips, so the
      // two lists never drift.
      this.refreshTrigger$.next();
    }
    this.integrationsApi
      // Apply the operator's global default-label filter when active (else browse all issues).
      .getGitHubIssues({ owner: repo.owner, repo: repo.name, label: this.activeLabel() })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (issues) => {
        // Superseded by a newer load: that newer load now owns the loading flag, so leave
        // it and drop this response (prevents an out-of-order same-repo overwrite).
        if (!this.issuesLoad.isCurrent(token)) return;
        // This is the current load, so it must clear the flag even if the user switched away
        // (or the flag would stick true forever). Only render the data when still on this repo.
        this.loadingIssues = false;
        if (this.selectedRepo?.full_name !== repo.full_name) return;
        this.issues = issues;
        this.pageIndex = 0;
        this.issuesLoaded = true;
        this.recomputeIssueVms();
      },
      error: (err: unknown) => {
        if (!this.issuesLoad.isCurrent(token)) return;
        this.loadingIssues = false;
        if (this.selectedRepo?.full_name !== repo.full_name) return;
        this.issueError = extractErrorDetail(err, 'Failed to load issues.');
      },
    });
  }

  /** Issues matching `issueSearch` (case-insensitive substring of `title`); all issues when empty. */
  get filteredIssues(): GitHubIssueItem[] {
    const query = this.issueSearch.trim().toLowerCase();
    if (!query) return this.issues;
    return this.issues.filter((issue) => issue.title.toLowerCase().includes(query));
  }

  /**
   * Polite live-region text for how many issues `filteredIssues` currently shows.
   *
   * Preconditions: none.
   * Postconditions: returns `''` before the expanded repo's issues have loaded or when it has no
   * open issues at all (`issues.length === 0`) — in both cases the search field isn't rendered, so
   * there's nothing to announce and announcing "0 issues shown" would contradict the no-open-issues
   * empty state. Otherwise returns
   * `resultCountAnnouncement(filteredIssues.length, 'issue', 'issues')`.
   */
  get issueCountAnnouncement(): string {
    if (!this.issuesLoaded || this.issues.length === 0) return '';
    return resultCountAnnouncement(this.filteredIssues.length, 'issue', 'issues');
  }

  /** True when there are issues to show but the search excludes every one. */
  get hasFilteredOutIssues(): boolean {
    return this.issues.length > 0 && this.filteredIssues.length === 0;
  }

  /** The slice of (filtered) issues visible on the current page. */
  get pagedIssues(): GitHubIssueItem[] {
    const start = this.pageIndex * this.pageSize;
    return this.filteredIssues.slice(start, start + this.pageSize);
  }

  /**
   * Handle a paginator event: adopt the new page index/size and rebuild the visible issue view-models.
   *
   * Preconditions: `event` carries the paginator's new `pageIndex`/`pageSize`.
   * Postconditions: `pageIndex`/`pageSize` reflect `event` and `pagedIssueVms` matches the new slice.
   */
  onPageChange(event: PageEvent): void {
    this.pageIndex = event.pageIndex;
    this.pageSize = event.pageSize;
    this.recomputeIssueVms();
  }

  /**
   * Handle an issue-search keystroke: re-page to the first page (a narrower result set may no
   * longer have a current page) and rebuild the visible issue view-models.
   */
  onIssueSearchChange(): void {
    this.pageIndex = 0;
    this.recomputeIssueVms();
  }

  /** Clear the issue search, restoring the full (repo-scoped) issue list. */
  clearIssueSearch(): void {
    this.issueSearch = '';
    this.onIssueSearchChange();
  }

  /**
   * Select an issue, surfacing the run-confirmation affordance inline under its row.
   *
   * Preconditions: none enforced — selecting a different issue while one is already selected
   *   simply moves the confirmation panel to the new row.
   * Postconditions: `selectedIssue` (and its derived `selectedIssueOpenDepsText`) is set, which
   *   renders `.github-confirm-panel` under this issue's row on the next change-detection pass;
   *   once rendered, focus moves into that panel (named via `aria-labelledby` pointing at its
   *   `<h3>`) so AT users hear the confirmation heading — and the blocked-dependency warning,
   *   when present — instead of focus staying on the row button they just activated.
   */
  selectIssue(issue: GitHubIssueItem): void {
    this.selectedIssue = issue;
    this.moveFocusToConfirmPanel(issue.number);
  }

  /**
   * Clear the issue selection, collapsing the inline confirmation panel.
   *
   * Preconditions: none enforced.
   * Postconditions: `selectedIssue` is cleared, unmounting `.github-confirm-panel` (including the
   *   Cancel button that currently holds focus) on the next change-detection pass; once the row
   *   has re-rendered, focus returns to that issue's `.github-issue-row` button so it never drops
   *   to `<body>`. If the issue search filter has removed that row from the list by the time the
   *   deferred callback runs, focus falls back to `.github-issues-list`, or further to the issue
   *   search input if the search matches nothing at all — never `<body>`.
   */
  cancelSelection(): void {
    const issueNumber = this.selectedIssue?.number ?? null;
    this.selectedIssue = null;
    if (issueNumber !== null) {
      this.moveFocusToIssueRow(issueNumber);
    }
  }

  /**
   * Repo-scope prefix shared by the confirm-panel ids and the row key below. Only one repo is
   * ever expanded at a time (see `toggleRepo`), but scoping by repo full name here means this
   * wiring stays correct even if that invariant ever changes — a bare issue number is only
   * unique within a repo. Extracted to one place so the three sites below can't drift apart.
   */
  private get repoScope(): string {
    return this.selectedRepo?.full_name ?? '';
  }

  /** Repo-scoped id for the confirm panel of `issueNumber` under the currently expanded repo. */
  confirmPanelId(issueNumber: number): string {
    return `confirm-panel-${this.repoScope}-${issueNumber}`;
  }

  /** Id for the confirm panel's heading, referenced by the panel's `aria-labelledby`. */
  confirmPanelHeadingId(issueNumber: number): string {
    return `confirm-panel-heading-${this.repoScope}-${issueNumber}`;
  }

  /** Repo-scoped key for a row's `data-issue-number` attribute. */
  issueRowKey(issueNumber: number): string {
    return `${this.repoScope}#${issueNumber}`;
  }

  /** Clear any pending focus-move timer, e.g. before scheduling a new one or on destroy. */
  private clearFocusTimer(): void {
    if (this.focusTimer !== null) {
      clearTimeout(this.focusTimer);
      this.focusTimer = null;
    }
  }

  /**
   * Supersede any pending focus move with a new one, deferred until the next render tick.
   * `find` resolves the target element within this component's host once rendered.
   */
  private scheduleFocusMove(find: (root: HTMLElement) => HTMLElement | null): void {
    this.clearFocusTimer();
    this.focusTimer = deferFocus(this.host.nativeElement, find);
  }

  /** After the confirm panel for `issueNumber` renders, move focus into it. */
  private moveFocusToConfirmPanel(issueNumber: number): void {
    const id = this.confirmPanelId(issueNumber);
    this.scheduleFocusMove((root) => root.querySelector<HTMLElement>(`[id="${id}"]`));
  }

  /**
   * After a run's detail panel renders, move focus into its hoisted `.run-detail-focus`
   * container (`#runDetail` in the template) — the one node that survives every
   * jobStatus/jobStatusError/pending branch swap, so this single move (even while the panel is
   * still showing "Starting…") is never stranded when the first status response lands and
   * swaps the id-less pending branch for the populated one.
   */
  private moveFocusToRunDetail(jobId: string): void {
    const id = `run-detail-${jobId}`;
    this.scheduleFocusMove((root) => root.querySelector<HTMLElement>(`[id="${id}"]`));
  }

  /**
   * After the confirm panel for `issueNumber` unmounts, move focus back to its row's button
   * (found via `data-issue-number`, which — unlike `aria-controls` — stays on the row regardless
   * of selection state, since `aria-controls` is removed the same tick `selectedIssue` clears).
   * Falls back to `.github-issues-list` when the row isn't found (e.g. filtered out by the issue
   * search), and further to the issue search input when the list itself isn't rendered either
   * (the search matches nothing) — the search input renders whenever the issue list section
   * does at all, so focus never drops to `<body>` in either edge case.
   */
  private moveFocusToIssueRow(issueNumber: number): void {
    const key = this.issueRowKey(issueNumber);
    this.scheduleFocusMove(
      (root) =>
        root.querySelector<HTMLElement>(`[data-issue-number="${key}"]`) ??
        root.querySelector<HTMLElement>('.github-issues-list') ??
        root.querySelector<HTMLElement>('.github-search-field input'),
    );
  }

  /** True when the issue is blocked by, or depends on, one or more other issues. */
  hasDependencies(issue: GitHubIssueItem): boolean {
    return issue.dependencies.length > 0;
  }

  /** All dependencies rendered as "#3, #5". */
  allDepRefs(issue: GitHubIssueItem): string {
    return issue.dependencies.map((d) => `#${d.number}`).join(', ');
  }

  /** The still-open dependencies rendered as "#3, #5" for warnings and tooltips. */
  openDepRefs(issue: GitHubIssueItem): string {
    return issue.dependencies
      .filter((d) => d.state === 'open')
      .map((d) => `#${d.number}`)
      .join(', ');
  }

  /** Hover/aria text describing the issue's dependencies and whether they block it. */
  dependencyTooltip(issue: GitHubIssueItem): string {
    return issue.blocked
      ? `Blocked by ${this.openDepRefs(issue)} — must be closed first`
      : `Depends on ${this.allDepRefs(issue)} (all complete)`;
  }

  /**
   * Composed tooltip for a repo row button: the repo's description (when present) plus a
   * clarification that GitHub's open-issue count includes pull requests.
   *
   * Preconditions: `repo` is a loaded GitHub repo item; `repo.description` may be null,
   *   undefined, or an empty/whitespace-only string.
   * Postconditions: returns `"<description> — Open issues and pull requests reported by
   *   GitHub"` when `repo.description` is non-empty after trimming; otherwise returns
   *   `"Open issues and pull requests reported by GitHub"` alone, with no leading separator
   *   or empty clause. The returned string is always non-empty. Pure — no side effects.
   */
  repoRowTooltip(repo: GitHubRepoItem): string {
    const clarification = 'Open issues and pull requests reported by GitHub';
    const description = repo.description?.trim();
    return description ? `${description} — ${clarification}` : clarification;
  }

  /**
   * Composed tooltip for an issue row button: the issue title, plus a clause noting the coding
   * team is already working the issue when it is in progress.
   *
   * Preconditions: `vm.title` is the issue's full (untruncated) title and is non-empty (GitHub
   *   issue titles cannot be blank).
   * Postconditions: returns `vm.title` alone when `vm.inProgress` is false; otherwise returns
   *   `"<title> — The coding team is already working on this issue"`. The returned string is
   *   always non-empty. Pure — no side effects.
   */
  issueRowTooltip(vm: { title: string; inProgress: boolean }): string {
    const clause = 'The coding team is already working on this issue';
    return vm.inProgress ? `${vm.title} — ${clause}` : vm.title;
  }

  /**
   * Rebuild the Running/Recent run-row view-models from the current `runningRuns`/`recentRuns`.
   *
   * Preconditions: `runningRuns`/`recentRuns` reflect the snapshot being rendered.
   * Postconditions: `runningRunVms`/`recentRunVms` are fresh `toRunVm` mappings of those lists.
   */
  private buildRunVms(): void {
    this.runningRunVms = this.runningRuns.map((r) => this.toRunVm(r));
    this.recentRunVms = this.recentRuns.map((r) => this.toRunVm(r));
  }

  /**
   * Build one run row's view-model so the template binds plain fields instead of calling helpers.
   *
   * Preconditions: none.
   * Postconditions: returns a `RunRowVm`; `detail` is null for a terminal run (no live status
   * line) and `repoLabel` is `''` when the run carries no GitHub context.
   */
  private toRunVm(run: CodingTeamJobListItem): RunRowVm {
    const ctx = run.github_context;
    const identity = runIdentity(ctx);
    return {
      run,
      issueNumber: ctx?.issue_number,
      prNumber: ctx?.pr_number,
      displayLabel: identity ? (identity.type === 'pr' ? `#${identity.number} (PR)` : `#${identity.number}`) : '',
      repoLabel: ctx ? `${ctx.owner}/${ctx.repo}` : '',
      status: run.status,
      badgeClass: this.badgeClass(run.status),
      waiting: this.isRunWaiting(run),
      detail: this.isRunActive(run) ? run.status_text || run.phase || null : null,
      timeAgo: this.timeAgo(run.updated_at),
    };
  }

  /**
   * Rebuild the visible issue-row view-models (current page × current "In progress" chip set).
   *
   * Preconditions: `issues`/`pageIndex`/`pageSize`/`activeRunKeys` reflect the state to render.
   * Postconditions: `pagedIssueVms` matches the current `pagedIssues` slice one-to-one.
   */
  private recomputeIssueVms(): void {
    this.pagedIssueVms = this.pagedIssues.map((issue) => ({
      issue,
      number: issue.number,
      title: issue.title,
      labels: issue.labels,
      inProgress: this.isIssueInProgress(issue),
      hasDeps: this.hasDependencies(issue),
      blocked: issue.blocked,
      openDepsCount: issue.open_dependencies.length,
      depsTooltip: this.dependencyTooltip(issue),
    }));
  }

  /**
   * Start a coding-team run for the selected issue in the expanded repo.
   *
   * Preconditions: none enforced — a no-op when `selectedIssue`/`selectedRepo` is null or a run is
   * already starting (`runningIssue`), so a double-click can't submit the same issue twice.
   * Postconditions: on success the issue is marked in progress (`activeRunKeys`), the returned
   * run is selected (so its live detail shows immediately) and focus is moved into that detail
   * (the disabled "Confirm & Start" button is blurred by the browser and the confirmation panel
   * is unmounted, so without this the user would otherwise be stranded on `<body>`), the Runs
   * list is refreshed, and the selection is cleared; on error `issueError` is surfaced.
   * `runningIssue` is toggled across the call.
   */
  confirmAndRun(): void {
    const repo = this.selectedRepo;
    if (!this.selectedIssue || !repo || this.runningIssue) return;
    this.runningIssue = true;
    this.issueError = null;
    this.integrationsApi
      .runGitHubIssue({ issue_number: this.selectedIssue.number, owner: repo.owner, repo: repo.name })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (resp: RunGitHubIssueResponse) => {
        this.runningIssue = false;
        this.selectedIssue = null;
        this.activeRunKeys.add(runKey(repo.owner, repo.name, { type: 'issue', number: resp.issue_number }));
        // Set the selected run's issue first so selectRun (which can't find the run in `runs`
        // until the next list tick) doesn't clear it.
        this.selectedRunNumber = resp.issue_number;
        this.selectedRunKind = 'issue';
        this.selectedRunOwner = repo.owner;
        this.selectedRunRepo = repo.name;
        this.selectRun(resp.job_id);
        this.recomputeIssueVms();
        // Trigger the runs-list refresh before scheduling the focus move: the new run's row (and
        // hence its detail's hoisted container) only exists once that refresh lands, and this
        // ordering gives it a chance to resolve first rather than racing it.
        this.refreshTrigger$.next();
        this.moveFocusToRunDetail(resp.job_id);
      },
      error: (err: unknown) => {
        this.runningIssue = false;
        // Only surface the failure while still on the repo the run targeted; `issueError`
        // is the expanded repo's banner, so an unguarded set would attribute this repo's
        // failure to whichever repo the user switched to while the request was in flight.
        if (this.selectedRepo?.full_name === repo.full_name) {
          this.issueError = extractErrorDetail(err, 'Failed to start job.');
        }
      },
    });
  }

  /**
   * Re-run the selected (typically terminal) run's issue, e.g. after a failure.
   *
   * Preconditions: none enforced — a no-op when the selected run has no resolvable issue
   * number/repository, is a PR-remediation run (`selectedRunKind === 'pr'` — there is no "issue" to
   * re-run), or a run is already starting (`runningIssue`).
   * Postconditions: on success a fresh run for the same issue (in the same repository) is started
   * and selected, focus is moved into its detail (the disabled "Run again" button is blurred by
   * the browser on click, so without this the user would otherwise be stranded on `<body>`), and
   * the issue is marked in progress; on error `issueError` is surfaced. `runningIssue` is toggled
   * across the call.
   */
  retrySelectedRun(): void {
    if (this.selectedRunKind === 'pr') return;
    const ctx = this.jobStatus?.github_context;
    const issueNumber = this.selectedRunNumber ?? ctx?.issue_number;
    const owner = this.selectedRunOwner || ctx?.owner || '';
    const repo = this.selectedRunRepo || ctx?.repo || '';
    if (issueNumber == null || !owner || !repo || this.runningIssue) return;
    this.runningIssue = true;
    this.issueError = null;
    this.integrationsApi
      .runGitHubIssue({ issue_number: issueNumber, owner, repo })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (resp: RunGitHubIssueResponse) => {
        this.runningIssue = false;
        this.activeRunKeys.add(runKey(owner, repo, { type: 'issue', number: resp.issue_number }));
        this.selectedRunNumber = resp.issue_number;
        this.selectedRunKind = 'issue';
        this.selectedRunOwner = owner;
        this.selectedRunRepo = repo;
        this.selectRun(resp.job_id);
        this.recomputeIssueVms();
        // See confirmAndRun: trigger the runs-list refresh before scheduling the focus move, so
        // the new run's row has a chance to land before deferFocus looks for its container.
        this.refreshTrigger$.next();
        this.moveFocusToRunDetail(resp.job_id);
      },
      error: (err: unknown) => {
        this.issueError = extractErrorDetail(err, 'Failed to start job.');
        this.runningIssue = false;
      },
    });
  }

  /**
   * Subscribe to the Runs list poll.
   *
   * Preconditions: the GitHub integration is configured (enabled + token) — the caller only invokes
   * this once configured. Runs from every repository the PAT can access are shown.
   * Postconditions: each `refreshTrigger$` emission re-fetches `/jobs` (terminal jobs included, so
   * "Recent" runs show) on a `timer(0, RUNS_POLL_MS)` cadence and applies them via `applyRuns`; a
   * failed fetch sets `runsError` and leaves the page usable. Idempotent — a second call while a
   * subscription is live is a no-op.
   */
  private startRunsPolling(): void {
    if (this.runsSub) return;
    this.runsSub = this.refreshTrigger$
      .pipe(
        switchMap(() => timer(0, RUNS_POLL_MS)),
        switchMap(() =>
          // listJobs(false): include terminal jobs so finished runs appear under "Recent".
          this.api.listJobs(false).pipe(
            // Clear the error only on a successful fetch — the catchError fallback below must not
            // wipe the error it just set.
            tap(() => {
              this.runsError = null;
            }),
            catchError((err: unknown) => {
              this.runsError = extractErrorDetail(err, 'Failed to load runs.');
              return of([] as CodingTeamJobListItem[]);
            }),
          ),
        ),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe((jobs) => this.applyRuns(jobs));
  }

  /**
   * Fold a fresh `/jobs` snapshot into the Runs panel.
   *
   * Preconditions: the poll only starts once configured (enabled + token).
   * Postconditions: `runs` holds every issue- or PR-bearing run for a repository the PAT can
   * currently access (running + terminal) and `runningRuns`/`recentRuns` hold its non-terminal/terminal
   * partitions; `activeRunKeys` holds the non-terminal subset's "owner/repo#number" (issue) or
   * "owner/repo#pr-number" (PR) keys, plus the selected run's key only while that run is absent from
   * this snapshot and not yet observed terminal (so a snapshot that lags a just-started run can't wipe
   * its chip, while a run the snapshot already reports terminal is trusted and dropped). On the first
   * load only, a run is auto-selected when none is selected.
   */
  private applyRuns(jobs: CodingTeamJobListItem[]): void {
    let mine = jobs.filter((j) => runIdentity(j.github_context) != null);
    // The coding-team `/jobs` endpoint is NOT PAT-scoped — it returns every stored issue-/PR-bearing
    // run, including runs from a previous token/config or shared job storage. Once the accessible-repo
    // list has loaded (it auto-loads on init alongside the first poll), drop runs whose repository is
    // not in it, so the panel never surfaces — or offers retries for — repos the current token can no
    // longer reach. Before the list loads, show everything rather than blanking the panel.
    if (this.reposLoaded) {
      const accessible = new Set(this.repos.map((r) => r.full_name.toLowerCase()));
      mine = mine.filter((j) => {
        const owner = j.github_context?.owner;
        const repo = j.github_context?.repo;
        return owner != null && repo != null && accessible.has(`${owner}/${repo}`.toLowerCase());
      });
    }
    this.runs = mine;
    this.runningRuns = mine.filter((j) => !isCodingTeamTerminalStatus(j.status));
    this.recentRuns = mine.filter((j) => isCodingTeamTerminalStatus(j.status));

    const active = new Set(
      this.runningRuns
        .map((j) => {
          const identity = runIdentity(j.github_context);
          return identity ? runKey(j.github_context?.owner, j.github_context?.repo, identity) : null;
        })
        .filter((key): key is string => key != null),
    );
    // Preserve the chip for a just-started run the snapshot does not list yet — but only while the
    // run is genuinely absent from the snapshot (`selectedInSnapshot`) AND the polled status has not
    // gone terminal. Once the snapshot lists the run, OR the polled status reports it finished, the
    // chip is dropped — so a finished run, or selecting a terminal (Recent) run, never re-adds an
    // "In progress" chip. Consulting the polled status here (not just the list snapshot) drops the
    // chip promptly, without waiting a full list-poll cycle for the run to move to Recent.
    const selectedInSnapshot =
      this.selectedRunId != null && mine.some((j) => j.job_id === this.selectedRunId);
    if (
      this.selectedRunId != null &&
      this.selectedRunNumber != null &&
      !selectedInSnapshot &&
      !isCodingTeamTerminalStatus(this.jobStatus?.status)
    ) {
      // `selectedRunKind` is null for a run selected before this field existed (or before the
      // snapshot ever confirmed it) — that always meant an issue-driven run, so default to it.
      active.add(
        runKey(this.selectedRunOwner, this.selectedRunRepo, {
          type: this.selectedRunKind ?? 'issue',
          number: this.selectedRunNumber,
        }),
      );
    }
    this.activeRunKeys = active;
    this.buildRunVms();
    this.recomputeIssueVms();

    if (this.initialRunsLoad) {
      this.initialRunsLoad = false;
      this.autoSelectRun(mine);
    }
  }

  /**
   * On the first list load, surface an in-flight run so a page reload restores the live panel.
   *
   * Preconditions: called once, from the first `applyRuns`; `runs` is this repo's filtered snapshot.
   * Postconditions: a no-op when a run is already selected or none is non-terminal; otherwise selects
   * a run paused on questions (so human-in-the-loop runs are reachable immediately) when present,
   * else the most recently updated non-terminal run. Never moves focus — this fires on a page load
   * or list refresh, not a user action, so `document.activeElement` is left untouched.
   */
  private autoSelectRun(runs: CodingTeamJobListItem[]): void {
    if (this.selectedRunId) return;
    const active = runs.filter((r) => !isCodingTeamTerminalStatus(r.status));
    if (active.length === 0) return;
    const waiting = active.find((r) => r.waiting_for_answers);
    // ISO-8601 timestamps sort lexicographically in chronological order, so localeCompare picks the
    // most recently updated run without parsing dates.
    const pick =
      waiting ??
      active.reduce((best, j) =>
        (j.updated_at ?? '').localeCompare(best.updated_at ?? '') > 0 ? j : best,
      );
    this.selectRun(pick.job_id);
  }

  /**
   * Show a run's live detail and start polling its status.
   *
   * Preconditions: `jobId` is a coding-team job id; when it is a row in `runs` it carries a
   * `github_context.issue_number` or `github_context.pr_number` (the list is pre-filtered to
   * issue-/PR-bearing runs).
   * Postconditions: a no-op when `jobId` is already selected; otherwise `selectedRunId` is `jobId`,
   * `selectedRunNumber`/`selectedRunKind` are taken from the matching run when it is in `runs` (its
   * issue or PR number and which kind it is, or both null if the run somehow carries neither — the
   * list is pre-filtered to issue-/PR-bearing runs, so this is a defensive fallback), and left
   * untouched when the run is not yet in `runs` (e.g. just started, so the caller's pre-set number
   * survives); `activityNarrative`/`activityAnnouncement` are cleared, `jobStatus`/`issueError`/
   * `jobStatusError` are cleared, and status polling for `jobId` is (re)started. Never moves focus:
   * this method is shared by four callers (`toggleRun`'s expand path, `confirmAndRun`,
   * `retrySelectedRun`, `autoSelectRun`) with opposite focus requirements, so each user-initiated
   * caller moves focus itself, afterward, via `moveFocusToRunDetail` — `autoSelectRun` deliberately
   * does not, and neither does a later status-poll tick (which calls `jobStatus =` directly, never
   * back through here).
   */
  selectRun(jobId: string): void {
    if (this.selectedRunId === jobId) return;
    this.selectedRunId = jobId;
    const run = this.runs.find((r) => r.job_id === jobId);
    if (run) {
      const identity = runIdentity(run.github_context);
      this.selectedRunNumber = identity?.number ?? null;
      this.selectedRunKind = identity?.type ?? null;
      this.selectedRunOwner = run.github_context?.owner ?? '';
      this.selectedRunRepo = run.github_context?.repo ?? '';
    }
    this.activityNarrative = emptyActivityNarrative();
    this.activityAnnouncement = '';
    this.activityAnnounceSeq = 0;
    this.jobStatus = null;
    this.issueError = null;
    this.jobStatusError = null;
    this.startPolling(jobId);
  }

  /**
   * Toggle a run row in the Jobs accordion: expand it (select + poll) when collapsed, or collapse it
   * (deselect + stop polling) when it is the open one.
   *
   * Preconditions: `run` is a row in `runs`.
   * Postconditions: when `run` was the selected row, `selectedRunId`/`selectedRunNumber`/`jobStatus`
   * are cleared and the status poll is stopped (the 15s list poll keeps the row's badge fresh) — so a
   * later snapshot can't re-add a stale "In progress" chip for the deselected run; focus is left where
   * it is (on the row button that was just clicked), never moved away. Otherwise `run` becomes the
   * selected, expanded row, its status poll starts, and focus moves into its detail panel (even while
   * still showing "Starting…") — the only one of `selectRun`'s four callers where the reveal is a
   * direct user action on this row, so the focus move lives here rather than inside `selectRun` itself.
   */
  toggleRun(run: CodingTeamJobListItem): void {
    if (this.selectedRunId === run.job_id) {
      this.stopPolling();
      this.selectedRunId = null;
      // Clear the issue/PR identity too: applyRuns keeps a chip alive for the selected run, so a
      // lingering selectedRunNumber would re-flag a deselected (and possibly finished) issue/PR.
      this.selectedRunNumber = null;
      this.selectedRunKind = null;
      this.selectedRunOwner = '';
      this.selectedRunRepo = '';
      this.jobStatus = null;
      this.jobStatusError = null;
      return;
    }
    this.selectRun(run.job_id);
    this.moveFocusToRunDetail(run.job_id);
  }

  /** The currently selected run's list row, or null. */
  get selectedRun(): CodingTeamJobListItem | null {
    return this.runs.find((r) => r.job_id === this.selectedRunId) ?? null;
  }

  /** GitHub issue/PR URL for the selected run's header link, if known. */
  get selectedIssueUrl(): string {
    return (
      this.selectedRun?.github_context?.issue_url ??
      this.jobStatus?.github_context?.issue_url ??
      this.selectedRun?.github_context?.pr_url ??
      this.jobStatus?.github_context?.pr_url ??
      ''
    );
  }

  /** "issue" or "pull request", for the run-detail header ("Working on {label}"). Defaults to "issue". */
  get selectedRunKindLabel(): 'issue' | 'pull request' {
    return this.selectedRunKind === 'pr' ? 'pull request' : 'issue';
  }

  /** "owner/repo" for the run-detail header, or '' when either half is unknown. */
  get selectedRunOwnerRepo(): string {
    return this.selectedRunOwner && this.selectedRunRepo
      ? `${this.selectedRunOwner}/${this.selectedRunRepo}`
      : '';
  }

  /** Issue/PR number for the run-detail header, preferring the freshest known source. */
  get selectedRunDisplayNumber(): number | null {
    const ctx = this.jobStatus?.github_context ?? this.selectedRun?.github_context;
    return runIdentity(ctx)?.number ?? this.selectedRunNumber;
  }

  /**
   * Map a job status to a shared `.kh-badge--*` modifier.
   *
   * Preconditions: none (any string or undefined is accepted).
   * Postconditions: returns one of `running`/`completed`/`failed`/`cancelled`/`warning`/`neutral`;
   * unrecognized or missing statuses map to `neutral`.
   */
  badgeClass(status: string | undefined): string {
    switch (status) {
      case 'running':
      case 'pending':
        return 'running';
      // already_complete is a terminal success (the work was already done) — show the success
      // badge, not the 'neutral' default.
      case 'completed':
      case 'already_complete':
        return 'completed';
      case 'failed':
        return 'failed';
      case 'cancelled':
        return 'cancelled';
      case 'completed_with_failures':
      case 'waiting_for_user':
        return 'warning';
      default:
        return 'neutral';
    }
  }

  /**
   * True when a run is still in flight (non-terminal). Scopes run-row affordances that only make
   * sense for active runs (the "needs answers" badge and the live status/phase detail line).
   *
   * Preconditions: none.
   * Postconditions: returns `false` for any terminal status, `true` otherwise.
   */
  isRunActive(run: CodingTeamJobListItem): boolean {
    return !isCodingTeamTerminalStatus(run.status);
  }

  /**
   * True when a run is actively paused on questions the user must answer.
   *
   * Preconditions: none.
   * Postconditions: returns `true` only when the run is non-terminal and flagged
   * `waiting_for_answers` — so a terminal run that still carries a stale flag is never shown as
   * needing answers.
   */
  isRunWaiting(run: CodingTeamJobListItem): boolean {
    return !!run.waiting_for_answers && this.isRunActive(run);
  }

  /**
   * Relative "x ago" label for a run's last update.
   *
   * Preconditions: none.
   * Postconditions: returns `''` for a missing or unparseable timestamp (so a malformed value can
   * never render as "NaNd ago"), else `just now` / `Nm ago` / `Nh ago` / `Nd ago` for the elapsed
   * time since `isoString`.
   */
  timeAgo(isoString?: string): string {
    if (!isoString) return '';
    const date = new Date(isoString);
    if (isNaN(date.getTime())) return '';
    const diff = Date.now() - date.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  }

  /**
   * Copy the selected run's full job id to the clipboard, flashing a confirmation icon.
   *
   * Preconditions: none (a no-op when no run is selected).
   * Postconditions: when a run is selected and the Clipboard API is available, its id is written to
   * the clipboard and `jobIdCopied` is true for ~1.5s (the reset timer is tracked so it is cancelled
   * on destroy and never fires twice); a rejected clipboard write is swallowed so it cannot surface
   * as an unhandled rejection. When the Clipboard API is unavailable the confirmation still flashes.
   */
  copyJobId(): void {
    if (!this.selectedRunId) return;
    navigator.clipboard?.writeText(this.selectedRunId).catch(() => {
      // Clipboard write can reject (permission denied, insecure context); ignore — the user can
      // still read the id from the panel, and we must not emit an unhandled rejection.
    });
    this.jobIdCopied = true;
    if (this.copyResetTimer) clearTimeout(this.copyResetTimer);
    this.copyResetTimer = setTimeout(() => {
      this.jobIdCopied = false;
      this.copyResetTimer = null;
    }, 1500);
  }

  /** True when the job is paused on questions the user must answer. */
  hasPendingQuestions(): boolean {
    return !!this.jobStatus?.waiting_for_answers && (this.jobStatus?.pending_questions?.length ?? 0) > 0;
  }

  /**
   * Fold the post-submit status into the panel and restart polling from scratch.
   *
   * Restarting (rather than letting the old poller run on) discards any status fetch that was
   * already in flight before the answers were stored — a stale response would otherwise re-render
   * the just-answered questions — and also revives polling after a connection loss (answering proves
   * the connection is back), so the stale "lost connection" error is cleared too.
   *
   * Preconditions: none.
   * Postconditions: a no-op unless the emitted status carries `job_id`/`status` AND its `job_id`
   * matches the currently selected run — so a slow submit that resolves after the user switched runs
   * can't fold the wrong run's status in or restart polling for it.
   */
  onAnswersSubmitted(status: AnswersSubmittedStatus): void {
    // This page always configures the panel with submitEndpoint="coding-team", so the emitted union
    // member is the coding-team status shape — but verify the discriminating fields are present
    // rather than asserting blindly, so a future change to the child's emission can't fold a foreign
    // shape into `jobStatus`.
    if (!('job_id' in status) || !('status' in status)) return;
    // Ignore a status for a run the user has since switched away from (a slow submit could resolve
    // after the selection changed).
    if (this.selectedRunId == null || status.job_id !== this.selectedRunId) return;
    this.jobStatus = status as CodingTeamJobStatus;
    this.issueError = null;
    this.jobStatusError = null;
    this.startPolling(this.selectedRunId);
  }

  /** True when a non-terminal coding-team run is already working this issue in the expanded repo. */
  isIssueInProgress(issue: GitHubIssueItem): boolean {
    const repo = this.selectedRepo;
    if (!repo) return false;
    return this.activeRunKeys.has(runKey(repo.owner, repo.name, { type: 'issue', number: issue.number }));
  }

  /**
   * Signal CodingTeamWorkflow to continue a Temporal-native paused run (`resume_token` present).
   * No-op when no run is selected or a resume is already in flight (`resumingJob`), so a double-click
   * can't fire overlapping resume requests. On success, restarts polling from scratch; on error,
   * surfaces `issueError`.
   */
  resumeJob(): void {
    if (!this.selectedRunId || this.resumingJob) return;
    const jobId = this.selectedRunId;
    this.resumingJob = true;
    this.issueError = null;
    this.api
      .resumeJob(jobId)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: () => {
        this.resumingJob = false;
        this.startPolling(jobId);
      },
      error: (err: unknown) => {
        this.resumingJob = false;
        this.issueError = extractErrorDetail(err, 'Failed to resume job.');
      },
    });
  }

  private startPolling(jobId: string): void {
    this.stopPolling();
    this.pollSub = pollJobStatus(
      this.api,
      jobId,
      (status) => {
        // Discard a stale poll for a run the user has since switched away from.
        if (this.selectedRunId !== jobId) return;
        this.jobStatus = status;
        const identity = runIdentity(status.github_context);
        if (identity) {
          if (this.selectedRunNumber == null) {
            this.selectedRunNumber = identity.number;
          }
          if (this.selectedRunKind == null) {
            this.selectedRunKind = identity.type;
          }
        }
        if (!this.selectedRunOwner && status.github_context?.owner) {
          this.selectedRunOwner = status.github_context.owner;
          this.selectedRunRepo = status.github_context.repo;
        }
        // The selected run finished: refresh the list so it moves Running → Recent and drops its
        // "In progress" chip. The selection stays so its finished detail remains on screen.
        if (isCodingTeamTerminalStatus(status.status)) {
          this.refreshTrigger$.next();
        }
      },
      () => {
        // The status poll gave up: surface it on the page banner and in the run detail, so a
        // never-arriving first status shows an error instead of a perpetual "Starting…" spinner.
        this.issueError = 'Lost connection to the coding team — status polling failed.';
        this.jobStatusError = this.issueError;
      },
    );
  }

  private stopPolling(): void {
    if (this.pollSub) {
      this.pollSub.unsubscribe();
      this.pollSub = null;
    }
  }

  /** True once the selected run has reached a terminal state (finished — pollable no further). */
  isJobTerminal(): boolean {
    return isCodingTeamTerminalStatus(this.jobStatus?.status);
  }

  /**
   * Confirm a launched coding workflow.
   *
   * Preconditions: `event` is emitted by the embedded assistant chat when a run
   * starts; `job_id` is the queued run id, or null when no run was created.
   * Postconditions: when a `job_id` is present, shows a transient snackbar
   * confirmation (the app's convention for successful actions — replacing the
   * former persistent "queued" banner); a null id is a no-op.
   */
  onWorkflowLaunched(event: { job_id: string | null; conversation_id: string }): void {
    if (event.job_id) {
      this.notifications.saved(`Coding job queued — id ${event.job_id}.`);
    }
  }

  // ---------------------------------------------------------------------------
  // Out-of-scope issues (Issues tab)
  // ---------------------------------------------------------------------------

  /**
   * Load all unfiled out-of-scope issue proposals for the currently selected repo.
   *
   * Preconditions: `selectedRepo` is set (a repo was expanded in the GitHub tab).
   * Postconditions: `oosProposals` is populated with unfiled proposals from all
   * reviews for this repo, or `oosError` is set on failure.
   */
  loadOutOfScopeIssues(): void {
    const repo = this.selectedRepo;
    if (!repo) return;

    this.oosLoading = true;
    this.oosError = null;
    this.integrationsApi
      .getOutOfScopeIssues(repo.owner, repo.name)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (resp) => {
          this.oosProposals = resp.proposals;
          this.oosLoading = false;
        },
        error: (err) => {
          this.oosError = extractErrorDetail(err, 'Failed to load out-of-scope issues.');
          this.oosLoading = false;
        },
      });
  }

  // ---------------------------------------------------------------------------
  // Pull Requests (Pull Requests tab)
  // ---------------------------------------------------------------------------

  /**
   * Load the open pull requests for the currently expanded repo.
   *
   * Preconditions: `selectedRepo` is set (a repo was expanded in the GitHub tab).
   * Postconditions: on success `pulls` holds every open PR for the repo and `pullsLoaded`
   * is true; on error `pullError` is set. A slow load superseded by a repo switch is
   * discarded (latest-wins), and the loading flag is always cleared by the current handler.
   */
  loadPulls(): void {
    const repo = this.selectedRepo;
    if (!repo) return;
    const token = this.pullsLoad.next();
    this.loadingPulls = true;
    this.pullError = null;
    this.integrationsApi
      .getGitHubPullRequests({ owner: repo.owner, repo: repo.name })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (pulls) => {
          if (!this.pullsLoad.isCurrent(token)) return;
          this.loadingPulls = false;
          // Guard against a repo switch mid-flight (pullError/pulls belong to the expanded repo).
          if (this.selectedRepo?.full_name !== repo.full_name) return;
          this.pulls = pulls;
          this.pullsLoaded = true;
        },
        error: (err) => {
          if (!this.pullsLoad.isCurrent(token)) return;
          this.loadingPulls = false;
          if (this.selectedRepo?.full_name !== repo.full_name) return;
          this.pullError = extractErrorDetail(err, 'Failed to load pull requests.');
        },
      });
  }

  /** True while the "address comments" job for `pr` in the expanded repo is being started. */
  isAddressingPr(pr: GitHubPullRequestItem): boolean {
    const repo = this.selectedRepo;
    if (!repo) return false;
    return this.addressingPrs.has(prAddressKey(repo.owner, repo.name, pr.number));
  }

  /**
   * Start the flow that addresses & responds to every unresolved review comment on `pr`.
   *
   * Preconditions: none enforced — a no-op when no repo is expanded or a job for this PR is
   * already starting (`isAddressingPr`), so a double-click can't submit the same PR twice.
   * Postconditions: on success a toast reports the started job and its unresolved-comment count,
   * and the Runs list is refreshed so the job appears there; on error `pullError` is surfaced
   * (only while still on the PR's repo). The per-PR in-flight flag is cleared across the call.
   */
  addressPrComments(pr: GitHubPullRequestItem): void {
    const repo = this.selectedRepo;
    if (!repo) return;
    const key = prAddressKey(repo.owner, repo.name, pr.number);
    if (this.addressingPrs.has(key)) return;
    this.addressingPrs.add(key);
    this.pullError = null;
    this.integrationsApi
      .addressPrComments(pr.number, { owner: repo.owner, repo: repo.name })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (resp: AddressPrCommentsResponse) => {
          this.addressingPrs.delete(key);
          this.notifications.saved(
            `Addressing ${resp.unresolved_comment_count} unresolved comment(s) on PR #${resp.pr_number} — run ${resp.job_id}.`,
          );
          // The new job shows in the Runs list; nudge its poll so it appears promptly.
          this.refreshTrigger$.next();
        },
        error: (err: unknown) => {
          this.addressingPrs.delete(key);
          // Only surface the failure while still on the repo the PR belongs to (see confirmAndRun).
          if (this.selectedRepo?.full_name === repo.full_name) {
            this.pullError = extractErrorDetail(err, 'Failed to start addressing comments.');
          }
        },
      });
  }

  /**
   * File selected out-of-scope proposals as enhanced GitHub issues.
   *
   * Preconditions: `compositeIds` is a non-empty array of "job_id:proposal_id" strings.
   * Postconditions: calls the backend to file/merge issues, then reloads the list.
   */
  onFileOutOfScopeIssues(compositeIds: string[]): void {
    const repo = this.selectedRepo;
    if (!repo || compositeIds.length === 0) return;

    this.oosFiling = true;
    this.oosError = null;
    this.integrationsApi
      .fileOutOfScopeIssues(repo.owner, repo.name, compositeIds)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (resp) => {
          this.oosFiling = false;
          const count = resp.created.length;
          const merged = resp.created.filter((c) => c.merged_into_existing).length;
          const created = count - merged;
          const parts: string[] = [];
          if (created > 0) parts.push(`${created} issue(s) created`);
          if (merged > 0) parts.push(`${merged} merged into existing`);
          if (parts.length > 0) {
            this.notifications.saved(parts.join(', ') + '.');
          }
          if (resp.errors.length > 0) {
            this.oosError = resp.errors.join('; ');
          }
          // Reload to reflect the updated state
          this.loadOutOfScopeIssues();
        },
        error: (err) => {
          this.oosFiling = false;
          this.oosError = extractErrorDetail(err, 'Failed to file issues.');
        },
      });
  }
}
