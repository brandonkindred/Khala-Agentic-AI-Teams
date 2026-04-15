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
  /** Lab record id currently being deleted (disables actions on that card). */
  deletingLabRecordId: string | null = null;

  // User-configurable batch settings (mirror backend Field bounds).
  readonly BATCH_SIZE_MIN = 1;
  readonly BATCH_SIZE_MAX = 25;
  readonly BATCH_COUNT_MIN = 1;
  readonly BATCH_COUNT_MAX = 10;
  batchSize = 10;
  batchCount = 1;

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
    this.loadResults();
    this.loadPaperTradingResults();
    this.checkForActiveRun();
  }

  ngOnDestroy(): void {
    this.sseSub?.unsubscribe();
    this.pollSub?.unsubscribe();
    this.activeRunCheckSub?.unsubscribe();
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

    if (event.type === 'complete') {
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

  runNewStrategy(): void {
    const batchSize = this.clamp(this.batchSize, this.BATCH_SIZE_MIN, this.BATCH_SIZE_MAX);
    const batchCount = this.clamp(this.batchCount, this.BATCH_COUNT_MIN, this.BATCH_COUNT_MAX);
    // Reflect any clamping back into the form so the user sees what was sent.
    this.batchSize = batchSize;
    this.batchCount = batchCount;

    this.running = true;
    this.error = null;
    this.api.runStrategyLab({ batch_size: batchSize, batch_count: batchCount }).subscribe({
      next: (res) => {
        this.activeRunId = res.run_id;
        this.runStatus = {
          run_id: res.run_id,
          status: 'running',
          started_at: new Date().toISOString(),
          total_cycles: res.total_cycles,
          completed_cycles: 0,
          skipped_cycles: 0,
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
   * A failed gate is "remedied" if it failed in an earlier refinement round
   * and the final round produced a passing result (i.e. `refinement_rounds > 0`
   * and this gate's round is not the last one that ran).
   */
  isRemedied(gate: QualityGateResult, record: StrategyLabRecord): boolean {
    if (gate.passed) return false;
    const maxRound = record.refinement_rounds ?? 0;
    if (maxRound === 0) return false;
    // Gate failed in an earlier round — the strategy continued past it
    return (gate.refinement_round ?? 0) < maxRound;
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
          // Keep the most recent session per lab record
          if (!sessions[s.lab_record_id] || s.completed_at > sessions[s.lab_record_id].completed_at) {
            sessions[s.lab_record_id] = s;
          }
        }
        this.paperTradingSessions = sessions;
      },
    });
  }

  runPaperTrading(record: StrategyLabRecord): void {
    this.error = null;
    this.paperTradingLabRecordId = record.lab_record_id;
    this.api.runPaperTrading({ lab_record_id: record.lab_record_id }).subscribe({
      next: (res) => {
        this.paperTradingLabRecordId = null;
        this.paperTradingSessions[record.lab_record_id] = res.session;
      },
      error: (err) => {
        this.paperTradingLabRecordId = null;
        this.error = err?.error?.detail || err?.message || 'Paper trading failed.';
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
