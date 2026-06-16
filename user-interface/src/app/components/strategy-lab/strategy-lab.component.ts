import { Component, ElementRef, OnDestroy, OnInit, ViewChild, inject } from '@angular/core';
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
import { Subscription, timer, switchMap, takeWhile } from 'rxjs';

import { InvestmentApiService } from '../../services/investment-api.service';
import type {
  PaperTradingSession,
  PaperTradingComparison,
  QualityGateResult,
  StrategyLabRecord,
  StrategyLabResultsResponse,
  StrategyLabRunStatus,
  StrategyLabStreamEvent,
  StrategyLabPhase,
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

const STRATEGY_LAB_PHASES: PhaseDefinition[] = [
  { id: 'ideating',     label: 'Ideate',    icon: 'psychology' },
  { id: 'coding',       label: 'Code',      icon: 'code' },
  { id: 'backtesting',  label: 'Backtest',  icon: 'play_circle' },
  { id: 'analyzing',    label: 'Analyze',   icon: 'summarize' },
];

/** Ordered phase IDs for determining completed/pending state. */
const PHASE_ORDER = STRATEGY_LAB_PHASES.map(p => p.id);

const ASSET_CLASS_ICONS: Record<string, string> = {
  stocks: 'show_chart',
  crypto: 'currency_bitcoin',
  forex: 'currency_exchange',
  commodities: 'oil_barrel',
  futures: 'schedule',
  options: 'tune',
};

interface AssetCategoryOption {
  value: string;
  label: string;
  icon: string;
}

/** Title-case an asset-category value for display (e.g. 'stocks' → 'Stocks'). */
function categoryLabel(value: string): string {
  return value.length ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

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
 * (see STRATEGY_LAB_PHASES, ASSET_CLASS_ICONS).
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
  imports: [
    CommonModule,
    DecimalPipe,
    DatePipe,
    CurrencyPipe,
    JsonPipe,
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
  ],
  templateUrl: './strategy-lab.component.html',
  styleUrl: './strategy-lab.component.scss',
})
export class StrategyLabComponent implements OnInit, OnDestroy {
  private readonly api = inject(InvestmentApiService);

  running = false;
  loading = false;
  clearingAll = false;
  error: string | null = null;
  /** Non-fatal warning banner shown when a run finishes with errored/skipped cycles. */
  completionWarning: string | null = null;
  /** Lab record id currently being deleted (disables actions on that card). */
  deletingLabRecordId: string | null = null;

  // User-configurable batch settings (mirror backend Field bounds).
  // BATCH_COUNT_MAX is hydrated from GET /strategy-lab/config on init so the
  // operator-tunable STRATEGY_LAB_MAX_BATCH_COUNT (default 100) propagates to
  // the UI; 100 here is only the fallback if the config fetch fails.
  readonly BATCH_SIZE_MIN = 1;
  readonly BATCH_SIZE_MAX = 25;
  readonly BATCH_COUNT_MIN = 1;
  BATCH_COUNT_MAX = 100;
  batchSize = 10;
  batchCount = 1;

  // Asset-category selection. `categoryOptions` is seeded from the fallback list
  // and replaced by the backend's authoritative set once `loadConfig` resolves
  // (single source of truth). Defaults to every category selected (equivalent to
  // no constraint); the user narrows it to steer the design agent. At least one
  // category must stay selected — a run with zero categories is invalid.
  // `selectedCategories` is a plain array so it binds directly to the multi-
  // select toggle group's ngModel; canonical order is reasserted at payload time.
  categoryOptions: AssetCategoryOption[] = DEFAULT_STRATEGY_LAB_CATEGORIES;
  selectedCategories: string[] = DEFAULT_STRATEGY_LAB_CATEGORIES.map((c) => c.value);
  // Set once the user touches the category toggles. Distinguishes an explicit
  // "I want exactly these" selection (even when that happens to be all of them)
  // from the untouched default, so a late backend category list reconciles
  // against the user's intent rather than inferring it from selection state.
  private userAdjustedCategories = false;

  /** A run requires at least one selected category. */
  get categoriesValid(): boolean {
    return this.selectedCategories.length > 0;
  }

  /** Toggle-group change handler: record the user's selection and mark it explicit. */
  onCategoriesChanged(values: string[]): void {
    this.selectedCategories = values;
    this.userAdjustedCategories = true;
  }

  filter: FilterMode = 'all';
  results: StrategyLabResultsResponse | null = null;
  displayedItems: StrategyLabRecord[] = [];

  totalCount = 0;
  winningCount = 0;
  losingCount = 0;

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

  // Per-card trade ledger state
  tradeLedgerPages: Record<string, number> = {};       // lab_record_id → current page index
  readonly PAGE_SIZE = 20;
  readonly TRADE_COLUMNS = [
    'trade_num', 'entry_date', 'exit_date', 'symbol',
    'entry_price', 'exit_price', 'shares', 'return_pct',
    'net_pnl', 'cumulative_pnl', 'outcome',
  ];

  // Paper trading state
  /** Lab record id currently being paper traded. */
  paperTradingLabRecordId: string | null = null;
  /** Paper trading sessions keyed by lab_record_id for quick lookup. */
  paperTradingSessions: Record<string, PaperTradingSession> = {};
  /** Active polling subscriptions per lab_record_id (so we can cancel on destroy). */
  private paperTradingPollSubs: Record<string, Subscription> = {};

  // Run progress tracking
  activeRunId: string | null = null;
  runStatus: StrategyLabRunStatus | null = null;
  private sseSub: Subscription | null = null;
  private pollSub: Subscription | null = null;

  // Phase stepper + activity log
  readonly STRATEGY_LAB_PHASES = STRATEGY_LAB_PHASES;
  activityLog: ActivityLogEntry[] = [];
  private lastCycleIndex = -1;

  @ViewChild('logContainer') logContainer?: ElementRef<HTMLElement>;

  ngOnInit(): void {
    this.loadConfig();
    this.loadResults();
    this.loadPaperTradingResults();
    this.checkForActiveRun();
  }

  private loadConfig(): void {
    this.api.getStrategyLabConfig().subscribe({
      next: (cfg) => {
        if (cfg.batch_count_max >= this.BATCH_COUNT_MIN) {
          this.BATCH_COUNT_MAX = cfg.batch_count_max;
        }
        this.applyCategoryConfig(cfg.asset_categories);
      },
      // Keep the fallback list silently; batch controls + categories still work.
      error: () => undefined,
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
    const selected = new Set(this.selectedCategories);
    this.categoryOptions = buildCategoryOptions(categories);
    const available = this.categoryOptions.map((c) => c.value);

    if (!this.userAdjustedCategories) {
      this.selectedCategories = available;
      return;
    }
    const preserved = available.filter((v) => selected.has(v));
    this.selectedCategories = preserved.length ? preserved : available;
  }

  ngOnDestroy(): void {
    this.sseSub?.unsubscribe();
    this.pollSub?.unsubscribe();
    this.activeRunCheckSub?.unsubscribe();
    for (const sub of Object.values(this.paperTradingPollSubs)) {
      sub.unsubscribe();
    }
    this.paperTradingPollSubs = {};
  }

  // ---------------------------------------------------------------------------
  // Active run detection (for navigate-away-and-back)
  // ---------------------------------------------------------------------------

  private activeRunCheckSub: Subscription | null = null;

  /**
   * Poll for active runs a few times on page load so that a running job
   * is always picked up — even if the first request races with the
   * backend becoming ready or the in-memory cache being repopulated.
   */
  private checkForActiveRun(): void {
    // Poll up to 4 times (0s, 3s, 6s, 9s), stop as soon as we find one
    // or if a run was started locally via runNewStrategy().
    this.activeRunCheckSub?.unsubscribe();
    let attempts = 0;
    this.activeRunCheckSub = timer(0, 3000).pipe(
      takeWhile(() => attempts < 4 && !this.running),
      switchMap(() => {
        attempts++;
        return this.api.getActiveRuns();
      }),
    ).subscribe({
      next: (res) => {
        const active = res.runs.find((r) => r.status === 'running');
        if (active) {
          this.activeRunId = active.run_id;
          this.runStatus = active;
          this.running = true;
          this.connectToStream(active.run_id);
          this.activeRunCheckSub?.unsubscribe();
        }
      },
    });
  }

  // ---------------------------------------------------------------------------
  // SSE streaming + polling fallback
  // ---------------------------------------------------------------------------

  private connectToStream(runId: string): void {
    this.sseSub?.unsubscribe();
    this.sseSub = this.api.streamRunStatus(runId).subscribe({
      next: (event) => this.handleStreamEvent(event),
      error: () => this.fallbackToPolling(runId),
      complete: () => this.onRunComplete(),
    });
  }

  private handleStreamEvent(event: StrategyLabStreamEvent): void {
    if (event.type === 'snapshot' && this.runStatus) {
      // Merge snapshot fields into runStatus
      Object.assign(this.runStatus, {
        status: event['status'] ?? this.runStatus.status,
        completed_cycles: event['completed_cycles'] ?? this.runStatus.completed_cycles,
        skipped_cycles: event['skipped_cycles'] ?? this.runStatus.skipped_cycles,
        errored_cycles: event['errored_cycles'] ?? this.runStatus.errored_cycles,
        errored_details: event['errored_details'] ?? this.runStatus.errored_details,
        current_cycle: event['current_cycle'] ?? this.runStatus.current_cycle,
        completed_record_ids: event['completed_record_ids'] ?? this.runStatus.completed_record_ids,
        error: event['error'] ?? this.runStatus.error,
        batch_size: event['batch_size'] ?? this.runStatus.batch_size,
        batch_count: event['batch_count'] ?? this.runStatus.batch_count,
        completed_batches: event['completed_batches'] ?? this.runStatus.completed_batches,
        current_batch: event['current_batch'] ?? this.runStatus.current_batch,
      });
    }

    if (event.type === 'batch_start' && this.runStatus) {
      this.runStatus.current_batch = (event['batch_index'] as number) ?? this.runStatus.current_batch;
      this.runStatus.batch_count = (event['total_batches'] as number) ?? this.runStatus.batch_count;
      this.runStatus.completed_batches = (event['completed_batches'] as number) ?? this.runStatus.completed_batches;
    }

    if (event.type === 'batch_complete' && this.runStatus) {
      this.runStatus.completed_batches = (event['completed_batches'] as number) ?? this.runStatus.completed_batches;
      this.runStatus.current_batch = null;
    }

    if (event.type === 'progress' && this.runStatus) {
      const cycleIndex = (event['cycle_index'] as number) ?? 0;
      const phase = (event['phase'] as StrategyLabPhase) ?? 'ideating';
      const subPhase = event['sub_phase'] as string | undefined;

      // Reset activity log when a new cycle starts
      if (cycleIndex !== this.lastCycleIndex) {
        this.activityLog = [];
        this.lastCycleIndex = cycleIndex;
      }

      // Merge strategy from completed ideation into current_cycle
      const prevStrategy = this.runStatus.current_cycle?.strategy;
      const newStrategy = event['strategy'] as { asset_class: string; hypothesis: string } | undefined;

      this.runStatus.current_cycle = {
        cycle_index: cycleIndex,
        phase,
        sub_phase: subPhase,
        refinement_round: event['refinement_round'] as number | undefined,
        strategy: newStrategy ?? prevStrategy,
        metrics: (event['metrics'] as Record<string, number> | undefined) ?? this.runStatus.current_cycle?.metrics,
        checks_passed: event['checks_passed'] as number | undefined,
        checks_total: event['checks_total'] as number | undefined,
        symbols_count: event['symbols_count'] as number | undefined,
        bars_count: event['bars_count'] as number | undefined,
        trades_count: event['trades_count'] as number | undefined,
        execution_time: event['execution_time'] as number | undefined,
        failure_phase: event['failure_phase'] as string | undefined,
        changes_made: event['changes_made'] as string | undefined,
        is_winning: event['is_winning'] as boolean | undefined,
      };

      this.addLogEntry(phase, subPhase, event);
    }

    if (event.type === 'cycle_complete' && this.runStatus) {
      this.runStatus.completed_cycles = (event['completed_cycles'] as number) ?? this.runStatus.completed_cycles + 1;
      this.runStatus.current_cycle = undefined;
      this.activityLog = [];
      this.lastCycleIndex = -1;
      this.loadResults();
    }

    if (event.type === 'cycle_skipped' && this.runStatus) {
      this.runStatus.skipped_cycles = (this.runStatus.skipped_cycles ?? 0) + 1;
      this.runStatus.current_cycle = undefined;
    }

    if (event.type === 'cycle_errored' && this.runStatus) {
      this.runStatus.errored_cycles = (this.runStatus.errored_cycles ?? 0) + 1;
      const detail = {
        cycle_index: (event['cycle_index'] as number) ?? 0,
        batch_index: event['batch_index'] as number | undefined,
        error: (event['error'] as string) || '',
        exception_type: event['reason'] as string | undefined,
      };
      const existing = this.runStatus.errored_details ?? [];
      this.runStatus.errored_details = [...existing, detail].slice(-50);
      this.runStatus.current_cycle = undefined;
    }

    if (event.type === 'batch_warning') {
      // Non-fatal pre-batch issue (e.g. signal-brief failure). Surface as a
      // gentle warning; the run is still progressing.
      this.completionWarning =
        (event['reason'] as string) === 'signal_brief_failed'
          ? 'Signal brief unavailable for a batch; strategies continued without it.'
          : (event['reason'] as string) || 'A non-fatal warning occurred during a batch.';
    }

    if (event.type === 'complete') {
      const erroredCount = (event['errored_count'] as number) ?? this.runStatus?.errored_cycles ?? 0;
      const skippedCount = (event['skipped_count'] as number) ?? this.runStatus?.skipped_cycles ?? 0;
      if (erroredCount > 0 || (event['status'] as string) === 'completed_with_errors') {
        const parts: string[] = [];
        if (erroredCount > 0) parts.push(`${erroredCount} cycle(s) errored`);
        if (skippedCount > 0) parts.push(`${skippedCount} cycle(s) skipped`);
        this.completionWarning = `Run finished with ${parts.join(' and ')}. See details below.`;
      }
      this.onRunComplete();
    }

    if (event.type === 'error') {
      this.error = (event['detail'] as string) || 'Run failed';
      this.onRunComplete();
    }
  }

  private onRunComplete(): void {
    this.running = false;
    this.activeRunId = null;
    this.runStatus = null;
    this.sseSub?.unsubscribe();
    this.sseSub = null;
    this.pollSub?.unsubscribe();
    this.pollSub = null;
    this.loadResults();
  }

  private fallbackToPolling(runId: string): void {
    this.pollSub?.unsubscribe();
    this.pollSub = timer(0, 5000).pipe(
      switchMap(() => this.api.getRunStatus(runId)),
      takeWhile((status) => status.status === 'running', true),
    ).subscribe({
      next: (status) => {
        this.runStatus = status;
        if (status.status !== 'running') {
          this.onRunComplete();
        }
      },
      error: () => {
        // Polling also failed — stop tracking
        this.onRunComplete();
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Results
  // ---------------------------------------------------------------------------

  loadResults(): void {
    this.loading = true;
    this.error = null;
    this.api.getStrategyLabResults().subscribe({
      next: (res) => {
        this.results = res;
        this.totalCount = res.count;
        this.winningCount = res.winning_count;
        this.losingCount = res.losing_count;
        this.applyFilter();
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Failed to load results.';
        this.loading = false;
      },
    });
  }

  /**
   * Start a new Strategy Lab run with the current form configuration.
   *
   * Preconditions: no run is already in progress (`running` is false — a
   *   re-entrant call is ignored) and at least one asset category is selected
   *   (`categoriesValid` is true; when violated this sets `error` and returns
   *   without calling the API).
   * Postconditions: clamps batch size/count into range and reflects them back to
   *   the form; sets `running = true` and clears `error`/`completionWarning`;
   *   POSTs a `RunStrategyLabRequest`. `allowed_asset_classes` is sent in
   *   canonical (`categoryOptions`) order only when the selection is a strict
   *   subset — when every category is selected the field is omitted, matching the
   *   backend's "no constraint" semantics and trimming the payload. On success it
   *   subscribes to the run's status stream; on error it resets `running` and
   *   surfaces the message.
   */
  runNewStrategy(): void {
    // Re-entrancy guard: the run button is disabled while a run is active, but a
    // programmatic call or double-click must not start a second run — that would
    // orphan the first run and open a duplicate status stream.
    if (this.running) {
      return;
    }
    // Guard the invalid-zero-categories case (the button is also disabled, but
    // a programmatic call must not start a run constrained to nothing).
    if (!this.categoriesValid) {
      this.error = 'Select at least one asset category to generate strategies for.';
      return;
    }

    const batchSize = this.clamp(this.batchSize, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
    const batchCount = this.clamp(this.batchCount, this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX);
    // Reflect any clamping back into the form so the user sees what was sent.
    this.batchSize = batchSize;
    this.batchCount = batchCount;

    // Preserve canonical order so the payload is stable regardless of click order.
    const allowedAssetClasses = this.categoryOptions
      .map((c) => c.value)
      .filter((v) => this.selectedCategories.includes(v));
    // Omit the field when every category is selected — equivalent to "no
    // constraint" server-side, and a smaller payload.
    const allConstraintsOff = allowedAssetClasses.length === this.categoryOptions.length;

    this.running = true;
    this.error = null;
    this.completionWarning = null;
    this.api
      .runStrategyLab({
        batch_size: batchSize,
        batch_count: batchCount,
        allowed_asset_classes: allConstraintsOff ? undefined : allowedAssetClasses,
      })
      .subscribe({
      next: (res) => {
        this.activeRunId = res.run_id;
        this.runStatus = {
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
        this.connectToStream(res.run_id);
      },
      error: (err) => {
        this.error = err?.error?.detail || err?.message || 'Strategy run failed.';
        this.running = false;
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
      return `Run ${this.batchSize} \u00d7 ${this.batchCount} = ${total} strategies`;
    }
    return `Run ${this.batchSize} strateg${this.batchSize === 1 ? 'y' : 'ies'}`;
  }

  onFilterChange(mode: FilterMode): void {
    this.filter = mode;
    this.applyFilter();
  }

  private applyFilter(): void {
    const all = this.results?.items ?? [];
    if (this.filter === 'winning') {
      this.displayedItems = all.filter((r) => r.is_winning);
    } else if (this.filter === 'losing') {
      this.displayedItems = all.filter((r) => !r.is_winning);
    } else {
      this.displayedItems = all;
    }
  }

  returnColor(annualized: number): string {
    if (annualized > 8) return 'winning';
    if (annualized >= 0) return 'neutral';
    return 'losing';
  }

  // ---------------------------------------------------------------------------
  // Phase stepper state
  // ---------------------------------------------------------------------------

  isPhaseCompleted(phaseId: string): boolean {
    const current = this.runStatus?.current_cycle?.phase;
    if (!current) return false;
    const currentIdx = PHASE_ORDER.indexOf(current);
    const phaseIdx = PHASE_ORDER.indexOf(phaseId);
    if (currentIdx < 0 || phaseIdx < 0) return false;
    return phaseIdx < currentIdx;
  }

  isCurrentPhase(phaseId: string): boolean {
    return this.runStatus?.current_cycle?.phase === phaseId;
  }

  isPhasePending(phaseId: string): boolean {
    return !this.isPhaseCompleted(phaseId) && !this.isCurrentPhase(phaseId);
  }

  getAssetClassIcon(assetClass: string): string {
    return ASSET_CLASS_ICONS[assetClass?.toLowerCase()] ?? 'trending_up';
  }

  // ---------------------------------------------------------------------------
  // Activity log
  // ---------------------------------------------------------------------------

  private addLogEntry(phase: string, subPhase: string | undefined, data: Record<string, unknown>): void {
    const now = new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

    // Mark previous active entry as done (if it's still active when a new entry arrives)
    for (let i = this.activityLog.length - 1; i >= 0; i--) {
      if (this.activityLog[i].status === 'active') {
        this.activityLog[i].status = 'done';
        break;
      }
    }

    const msg = this.buildLogMessage(phase, subPhase, data);
    if (!msg) return;

    const isTerminal = subPhase === 'completed' || subPhase === 'data_loaded';

    this.activityLog.push({
      time: now,
      status: isTerminal ? 'done' : 'active',
      message: msg,
    });

    // Auto-scroll the log container
    setTimeout(() => {
      this.logContainer?.nativeElement?.scrollTo({ top: 999999, behavior: 'smooth' });
    }, 50);
  }

  private buildLogMessage(phase: string, subPhase: string | undefined, data: Record<string, unknown>): string {
    const strategy = data['strategy'] as { asset_class?: string; hypothesis?: string } | undefined;
    const round = data['refinement_round'] as number | undefined;

    switch (phase) {
      case 'ideating':
        if (subPhase === 'started') return 'Ideating new trading strategy & generating code...';
        if (subPhase === 'completed') return `Strategy ideated \u2014 ${strategy?.asset_class ?? 'unknown'} asset class`;
        return 'Ideating...';
      case 'coding':
        if (subPhase === 'started') return 'Validating strategy spec and code safety...';
        if (subPhase === 'completed') return `Code validated (${data['checks_total'] ?? '?'} checks, ${data['checks_passed'] ?? '?'} passed)`;
        if (subPhase === 'failed') return `Validation failed (${(data['checks_total'] as number ?? 0) - (data['checks_passed'] as number ?? 0)} critical issue(s))`;
        if (subPhase === 'refining') return `Refining code (round ${(round ?? 0) + 1}/10) \u2014 fixing ${data['failure_phase'] ?? 'issues'}...`;
        if (subPhase === 'refined') return `Code refined \u2014 ${data['changes_made'] ?? 'code updated'}`;
        return 'Coding...';
      case 'backtesting':
        if (subPhase === 'fetching_data') return 'Fetching historical market data...';
        if (subPhase === 'data_loaded') return `Market data loaded (${data['symbols_count'] ?? '?'} symbols, ${(data['bars_count'] as number ?? 0).toLocaleString()} bars)`;
        if (subPhase === 'running_code') return 'Executing strategy backtest in sandbox...';
        if (subPhase === 'completed') return `Backtest complete \u2014 ${data['trades_count'] ?? '?'} trades in ${((data['execution_time'] as number) ?? 0).toFixed(1)}s`;
        return 'Backtesting...';
      case 'analyzing':
        if (subPhase === 'draft') return 'Generating analysis narrative...';
        if (subPhase === 'review') return 'Self-reviewing analysis against metrics...';
        if (subPhase === 'completed') return `Analysis complete \u2014 ${data['is_winning'] ? 'WINNING' : 'LOSING'}`;
        return 'Analyzing...';
      default:
        return `${phase} \u2014 ${subPhase ?? 'processing'}`;
    }
  }

  progressPercent(): number {
    if (!this.runStatus || this.runStatus.total_cycles === 0) return 0;
    return Math.round((this.runStatus.completed_cycles / this.runStatus.total_cycles) * 100);
  }

  /** Short multi-line tooltip summarizing recent errored cycles for hover. */
  erroredTooltip(): string {
    const details = this.runStatus?.errored_details ?? [];
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

  pagedTrades(record: StrategyLabRecord): TradeRecord[] {
    const page = this.getPageIndex(record.lab_record_id);
    const start = page * this.PAGE_SIZE;
    return record.backtest.trades.slice(start, start + this.PAGE_SIZE);
  }

  tradeCount(record: StrategyLabRecord): number {
    return record.backtest.trades.length;
  }

  winCount(record: StrategyLabRecord): number {
    return record.backtest.trades.filter((t) => t.outcome === 'win').length;
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
   * - Pre-synthesis gates (refinement_round = -1, #547 item 1):
   *   refinement itself is code-only and cannot fix them, but the
   *   zero-trade repair path (#547 review) re-runs the spec validator
   *   after committing whitelisted spec updates and emits gates with
   *   gate_name `zero_trade_repair_<original>`. If any such later
   *   validator pass produced a passing result for the same logical
   *   check, the original pre-synthesis warning is remedied.
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
    if (gate.passed) return 'check_circle';
    if (this.isRemedied(gate, record)) return 'build_circle';
    return gate.severity === 'critical' ? 'cancel' : 'warning';
  }

  gateSeverityClass(gate: QualityGateResult, record: StrategyLabRecord): string {
    if (this.isRemedied(gate, record)) return 'gate-remedied';
    return 'gate-' + gate.severity;
  }

  deleteRecord(record: StrategyLabRecord): void {
    const id = record.lab_record_id;
    const shortHyp = record.strategy.hypothesis.slice(0, 60) + (record.strategy.hypothesis.length > 60 ? '…' : '');
    if (
      !confirm(
        `Delete this strategy lab run?\n\n${shortHyp}\n\nThis removes the record, its backtest, and any paper-trading sessions for it. This cannot be undone.`
      )
    ) {
      return;
    }
    this.error = null;
    this.deletingLabRecordId = id;
    this.api.deleteStrategyLabRecord(id).subscribe({
      next: () => {
        this.deletingLabRecordId = null;
        this.loadResults();
      },
      error: (err) => {
        this.deletingLabRecordId = null;
        this.error = err?.error?.detail || err?.message || 'Failed to delete strategy.';
      },
    });
  }

  clearAllLabData(): void {
    if (
      !confirm(
        'Delete ALL strategy lab runs, lab strategies/backtests, and paper-trading sessions?\n\nThis cannot be undone.'
      )
    ) {
      return;
    }
    this.error = null;
    this.clearingAll = true;
    this.api.clearStrategyLabStorage().subscribe({
      next: () => {
        this.clearingAll = false;
        this.paperTradingSessions = {};
        this.loadResults();
      },
      error: (err) => {
        this.clearingAll = false;
        this.error = err?.error?.detail || err?.message || 'Failed to clear strategy lab data.';
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Paper Trading
  // ---------------------------------------------------------------------------

  loadPaperTradingResults(): void {
    this.api.getPaperTradingResults().subscribe({
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
        this.paperTradingSessions = sessions;
        // Resume polling for any sessions still running (e.g. after a page reload).
        for (const [labRecordId, s] of Object.entries(sessions)) {
          if (s.status === 'running') {
            this.pollPaperTradingSession(labRecordId, s.session_id);
          }
        }
      },
    });
  }

  /** Sortable recency key for a paper-trading session. */
  private paperSessionRecencyKey(s: PaperTradingSession): string {
    return s.started_at || s.completed_at || '';
  }

  runPaperTrading(record: StrategyLabRecord): void {
    this.error = null;
    this.paperTradingLabRecordId = record.lab_record_id;
    this.api.runPaperTrading({ lab_record_id: record.lab_record_id }).subscribe({
      next: (res) => {
        // Backend returns a "running" session immediately; store it so the UI
        // shows in-progress state, then poll until the worker finishes.
        this.paperTradingSessions[record.lab_record_id] = res.session;
        this.pollPaperTradingSession(record.lab_record_id, res.session.session_id);
      },
      error: (err) => {
        this.paperTradingLabRecordId = null;
        this.error = err?.error?.detail || err?.message || 'Paper trading failed.';
      },
    });
  }

  /** Poll GET /strategy-lab/paper-trade/{session_id} until status is terminal. */
  private pollPaperTradingSession(labRecordId: string, sessionId: string): void {
    this.paperTradingPollSubs[labRecordId]?.unsubscribe();
    this.paperTradingPollSubs[labRecordId] = timer(3000, 3000)
      .pipe(
        switchMap(() => this.api.getPaperTradingSession(sessionId)),
        takeWhile((res) => res.session.status === 'running', true),
      )
      .subscribe({
        next: (res) => {
          this.paperTradingSessions[labRecordId] = res.session;
          if (res.session.status !== 'running') {
            this.paperTradingLabRecordId = null;
            delete this.paperTradingPollSubs[labRecordId];
          }
        },
        error: (err) => {
          this.paperTradingLabRecordId = null;
          delete this.paperTradingPollSubs[labRecordId];
          this.error = err?.error?.detail || err?.message || 'Paper trading polling failed.';
        },
      });
  }

  getPaperSession(record: StrategyLabRecord): PaperTradingSession | null {
    return this.paperTradingSessions[record.lab_record_id] ?? null;
  }

  verdictLabel(verdict: string | undefined | null): string {
    if (verdict === 'ready_for_live') return 'READY FOR LIVE';
    if (verdict === 'not_performant') return 'NOT PERFORMANT';
    return 'INCONCLUSIVE';
  }

  verdictColor(verdict: string | undefined | null): string {
    if (verdict === 'ready_for_live') return 'winning';
    if (verdict === 'not_performant') return 'losing';
    return 'neutral';
  }

  comparisonMetrics(c: PaperTradingComparison): { label: string; backtest: string; paper: string; aligned: boolean }[] {
    return [
      { label: 'Win Rate', backtest: c.backtest_win_rate_pct.toFixed(1) + '%', paper: c.paper_win_rate_pct.toFixed(1) + '%', aligned: c.win_rate_aligned },
      { label: 'Annual Return', backtest: c.backtest_annualized_return_pct.toFixed(1) + '%', paper: c.paper_annualized_return_pct.toFixed(1) + '%', aligned: c.return_aligned },
      { label: 'Sharpe', backtest: c.backtest_sharpe_ratio.toFixed(2), paper: c.paper_sharpe_ratio.toFixed(2), aligned: c.sharpe_aligned },
      { label: 'Max Drawdown', backtest: c.backtest_max_drawdown_pct.toFixed(1) + '%', paper: c.paper_max_drawdown_pct.toFixed(1) + '%', aligned: c.drawdown_aligned },
      { label: 'Profit Factor', backtest: c.backtest_profit_factor.toFixed(2), paper: c.paper_profit_factor.toFixed(2), aligned: c.profit_factor_aligned },
    ];
  }
}
