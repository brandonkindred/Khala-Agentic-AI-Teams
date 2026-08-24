import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Input,
  OnInit,
  ViewChild,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CommonModule, DecimalPipe } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatButtonToggleModule } from '@angular/material/button-toggle';
import type { PageEvent } from '@angular/material/paginator';
import { RouterLink } from '@angular/router';
import { Observable } from 'rxjs';

import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { StrategyLabRunService } from '../../services/strategy-lab-run.service';
import { StrategyLabActivityLogService } from '../../services/strategy-lab-activity-log.service';
import { StrategyLabPaperTradingService } from '../../services/strategy-lab-paper-trading.service';
import { StrategyLabDestructiveActionsService } from '../../services/strategy-lab-destructive-actions.service';
import { ConfirmDestructiveService } from '../../shared/confirm-destructive.service';
import { describeRunStatus } from '../../services/strategy-lab-log-message';
import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { extractErrorDetail } from '../../shared/extract-error-detail';
import {
  ASSET_CLASS_ICONS,
  returnColor,
  returnColorLabel,
  getAssetClassIcon,
} from './strategy-lab.formatters';
import { PhaseStepperComponent, phaseLabel } from './phase-stepper/phase-stepper.component';
import { StrategyCardComponent } from './strategy-card/strategy-card.component';
import {
  GenerateStrategiesDialogComponent,
  type GenerateStrategiesDialogData,
  type GenerateStrategiesDialogResult,
} from './generate-strategies-dialog/generate-strategies-dialog.component';
import { clamp } from '../../shared/clamp.util';
import type {
  PaperTradingSession,
  StrategyLabRecord,
  StrategyLabResultsResponse,
  StrategyLabRunStatus,
} from '../../models';

type FilterMode = 'all' | 'winning' | 'losing';

export interface AssetCategoryOption {
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
 * (ASSET_CLASS_ICONS lives in `./strategy-lab.formatters`; STRATEGY_LAB_PHASES
 * lives in `./phase-stepper/phase-stepper.component`).
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
  providers: [
    StrategyLabRunService,
    StrategyLabActivityLogService,
    StrategyLabPaperTradingService,
    StrategyLabDestructiveActionsService,
    ConfirmDestructiveService,
  ],
  imports: [
    CommonModule,
    DecimalPipe,
    MatButtonModule,
    MatIconModule,
    MatDialogModule,
    MatProgressSpinnerModule,
    MatProgressBarModule,
    MatChipsModule,
    MatTooltipModule,
    MatButtonToggleModule,
    RouterLink,
    InlineBannerComponent,
    PhaseStepperComponent,
    StrategyCardComponent,
  ],
  templateUrl: './strategy-lab.component.html',
  styleUrl: './strategy-lab.component.scss',
})
export class StrategyLabComponent implements OnInit {
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
  /** Owns SSE/polling/active-run tracking and per-record paper-trading polling. */
  readonly runService = inject(StrategyLabRunService);
  /** Owns activity-log bookkeeping and the completion/error/warning banners driven by runService.events$. */
  private readonly activityLogService = inject(StrategyLabActivityLogService);
  /** Owns paper-trading initiation: the publishability guard, the start POST, and the initial results fetch. */
  private readonly paperTradingService = inject(StrategyLabPaperTradingService);
  /** Owns per-record deletion and "clear all" strategy lab data, including the destructive-action confirm dialog. */
  private readonly destructiveActionsService = inject(StrategyLabDestructiveActionsService);

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
  /** Owned by destructiveActionsService. */
  readonly clearingAll = this.destructiveActionsService.clearingAll;
  readonly error = signal<string | null>(null);
  /**
   * Non-fatal notice banner (dismissible, non-error styling): shown when a run
   * finishes with errored/skipped cycles, when a non-fatal `batch_warning`
   * arrives mid-run, or when a run is cancelled by the user (a deliberate stop
   * is a notice, not a red-banner error). Owned by `activityLogService`.
   */
  readonly completionWarning = this.activityLogService.completionWarning;
  /** Lab record id currently being deleted (disables actions on that card). Owned by destructiveActionsService. */
  readonly deletingLabRecordId = this.destructiveActionsService.deletingLabRecordId;

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

  // Per-card trade ledger state (pagination index only — rendering lives in StrategyCardComponent)
  tradeLedgerPages: Record<string, number> = {};       // lab_record_id → current page index

  // Paper trading state — owned by paperTradingService.
  readonly paperTradingLabRecordId = this.paperTradingService.paperTradingLabRecordId;

  // Activity log — owned by activityLogService.
  readonly activityLog = this.activityLogService.activityLog;

  /**
   * Terminal-outcome text for the aria-live status region (`runAnnouncement`).
   * Owned by `activityLogService`, set from its `complete`/`error` handling
   * — using the terminal event's own data, while `runService.runStatus()` is
   * still populated — or, when neither branch fires (SSE degrades to
   * polling, or a reconnect gets only a terminal snapshot then `done`),
   * backstopped by `refreshResultsOnRunFinish`'s fallback below once
   * `runStatus()` has already cleared. Reset to null when a new run starts
   * so a stale outcome from a previous run can't leak into the next run's
   * brief "Starting…" window.
   */
  private readonly runOutcomeAnnouncement = this.activityLogService.runOutcomeAnnouncement;

  @ViewChild('logContainer') logContainer?: ElementRef<HTMLElement>;

  /** Tracks the previous `runService.running()` value so the effect below can detect a true→false transition. */
  private wasRunning = false;

  /**
   * Refreshes the results list exactly once whenever `runService.running()`
   * transitions from true to false — covering every way a run ends (an
   * explicit `complete`/`error` event, the SSE stream's own `done`-then-close,
   * or the REST-polling fallback detecting a terminal status) with one rule,
   * rather than duplicating a `loadResults()` call at each of those call sites.
   *
   * Also backstops `runOutcomeAnnouncement` for the same transition:
   * `activityLogService`'s `complete`/`error` handling already sets it from
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
   *   (either one `activityLogService` already set, or this fallback's
   *   `describeRunStatus()` derivation) by the time the effect returns. A
   *   terminal failure/interrupt/connection-lost message (captured in
   *   `activityLogService.terminalErrorBanner`) survives the `loadResults()`
   *   refresh — which clears `error` before re-fetching — so the sighted
   *   banner is durable rather than flashing and vanishing; an unrelated
   *   ambient error is NOT preserved.
   */
  private readonly refreshResultsOnRunFinish = effect(() => {
    const isRunning = this.runService.running();
    if (!isRunning && this.wasRunning) {
      this.runOutcomeAnnouncement.update(
        (current) => current ?? describeRunStatus(this.runService.lastTerminalStatus()),
      );
      // loadResults() clears `error` synchronously before its async fetch, so a
      // terminal 'error'/reclaim message activityLogService set would be
      // wiped on the very transition that produced it — leaving sighted users no
      // visible reason the run stopped. Re-assert it afterward. Crucially this
      // re-asserts only `terminalErrorBanner` (set solely by a terminal run
      // error), NOT whatever `error()` happens to hold: an unrelated ambient
      // error still showing (e.g. an errors$ paper-trading poll failure) is left
      // cleared, not resurrected onto a cleanly-completed run. (A cancellation
      // uses `completionWarning`, which loadResults() never clears.) If the
      // reload itself errors, its own handler's message wins — the run is over.
      const terminalError = this.activityLogService.terminalErrorBanner;
      this.loadResults();
      if (terminalError) this.error.set(terminalError);
    }
    this.wasRunning = isRunning;
  });

  /**
   * Subscribes `source$` to mirror each of its emissions onto the `error`
   * signal for the component's lifetime. Shared by every "forward this
   * service's error stream into the banner" wiring site (`activityLogService`,
   * `runService`, `paperTradingService`) instead of each hand-rolling the same
   * `pipe(takeUntilDestroyed) -> subscribe` boilerplate.
   *
   * Preconditions: none.
   * Postconditions: every value `source$` emits — including `null`, which
   *   clears the banner — is set on `error` until the component is destroyed.
   */
  private mirrorErrorsIntoBanner<T extends string | null>(source$: Observable<T>): void {
    source$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((message) => this.error.set(message));
  }

  /**
   * Wires `paperTradingService.errors$` into the `error` signal. A field
   * initializer (not wired inside `ngOnInit()`) so it's active the instant
   * the component is constructed — `runPaperTrading()` can be called, and
   * its guard/POST error surfaced, before `ngOnInit()` ever runs.
   * `mirrorErrorsIntoBanner` returns `void`, so this field only exists to
   * trigger that call at construction time — it holds no state of its own.
   */
  private readonly wirePaperTradingErrors = this.mirrorErrorsIntoBanner(this.paperTradingService.errors$);

  ngOnInit(): void {
    this.loadConfig();
    this.loadResults();
    this.paperTradingService.loadPaperTradingResults();
    this.runService.checkForActiveRun();
    this.loadTradingViewStatus();

    this.activityLogService.resultsRefreshRequested$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.loadResults());
    this.mirrorErrorsIntoBanner(this.activityLogService.terminalError$);
    this.activityLogService.scrollRequested$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.logContainer?.nativeElement?.scrollTo({ top: 999999, behavior: 'smooth' }));
    this.mirrorErrorsIntoBanner(this.runService.errors$);
    this.destructiveActionsService.resultsRefreshRequested$
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe(() => this.loadResults());
    this.mirrorErrorsIntoBanner(this.destructiveActionsService.errors$);
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
   * Opens the "Generate strategies" modal, seeded with the current batch
   * size/count and category selection. If the user submits, applies the
   * returned configuration and immediately starts a run via
   * `runNewStrategy()`; a cancelled dialog (result `undefined`) leaves the
   * current configuration and run state untouched.
   */
  openGenerateStrategiesDialog(): void {
    const ref = this.dialog.open<
      GenerateStrategiesDialogComponent,
      GenerateStrategiesDialogData,
      GenerateStrategiesDialogResult
    >(GenerateStrategiesDialogComponent, {
      data: {
        batchSize: this.batchSize,
        batchCount: this.batchCount,
        batchSizeMin: this.BATCH_SIZE_MIN,
        batchSizeMax: this.BATCH_SIZE_MAX,
        batchCountMin: this.BATCH_COUNT_MIN,
        // Passed by reference (not invoked) so the dialog stays synchronized
        // if a config fetch changes the operator-configured max while it's
        // still open — see GenerateStrategiesDialogData.batchCountMax.
        batchCountMax: this.BATCH_COUNT_MAX,
        categoryOptions: this.categoryOptions(),
        selectedCategories: this.selectedCategories(),
      },
      width: '480px',
    });
    ref.afterClosed().subscribe((result) => {
      if (!result) {
        return;
      }
      // A run can start elsewhere (another tab, or checkForActiveRun's
      // reconnect polling) while this dialog was open, without the dialog's
      // own snapshot ever finding out. runNewStrategy()'s re-entrancy guard
      // would otherwise silently drop the user's just-confirmed
      // configuration — surface it instead of doing nothing.
      if (this.running()) {
        this.error.set('A strategy run is already in progress — try again once it finishes.');
        return;
      }
      this.batchSize = result.batchSize;
      // result.batchCount is already clamped to BATCH_COUNT_MIN/MAX() as of
      // the moment the dialog closed (see GenerateStrategiesDialogResult's
      // postcondition) — the dialog stays live-synchronized to this
      // component's own BATCH_COUNT_MAX signal for exactly this reason, so
      // no further clamping is needed here.
      this.batchCount = result.batchCount;
      // If the user never touched the category toggles, `result.selectedCategories`
      // is just the dialog's seeded snapshot — prefer this component's own
      // current selection (already kept correct by applyCategoryConfig, including
      // any category a config fetch added or removed while the dialog was open)
      // rather than reconciling a stale copy, which — on a partial overlap —
      // would silently exclude a newly added category the untouched selection
      // should still include.
      if (result.categoriesTouched) {
        // categoryOptions() may have been refreshed while the dialog was open —
        // intersect against the CURRENT options (falling back to all, same as
        // applyCategoryConfig's own reconciliation) so a run can never be
        // constrained to a selection the backend no longer recognizes.
        const currentOptionValues = this.categoryOptions().map((c) => c.value);
        const reconciled = currentOptionValues.filter((v) => result.selectedCategories.includes(v));
        this.selectedCategories.set(reconciled.length ? reconciled : currentOptionValues);
        this.userAdjustedCategories = true;
      }
      this.runNewStrategy();
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

    const batchSize = clamp(this.batchSize, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
    const batchCount = clamp(this.batchCount, this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX());
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
    this.activityLogService.terminalErrorBanner = null;
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
  readonly getAssetClassIcon = getAssetClassIcon;
  readonly phaseLabel = phaseLabel;

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
   * Human-readable meter value text for the run progress bar (e.g. "63% —
   * Strategy 5 of 8", or "63% — Batch 2 of 3 — Strategy 5 of 8"), read by
   * assistive tech in place of the raw percentage a `<mat-progress-bar>`
   * alone would expose. Mirrors `.progress-title`'s own visible text shape
   * so the two can never disagree. Deliberately independent of
   * `runAnnouncement` (the SR-only live region) rather than sharing its
   * segment-building — that computed has its own "Finishing up" terminal-gap
   * handling this meter doesn't need, and is out of scope for this change.
   *
   * Preconditions: none.
   * Postconditions: returns '' when idle; otherwise a non-empty string
   *   combining `progressPercent()` with the same batch/strategy position
   *   shown in `.progress-title`.
   */
  readonly progressValueText = computed(() => {
    const status = this.runService.runStatus();
    if (!status) return '';
    const batchPrefix = status.batch_count && status.batch_count > 1
      ? `Batch ${this.currentBatchNumber()} of ${status.batch_count} — `
      : '';
    return `${this.progressPercent()}% — ${batchPrefix}Strategy ${this.currentStrategyNumber()} of ${status.total_cycles}`;
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

  /**
   * Current trade-ledger paginator page for a record's `StrategyCardComponent`.
   *
   * Preconditions: `id` is a non-empty `lab_record_id`.
   * Postconditions: returns the zero-based page index last set via
   *   `onPageChange` for `id`, or `0` if the record's ledger has never been paged.
   */
  getPageIndex(id: string): number {
    return this.tradeLedgerPages[id] ?? 0;
  }

  /**
   * Handles a `pageChanged` output from a `StrategyCardComponent`'s trade-ledger
   * paginator, persisting the new page so it survives the card re-rendering
   * (e.g. on the next results poll).
   *
   * Preconditions: `id` is a non-empty `lab_record_id`; `event` is the
   *   Material paginator's `PageEvent` for that record's ledger.
   * Postconditions: `getPageIndex(id)` returns `event.pageIndex` on the next call.
   */
  onPageChange(id: string, event: PageEvent): void {
    this.tradeLedgerPages[id] = event.pageIndex;
  }

  /**
   * Preconditions: `record` is a loaded lab row.
   * Postconditions: delegates entirely to `destructiveActionsService.deleteRecord`
   *   (see its own contract) — this method has no logic of its own beyond
   *   forwarding the call.
   */
  deleteRecord(record: StrategyLabRecord): void {
    this.destructiveActionsService.deleteRecord(record);
  }

  /**
   * Postconditions: delegates entirely to `destructiveActionsService.clearAllLabData`
   *   (see its own contract) — this method has no logic of its own beyond
   *   forwarding the call.
   */
  clearAllLabData(): void {
    this.destructiveActionsService.clearAllLabData();
  }

  // ---------------------------------------------------------------------------
  // Paper Trading — delegates to paperTradingService.
  // ---------------------------------------------------------------------------

  /**
   * Preconditions: `record` is a loaded lab row.
   * Postconditions: delegates entirely to `paperTradingService.runPaperTrading`
   *   (see its own contract) — this method has no logic of its own beyond
   *   forwarding the call.
   */
  runPaperTrading(record: StrategyLabRecord): void {
    this.paperTradingService.runPaperTrading(record);
  }

  /**
   * Preconditions: `record` is a loaded lab row.
   * Postconditions: returns `paperTradingService.getPaperSession(record)`
   *   verbatim (see its own contract) — this method has no logic of its own
   *   beyond forwarding the call.
   */
  getPaperSession(record: StrategyLabRecord): PaperTradingSession | null {
    return this.paperTradingService.getPaperSession(record);
  }
}
