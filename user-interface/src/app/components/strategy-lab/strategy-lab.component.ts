import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Input,
  OnDestroy,
  OnInit,
  ViewChild,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CommonModule, DecimalPipe, DatePipe, CurrencyPipe, JsonPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDividerModule } from '@angular/material/divider';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatTableModule } from '@angular/material/table';
import { MatSortModule } from '@angular/material/sort';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';
import { MatDialog } from '@angular/material/dialog';
import { RouterLink } from '@angular/router';
import { of } from 'rxjs';
import { finalize, map } from 'rxjs/operators';

import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { StrategyLabRunService } from '../../services/strategy-lab-run.service';
import { NotificationService } from '../../core/notification.service';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import { formatPct, formatRatio } from '../../shared/number-format';
import { DateOnlyPipe } from '../../shared/date-only.pipe';
import {
  ConfirmDialogComponent,
  type ConfirmDialogData,
} from '../../shared/confirm-dialog/confirm-dialog.component';
import {
  ASSET_CLASS_ICONS,
  returnColor,
  returnColorLabel,
  getAssetClassIcon,
  verdictLabel,
  verdictColor,
  gateIcon as pureGateIcon,
  gateSeverityClass as pureGateSeverityClass,
} from './strategy-lab.formatters';
import type {
  PaperTradingSession,
  PaperTradingComparison,
  QualityGateResult,
  StrategyLabRecord,
  StrategyLabResultsResponse,
  StrategyLabRunStatus,
  StrategyLabStreamEvent,
  StrategyLabProgressEvent,
  TradeRecord,
} from '../../models';

type FilterMode = 'all' | 'winning' | 'losing';

interface PhaseDefinition {
  id: string;
  label: string;
  icon: string;
}

interface ActivityLogEntry {
  time: string;
  status: 'active' | 'done' | 'error';
  message: string;
}

/** Precomputed per-gate template data — see `gateViewModels`. */
interface GateViewModel {
  gate: QualityGateResult;
  icon: string;
  severityClass: string;
  isRemedied: boolean;
}

/** Precomputed comparison-table row — see `comparisonMetrics`. */
interface ComparisonRow {
  label: string;
  backtest: string;
  paper: string;
  aligned: boolean;
}

const STRATEGY_LAB_PHASES: PhaseDefinition[] = [
  { id: 'ideating',     label: 'Ideate',    icon: 'psychology' },
  { id: 'coding',       label: 'Code',      icon: 'code' },
  { id: 'backtesting',  label: 'Backtest',  icon: 'play_circle' },
  { id: 'analyzing',    label: 'Analyze',   icon: 'summarize' },
];

/** Ordered phase IDs for determining completed/pending state. */
const PHASE_ORDER = STRATEGY_LAB_PHASES.map(p => p.id);

interface AssetCategoryOption {
  value: string;
  label: string;
  icon: string;
}

/** Title-case an asset-category value for display (e.g. 'stocks' → 'Stocks'). */
function categoryLabel(value: string): string {
  return value.length ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

/**
 * Shared text for both places the run's fate is genuinely unknown (not a
 * known failure/cancellation/completion) — `describeRunStatus()`'s
 * null-status branch and `handleStreamEvent()`'s SSE-reclaim branch — so the
 * wording can't drift between the two.
 */
const CONNECTION_LOST_MESSAGE = 'Strategy Lab lost track of the run — status unavailable.';

/** Build selector options from category values, deriving label + Material icon. */
function buildCategoryOptions(values: string[]): AssetCategoryOption[] {
  return values.map((value) => ({
    value,
    label: categoryLabel(value),
    icon: ASSET_CLASS_ICONS[value] ?? 'category',
  }));
}

/**
 * Fallback asset categories, used only until `GET /strategy-lab/config` supplies
 * the authoritative list (and if that fetch ever fails). The backend is the
 * source of truth — `applyCategoryConfig` overwrites this with the server's
 * `asset_categories` on init, so a stale fallback is visible only on the first
 * paint / on a config failure. Keep this list in sync with the backend's
 * `PROMPT_ASSET_CLASSES`; `options` is omitted because it is never a valid
 * ideation target. Module-level constants in this file use SCREAMING_SNAKE_CASE
 * (see STRATEGY_LAB_PHASES; ASSET_CLASS_ICONS lives in `./strategy-lab.formatters`).
 */
const DEFAULT_STRATEGY_LAB_CATEGORIES: AssetCategoryOption[] = buildCategoryOptions([
  'stocks',
  'crypto',
  'forex',
  'futures',
  'commodities',
]);

@Component({
  selector: 'app-strategy-lab',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [StrategyLabRunService],
  imports: [
    CommonModule,
    DecimalPipe,
    DatePipe,
    CurrencyPipe,
    JsonPipe,
    DateOnlyPipe,
    FormsModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatFormFieldModule,
    MatInputModule,
    MatProgressSpinnerModule,
    MatProgressBarModule,
    MatChipsModule,
    MatTooltipModule,
    MatDividerModule,
    MatButtonToggleModule,
    MatExpansionModule,
    MatTableModule,
    MatSortModule,
    MatPaginatorModule,
    RouterLink,
    InlineBannerComponent,
  ],
  templateUrl: './strategy-lab.component.html',
  styleUrl: './strategy-lab.component.scss',
})
export class StrategyLabComponent implements OnInit, OnDestroy {
  /**
   * Whether to render the component's own "Strategy Lab" `<h2>` heading.
   * Defaults to `true` (the dashboard-tab context, where this heading is the
   * only heading anchor). The standalone route wrapper — which already
   * renders its own `<h1>Strategy Lab</h1>` — sets this to `false` so the
   * title isn't duplicated.
   */
  @Input() showTitle = true;

  private readonly api = inject(InvestmentApiService);
  private readonly integrations = inject(IntegrationsApiService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly dialog = inject(MatDialog);
  private readonly notify = inject(NotificationService);
  /** Owns SSE/polling/active-run tracking and per-record paper-trading polling. */
  readonly runService = inject(StrategyLabRunService);

  /** True while a destructive confirm dialog is open — blocks re-entrant opens. */
  private confirmingDestructive = false;

  /**
   * TradingView data-source status, used to show/hide the "using free public
   * data" notice. `tradingViewStatusKnown` gates the banner so it stays hidden
   * until we've confirmed the status (and stays hidden if the status call fails —
   * we never nag when we can't tell).
   */
  readonly tradingViewStatusKnown = signal(false);
  readonly tradingViewConfigured = signal(false);

  /** True while a "start new run" POST is in flight, before runService begins tracking it. */
  private readonly startingRun = signal(false);
  /**
   * True while starting a new run OR `runService` is actively tracking one.
   * Mirrors the pre-extraction component's single `running` flag: the button
   * must disable (and show "Starting…") for the whole window from click to
   * the first run-status update, not just once the service takes over.
   */
  readonly running = computed(() => this.startingRun() || this.runService.running());

  readonly loading = signal(false);
  readonly clearingAll = signal(false);
  readonly error = signal<string | null>(null);
  /**
   * Non-fatal notice banner (dismissible, non-error styling): shown when a run
   * finishes with errored/skipped cycles, when a non-fatal `batch_warning`
   * arrives mid-run, or when a run is cancelled by the user (a deliberate stop
   * is a notice, not a red-banner error).
   */
  readonly completionWarning = signal<string | null>(null);
  /** Lab record id currently being deleted (disables actions on that card). */
  readonly deletingLabRecordId = signal<string | null>(null);

  // User-configurable batch settings (mirror backend Field bounds).
  // BATCH_COUNT_MAX is hydrated from GET /strategy-lab/config on init so the
  // operator-tunable STRATEGY_LAB_MAX_BATCH_COUNT (default 100) propagates to
  // the UI; 100 here is only the fallback if the config fetch fails.
  readonly BATCH_SIZE_MIN = 1;
  readonly BATCH_SIZE_MAX = 25;
  readonly BATCH_COUNT_MIN = 1;
  readonly BATCH_COUNT_MAX = signal(100);
  batchSize = 10;
  batchCount = 1;

  // Asset-category selection. `categoryOptions` is seeded from the fallback list
  // and replaced by the backend's authoritative set once `loadConfig` resolves
  // (single source of truth). Defaults to every category selected (equivalent to
  // no constraint); the user narrows it to steer the design agent. At least one
  // category must stay selected — a run with zero categories is invalid.
  // `selectedCategories` binds directly to the multi-select toggle group's
  // ngModel; canonical order is reasserted at payload time.
  readonly categoryOptions = signal<AssetCategoryOption[]>(DEFAULT_STRATEGY_LAB_CATEGORIES);
  readonly selectedCategories = signal<string[]>(DEFAULT_STRATEGY_LAB_CATEGORIES.map((c) => c.value));
  // Set once the user touches the category toggles. Distinguishes an explicit
  // "I want exactly these" selection (even when that happens to be all of them)
  // from the untouched default, so a late backend category list reconciles
  // against the user's intent rather than inferring it from selection state.
  private userAdjustedCategories = false;

  /** A run requires at least one selected category. */
  get categoriesValid(): boolean {
    return this.selectedCategories().length > 0;
  }

  /** Toggle-group change handler: record the user's selection and mark it explicit. */
  onCategoriesChanged(values: string[]): void {
    this.selectedCategories.set(values);
    this.userAdjustedCategories = true;
  }

  filter: FilterMode = 'all';
  readonly results = signal<StrategyLabResultsResponse | null>(null);
  readonly displayedItems = signal<StrategyLabRecord[]>([]);

  readonly totalCount = signal(0);
  readonly winningCount = signal(0);
  readonly losingCount = signal(0);

  // Per-card expand/collapse state (collapsed by default)
  expandedCards = new Set<string>();

  toggleCard(id: string): void {
    if (this.expandedCards.has(id)) {
      this.expandedCards.delete(id);
    } else {
      this.expandedCards.add(id);
    }
  }

  isCardExpanded(id: string): boolean {
    return this.expandedCards.has(id);
  }

  /**
   * DOM id for a card's disclosure region, shared by the toggle button's
   * `aria-controls` and the region's `id` so the two can't drift apart.
   *
   * Preconditions: `id` is a non-empty `lab_record_id`.
   * Postconditions: returns a non-empty string unique per `id`, stable across
   *   change-detection cycles for the same `id`.
   */
  cardBodyId(id: string): string {
    return 'card-body-' + id;
  }

  /**
   * Accessible name for a card's disclosure button. States the action
   * available (Show/Hide) rather than the current state, per standard
   * toggle-button ARIA labeling convention.
   *
   * Preconditions: `record.strategy.asset_class` is a non-empty string.
   * Postconditions: returns "Show details for {asset_class} strategy" when
   *   collapsed, "Hide details for {asset_class} strategy" when expanded.
   */
  cardToggleLabel(record: StrategyLabRecord): string {
    const verb = this.isCardExpanded(record.lab_record_id) ? 'Hide' : 'Show';
    return `${verb} details for ${record.strategy.asset_class} strategy`;
  }

  /**
   * Accessible name for a card's disclosure region (`role="region"`).
   *
   * Preconditions: `record.strategy.asset_class` is a non-empty string.
   * Postconditions: returns a non-empty, state-independent label.
   */
  cardRegionLabel(record: StrategyLabRecord): string {
    return `${record.strategy.asset_class} strategy details`;
  }

  /**
   * Accessible name for the trade-table's scrollable wrapper (WCAG 2.4.7 —
   * the wrapper, not the table, must be focusable since the global outline
   * would otherwise be clipped by `overflow-x: auto`).
   *
   * Preconditions: `record.strategy.asset_class` is a non-empty string.
   * Postconditions: returns a non-empty, state-independent label.
   */
  tradeTableRegionLabel(record: StrategyLabRecord): string {
    return `${record.strategy.asset_class} strategy trade history, scrollable`;
  }

  /**
   * Accessible name for the paper-trading comparison table's scrollable
   * wrapper (same rationale as `tradeTableRegionLabel`).
   *
   * Preconditions: `record.strategy.asset_class` is a non-empty string.
   * Postconditions: returns a non-empty, state-independent label.
   */
  comparisonTableRegionLabel(record: StrategyLabRecord): string {
    return `${record.strategy.asset_class} strategy backtest vs. paper-trading comparison, scrollable`;
  }

  /**
   * Accessible name for the trade-ledger `<table>` itself — distinct from
   * `tradeTableRegionLabel`, which names the scrollable wrapper div: a
   * screen reader's table-navigation commands read the `<table>`'s own
   * accessible name, not the wrapper's.
   *
   * Preconditions: `record.backtest.trades` is defined (may be empty).
   * Postconditions: returns a non-empty, state-independent label.
   */
  tradeTableAccessibleName(record: StrategyLabRecord): string {
    return `Trade ledger, ${this.tradeCount(record)} trades`;
  }

  // Per-card trade ledger state
  tradeLedgerPages: Record<string, number> = {};       // lab_record_id → current page index
  readonly PAGE_SIZE = 20;
  readonly TRADE_COLUMNS = [
    'trade_num', 'entry_date', 'exit_date', 'symbol',
    'entry_price', 'exit_price', 'shares', 'return_pct',
    'net_pnl', 'cumulative_pnl', 'outcome',
  ];

  // Paper trading state
  /** True while a "run paper trading" POST is in flight for this record, before runService takes over. */
  private readonly startingPaperTrade = signal<string | null>(null);
  /** Lab record id currently being paper traded — see `running`'s doc comment for why this merges two sources. */
  readonly paperTradingLabRecordId = computed(
    () => this.startingPaperTrade() ?? this.runService.paperTradingLabRecordId(),
  );

  // Phase stepper + activity log
  readonly STRATEGY_LAB_PHASES = STRATEGY_LAB_PHASES;
  readonly activityLog = signal<ActivityLogEntry[]>([]);
  private lastCycleIndex = -1;

  /**
   * Terminal-outcome text for the aria-live status region (`runAnnouncement`).
   * A signal (not a plain field) so `runAnnouncement` can be a `computed()`
   * that reactively tracks it. Set from `handleStreamEvent()`'s
   * `complete`/`error` branches — using the terminal event's own data, while
   * `runService.runStatus()` is still populated — or, when neither branch
   * fires (SSE degrades to polling, or a reconnect gets only a terminal
   * snapshot then `done`), backstopped by `refreshResultsOnRunFinish`'s
   * fallback once `runStatus()` has already cleared. Reset to null when a
   * new run starts so a stale outcome from a previous run can't leak into
   * the next run's brief "Starting…" window.
   */
  private readonly runOutcomeAnnouncement = signal<string | null>(null);

  /**
   * The red error-banner message a terminal 'error' stream event set for THIS
   * run (the failure/interrupt detail, or the connection-lost message), or null
   * if the run has not ended in an error. `refreshResultsOnRunFinish` re-asserts
   * exactly this after `loadResults()` clears `error`, so ONLY a genuine
   * terminal run error survives the run-finish refresh — an unrelated ambient
   * error still showing (e.g. an `errors$` paper-trading poll failure) is left
   * to be cleared, not resurrected onto a cleanly-completed run. A plain field
   * (not a signal) so reading it in the effect adds no reactive dependency.
   * Reset to null when a new run starts.
   */
  private terminalErrorBanner: string | null = null;

  @ViewChild('logContainer') logContainer?: ElementRef<HTMLElement>;

  /** Pending auto-scroll timer id, cleared on destroy. */
  private autoScrollTimeoutId: ReturnType<typeof setTimeout> | null = null;

  /** Tracks the previous `runService.running()` value so the effect below can detect a true→false transition. */
  private wasRunning = false;

  /**
   * Refreshes the results list exactly once whenever `runService.running()`
   * transitions from true to false — covering every way a run ends (an
   * explicit `complete`/`error` event, the SSE stream's own `done`-then-close,
   * or the REST-polling fallback detecting a terminal status) with one rule,
   * rather than duplicating a `loadResults()` call at each of those call sites.
   *
   * Also backstops `runOutcomeAnnouncement` for the same transition: the
   * `complete`/`error` branches of `handleStreamEvent()` already set it from
   * the event's own data, but a run that ends WITHOUT either of those events
   * reaching this component (SSE degrades to polling and polling itself
   * observes the terminal status; or a reconnect's terminal `snapshot` is
   * followed straight by `done`) leaves it unset. The fallback only fills
   * that gap — it never overwrites an outcome the explicit branches already
   * derived.
   *
   * Preconditions: none — constructed once as a field initializer, runs for
   *   the component's lifetime.
   * Postconditions: on every `running()` true→false transition, `loadResults()`
   *   runs exactly once and `runOutcomeAnnouncement` holds a non-null value
   *   (either one `handleStreamEvent()` already set, or this fallback's
   *   `describeRunStatus()` derivation) by the time the effect returns. A
   *   terminal failure/interrupt/connection-lost message (captured in
   *   `terminalErrorBanner`) survives the `loadResults()` refresh — which
   *   clears `error` before re-fetching — so the sighted banner is durable
   *   rather than flashing and vanishing; an unrelated ambient error is NOT
   *   preserved.
   */
  private readonly refreshResultsOnRunFinish = effect(() => {
    const isRunning = this.runService.running();
    if (!isRunning && this.wasRunning) {
      this.runOutcomeAnnouncement.update(
        (current) => current ?? this.describeRunStatus(this.runService.lastTerminalStatus()),
      );
      // loadResults() clears `error` synchronously before its async fetch, so a
      // terminal 'error'/reclaim message set by handleStreamEvent() would be
      // wiped on the very transition that produced it — leaving sighted users no
      // visible reason the run stopped. Re-assert it afterward. Crucially this
      // re-asserts only `terminalErrorBanner` (set solely by a terminal run
      // error), NOT whatever `error()` happens to hold: an unrelated ambient
      // error still showing (e.g. an errors$ paper-trading poll failure) is left
      // cleared, not resurrected onto a cleanly-completed run. (A cancellation
      // uses `completionWarning`, which loadResults() never clears.) If the
      // reload itself errors, its own handler's message wins — the run is over.
      const terminalError = this.terminalErrorBanner;
      this.loadResults();
      if (terminalError) this.error.set(terminalError);
    }
    this.wasRunning = isRunning;
  });

  /**
   * The single source of terminal-outcome sentences for the aria-live region.
   * Both `handleStreamEvent()`'s terminal branches (fed a minimal object built
   * from the terminal event's own fields) and `refreshResultsOnRunFinish`'s
   * fallback (fed `runService.lastTerminalStatus()`) route through here, so the
   * live-SSE announcement and the poll-fallback announcement can never word the
   * same outcome differently.
   *
   * Preconditions: `status` is `null` ONLY to mean "the run's fate is genuinely
   *   unknown" (`StrategyLabRunService` clears `runStatus` before capturing it
   *   into `lastTerminalStatus` specifically when its polling fallback itself
   *   errors) — never "no run happened"; callers only invoke this at run end.
   * Postconditions: returns a sentence reflecting `status.status`/
   *   `errored_cycles`/`skipped_cycles` when `status` is non-null (errors
   *   outrank skips outrank a clean finish); a distinct connection-lost
   *   sentence when `status` is null — never the generic "complete" sentence
   *   for an outcome that isn't actually known. Pure; always non-empty.
   */
  private describeRunStatus(
    status: { status?: string; errored_cycles?: number; skipped_cycles?: number } | null,
  ): string {
    if (!status) return CONNECTION_LOST_MESSAGE;
    if (status.status === 'failed') return 'Strategy Lab run failed.';
    if (status.status === 'cancelled') return 'Strategy Lab run cancelled.';
    if (status.status === 'interrupted') return 'Strategy Lab run interrupted.';
    if (status.status === 'completed_with_errors' || (status.errored_cycles ?? 0) > 0) {
      return 'Strategy Lab run finished with errors.';
    }
    if ((status.skipped_cycles ?? 0) > 0) {
      return 'Strategy Lab run finished with some strategies skipped.';
    }
    return 'Strategy Lab run complete.';
  }

  ngOnInit(): void {
    this.loadConfig();
    this.loadResults();
    this.loadPaperTradingResults();
    this.runService.checkForActiveRun();
    this.loadTradingViewStatus();

    this.runService.events$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((event) => this.handleStreamEvent(event));
    this.runService.errors$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((message) => this.error.set(message));
  }

  /**
   * Read TradingView data-source status to decide whether to show the "using
   * free public data" notice.
   *
   * Postconditions: on success, `tradingViewConfigured` reflects whether an
   *   enabled server URL is stored and `tradingViewStatusKnown` is true; on
   *   error, status stays unknown so the notice remains hidden.
   */
  private loadTradingViewStatus(): void {
    this.integrations
      .getTradingViewConfig()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (cfg) => {
          this.tradingViewConfigured.set(cfg.enabled && !!cfg.mcp_server_url);
          this.tradingViewStatusKnown.set(true);
        },
        error: () => {
          // Can't determine status → don't nag.
          this.tradingViewStatusKnown.set(false);
        },
      });
  }

  private loadConfig(): void {
    this.api
      .getStrategyLabConfig()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (cfg) => {
        if (cfg.batch_count_max >= this.BATCH_COUNT_MIN) {
          this.BATCH_COUNT_MAX.set(cfg.batch_count_max);
        }
        this.applyCategoryConfig(cfg.asset_categories);
      },
      // Keep the fallback list; batch controls + categories still work. Warn so
      // an unreachable config endpoint is diagnosable in production.
      error: (err) =>
        console.warn('Failed to load strategy lab config; using fallback categories', err),
    });
  }

  /**
   * Adopt the backend's authoritative category list when present, keeping the
   * UI in sync with the server's ideation-valid classes. A missing/empty list
   * leaves the fallback options untouched.
   *
   * The selection is reconciled rather than blindly reset: if the user has not
   * touched the toggles (`userAdjustedCategories` is false), it becomes all of
   * the new options (the no-constraint default); if the user made an explicit
   * choice — e.g. while a slow config request was in flight — that choice is
   * preserved, dropping only values the backend no longer offers (falling back
   * to all when nothing valid remains, so a run is never left with zero
   * categories).
   */
  private applyCategoryConfig(categories: string[] | undefined): void {
    if (!categories?.length) {
      return;
    }
    const selected = new Set(this.selectedCategories());
    const options = buildCategoryOptions(categories);
    this.categoryOptions.set(options);
    const available = options.map((c) => c.value);

    if (!this.userAdjustedCategories) {
      this.selectedCategories.set(available);
      return;
    }
    const preserved = available.filter((v) => selected.has(v));
    this.selectedCategories.set(preserved.length ? preserved : available);
  }

  ngOnDestroy(): void {
    if (this.autoScrollTimeoutId !== null) {
      clearTimeout(this.autoScrollTimeoutId);
      this.autoScrollTimeoutId = null;
    }
  }

  // ---------------------------------------------------------------------------
  // SSE stream event side effects (run-status folding itself is runService's job)
  // ---------------------------------------------------------------------------

  /**
   * Reacts to a `runService.events$` emission for the side effects that
   * aren't `StrategyLabRunStatus` fields: activity-log bookkeeping,
   * refreshing results after a completed cycle, and the completion/error
   * banners. Run-status folding already happened inside `runService` before
   * this fires.
   *
   * Preconditions: none — every branch is safe regardless of `runService`
   *   state.
   * Postconditions: `activityLog`, `completionWarning`, `error`, and
   *   `runOutcomeAnnouncement` are updated for event types that carry them
   *   (`complete`/`error`/`cancelled` set `runOutcomeAnnouncement` directly
   *   from the terminal event's own data); `loadResults()` runs after a completed
   *   cycle (in addition to `refreshResultsOnRunFinish`'s once-per-run
   *   refresh — a multi-cycle run's earlier cycles need this mid-run call
   *   since `running()` stays true until the whole run ends).
   */
  private handleStreamEvent(event: StrategyLabStreamEvent): void {
    if (event.type === 'progress' && this.runService.runStatus()) {
      // Reset activity log when a new cycle starts.
      if (event.cycle_index !== this.lastCycleIndex) {
        this.activityLog.set([]);
        this.lastCycleIndex = event.cycle_index;
      }
      this.addLogEntry(event.phase, event.sub_phase, event);
    }

    if (event.type === 'cycle_complete' && this.runService.runStatus()) {
      this.activityLog.set([]);
      this.lastCycleIndex = -1;
      this.loadResults();
    }

    if (event.type === 'batch_warning' && this.runService.runStatus()) {
      // Non-fatal pre-batch issue (e.g. signal-brief failure). Surface as a
      // gentle warning; the run is still progressing. Guarded like the
      // 'progress'/'cycle_complete' branches above so a stale event for a
      // run that has already ended can't resurrect the warning banner.
      this.completionWarning.set(
        event.reason === 'signal_brief_failed'
          ? 'Signal brief unavailable for a batch; strategies continued without it.'
          : event.reason || 'A non-fatal warning occurred during a batch.',
      );
    }

    if (event.type === 'complete') {
      // completionWarning is the sighted dismissible banner — kept scoped to
      // genuine errors only (its long-standing condition): a skip-only
      // completion already has a dedicated in-progress skipped-badge, so a
      // banner re-announcing it at the end would be new behavior. The aria-live
      // sentence, by contrast, covers skips too — the badge disappears once
      // `running()` goes false, so this terminal announcement is a
      // screen-reader user's only remaining signal that not every requested
      // strategy was produced.
      const hasErrors = event.errored_count > 0 || event.status === 'completed_with_errors';
      const hasSkips = event.skipped_count > 0;
      if (hasErrors) {
        const parts: string[] = [`${event.errored_count} cycle(s) errored`];
        if (hasSkips) parts.push(`${event.skipped_count} cycle(s) skipped`);
        this.completionWarning.set(`Run finished with ${parts.join(' and ')}. See details below.`);
      }
      // Route through describeRunStatus (fed the event's own counts) so the
      // live-SSE sentence can never drift from the poll-fallback one.
      this.runOutcomeAnnouncement.set(
        this.describeRunStatus({
          status: event.status,
          errored_cycles: event.errored_count,
          skipped_cycles: event.skipped_count,
        }),
      );
    }

    if (event.type === 'error') {
      if (event.detail === undefined) {
        // The shared-infra "subscription reclaimed" wire shape
        // (StrategyLabErrorReclaimEvent) carries only `.error`, never
        // `.detail` — a connection-level event (e.g. eviction under load),
        // not necessarily a job failure, so it gets its own message rather
        // than confidently announcing a failure the run may not have had.
        this.setTerminalError(CONNECTION_LOST_MESSAGE);
        this.runOutcomeAnnouncement.set(CONNECTION_LOST_MESSAGE);
      } else {
        // A genuine user cancellation is never routed through 'error' — it's
        // its own 'cancelled' event type (branch below) — so every 'error'
        // event reaching here is a real failure or an external stop. The
        // backend marks an external stop 'failed' or 'interrupted' and carries
        // that on `terminal_status`; describeRunStatus announces the two
        // distinctly so an externally-interrupted run isn't spoken as "failed".
        this.setTerminalError(event.detail || 'Run failed');
        this.runOutcomeAnnouncement.set(
          this.describeRunStatus({ status: event.terminal_status ?? 'failed' }),
        );
      }
    }

    if (event.type === 'cancelled') {
      // A deliberate user cancellation is not an error — surface it in the
      // non-error (dismissible warning) banner rather than the red error one,
      // now that cancellation has its own event type. The aria-live region
      // announces the outcome regardless.
      this.completionWarning.set(event.detail || 'Run cancelled by user.');
      this.runOutcomeAnnouncement.set(this.describeRunStatus({ status: 'cancelled' }));
    }
  }

  /**
   * Set the red error banner for a terminal run error AND record it in
   * `terminalErrorBanner` so `refreshResultsOnRunFinish` re-asserts it across
   * the run-finish `loadResults()` clear.
   *
   * Preconditions: called only from `handleStreamEvent()`'s terminal 'error'
   *   branches (a real failure/interrupt, or a connection-lost reclaim).
   * Postconditions: `error()` and `terminalErrorBanner` both hold `message`.
   */
  private setTerminalError(message: string): void {
    this.error.set(message);
    this.terminalErrorBanner = message;
  }

  // ---------------------------------------------------------------------------
  // Results
  // ---------------------------------------------------------------------------

  loadResults(): void {
    this.loading.set(true);
    this.error.set(null);
    this.api
      .getStrategyLabResults()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (res) => {
        this.results.set(res);
        this.totalCount.set(res.count);
        this.winningCount.set(res.winning_count);
        this.losingCount.set(res.losing_count);
        this.applyFilter();
        this.loading.set(false);
      },
      error: (err) => {
        this.error.set(extractErrorDetail(err, 'Failed to load results.'));
        this.loading.set(false);
      },
    });
  }

  /**
   * Start a new Strategy Lab run with the current form configuration.
   *
   * Preconditions: no run is already in progress (`running()` is false — a
   *   re-entrant call is ignored) and at least one asset category is selected
   *   (`categoriesValid` is true; when violated this sets `error` and returns
   *   without calling the API).
   * Postconditions: clamps batch size/count into range and reflects them back to
   *   the form; `running()` reads true (via `startingRun`) and `error`/
   *   `completionWarning`/`runOutcomeAnnouncement`/`terminalErrorBanner` all
   *   reset so no prior run's outcome leaks into this one; POSTs a
   *   `RunStrategyLabRequest`.
   *   `allowed_asset_classes` is sent in canonical (`categoryOptions`) order
   *   only when the selection is a strict subset — when every category is
   *   selected the field is omitted, matching the backend's "no constraint"
   *   semantics and trimming the payload. On success `runService` begins
   *   tracking the run; on error `startingRun` clears and the message surfaces.
   */
  runNewStrategy(): void {
    // Re-entrancy guard: the run button is disabled while a run is active, but a
    // programmatic call or double-click must not start a second run — that would
    // orphan the first run and open a duplicate status stream.
    if (this.running()) {
      return;
    }
    // Guard the invalid-zero-categories case (the button is also disabled, but
    // a programmatic call must not start a run constrained to nothing).
    if (!this.categoriesValid) {
      this.error.set('Select at least one asset category to generate strategies for.');
      return;
    }

    const batchSize = this.clamp(this.batchSize, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
    const batchCount = this.clamp(this.batchCount, this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX());
    // Reflect any clamping back into the form so the user sees what was sent.
    this.batchSize = batchSize;
    this.batchCount = batchCount;

    // Preserve canonical order so the payload is stable regardless of click order.
    const categoryOptions = this.categoryOptions();
    const selectedCategories = this.selectedCategories();
    const allowedAssetClasses = categoryOptions
      .map((c) => c.value)
      .filter((v) => selectedCategories.includes(v));
    // Omit the field when every category is selected — equivalent to "no
    // constraint" server-side, and a smaller payload.
    const allConstraintsOff = allowedAssetClasses.length === categoryOptions.length;

    this.startingRun.set(true);
    this.error.set(null);
    this.completionWarning.set(null);
    this.runOutcomeAnnouncement.set(null);
    this.terminalErrorBanner = null;
    this.api
      .runStrategyLab({
        batch_size: batchSize,
        batch_count: batchCount,
        allowed_asset_classes: allConstraintsOff ? undefined : allowedAssetClasses,
      })
      .subscribe({
      next: (res) => {
        const initialStatus: StrategyLabRunStatus = {
          run_id: res.run_id,
          status: 'running',
          started_at: new Date().toISOString(),
          total_cycles: res.total_cycles,
          completed_cycles: 0,
          skipped_cycles: 0,
          errored_cycles: 0,
          errored_details: [],
          completed_record_ids: [],
          batch_size: batchSize,
          batch_count: batchCount,
          completed_batches: 0,
          current_batch: batchCount > 1 ? 1 : null,
        };
        this.runService.startRun(res.run_id, initialStatus);
        this.startingRun.set(false);
      },
      error: (err) => {
        this.startingRun.set(false);
        this.error.set(extractErrorDetail(err, 'Strategy run failed.'));
      },
    });
  }

  private clamp(value: number, min: number, max: number): number {
    const n = Number.isFinite(value) ? Math.floor(value) : min;
    return Math.max(min, Math.min(max, n));
  }

  /** Label for the run button — adapts to single- vs multi-batch mode. */
  runButtonLabel(): string {
    if (this.batchCount > 1) {
      const total = this.batchSize * this.batchCount;
      return `Run ${this.batchSize} × ${this.batchCount} = ${total} strategies`;
    }
    return `Run ${this.batchSize} strateg${this.batchSize === 1 ? 'y' : 'ies'}`;
  }

  onFilterChange(mode: FilterMode): void {
    this.filter = mode;
    this.applyFilter();
  }

  private applyFilter(): void {
    const all = this.results()?.items ?? [];
    if (this.filter === 'winning') {
      this.displayedItems.set(all.filter((r) => r.is_winning));
    } else if (this.filter === 'losing') {
      this.displayedItems.set(all.filter((r) => !r.is_winning));
    } else {
      this.displayedItems.set(all);
    }
  }

  readonly returnColor = returnColor;
  readonly returnColorLabel = returnColorLabel;

  // ---------------------------------------------------------------------------
  // Phase stepper state
  // ---------------------------------------------------------------------------

  isPhaseCompleted(phaseId: string): boolean {
    const current = this.runService.runStatus()?.current_cycle?.phase;
    if (!current) return false;
    const currentIdx = PHASE_ORDER.indexOf(current);
    const phaseIdx = PHASE_ORDER.indexOf(phaseId);
    if (currentIdx < 0 || phaseIdx < 0) return false;
    return phaseIdx < currentIdx;
  }

  isCurrentPhase(phaseId: string): boolean {
    return this.runService.runStatus()?.current_cycle?.phase === phaseId;
  }

  isPhasePending(phaseId: string): boolean {
    return !this.isPhaseCompleted(phaseId) && !this.isCurrentPhase(phaseId);
  }

  /**
   * Screen-reader state text for a phase-stepper step, mirroring the visual
   * completed/current/pending cue that sighted users get from CSS classes and
   * the icon swap.
   *
   * Preconditions: `phaseId` is one of the STRATEGY_LAB_PHASES ids; called only
   *   while a run is active (the stepper renders under `runStatus.current_cycle`).
   * Postconditions: returns exactly one of `'completed'`, `'current step'`, or
   *   `'not started'`, derived solely from `isPhaseCompleted`/`isCurrentPhase`
   *   (no independent ordering logic), so it never disagrees with the visual state.
   */
  phaseStateLabel(phaseId: string): string {
    if (this.isPhaseCompleted(phaseId)) return 'completed';
    if (this.isCurrentPhase(phaseId)) return 'current step';
    return 'not started';
  }

  readonly getAssetClassIcon = getAssetClassIcon;

  // ---------------------------------------------------------------------------
  // Activity log
  // ---------------------------------------------------------------------------

  private addLogEntry(phase: string, subPhase: string | undefined, data: StrategyLabProgressEvent): void {
    const now = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

    const msg = this.buildLogMessage(phase, subPhase, data);
    if (!msg) return;

    const isTerminal = subPhase === 'completed' || subPhase === 'data_loaded';
    const newEntry: ActivityLogEntry = { time: now, status: isTerminal ? 'done' : 'active', message: msg };

    this.activityLog.update((log) => {
      // Mark the previous active entry as done (if it's still active when a new entry arrives).
      let lastActiveIndex = -1;
      for (let i = log.length - 1; i >= 0; i--) {
        if (log[i].status === 'active') {
          lastActiveIndex = i;
          break;
        }
      }
      const closed = lastActiveIndex === -1
        ? log
        : log.map((entry, i) => (i === lastActiveIndex ? { ...entry, status: 'done' as const } : entry));
      return [...closed, newEntry];
    });

    // Auto-scroll the log container. Track the timer so a destroy mid-wait
    // cancels it — the callback would otherwise touch a detached element.
    if (this.autoScrollTimeoutId !== null) {
      clearTimeout(this.autoScrollTimeoutId);
    }
    this.autoScrollTimeoutId = setTimeout(() => {
      this.autoScrollTimeoutId = null;
      this.logContainer?.nativeElement?.scrollTo({ top: 999999, behavior: 'smooth' });
    }, 50);
  }

  private buildLogMessage(phase: string, subPhase: string | undefined, data: StrategyLabProgressEvent): string {
    const strategy = data['strategy'] as { asset_class?: string; hypothesis?: string } | undefined;
    const round = data['refinement_round'] as number | undefined;

    switch (phase) {
      case 'ideating':
        if (subPhase === 'started') return 'Ideating new trading strategy & generating code...';
        if (subPhase === 'completed') return `Strategy ideated — ${strategy?.asset_class ?? 'unknown'} asset class`;
        return 'Ideating...';
      case 'coding':
        if (subPhase === 'started') return 'Validating strategy spec and code safety...';
        if (subPhase === 'completed') return `Code validated (${data['checks_total'] ?? '?'} checks, ${data['checks_passed'] ?? '?'} passed)`;
        if (subPhase === 'failed') return `Validation failed (${(data['checks_total'] as number ?? 0) - (data['checks_passed'] as number ?? 0)} critical issue(s))`;
        if (subPhase === 'refining') return `Refining code (round ${(round ?? 0) + 1}/10) — fixing ${data['failure_phase'] ?? 'issues'}...`;
        if (subPhase === 'refined') return `Code refined — ${data['changes_made'] ?? 'code updated'}`;
        return 'Coding...';
      case 'backtesting':
        if (subPhase === 'fetching_data') return 'Fetching historical market data...';
        if (subPhase === 'data_loaded') return `Market data loaded (${data['symbols_count'] ?? '?'} symbols, ${(data['bars_count'] as number ?? 0).toLocaleString()} bars)`;
        if (subPhase === 'running_code') return 'Executing strategy backtest in sandbox...';
        if (subPhase === 'completed') return `Backtest complete — ${data['trades_count'] ?? '?'} trades in ${((data['execution_time'] as number) ?? 0).toFixed(1)}s`;
        return 'Backtesting...';
      case 'analyzing':
        if (subPhase === 'draft') return 'Generating analysis narrative...';
        if (subPhase === 'review') return 'Self-reviewing analysis against metrics...';
        if (subPhase === 'completed') return `Analysis complete — ${data['is_winning'] ? 'WINNING' : 'LOSING'}`;
        return 'Analyzing...';
      default:
        return `${phase} — ${subPhase ?? 'processing'}`;
    }
  }

  /**
   * Precomputed (via `computed()`, not a template-bound method call) so it's
   * only recalculated when `runService.runStatus()` actually changes, not on
   * every change-detection pass that happens to touch this template region.
   */
  readonly progressPercent = computed(() => {
    const status = this.runService.runStatus();
    if (!status || status.total_cycles === 0) return 0;
    return Math.round((status.completed_cycles / status.total_cycles) * 100);
  });

  /**
   * Cycles the run has finished attempting (successfully or not) as of
   * `status`. Shared by `currentStrategyNumber` and `runAnnouncement`'s
   * "Finishing up" decision so both derive the same number from one
   * computation.
   *
   * Preconditions: `status` is the caller's own already-read
   *   `runService.runStatus()` snapshot (never re-read here, so this stays
   *   safe to call from inside a `computed()`).
   * Postconditions: returns `completed_cycles` + `skipped_cycles` +
   *   `errored_cycles`, minus cycles double-counted by a post-completion
   *   tracker-merge failure (main.py's wave loop — published as
   *   `cycle_complete`, then separately re-published as `cycle_errored` with
   *   `reason: 'tracker_merge_failed'` for the same `cycle_index`). The
   *   subtraction reads `tracker_merge_error_count`, the backend's own
   *   uncapped counter for that reason, rather than filtering
   *   `errored_details` (capped at 50 entries server-side), so the result
   *   stays exact even once matching entries have evicted from that array.
   */
  private cycleProgress(status: StrategyLabRunStatus): number {
    return (
      status.completed_cycles +
      status.skipped_cycles +
      (status.errored_cycles ?? 0) -
      (status.tracker_merge_error_count ?? 0)
    );
  }

  /**
   * The strategy position to display — shared by the sighted "Strategy N of
   * M" text and `runAnnouncement`'s aria-live equivalent so the two can
   * never disagree for the same `runStatus`.
   *
   * Deliberately derived from the MONOTONIC attempted-cycle count, never from
   * `current_cycle.cycle_index`: the default run executes up to `max_parallel`
   * cycles concurrently (3), and the backend rewrites the single shared
   * `current_cycle` from whichever sibling most recently emitted progress, so
   * `cycle_index` oscillates (3→1→2) as siblings interleave. `cycleProgress`
   * only ever increases, so this never moves backwards. (For a sequential
   * run — max_parallel 1 — `completed + 1` equals the active `cycle_index`, so
   * nothing is lost there.)
   *
   * Preconditions: none.
   * Postconditions: returns `1` when idle (no run tracked); otherwise
   *   `attemptedCycles + 1` clamped to `total_cycles`, so it is monotonic and
   *   never reports an impossible position (e.g. "Strategy 6 of 5").
   */
  readonly currentStrategyNumber = computed(() => {
    const status = this.runService.runStatus();
    if (!status) return 1;
    return Math.min(this.cycleProgress(status) + 1, status.total_cycles);
  });

  /**
   * The batch position to display — shared by the sighted "Batch N of M"
   * text and `runAnnouncement`'s aria-live equivalent so the two can never
   * disagree for the same `runStatus`.
   *
   * Preconditions: none.
   * Postconditions: returns `1` when idle or the run has no batching;
   *   otherwise `current_batch` (or, between batches, `completed_batches +
   *   1`) clamped to `batch_count` — after the last batch's
   *   `batch_complete`, `current_batch` is null and `completed_batches`
   *   already equals `batch_count`, so without this clamp the naive
   *   `+ 1` would report an impossible "Batch 4 of 3" for the brief window
   *   before the terminal `complete` event arrives.
   */
  readonly currentBatchNumber = computed(() => {
    const status = this.runService.runStatus();
    if (!status || !status.batch_count) return 1;
    return Math.min(status.current_batch ?? (status.completed_batches ?? 0) + 1, status.batch_count);
  });

  /**
   * Concise text for the always-present aria-live status region: batch and
   * strategy position while a run proceeds, the terminal outcome just after a
   * run ends, or '' when idle.
   *
   * Reports only MONOTONIC coarse progress (batch + `currentStrategyNumber`),
   * deliberately NOT the per-cycle phase. The default run executes up to
   * `max_parallel` (3) cycles concurrently, each moving through phases
   * independently, so there is no single "current phase" to speak — announcing
   * one sibling's phase would both churn (a fresh polite announcement on nearly
   * every interleaved progress event) and mislead (it describes one of several
   * active cycles). Per-phase detail remains visible in the on-screen phase
   * stepper; the live region gives screen-reader users a stable progress
   * summary instead of a blow-by-blow feed. `activityLog` likewise stays out of
   * the live region. A `computed()` so it only recomputes when `running`,
   * `runService.runStatus`, or `runOutcomeAnnouncement` change.
   *
   * Preconditions: none.
   * Postconditions: returns a non-empty sentence while `running` is true and
   *   `runStatus` is populated (updating roughly once per completed cycle, never
   *   moving backwards), or immediately after a run ends (until the next run
   *   starts clears it); returns '' otherwise.
   */
  readonly runAnnouncement = computed(() => {
    const status = this.runService.runStatus();
    if (this.running() && status) {
      const segments: string[] = [];
      if (status.batch_count && status.batch_count > 1) {
        segments.push(`Batch ${this.currentBatchNumber()} of ${status.batch_count}`);
      }
      // Once every cycle has been attempted (cycleProgress === total_cycles),
      // there is no "next" strategy to report — say "Finishing up" rather than
      // an impossible position — for the brief window before the terminal
      // `complete` event arrives.
      if (this.cycleProgress(status) >= status.total_cycles) {
        segments.push('Finishing up');
      } else {
        segments.push(`Strategy ${this.currentStrategyNumber()} of ${status.total_cycles}`);
      }
      return segments.join(' — ') + '.';
    }
    return this.runOutcomeAnnouncement() ?? '';
  });

  /** Short multi-line tooltip summarizing recent errored cycles for hover. */
  erroredTooltip(): string {
    const details = this.runService.runStatus()?.errored_details ?? [];
    if (!details.length) return '';
    return details
      .slice(-10)
      .map((d) => `#${d.cycle_index}${d.batch_index ? ` (batch ${d.batch_index})` : ''}: ${d.error}`)
      .join('\n');
  }

  // ---------------------------------------------------------------------------
  // Trade ledger helpers
  // ---------------------------------------------------------------------------

  getPageIndex(id: string): number {
    return this.tradeLedgerPages[id] ?? 0;
  }

  onPageChange(id: string, event: PageEvent): void {
    this.tradeLedgerPages[id] = event.pageIndex;
  }

  // Per-record caches keyed by the record object. A status poll replaces records
  // with new objects, so the WeakMap entries fall away naturally; within a poll
  // cycle these are called per change-detection tick for every visible card.
  private readonly _pagedTradesCache = new WeakMap<StrategyLabRecord, { page: number; trades: TradeRecord[] }>();
  private readonly _winCountCache = new WeakMap<StrategyLabRecord, number>();

  pagedTrades(record: StrategyLabRecord): TradeRecord[] {
    const page = this.getPageIndex(record.lab_record_id);
    const cached = this._pagedTradesCache.get(record);
    // Returning a stable array reference for the same (record, page) lets the
    // mat-table dataSource diff instead of re-rendering every row each CD cycle.
    if (cached && cached.page === page) {
      return cached.trades;
    }
    const start = page * this.PAGE_SIZE;
    const trades = record.backtest.trades.slice(start, start + this.PAGE_SIZE);
    this._pagedTradesCache.set(record, { page, trades });
    return trades;
  }

  tradeCount(record: StrategyLabRecord): number {
    return record.backtest.trades.length;
  }

  winCount(record: StrategyLabRecord): number {
    let count = this._winCountCache.get(record);
    if (count === undefined) {
      count = record.backtest.trades.filter((t) => t.outcome === 'win').length;
      this._winCountCache.set(record, count);
    }
    return count;
  }

  totalNetPnl(record: StrategyLabRecord): number {
    const trades = record.backtest.trades;
    return trades.length ? trades[trades.length - 1].cumulative_pnl : 0;
  }

  tradeReturnColor(t: TradeRecord): string {
    return t.outcome === 'win' ? 'win-cell' : 'loss-cell';
  }

  formatPrice(price: number): string {
    if (price >= 1000) return price.toFixed(0);
    if (price >= 1) return price.toFixed(2);
    return price.toFixed(4);
  }

  hasSignalBrief(record: StrategyLabRecord): boolean {
    return record.signal_intelligence_brief != null && Object.keys(record.signal_intelligence_brief).length > 0;
  }

  /**
   * A failed gate is "remedied" if a later run of the same logical
   * validator produced a passing result. Two channels:
   *
   * - Standard refinement-loop gates (refinement_round >= 0): remedied
   *   when the gate's round is earlier than the cycle's last round
   *   (the existing same-round-as-failure rule).
   * - Pre-synthesis gates (refinement_round = -1): refinement itself is
   *   code-only and cannot fix them, but the zero-trade repair path
   *   re-runs the spec validator after committing whitelisted spec
   *   updates and emits gates with gate_name `zero_trade_repair_<original>`.
   *   If any such later validator pass produced a passing result for the
   *   same logical check, the original pre-synthesis warning is remedied.
   */
  isRemedied(gate: QualityGateResult, record: StrategyLabRecord): boolean {
    if (gate.passed) return false;

    const gateRound = gate.refinement_round ?? 0;
    if (gateRound < 0) {
      // Pre-synthesis gate. Remedied only if a later validator pass for
      // the same logical check returned a passing result.
      const gates = record.quality_gate_results ?? [];
      const baseName = gate.gate_name;
      const repairName = `zero_trade_repair_${baseName}`;
      return gates.some(
        (g) =>
          g.passed &&
          (g.refinement_round ?? -1) >= 0 &&
          (g.gate_name === baseName || g.gate_name === repairName),
      );
    }

    const maxRound = record.refinement_rounds ?? 0;
    if (maxRound === 0) return false;
    // Gate failed in an earlier round — the strategy continued past it
    return gateRound < maxRound;
  }

  gateIcon(gate: QualityGateResult, record: StrategyLabRecord): string {
    return pureGateIcon(gate, this.isRemedied(gate, record));
  }

  gateSeverityClass(gate: QualityGateResult, record: StrategyLabRecord): string {
    return pureGateSeverityClass(gate, this.isRemedied(gate, record));
  }

  // record.quality_gate_results is replaced wholesale (a new array/objects)
  // whenever loadResults() re-polls, so the WeakMap entry falls away
  // naturally — same lifecycle as _pagedTradesCache/_winCountCache above.
  private readonly _gateViewModelsCache = new WeakMap<StrategyLabRecord, GateViewModel[]>();

  /**
   * Per-gate template data (icon, severity class, remedied flag), computed
   * once per record on first access and cached — the template iterates this
   * instead of calling `gateIcon`/`gateSeverityClass`/`isRemedied` directly
   * per gate on every change-detection pass.
   */
  gateViewModels(record: StrategyLabRecord): GateViewModel[] {
    const cached = this._gateViewModelsCache.get(record);
    if (cached) return cached;
    const viewModels = (record.quality_gate_results ?? []).map((gate) => {
      const isRemedied = this.isRemedied(gate, record);
      return {
        gate,
        icon: pureGateIcon(gate, isRemedied),
        severityClass: pureGateSeverityClass(gate, isRemedied),
        isRemedied,
      };
    });
    this._gateViewModelsCache.set(record, viewModels);
    return viewModels;
  }

  /**
   * Open the shared Material confirm dialog for a destructive action.
   *
   * Preconditions: `data.title` and `data.message` are non-empty; the caller
   *   treats a `false` emission as "do not proceed".
   * Postconditions: emits exactly once — `true` only when the user confirms,
   *   `false` on cancel, backdrop/ESC dismissal, or when a confirmation is
   *   already pending. The re-entrancy guard is released when the dialog closes.
   *
   * The native `confirm()` this replaced blocked synchronously; the async
   * dialog does not, so a rapid double-activation (e.g. Enter pressed twice
   * before the dialog traps focus) could otherwise stack dialogs and fire
   * duplicate destructive requests. The guard collapses that window.
   */
  private confirmDestructive(data: ConfirmDialogData) {
    if (this.confirmingDestructive) return of(false);
    this.confirmingDestructive = true;
    return this.dialog
      .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(ConfirmDialogComponent, { data })
      .afterClosed()
      .pipe(
        map((result) => result === true),
        finalize(() => {
          this.confirmingDestructive = false;
        }),
      );
  }

  deleteRecord(record: StrategyLabRecord): void {
    const id = record.lab_record_id;
    const shortHyp = record.strategy.hypothesis.slice(0, 60) + (record.strategy.hypothesis.length > 60 ? '…' : '');
    this.confirmDestructive({
      title: 'Delete strategy lab run',
      message: `Delete this strategy lab run?\n\n${shortHyp}\n\nThis removes the record, its backtest, and any paper-trading sessions for it. This cannot be undone.`,
      confirmLabel: 'Delete',
      variant: 'danger',
    })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) return;
        this.error.set(null);
        this.deletingLabRecordId.set(id);
        this.api
          .deleteStrategyLabRecord(id)
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: () => {
              this.deletingLabRecordId.set(null);
              this.loadResults();
              this.notify.saved('Strategy lab run deleted.');
            },
            error: (err) => {
              this.deletingLabRecordId.set(null);
              this.error.set(extractErrorDetail(err, 'Failed to delete strategy.'));
            },
          });
      });
  }

  /**
   * Prompts the user to confirm before wiping all strategy lab data, then calls the API to
   * delete every lab run, lab strategy/backtest, and paper-trading session and refreshes the view.
   */
  clearAllLabData(): void {
    this.confirmDestructive({
      title: 'Clear all strategy lab data',
      message:
        'Delete ALL strategy lab runs, lab strategies/backtests, and paper-trading sessions?\n\nThis cannot be undone.',
      confirmLabel: 'Delete all',
      variant: 'danger',
    })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((confirmed) => {
        if (!confirmed) return;
        this.error.set(null);
        this.clearingAll.set(true);
        this.api
          .clearStrategyLabStorage()
          .pipe(takeUntilDestroyed(this.destroyRef))
          .subscribe({
            next: () => {
              this.clearingAll.set(false);
              this.runService.clearPaperTradingSessions();
              this.loadResults();
              this.notify.saved('Strategy lab data cleared.');
            },
            error: (err) => {
              this.clearingAll.set(false);
              this.error.set(extractErrorDetail(err, 'Failed to clear strategy lab data.'));
            },
          });
      });
  }

  // ---------------------------------------------------------------------------
  // Paper Trading
  // ---------------------------------------------------------------------------

  /**
   * Fetches paper trading sessions and hydrates run-service state from them.
   *
   * Preconditions: none.
   * Postconditions: for each `lab_record_id`, only the most recent session
   * (by `paperSessionRecencyKey`) is kept and handed to
   * `runService.hydratePaperTradingSessions`, so any still-running sessions
   * resume polling.
   */
  loadPaperTradingResults(): void {
    this.api
      .getPaperTradingResults()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (res) => {
        const sessions: Record<string, PaperTradingSession> = {};
        for (const s of res.items) {
          // Keep the newest session per lab record, using started_at as the
          // recency key (completed_at is empty for still-running sessions, so
          // relying on it would systematically lose to older completed ones).
          const existing = sessions[s.lab_record_id];
          if (!existing || this.paperSessionRecencyKey(s) > this.paperSessionRecencyKey(existing)) {
            sessions[s.lab_record_id] = s;
          }
        }
        // Resumes polling for any sessions still running (e.g. after a page reload).
        this.runService.hydratePaperTradingSessions(sessions);
      },
    });
  }

  /** Sortable recency key for a paper-trading session. */
  private paperSessionRecencyKey(s: PaperTradingSession): string {
    return s.started_at || s.completed_at || '';
  }

  runPaperTrading(record: StrategyLabRecord): void {
    if (!record.is_publishable) {
      const reason = this.publishabilitySkipLabel(record);
      this.error.set(
        'This strategy is not publishable and cannot be paper traded' +
        (reason ? ` (${reason})` : '.'),
      );
      return;
    }
    this.error.set(null);
    this.startingPaperTrade.set(record.lab_record_id);
    this.api
      .runPaperTrading({ lab_record_id: record.lab_record_id })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
      next: (res) => {
        // Backend returns a "running" session immediately; runService stores it
        // so the UI shows in-progress state, then polls until the worker finishes.
        this.runService.trackPaperTradingSession(record.lab_record_id, res.session);
        this.startingPaperTrade.set(null);
      },
      error: (err) => {
        this.startingPaperTrade.set(null);
        this.error.set(extractErrorDetail(err, 'Paper trading failed.'));
      },
    });
  }

  /**
   * Human-readable publishability skip reason for a winning-but-blocked record.
   *
   * Preconditions: ``record`` is a loaded lab row.
   * Postconditions: returns the persisted skip reason when present, else null.
   */
  publishabilitySkipLabel(record: StrategyLabRecord): string | null {
    const reason =
      record.publishability_skip_reason ||
      (record.paper_trading_skipped_reason &&
      record.paper_trading_skipped_reason !== 'not_winning' &&
      record.paper_trading_skipped_reason !== 'disabled'
        ? record.paper_trading_skipped_reason
        : null);
    return reason || null;
  }

  getPaperSession(record: StrategyLabRecord): PaperTradingSession | null {
    return this.runService.paperTradingSessions()[record.lab_record_id] ?? null;
  }

  readonly verdictLabel = verdictLabel;

  readonly verdictColor = verdictColor;

  // Keyed by the PaperTradingComparison object itself: a poll tick replaces
  // the whole PaperTradingSession (and its nested `comparison`) with a fresh
  // object, so this cache naturally invalidates on real data changes while
  // giving repeat calls for the same object a stable array — same lifecycle
  // as _pagedTradesCache/_winCountCache/_gateViewModelsCache above.
  private readonly _comparisonMetricsCache = new WeakMap<PaperTradingComparison, ComparisonRow[]>();

  comparisonMetrics(c: PaperTradingComparison): ComparisonRow[] {
    const cached = this._comparisonMetricsCache.get(c);
    if (cached) return cached;
    const rows: ComparisonRow[] = [
      { label: 'Win Rate', backtest: formatPct(c.backtest_win_rate_pct), paper: formatPct(c.paper_win_rate_pct), aligned: c.win_rate_aligned },
      { label: 'Annual Return', backtest: formatPct(c.backtest_annualized_return_pct), paper: formatPct(c.paper_annualized_return_pct), aligned: c.return_aligned },
      { label: 'Sharpe', backtest: formatRatio(c.backtest_sharpe_ratio), paper: formatRatio(c.paper_sharpe_ratio), aligned: c.sharpe_aligned },
      { label: 'Max Drawdown', backtest: formatPct(c.backtest_max_drawdown_pct), paper: formatPct(c.paper_max_drawdown_pct), aligned: c.drawdown_aligned },
      { label: 'Profit Factor', backtest: formatRatio(c.backtest_profit_factor), paper: formatRatio(c.paper_profit_factor), aligned: c.profit_factor_aligned },
    ];
    this._comparisonMetricsCache.set(c, rows);
    return rows;
  }
}
