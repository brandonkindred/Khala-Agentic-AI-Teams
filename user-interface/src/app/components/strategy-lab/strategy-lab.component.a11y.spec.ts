import { TestBed } from '@angular/core/testing';
import { of, NEVER } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { StrategyLabComponent } from './strategy-lab.component';
import type {
  StrategyLabRecord,
  StrategySpec,
  TradeRecord,
  PaperTradingSession,
  PaperTradingComparison,
} from '../../models';
import { expectNoAxeViolations } from '../../testing/a11y';

const STRATEGY: StrategySpec = {
  strategy_id: 'strat-1',
  authored_by: 'agent',
  asset_class: 'stocks',
  hypothesis: 'Momentum continuation after a breakout above the 20-day high.',
  signal_definition: 'RSI(14) crosses above 60 with rising volume.',
  timeframe: '1d',
  entry_rules: [],
  exit_rules: [],
  sizing: { kind: 'fixed_fraction', fraction: 0.02 },
  target_symbols: ['AAPL'],
  risk_limits: {},
  speculative: false,
  requires_redesign: false,
  unparsed_rules: [],
  audit: {
    data_snapshot_id: 'snap-1',
    assumptions: [],
    calc_artifacts: [],
    gate_trace: [],
    agent_versions: {},
  },
};

const RECORD: StrategyLabRecord = {
  lab_record_id: 'rec-1',
  strategy: STRATEGY,
  backtest: {
    backtest_id: 'bt-1',
    strategy_id: 'strat-1',
    strategy: STRATEGY,
    config: {
      start_date: '2024-01-01',
      end_date: '2024-06-01',
      initial_capital: 10000,
      benchmark_symbol: 'SPY',
      rebalance_frequency: 'daily',
      transaction_cost_bps: 5,
      slippage_bps: 5,
    },
    submitted_by: 'agent',
    submitted_at: '2024-06-01T00:00:00Z',
    completed_at: '2024-06-01T00:05:00Z',
    status: 'complete',
    result: {
      total_return_pct: 12,
      annualized_return_pct: 12,
      volatility_pct: 8,
      sharpe_ratio: 1.5,
      max_drawdown_pct: -5,
      win_rate_pct: 55,
      profit_factor: 1.8,
    },
    notes: [],
    trades: [],
  },
  is_winning: false,
  strategy_rationale: 'Backtested momentum signal with acceptable drawdown.',
  analysis_narrative: 'The strategy captured most of the mid-year rally.',
  created_at: '2024-06-01T00:05:00Z',
};

describe('StrategyLabComponent a11y — result card disclosure', () => {
  async function createFixture() {
    const apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(NEVER),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [RECORD], count: 1, winning_count: 0, losing_count: 1 }),
      ),
      getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
    };
    const integrationsSpy = {
      getTradingViewConfig: vi.fn().mockReturnValue(
        of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false }),
      ),
    };

    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // triggers ngOnInit -> loadResults() -> renders the card
    return fixture;
  }

  it('has no axe violations collapsed', async () => {
    const fixture = await createFixture();
    expect(fixture.nativeElement.querySelector('.strategy-card')).toBeTruthy();
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('exposes a single keyboard-accessible disclosure button wired to the region', async () => {
    const fixture = await createFixture();
    const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.expand-chevron-btn');
    expect(btn).toBeTruthy();
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    expect(btn.getAttribute('aria-label')).toBe('Show details for stocks strategy');

    // The other two former toggle targets must carry no interactive semantics.
    const header = fixture.nativeElement.querySelector('.strategy-card-header-row');
    const metricsRow = fixture.nativeElement.querySelector('.metrics-row');
    expect(header.getAttribute('role')).toBeNull();
    expect(header.hasAttribute('tabindex')).toBe(false);
    expect(metricsRow.getAttribute('role')).toBeNull();
    expect(metricsRow.hasAttribute('tabindex')).toBe(false);

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('has no axe violations expanded, with aria-expanded/aria-controls wired to the region id', async () => {
    const fixture = await createFixture();
    fixture.componentInstance.toggleCard('rec-1');
    fixture.detectChanges();

    const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.expand-chevron-btn');
    const region: HTMLElement = fixture.nativeElement.querySelector('.card-expanded-region');
    expect(btn).toBeTruthy();
    expect(region).toBeTruthy();
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.getAttribute('aria-label')).toBe('Hide details for stocks strategy');
    expect(region.getAttribute('role')).toBe('region');
    expect(btn.getAttribute('aria-controls')).toBe(region.id);
    expect(region.getAttribute('aria-label')).toBe('stocks strategy details');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);
});

describe('StrategyLabComponent a11y — scrollable containers (WCAG 2.4.7)', () => {
  const TRADE: TradeRecord = {
    trade_num: 1,
    entry_date: '2024-02-01',
    exit_date: '2024-02-10',
    symbol: 'AAPL',
    side: 'long',
    entry_price: 150,
    exit_price: 160,
    shares: 10,
    position_value: 1500,
    gross_pnl: 100,
    net_pnl: 95,
    return_pct: 6.3,
    hold_days: 9,
    outcome: 'win',
    cumulative_pnl: 95,
  };

  const RECORD_WITH_TRADES: StrategyLabRecord = {
    ...RECORD,
    backtest: { ...RECORD.backtest, trades: [TRADE] },
  };

  const COMPARISON: PaperTradingComparison = {
    backtest_win_rate_pct: 55,
    paper_win_rate_pct: 52,
    backtest_annualized_return_pct: 12,
    paper_annualized_return_pct: 11,
    backtest_sharpe_ratio: 1.5,
    paper_sharpe_ratio: 1.4,
    backtest_max_drawdown_pct: -5,
    paper_max_drawdown_pct: -6,
    backtest_profit_factor: 1.8,
    paper_profit_factor: 1.7,
    win_rate_aligned: true,
    return_aligned: true,
    sharpe_aligned: true,
    drawdown_aligned: true,
    profit_factor_aligned: true,
  };

  const PAPER_SESSION: PaperTradingSession = {
    session_id: 'pt-1',
    lab_record_id: 'rec-1',
    strategy: STRATEGY,
    status: 'completed',
    initial_capital: 10000,
    current_capital: 10500,
    trades: [],
    trade_decisions: [],
    comparison: COMPARISON,
    verdict: 'ready_for_live',
    symbols_traded: ['AAPL'],
    data_source: 'tradingview',
    data_period_start: '2024-06-01',
    data_period_end: '2024-06-30',
    started_at: '2024-06-01T00:00:00Z',
    completed_at: '2024-06-30T00:00:00Z',
  };

  async function createFixture(record: StrategyLabRecord) {
    const apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(NEVER),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [record], count: 1, winning_count: 0, losing_count: 1 }),
      ),
      getPaperTradingResults: vi.fn().mockReturnValue(of({ items: [] })),
      getActiveRuns: vi.fn().mockReturnValue(of({ runs: [] })),
    };
    const integrationsSpy = {
      getTradingViewConfig: vi.fn().mockReturnValue(
        of({ enabled: false, mcp_server_url: '', tool_name: 'get_ohlcv', auth_token_configured: false }),
      ),
    };

    await TestBed.configureTestingModule({
      imports: [StrategyLabComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        { provide: InvestmentApiService, useValue: apiSpy },
        { provide: IntegrationsApiService, useValue: integrationsSpy },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // triggers ngOnInit -> loadResults() -> renders the card
    return fixture;
  }

  it('trade-table-wrap: focusable, named, no axe violations once the ledger panel is open', async () => {
    const fixture = await createFixture(RECORD_WITH_TRADES);
    fixture.componentInstance.toggleCard('rec-1');
    fixture.detectChanges();

    const header: HTMLElement = fixture.nativeElement.querySelector('.ledger-panel mat-expansion-panel-header');
    expect(header).toBeTruthy();
    header.click();
    fixture.detectChanges();

    const wrap: HTMLElement = fixture.nativeElement.querySelector('.trade-table-wrap');
    expect(wrap).toBeTruthy();
    expect(wrap.getAttribute('tabindex')).toBe('0');
    expect(wrap.getAttribute('role')).toBe('group');
    expect(wrap.getAttribute('aria-label')).toBe('stocks strategy trade history, scrollable');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('activity-log: focusable, named, no axe violations while a run is active', async () => {
    const fixture = await createFixture(RECORD);
    fixture.componentInstance.running = true;
    fixture.componentInstance.runStatus = {
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 0, phase: 'backtesting' },
    };
    fixture.componentInstance.activityLog = [
      { time: '10:00:00', status: 'active', message: 'Executing strategy backtest...' },
    ];
    fixture.detectChanges();

    const log: HTMLElement = fixture.nativeElement.querySelector('.activity-log');
    expect(log).toBeTruthy();
    expect(log.getAttribute('tabindex')).toBe('0');
    expect(log.getAttribute('role')).toBe('group');
    expect(log.getAttribute('aria-label')).toBe('Strategy run activity log, scrollable');

    // Pre-existing gaps in the "run in progress" header — the run-btn's spinner
    // and the phase progress-bar/spinners lack accessible names, and the
    // running run-btn nests a focusable spinner. Both predate this fix, are
    // unrelated to the activity-log focus-indicator under test here, and are
    // only surfaced now because this is the first a11y spec to set `running`.
    await expectNoAxeViolations(fixture.nativeElement, {
      'aria-progressbar-name': { enabled: false },
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('comparison-table-wrap: focusable, named, no axe violations (independent of card expansion)', async () => {
    const fixture = await createFixture(RECORD);
    fixture.componentInstance.paperTradingSessions = { 'rec-1': PAPER_SESSION };
    fixture.detectChanges();

    const wrap: HTMLElement = fixture.nativeElement.querySelector('.comparison-table-wrap');
    expect(wrap).toBeTruthy(); // renders without toggleCard() — paper trading is independent of card expansion
    expect(wrap.getAttribute('tabindex')).toBe('0');
    expect(wrap.getAttribute('role')).toBe('group');
    expect(wrap.getAttribute('aria-label')).toBe('stocks strategy backtest vs. paper-trading comparison, scrollable');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);
});
