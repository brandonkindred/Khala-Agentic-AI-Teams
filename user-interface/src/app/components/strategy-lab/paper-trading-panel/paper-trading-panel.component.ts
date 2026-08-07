import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatDividerModule } from '@angular/material/divider';

import { DateOnlyPipe } from '../../../shared/date-only.pipe';
import { formatPct, formatRatio } from '../../../shared/number-format';
import { publishabilitySkipLabel } from '../../../shared/publishability';
import { verdictLabel } from '../strategy-lab.formatters';
import { isPaperTradingStatusTerminal } from '../../../models';
import type { PaperTradingSession, PaperTradingComparison, StrategyLabRecord } from '../../../models';

/** Precomputed comparison-table row — see `comparisonMetrics`. */
interface ComparisonRow {
  label: string;
  backtest: string;
  paper: string;
  aligned: boolean;
}

/**
 * Presentational paper-trading panel for a single strategy-lab record.
 * Renders the "run paper trading" call to action, the running/completed
 * session state, the backtest-vs-paper comparison table, and the
 * not-publishable skip notice. Owns no external state of its own (the host
 * tracks paper-trading-in-flight and the session map); keeps only an
 * internal memoization cache for `comparisonMetrics`. Performs no
 * externally-observable side effects — the user's "run"/"re-run" action is
 * reported up via `paperTradeRequested` for the host to act on.
 *
 * Preconditions: `record` is set before the first render (required input).
 * Postconditions: renders identically for the same inputs regardless of how
 *   many times change detection runs (OnPush; every derived value is a pure
 *   function of the inputs).
 */
@Component({
  selector: 'app-paper-trading-panel',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, MatButtonModule, MatIconModule, MatProgressSpinnerModule, MatDividerModule, DateOnlyPipe],
  templateUrl: './paper-trading-panel.component.html',
  styleUrl: './paper-trading-panel.component.scss',
})
export class PaperTradingPanelComponent {
  /** The lab record this panel renders paper-trading state for. */
  @Input({ required: true }) record!: StrategyLabRecord;
  /** This record's paper-trading session, if one exists. */
  @Input() paperSession: PaperTradingSession | null = null;
  /** True while a paper-trading run is in flight for this specific record. */
  @Input() paperTradingInProgress = false;
  /** True when starting/re-running paper trading should be disabled (another record's run, or a strategy run, is in flight). */
  @Input() paperTradingBlocked = false;

  /** User asked to run (or re-run) paper trading for this record. */
  @Output() paperTradeRequested = new EventEmitter<void>();

  readonly verdictLabel = verdictLabel;
  readonly publishabilitySkipLabel = publishabilitySkipLabel;
  readonly isPaperTradingStatusTerminal = isPaperTradingStatusTerminal;

  /** Paper-trade / re-run button click handler. Postconditions: `paperTradeRequested` emits exactly once; no local state changes (the host owns paper-trading-in-flight tracking). */
  onRunPaperTrading(): void {
    this.paperTradeRequested.emit();
  }

  /**
   * Accessible name for the paper-trading comparison table's scrollable
   * wrapper (same rationale as the trade-ledger's own equivalent in
   * `TradeLedgerComponent`).
   *
   * Preconditions: `record.strategy.asset_class` is a non-empty string.
   * Postconditions: returns a non-empty, state-independent label.
   */
  comparisonTableRegionLabel(): string {
    return `${this.record.strategy.asset_class} strategy backtest vs. paper-trading comparison, scrollable`;
  }

  // Invalidates on the comparison object's reference identity, not deep
  // equality — relies on the host always replacing the session (and its
  // comparison) with a new object on change, never mutating one in place.
  private comparisonMetricsCache: { comparison: PaperTradingComparison; rows: ComparisonRow[] } | null = null;

  comparisonMetrics(c: PaperTradingComparison): ComparisonRow[] {
    const cached = this.comparisonMetricsCache;
    if (cached && cached.comparison === c) return cached.rows;
    const rows: ComparisonRow[] = [
      { label: 'Win Rate', backtest: formatPct(c.backtest_win_rate_pct), paper: formatPct(c.paper_win_rate_pct), aligned: c.win_rate_aligned },
      { label: 'Annual Return', backtest: formatPct(c.backtest_annualized_return_pct), paper: formatPct(c.paper_annualized_return_pct), aligned: c.return_aligned },
      { label: 'Sharpe', backtest: formatRatio(c.backtest_sharpe_ratio), paper: formatRatio(c.paper_sharpe_ratio), aligned: c.sharpe_aligned },
      { label: 'Max Drawdown', backtest: formatPct(c.backtest_max_drawdown_pct), paper: formatPct(c.paper_max_drawdown_pct), aligned: c.drawdown_aligned },
      { label: 'Profit Factor', backtest: formatRatio(c.backtest_profit_factor), paper: formatRatio(c.paper_profit_factor), aligned: c.profit_factor_aligned },
    ];
    this.comparisonMetricsCache = { comparison: c, rows };
    return rows;
  }
}
