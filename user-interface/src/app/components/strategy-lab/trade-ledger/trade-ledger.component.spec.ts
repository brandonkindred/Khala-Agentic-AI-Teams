import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import type { StrategyLabRecord, TradeRecord } from '../../../models';
import { TradeLedgerComponent } from './trade-ledger.component';

function makeBacktest(overrides: Partial<StrategyLabRecord['backtest']> = {}): StrategyLabRecord['backtest'] {
  return {
    backtest_id: 'bt-1',
    strategy_id: 'strat-1',
    config: {
      start_date: '2025-01-01',
      end_date: '2025-12-31',
      initial_capital: 100000,
      benchmark_symbol: 'SPY',
      rebalance_frequency: 'daily',
      transaction_cost_bps: 5,
      slippage_bps: 5,
    },
    result: {
      total_return_pct: 15,
      annualized_return_pct: 12.5,
      volatility_pct: 18,
      sharpe_ratio: 1.4,
      max_drawdown_pct: -8,
      win_rate_pct: 58,
      profit_factor: 1.6,
    },
    trades: [],
    ...overrides,
  } as unknown as StrategyLabRecord['backtest'];
}

function makeRecord(overrides: Partial<StrategyLabRecord> = {}): StrategyLabRecord {
  return {
    lab_record_id: 'rec-1',
    is_winning: true,
    is_publishable: true,
    strategy_rationale: 'Momentum persists after a volume breakout.',
    analysis_narrative: 'This strategy performed well across the backtest window.',
    created_at: '2026-01-01T00:00:00Z',
    strategy: {
      strategy_id: 'strat-1',
      authored_by: 'design-agent',
      asset_class: 'stocks',
      hypothesis: 'Stocks with rising volume tend to continue trending.',
      signal_definition: 'volume_zscore > 2',
      timeframe: 'daily',
      entry_rules: [],
      exit_rules: [],
      sizing: {},
      target_symbols: ['AAPL'],
      risk_limits: {},
      speculative: false,
      requires_redesign: false,
      unparsed_rules: [],
      audit: {},
    } as unknown as StrategyLabRecord['strategy'],
    backtest: makeBacktest(),
    ...overrides,
  };
}

function makeTrade(overrides: Partial<TradeRecord> = {}): TradeRecord {
  return {
    trade_num: 1,
    entry_date: '2025-02-01',
    exit_date: '2025-02-05',
    symbol: 'AAPL',
    side: 'long',
    entry_price: 150,
    exit_price: 155,
    shares: 10,
    position_value: 1500,
    gross_pnl: 50,
    net_pnl: 48,
    return_pct: 3.2,
    hold_days: 4,
    outcome: 'win',
    cumulative_pnl: 48,
    ...overrides,
  };
}

describe('TradeLedgerComponent', () => {
  let fixture: ComponentFixture<TradeLedgerComponent>;
  let component: TradeLedgerComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [TradeLedgerComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(TradeLedgerComponent);
    component = fixture.componentInstance;
    component.record = makeRecord();
  });

  describe('pagedTrades / winCount', () => {
    it('memoizes pagedTrades and winCount per record', () => {
      component.record = makeRecord({
        backtest: makeBacktest({
          trades: [
            makeTrade({ outcome: 'win', cumulative_pnl: 10 }),
            makeTrade({ outcome: 'loss', cumulative_pnl: 4 }),
            makeTrade({ outcome: 'win', cumulative_pnl: 9 }),
          ],
        }),
      });

      const paged1 = component.pagedTrades();
      expect(component.pagedTrades()).toBe(paged1); // stable reference (same record + page)
      expect(component.winCount()).toBe(2);
      expect(component.winCount()).toBe(2); // served from cache on the second call
    });

    it('invalidates the cache when a new record object replaces the old one (status poll)', () => {
      const winningTrades = [makeTrade({ outcome: 'win' }), makeTrade({ outcome: 'win' })];
      const losingTrades = [makeTrade({ outcome: 'loss' })];
      component.record = makeRecord({ backtest: makeBacktest({ trades: winningTrades }) });
      expect(component.winCount()).toBe(2);

      component.record = makeRecord({ backtest: makeBacktest({ trades: losingTrades }) });
      expect(component.winCount()).toBe(0);
    });
  });

  describe('formatPrice', () => {
    it('formats prices >= 1000 with no decimal places', () => {
      expect(component.formatPrice(1234.567)).toBe('1235');
      expect(component.formatPrice(1000)).toBe('1000');
    });

    it('formats prices in [1, 1000) with 2 decimal places', () => {
      expect(component.formatPrice(150.126)).toBe('150.13');
      expect(component.formatPrice(1)).toBe('1.00');
    });

    it('formats prices below 1 with 4 decimal places', () => {
      expect(component.formatPrice(0.12345)).toBe('0.1235');
      expect(component.formatPrice(0)).toBe('0.0000');
    });
  });

  describe('tradeReturnColor', () => {
    it('returns win-cell for a winning trade and loss-cell for a losing trade', () => {
      expect(component.tradeReturnColor(makeTrade({ outcome: 'win' }))).toBe('win-cell');
      expect(component.tradeReturnColor(makeTrade({ outcome: 'loss' }))).toBe('loss-cell');
    });
  });

  describe('tradeCount / totalNetPnl', () => {
    it('returns 0 trades and 0 net P&L for an empty ledger', () => {
      component.record = makeRecord({ backtest: makeBacktest({ trades: [] }) });
      expect(component.tradeCount()).toBe(0);
      expect(component.totalNetPnl()).toBe(0);
    });

    it('returns the last trade cumulative_pnl as the total net P&L', () => {
      component.record = makeRecord({
        backtest: makeBacktest({
          trades: [makeTrade({ cumulative_pnl: 10 }), makeTrade({ trade_num: 2, cumulative_pnl: 34 })],
        }),
      });
      expect(component.tradeCount()).toBe(2);
      expect(component.totalNetPnl()).toBe(34);
    });
  });

  describe('accessible names', () => {
    it('includes the record asset class and trade count', () => {
      component.record = makeRecord({
        strategy: { ...makeRecord().strategy, asset_class: 'crypto' },
        backtest: makeBacktest({ trades: [makeTrade()] }),
      });
      expect(component.tradeTableRegionLabel()).toBe('crypto strategy trade history, scrollable');
      expect(component.tradeTableAccessibleName()).toBe('Trade ledger, 1 trades');
    });
  });

  describe('outputs', () => {
    it('emits pageChanged with the paginator event when the ledger page changes', () => {
      component.record = makeRecord({
        backtest: makeBacktest({ trades: [makeTrade(), makeTrade({ trade_num: 2 })] }),
      });
      fixture.detectChanges();
      const spy = vi.fn();
      component.pageChanged.subscribe(spy);
      const event = { pageIndex: 1, pageSize: 20, length: 2 };
      component.onPageChange(event);
      expect(spy).toHaveBeenCalledWith(event);
    });
  });

  describe('rendered template', () => {
    it('does not render the panel when there are no trades', () => {
      component.record = makeRecord({ backtest: makeBacktest({ trades: [] }) });
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.ledger-panel')).toBeNull();
    });

    it('renders the trade table and ledger summary bar when trades exist', () => {
      component.record = makeRecord({
        backtest: makeBacktest({
          trades: [
            makeTrade({ outcome: 'win', net_pnl: 48, cumulative_pnl: 48 }),
            makeTrade({ trade_num: 2, outcome: 'loss', net_pnl: -10, cumulative_pnl: 38 }),
          ],
        }),
      });
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.ledger-panel')).toBeTruthy();
      const table: HTMLElement = fixture.nativeElement.querySelector('.trade-table');
      expect(table).toBeTruthy();
      expect(table.getAttribute('aria-label')).toBe('Trade ledger, 2 trades');

      const rows = fixture.nativeElement.querySelectorAll('.trade-table tbody tr');
      expect(rows.length).toBe(2);

      const stats: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.ledger-stat .ls-value'));
      expect(stats[0].textContent?.trim()).toBe('2'); // Trades
      expect(stats[1].textContent?.trim()).toBe('1'); // Wins
      expect(stats[2].textContent?.trim()).toBe('1'); // Losses
    });

    it('reflects the pageIndex input on the paginator', () => {
      component.record = makeRecord({
        backtest: makeBacktest({ trades: Array.from({ length: 25 }, (_, i) => makeTrade({ trade_num: i + 1 })) }),
      });
      component.pageIndex = 1;
      fixture.detectChanges();

      const paginator = fixture.nativeElement.querySelector('mat-paginator');
      expect(paginator).toBeTruthy();
      expect(component.pagedTrades().length).toBe(5); // 25 trades, page 2 of 20-per-page
    });
  });
});
