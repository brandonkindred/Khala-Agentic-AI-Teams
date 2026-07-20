import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';
import { CommonModule, DecimalPipe, CurrencyPipe } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatTableModule } from '@angular/material/table';
import { MatPaginatorModule, PageEvent } from '@angular/material/paginator';

import { DateOnlyPipe } from '../../../shared/date-only.pipe';
import type { StrategyLabRecord, TradeRecord } from '../../../models';

/**
 * Presentational trade-ledger panel for a single Strategy Lab result. Renders
 * the ledger summary bar, the paginated trade table, and the paginator itself
 * — extracted from `StrategyCardComponent`, which owns no trade-ledger state
 * beyond the host-fed `pageIndex`.
 *
 * Preconditions: `record` is set before the first render (required input);
 *   the panel itself only renders (`@if`) when `record.backtest.trades` is
 *   non-empty.
 * Postconditions: renders identically for the same `record`/`pageIndex`
 *   regardless of how many times change detection runs (OnPush; every
 *   derived value is a pure function of the inputs).
 */
@Component({
  selector: 'app-trade-ledger',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, DecimalPipe, CurrencyPipe, DateOnlyPipe, MatIconModule, MatExpansionModule, MatTableModule, MatPaginatorModule],
  templateUrl: './trade-ledger.component.html',
  styleUrl: './trade-ledger.component.scss',
})
export class TradeLedgerComponent {
  /** The lab record whose trade ledger this panel renders. */
  @Input({ required: true }) record!: StrategyLabRecord;
  /** Current paginator page index (host-owned, keyed by `lab_record_id`). */
  @Input() pageIndex = 0;

  /** User changed the paginator page. */
  @Output() pageChanged = new EventEmitter<PageEvent>();

  readonly PAGE_SIZE = 20;
  readonly TRADE_COLUMNS = [
    'trade_num', 'entry_date', 'exit_date', 'symbol',
    'entry_price', 'exit_price', 'shares', 'return_pct',
    'net_pnl', 'cumulative_pnl', 'outcome',
  ];

  /** Paginator page-change handler. Postconditions: `pageChanged` emits `event` unchanged exactly once. */
  onPageChange(event: PageEvent): void {
    this.pageChanged.emit(event);
  }

  // A `TradeLedgerComponent` instance is reused (per the host's `@for track
  // record.lab_record_id`) across status polls for the same record — a poll
  // replaces `record` with a brand-new object, so every cache below is keyed
  // on `this.record` identity (not just derived inputs like `pageIndex`) to
  // invalidate correctly when that happens.
  private pagedTradesCache: { record: StrategyLabRecord; page: number; trades: TradeRecord[] } | null = null;
  private winCountCache: { record: StrategyLabRecord; count: number } | null = null;

  /**
   * Trade-ledger rows for the current paginator page. Cached per
   * (record, page) so the mat-table `dataSource` gets a stable array
   * reference across change-detection cycles that don't change either,
   * letting it diff instead of re-rendering every row.
   *
   * Postconditions: returns `PAGE_SIZE` trades starting at `pageIndex *
   *   PAGE_SIZE` (fewer on the last page); same array reference as the
   *   previous call when `record` and `pageIndex` are both unchanged.
   */
  pagedTrades(): TradeRecord[] {
    const page = this.pageIndex;
    const cached = this.pagedTradesCache;
    // Returning a stable array reference for the same (record, page) lets the
    // mat-table dataSource diff instead of re-rendering every row each CD cycle.
    if (cached && cached.record === this.record && cached.page === page) {
      return cached.trades;
    }
    const start = page * this.PAGE_SIZE;
    const trades = this.record.backtest.trades.slice(start, start + this.PAGE_SIZE);
    this.pagedTradesCache = { record: this.record, page, trades };
    return trades;
  }

  /** Postconditions: returns the total number of trades in `record.backtest.trades`, independent of pagination. */
  tradeCount(): number {
    return this.record.backtest.trades.length;
  }

  /**
   * Count of trades with `outcome === 'win'`, cached per record (see the
   * cache-invalidation note above `pagedTradesCache`).
   *
   * Postconditions: returns a value in `[0, tradeCount()]`.
   */
  winCount(): number {
    const cached = this.winCountCache;
    if (cached && cached.record === this.record) {
      return cached.count;
    }
    const count = this.record.backtest.trades.filter((t) => t.outcome === 'win').length;
    this.winCountCache = { record: this.record, count };
    return count;
  }

  /**
   * Net P&L across the whole trade history. `cumulative_pnl` on each trade
   * is already a running total, so the last trade's value is the total —
   * no need to sum every trade's `net_pnl` here.
   *
   * Postconditions: returns `0` when there are no trades, else the last
   *   trade's `cumulative_pnl`.
   */
  totalNetPnl(): number {
    const trades = this.record.backtest.trades;
    return trades.length ? trades[trades.length - 1].cumulative_pnl : 0;
  }

  /** Postconditions: returns `'win-cell'` when `t.outcome === 'win'`, else `'loss-cell'`. */
  tradeReturnColor(t: TradeRecord): string {
    return t.outcome === 'win' ? 'win-cell' : 'loss-cell';
  }

  /**
   * Precision tiering for the trade-ledger entry/exit price columns: fewer
   * decimals for large prices (where sub-dollar precision is noise) and more
   * for sub-$1 prices (where it's signal).
   *
   * Preconditions: `price` is finite (not `NaN`/`Infinity`).
   * Postconditions: returns `price` formatted with 0 decimals when `>= 1000`,
   *   2 decimals when in `[1, 1000)`, 4 decimals when `< 1`.
   */
  formatPrice(price: number): string {
    if (price >= 1000) return price.toFixed(0);
    if (price >= 1) return price.toFixed(2);
    return price.toFixed(4);
  }

  /**
   * Accessible name for the trade-table's scrollable wrapper (WCAG 2.4.7 —
   * the wrapper, not the table, must be focusable since the global outline
   * would otherwise be clipped by `overflow-x: auto`).
   */
  tradeTableRegionLabel(): string {
    return `${this.record.strategy.asset_class} strategy trade history, scrollable`;
  }

  /**
   * Accessible name for the trade-ledger `<table>` itself — distinct from
   * `tradeTableRegionLabel`, which names the scrollable wrapper div: a
   * screen reader's table-navigation commands read the `<table>`'s own
   * accessible name, not the wrapper's.
   *
   * Postconditions: returns a non-empty, state-independent label.
   */
  tradeTableAccessibleName(): string {
    return `Trade ledger, ${this.tradeCount()} trades`;
  }
}
