import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import type { PaperTradingComparison, PaperTradingSession, StrategyLabRecord } from '../../../models';
import { PaperTradingPanelComponent } from './paper-trading-panel.component';

function makeBacktest(): StrategyLabRecord['backtest'] {
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

const COMPARISON: PaperTradingComparison = {
  backtest_win_rate_pct: 55, paper_win_rate_pct: 52,
  backtest_annualized_return_pct: 12, paper_annualized_return_pct: 11,
  backtest_sharpe_ratio: 1.5, paper_sharpe_ratio: 1.4,
  backtest_max_drawdown_pct: -5, paper_max_drawdown_pct: -6,
  backtest_profit_factor: 1.8, paper_profit_factor: 1.7,
  win_rate_aligned: true, return_aligned: true, sharpe_aligned: true,
  drawdown_aligned: true, profit_factor_aligned: true, overall_aligned: true,
};

function makeSession(overrides: Partial<PaperTradingSession> = {}): PaperTradingSession {
  return {
    session_id: 'sess-1',
    lab_record_id: 'rec-1',
    status: 'completed',
    verdict: 'ready_for_live',
    trades: [],
    data_period_start: '2025-01-01',
    data_period_end: '2025-06-01',
    comparison: COMPARISON,
    ...overrides,
  } as unknown as PaperTradingSession;
}

describe('PaperTradingPanelComponent', () => {
  let fixture: ComponentFixture<PaperTradingPanelComponent>;
  let component: PaperTradingPanelComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PaperTradingPanelComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(PaperTradingPanelComponent);
    component = fixture.componentInstance;
    component.record = makeRecord();
  });

  describe('comparisonMetrics (precomputed comparison-table rows)', () => {
    it('formats each metric row', () => {
      expect(component.comparisonMetrics(COMPARISON)).toEqual([
        { label: 'Win Rate', backtest: '55.0%', paper: '52.0%', aligned: true },
        { label: 'Annual Return', backtest: '12.0%', paper: '11.0%', aligned: true },
        { label: 'Sharpe', backtest: '1.50', paper: '1.40', aligned: true },
        { label: 'Max Drawdown', backtest: '-5.0%', paper: '-6.0%', aligned: true },
        { label: 'Profit Factor', backtest: '1.80', paper: '1.70', aligned: true },
      ]);
    });

    it('keeps a stable array reference across repeat calls for the same comparison object (so @for does not thrash)', () => {
      const first = component.comparisonMetrics(COMPARISON);
      expect(component.comparisonMetrics(COMPARISON)).toBe(first);

      const differentObject = { ...COMPARISON };
      expect(component.comparisonMetrics(differentObject)).not.toBe(first);
    });
  });

  it('verdictLabel covers ready_for_live, not_performant, and inconclusive', () => {
    expect(component.verdictLabel('ready_for_live')).toBe('READY FOR LIVE');
    expect(component.verdictLabel('not_performant')).toBe('NOT PERFORMANT');
    expect(component.verdictLabel(null)).toBe('INCONCLUSIVE');
  });

  it('publishabilitySkipLabel prefers publishability_skip_reason', () => {
    expect(
      component.publishabilitySkipLabel(
        makeRecord({ publishability_skip_reason: 'realism_failed', paper_trading_skipped_reason: 'alignment_unresolved' }),
      ),
    ).toBe('realism_failed');
  });

  it('publishabilitySkipLabel falls back to paper_trading_skipped_reason when publishability_skip_reason is unset', () => {
    expect(
      component.publishabilitySkipLabel(
        makeRecord({ publishability_skip_reason: undefined, paper_trading_skipped_reason: 'alignment_unresolved' }),
      ),
    ).toBe('alignment_unresolved');
  });

  it('publishabilitySkipLabel returns null when neither reason is set', () => {
    expect(
      component.publishabilitySkipLabel(
        makeRecord({ publishability_skip_reason: undefined, paper_trading_skipped_reason: undefined }),
      ),
    ).toBeNull();
  });

  it('comparisonTableRegionLabel names the scrollable wrapper by asset class', () => {
    expect(component.comparisonTableRegionLabel()).toBe('stocks strategy backtest vs. paper-trading comparison, scrollable');
  });

  describe('onRunPaperTrading', () => {
    it('emits paperTradeRequested exactly once', () => {
      const spy = vi.fn();
      component.paperTradeRequested.subscribe(spy);
      component.onRunPaperTrading();
      expect(spy).toHaveBeenCalledTimes(1);
    });
  });

  describe('rendered template', () => {
    it('renders nothing when the record is not winning and there is no session', () => {
      component.record = makeRecord({ is_winning: false });
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.paper-trading-section')).toBeNull();
      expect(fixture.nativeElement.textContent.trim()).toBe('');
    });

    it('shows the "Paper Trade This Strategy" button for a winning, publishable record with no session yet', () => {
      component.record = makeRecord({ is_winning: true, is_publishable: true });
      fixture.detectChanges();

      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.paper-trade-btn');
      expect(btn).toBeTruthy();
      expect(btn.disabled).toBe(false);
      expect(btn.textContent).toContain('Paper Trade This Strategy');
    });

    it('disables the "Paper Trade This Strategy" button when paperTradingBlocked', () => {
      component.record = makeRecord({ is_winning: true, is_publishable: true });
      component.paperTradingBlocked = true;
      fixture.detectChanges();

      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.paper-trade-btn');
      expect(btn.disabled).toBe(true);
    });

    it('shows a disabled spinner button while paperTradingInProgress with no session yet', () => {
      component.record = makeRecord({ is_winning: true, is_publishable: true });
      component.paperTradingInProgress = true;
      fixture.detectChanges();

      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.paper-trade-btn');
      expect(btn.disabled).toBe(true);
      expect(btn.textContent).toContain('Running paper trading…');
      expect(btn.querySelector('mat-spinner')).toBeTruthy();
    });

    it('emits paperTradeRequested when "Paper Trade This Strategy" is clicked', () => {
      component.record = makeRecord({ is_winning: true, is_publishable: true });
      fixture.detectChanges();
      const spy = vi.fn();
      component.paperTradeRequested.subscribe(spy);
      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.paper-trade-btn');
      btn.click();
      expect(spy).toHaveBeenCalledTimes(1);
    });

    it('shows a not-publishable skip notice (no button) for a winning, non-publishable record with no session', () => {
      component.record = makeRecord({
        is_winning: true,
        is_publishable: false,
        publishability_skip_reason: 'realism_failed',
      });
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.paper-trade-btn')).toBeNull();
      const skipped: HTMLElement = fixture.nativeElement.querySelector('.paper-trading-skipped');
      expect(skipped).toBeTruthy();
      expect(skipped.textContent).toContain('Not publishable');
      expect(skipped.textContent).toContain('realism_failed');
    });

    it.each(['running', 'opening', 'warming_up', 'live'] as const)(
      'shows the RUNNING badge and progress message while a session is in flight (status=%s)',
      (status) => {
        component.record = makeRecord();
        component.paperSession = makeSession({ status, verdict: undefined, comparison: undefined });
        fixture.detectChanges();

        const badge: HTMLElement = fixture.nativeElement.querySelector('.paper-verdict-badge');
        expect(badge.classList.contains('verdict-running')).toBe(true);
        expect(badge.textContent).toContain('RUNNING');
        expect(fixture.nativeElement.querySelector('.comparison-table')).toBeNull();
      },
    );

    it('shows the verdict badge, trade count, date range, and comparison table for a completed session', () => {
      component.record = makeRecord({ is_publishable: true });
      component.paperSession = makeSession({ trades: [{} as never, {} as never] });
      fixture.detectChanges();

      const badge: HTMLElement = fixture.nativeElement.querySelector('.paper-verdict-badge');
      expect(badge.classList.contains('verdict-ready')).toBe(true);
      expect(badge.textContent).toContain('READY FOR LIVE');

      const meta: HTMLElement = fixture.nativeElement.querySelector('.paper-meta');
      expect(meta.textContent).toContain('2 trades');

      expect(fixture.nativeElement.querySelector('table.comparison-table')).toBeTruthy();
    });

    it('gives the comparison table its own accessible name via a visually-hidden caption', () => {
      component.record = makeRecord({ is_publishable: true });
      component.paperSession = makeSession();
      fixture.detectChanges();

      const caption: HTMLElement | null = fixture.nativeElement.querySelector('table.comparison-table > caption');
      expect(caption).toBeTruthy();
      expect(caption?.className).toContain('visually-hidden');
      expect(caption?.textContent?.trim()).toBe('Backtest vs. paper-trading metric comparison');
    });

    it('shows an enabled Re-run button for a completed session on a publishable record', () => {
      component.record = makeRecord({ is_publishable: true });
      component.paperSession = makeSession();
      fixture.detectChanges();

      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.rerun-paper-btn');
      expect(btn).toBeTruthy();
      expect(btn.disabled).toBe(false);
    });

    it('emits paperTradeRequested when Re-run is clicked', () => {
      component.record = makeRecord({ is_publishable: true });
      component.paperSession = makeSession();
      fixture.detectChanges();
      const spy = vi.fn();
      component.paperTradeRequested.subscribe(spy);

      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.rerun-paper-btn');
      btn.click();
      expect(spy).toHaveBeenCalledTimes(1);
    });

    it('shows a disabled, spinner Re-run button while paperTradingInProgress', () => {
      component.record = makeRecord({ is_publishable: true });
      component.paperSession = makeSession();
      component.paperTradingInProgress = true;
      fixture.detectChanges();

      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.rerun-paper-btn');
      expect(btn.disabled).toBe(true);
      expect(btn.querySelector('mat-spinner')).toBeTruthy();
    });

    it('disables the Re-run button when paperTradingBlocked', () => {
      component.record = makeRecord({ is_publishable: true });
      component.paperSession = makeSession();
      component.paperTradingBlocked = true;
      fixture.detectChanges();

      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.rerun-paper-btn');
      expect(btn.disabled).toBe(true);
    });

    it('hides the Re-run button, and shows a "re-run unavailable" notice, for a non-publishable record with an existing session', () => {
      component.record = makeRecord({ is_publishable: false, publishability_skip_reason: 'alignment_unresolved' });
      component.paperSession = makeSession();
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.rerun-paper-btn')).toBeNull();
      const skipped: HTMLElement = fixture.nativeElement.querySelector('.paper-trading-skipped-inline');
      expect(skipped).toBeTruthy();
      expect(skipped.textContent).toContain('Re-run unavailable');
      expect(skipped.textContent).toContain('alignment_unresolved');
    });

    it('shows the divergence analysis when the session has one', () => {
      component.record = makeRecord({ is_publishable: true });
      component.paperSession = makeSession({ verdict: 'not_performant', divergence_analysis: 'Paper trading underperformed on drawdown.' });
      fixture.detectChanges();

      const div: HTMLElement = fixture.nativeElement.querySelector('.divergence-analysis');
      expect(div).toBeTruthy();
      expect(div.textContent).toContain('Paper trading underperformed on drawdown.');
    });

    it('omits the comparison table when the session has no comparison', () => {
      component.record = makeRecord();
      component.paperSession = makeSession({ comparison: undefined });
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.comparison-table-wrap')).toBeNull();
    });

    it('still renders an existing session for a legacy non-publishable-but-winning record', () => {
      component.record = makeRecord({ is_winning: true, is_publishable: undefined });
      component.paperSession = makeSession();
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.paper-trading-result')).toBeTruthy();
    });
  });
});
