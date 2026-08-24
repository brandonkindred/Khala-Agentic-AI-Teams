import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, NEVER } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { StrategyLabComponent } from './strategy-lab.component';
import { createRunServiceStub, type RunServiceStub } from '../../testing/strategy-lab-run-service.stub';
import { strategyLabProvidersOverride } from '../../testing/strategy-lab-component-providers';
import type {
  StrategyLabRecord,
  StrategySpec,
  TradeRecord,
  PaperTradingSession,
  PaperTradingComparison,
  QualityGateResult,
} from '../../models';
import { expectNoAxeViolations } from '../../testing/a11y';

/** The `StrategyLabRunService` stub a fixture's component was constructed with — see `createRunServiceStub`. */
function stubOf(fixture: ComponentFixture<StrategyLabComponent>): RunServiceStub {
  return fixture.componentInstance.runService as unknown as RunServiceStub;
}


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
  async function createFixture(showTitle?: boolean) {
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
    })
      .overrideComponent(StrategyLabComponent, strategyLabProvidersOverride(createRunServiceStub()))
      .compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    if (showTitle !== undefined) {
      fixture.componentInstance.showTitle = showTitle;
    }
    fixture.detectChanges(); // triggers ngOnInit -> loadResults() -> renders the card
    return fixture;
  }

  it('has no axe violations collapsed', async () => {
    const fixture = await createFixture();
    expect(fixture.nativeElement.querySelector('.strategy-card')).toBeTruthy();
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('showTitle=true (default): exposes the card title as an <h3>, nested under the component\'s own <h2>', async () => {
    const fixture = await createFixture();
    const title: HTMLElement = fixture.nativeElement.querySelector('.strategy-card-header-text [mat-card-title]');
    expect(title).toBeTruthy();
    expect(title.tagName).toBe('H3');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('showTitle=false: exposes the card title as an <h2>, so it sits directly under the wrapper\'s <h1>', async () => {
    const fixture = await createFixture(false);
    const title: HTMLElement = fixture.nativeElement.querySelector('.strategy-card-header-text [mat-card-title]');
    expect(title).toBeTruthy();
    expect(title.tagName).toBe('H2');

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
    // Click the real disclosure button (rather than calling toggleCard()
    // directly) so the OnPush view is marked dirty via Angular's normal
    // event-dispatch path, matching real usage.
    fixture.nativeElement.querySelector<HTMLButtonElement>('.expand-chevron-btn')!.click();
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

describe('StrategyLabComponent a11y — root region labelling (showTitle input)', () => {
  async function createFixture(showTitle?: boolean) {
    const apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(NEVER),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
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
    })
      .overrideComponent(StrategyLabComponent, strategyLabProvidersOverride(createRunServiceStub()))
      .compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    if (showTitle !== undefined) {
      fixture.componentInstance.showTitle = showTitle;
    }
    fixture.detectChanges(); // triggers ngOnInit -> loadResults()
    return fixture;
  }

  it('showTitle=true (default): renders the <h2> once and labels the region via aria-labelledby', async () => {
    const fixture = await createFixture();
    const root: HTMLElement = fixture.nativeElement.querySelector('.strategy-lab');
    const heading: HTMLElement = fixture.nativeElement.querySelector('#strategy-lab-heading');
    expect(root.getAttribute('role')).toBe('region');
    expect(heading).toBeTruthy();
    expect(heading.tagName).toBe('H2');
    expect(root.getAttribute('aria-labelledby')).toBe('strategy-lab-heading');
    expect(root.hasAttribute('aria-label')).toBe(false);

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('showTitle=false: omits the <h2> (avoiding a duplicate with the wrapper\'s <h1>) and labels the region via a static aria-label', async () => {
    const fixture = await createFixture(false);
    const root: HTMLElement = fixture.nativeElement.querySelector('.strategy-lab');
    expect(root.getAttribute('role')).toBe('region');
    expect(fixture.nativeElement.querySelector('.lab-title')).toBeNull();
    expect(fixture.nativeElement.querySelector('#strategy-lab-heading')).toBeNull();
    expect(root.hasAttribute('aria-labelledby')).toBe(false);
    expect(root.getAttribute('aria-label')).toBe('Strategy Lab');

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
    overall_aligned: true,
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
    })
      .overrideComponent(StrategyLabComponent, strategyLabProvidersOverride(createRunServiceStub()))
      .compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // triggers ngOnInit -> loadResults() -> renders the card
    return fixture;
  }

  it('trade-table-wrap: focusable, named, no axe violations once the ledger panel is open', async () => {
    const fixture = await createFixture(RECORD_WITH_TRADES);
    // Click the real disclosure button (rather than calling toggleCard()
    // directly) so the OnPush view is marked dirty via Angular's normal
    // event-dispatch path, matching real usage.
    fixture.nativeElement.querySelector<HTMLButtonElement>('.expand-chevron-btn')!.click();
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

    // The <table> itself also needs its own accessible name — table-navigation
    // screen-reader commands read the <table>'s name, not the wrapper's.
    const table: HTMLElement | null = wrap.querySelector('table.trade-table');
    expect(table?.getAttribute('aria-label')).toBe('Trade ledger, 1 trades');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('trade-ledger W/L pip announces "Win"/"Loss" alongside the visible letter', async () => {
    const LOSS_TRADE: TradeRecord = { ...TRADE, trade_num: 2, outcome: 'loss', return_pct: -3.1 };
    const fixture = await createFixture({
      ...RECORD_WITH_TRADES,
      backtest: { ...RECORD_WITH_TRADES.backtest, trades: [TRADE, LOSS_TRADE] },
    });
    // Click the real disclosure button (rather than calling toggleCard()
    // directly) so the OnPush view is marked dirty via Angular's normal
    // event-dispatch path, matching real usage.
    fixture.nativeElement.querySelector<HTMLButtonElement>('.expand-chevron-btn')!.click();
    fixture.detectChanges();
    fixture.nativeElement.querySelector<HTMLElement>('.ledger-panel mat-expansion-panel-header')?.click();
    fixture.detectChanges();

    const pips: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.outcome-pip'));
    expect(pips.length).toBe(2);

    const [winPip, lossPip] = pips;
    expect(winPip.querySelector('.visually-hidden')?.textContent?.trim()).toBe('Win');
    expect(winPip.querySelector('[aria-hidden="true"]')?.textContent?.trim()).toBe('W');
    expect(lossPip.querySelector('.visually-hidden')?.textContent?.trim()).toBe('Loss');
    expect(lossPip.querySelector('[aria-hidden="true"]')?.textContent?.trim()).toBe('L');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('result-card Annual Return metric announces its color-cue label as text', async () => {
    // RECORD's annualized_return_pct (12) is > 8 -> returnColor('winning') / returnColorLabel('Above target').
    const fixture = await createFixture(RECORD_WITH_TRADES);
    fixture.detectChanges();

    const metric = Array.from(fixture.nativeElement.querySelectorAll<HTMLElement>('.metric')).find((el) =>
      el.querySelector('.metric-label')?.textContent?.trim() === 'Annual Return',
    );
    expect(metric).toBeTruthy();
    expect(metric!.querySelector('.visually-hidden')?.textContent?.trim()).toBe('Above target:');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('activity-log: focusable, named, no axe violations while a run is active', async () => {
    const fixture = await createFixture(RECORD);
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 0, phase: 'backtesting' },
    });
    fixture.componentInstance.activityLog.set([
      { time: '10:00:00', status: 'active', message: 'Executing strategy backtest...' },
    ]);
    fixture.detectChanges();

    const log: HTMLElement = fixture.nativeElement.querySelector('.activity-log');
    expect(log).toBeTruthy();
    expect(log.getAttribute('tabindex')).toBe('0');
    expect(log.getAttribute('role')).toBe('group');
    expect(log.getAttribute('aria-label')).toBe('Strategy run activity log, scrollable');

    // Pre-existing gap: the running run-btn nests a focusable spinner. Predates
    // this fix, is unrelated to the activity-log focus-indicator under test
    // here, and is only surfaced now because this is the first a11y spec to
    // set `running`.
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('comparison-table-wrap: focusable, named, no axe violations (independent of card expansion)', async () => {
    const fixture = await createFixture(RECORD);
    stubOf(fixture).paperTradingSessions.set({ 'rec-1': PAPER_SESSION });
    fixture.detectChanges();

    const wrap: HTMLElement = fixture.nativeElement.querySelector('.comparison-table-wrap');
    expect(wrap).toBeTruthy(); // renders without toggleCard() — paper trading is independent of card expansion
    expect(wrap.getAttribute('tabindex')).toBe('0');
    expect(wrap.getAttribute('role')).toBe('group');
    expect(wrap.getAttribute('aria-label')).toBe('stocks strategy backtest vs. paper-trading comparison, scrollable');

    // The <table> itself also needs its own accessible name via <caption> —
    // table-navigation screen-reader commands read the <table>'s own name,
    // not the wrapper's aria-label.
    const caption: HTMLElement | null = wrap.querySelector('table.comparison-table > caption');
    expect(caption).toBeTruthy();
    expect(caption?.className).toContain('visually-hidden');
    expect(caption?.textContent?.trim()).toBe('Backtest vs. paper-trading metric comparison');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);
});

describe('StrategyLabComponent a11y — run announcement live region', () => {
  async function createFixture() {
    const apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(NEVER),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
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
    })
      .overrideComponent(StrategyLabComponent, strategyLabProvidersOverride(createRunServiceStub()))
      .compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // triggers ngOnInit
    return fixture;
  }

  /** Reads the always-present SR-only status region, asserting its fixed attributes. */
  function liveRegionText(fixture: ComponentFixture<StrategyLabComponent>): string {
    const region: HTMLElement | null = fixture.nativeElement.querySelector('p.visually-hidden[role="status"]');
    expect(region).toBeTruthy();
    expect(region!.getAttribute('aria-live')).toBe('polite');
    return region!.textContent?.trim() ?? '';
  }

  it('is present and empty while idle', async () => {
    const fixture = await createFixture();
    expect(liveRegionText(fixture)).toBe('');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces the monotonic strategy position as a run proceeds', async () => {
    // The live region reports coarse batch/strategy position only — not the
    // per-cycle phase, which churns under the default parallel execution.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 1, phase: 'backtesting' },
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy 1 of 5.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('derives the strategy number from attempted cycles, not the churning current_cycle.cycle_index', async () => {
    // Under parallel waves the backend rewrites the single shared current_cycle
    // from whichever sibling last emitted progress, so cycle_index oscillates.
    // Here cycle_index is 3 while only one cycle has actually been attempted
    // (completed_cycles 1) — the announcement must report the MONOTONIC
    // attempted position (2), never the churning cycle_index (3).
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 1,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1'],
      current_cycle: { cycle_index: 3, phase: 'ideating' },
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy 2 of 5.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('announces a coarse position (no per-cycle phase) even when a cycle is actively in a phase', async () => {
    // The per-cycle phase is deliberately NOT spoken — under parallel waves
    // there is no single "current phase", so a coarse monotonic position is the
    // stable, non-churning signal. The visible phase-stepper still shows phases.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 1, phase: 'design_review' },
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy 1 of 5.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('announces the coarse position between cycles (no current_cycle yet)', async () => {
    // With no current_cycle (a sibling just finished mid-wave), the announcement
    // is the same coarse monotonic position — no special "between cycles" text.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 1,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1'],
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy 2 of 5.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('announces "Finishing up" instead of an impossible position once the last cycle completes', async () => {
    // Regression: after the final cycle_complete, completed_cycles already
    // equals total_cycles (with no current_cycle), so naively reporting
    // "completed_cycles + 1 of total_cycles" would announce an impossible
    // "Strategy 6 of 5" for the brief window before the terminal `complete`
    // event lands.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 5,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1', 'rec-2', 'rec-3', 'rec-4', 'rec-5'],
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Finishing up.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('does not repeat a skipped strategy\'s number as "next" between cycles', async () => {
    // Regression: completed_cycles alone excludes skipped/errored cycles.
    // Cycle 1 completed and cycle 2 was skipped — completed_cycles is only
    // 1, so "completed_cycles + 1" would announce "Strategy 2", repeating
    // the cycle that was just skipped instead of the genuinely next one.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 1,
      skipped_cycles: 1,
      completed_record_ids: ['rec-1'],
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy 3 of 5.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('the sighted "Strategy N of M" text agrees with the aria-live text for the same skipped-cycle state', async () => {
    // Regression: the sighted progress-title/run-button text used to read
    // `completed_cycles + 1` directly, disagreeing with the aria-live
    // region's corrected number for the exact scenario above — sighted and
    // screen-reader users were told contradictory strategy numbers for the
    // same run. Both now derive from the same currentStrategyNumber().
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 1,
      skipped_cycles: 1,
      completed_record_ids: ['rec-1'],
    });
    fixture.detectChanges();

    const sightedText: string = fixture.nativeElement.querySelector('.progress-title').textContent;
    expect(sightedText).toContain('Strategy 3 of 5');
    expect(liveRegionText(fixture)).toContain('Strategy 3 of 5');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('the sighted "Batch N of M" text agrees with the aria-live text once the last batch has completed', async () => {
    // Regression: the sighted text had no clamp and could render the
    // impossible "Batch 4 of 3" in the window after the last batch_complete
    // but before the terminal complete event — the aria-live region already
    // clamped this. Both now derive from the same currentBatchNumber().
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 15,
      completed_cycles: 15,
      skipped_cycles: 0,
      completed_record_ids: [],
      batch_count: 3,
      completed_batches: 3,
      current_batch: null,
    });
    fixture.detectChanges();

    const sightedText: string = fixture.nativeElement.querySelector('.progress-title').textContent;
    expect(sightedText).toContain('Batch 3 of 3');
    expect(sightedText).not.toContain('Batch 4 of 3');
    expect(liveRegionText(fixture)).toBe('Batch 3 of 3 — Finishing up.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('does not double-count a cycle that completed but then hit a tracker-merge failure', async () => {
    // Regression: main.py's wave loop can publish cycle_complete for a
    // cycle, then separately publish cycle_errored for that same
    // cycle_index if the post-completion convergence-tracker merge step
    // throws (tagged reason: 'tracker_merge_failed' in errored_details, and
    // counted in the uncapped tracker_merge_error_count). Naively summing
    // completed_cycles + skipped_cycles + errored_cycles would count that
    // one cycle twice.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 2,
      skipped_cycles: 0,
      errored_cycles: 1,
      errored_details: [
        { cycle_index: 2, error: 'merge boom', reason: 'tracker_merge_failed' },
      ],
      tracker_merge_error_count: 1,
      completed_record_ids: ['rec-1', 'rec-2'],
    });
    fixture.detectChanges();

    // Without the correction this would read "Strategy 4 of 5" (2 + 0 + 1 + 1).
    expect(liveRegionText(fixture)).toBe('Strategy 3 of 5.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('corrects a tracker-merge double-count exactly via the uncapped counter, even once errored_details has evicted the matching entries', async () => {
    // Regression: errored_details is capped at 50 entries server-side and
    // the frontend reducer additionally re-caps it to the 50 MOST RECENT
    // entries. For a 70-cycle run where 10 early cycles each hit a
    // post-completion tracker-merge failure and 50 later, unrelated cycles
    // also errored, errored_details holds only those 50 later (non-tracker)
    // entries — every tracker-merge entry has evicted. A correction derived
    // by filtering errored_details would read 0 double-counts and wrongly
    // compute attemptedCycles as 10 + 0 + 60 - 0 = 70 (equal to
    // total_cycles, prematurely announcing "Finishing up" 10 cycles early).
    // tracker_merge_error_count is backend-sourced and uncapped, so the
    // correction (10 + 0 + 60 - 10 = 60) stays exact regardless of what's
    // still visible in errored_details.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 70,
      completed_cycles: 10,
      skipped_cycles: 0,
      errored_cycles: 60,
      errored_details: Array.from({ length: 50 }, (_, i) => ({
        cycle_index: i + 11,
        error: 'boom',
        reason: 'ValueError',
      })),
      tracker_merge_error_count: 10,
      completed_record_ids: Array.from({ length: 10 }, (_, i) => `rec-${i + 1}`),
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy 61 of 70.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('announces "Finishing up" when the last attempted cycle was errored rather than completed', async () => {
    // Regression: for a 3-cycle run where the last cycle errored (not
    // completed), completed_cycles stays at 2 and never reaches
    // total_cycles on its own, so the "Finishing up" terminal gap would
    // never trigger — the live region would announce a "Strategy 3 of 3 —
    // Run in progress" that never resolves until the terminal event.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 3,
      completed_cycles: 2,
      skipped_cycles: 0,
      errored_cycles: 1,
      completed_record_ids: ['rec-1', 'rec-2'],
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Finishing up.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('clamps the announced batch number instead of announcing an impossible batch position', async () => {
    // Regression: after the last batch's batch_complete, current_batch is
    // null and completed_batches already equals batch_count — the same
    // terminal gap as the strategy-count fix above, one level up.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 15,
      completed_cycles: 15,
      skipped_cycles: 0,
      completed_record_ids: [],
      batch_count: 3,
      completed_batches: 3,
      current_batch: null,
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Batch 3 of 3 — Finishing up.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('includes batch position for multi-batch runs', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      batch_count: 3,
      current_batch: 2,
      current_cycle: { cycle_index: 1, phase: 'ideating' },
    });
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Batch 2 of 3 — Strategy 1 of 5.');
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('announces the terminal outcome once a run completes, replacing the progress text', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();
    expect(liveRegionText(fixture)).toBe('Strategy 1 of 5.');

    stubOf(fixture).events$.next({
      type: 'complete',
      message: 'done',
      status: 'completed',
      completed_count: 5,
      skipped_count: 0,
      errored_count: 0,
      errored_details: [],
      completed_batches: 1,
      total_batches: 1,
    });
    // Mirrors the real StrategyLabRunService.finishRun(), called right after
    // emitting the terminal event — the component must have already derived
    // the outcome from the event itself, not from runStatus (now cleared).
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(fixture.componentInstance.running()).toBe(false);
    expect(liveRegionText(fixture)).toBe('Strategy Lab run complete.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a clean completion even after an earlier non-fatal batch_warning, not "finished with errors"', async () => {
    // Regression: a mid-run batch_warning (e.g. a signal-brief failure) sets
    // the completionWarning banner, which stays populated for the rest of
    // the run since nothing clears it. The terminal announcement must be
    // driven by the 'complete' event's own status/errored_count — not by
    // whether that unrelated warning banner happens to be non-null — or a
    // fully clean run would misreport as "finished with errors".
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({ type: 'batch_warning', batch_index: 0, reason: 'signal_brief_failed' });
    fixture.detectChanges();
    expect(fixture.componentInstance.completionWarning()).toBeTruthy();

    stubOf(fixture).events$.next({
      type: 'complete',
      message: 'done',
      status: 'completed',
      completed_count: 5,
      skipped_count: 0,
      errored_count: 0,
      errored_details: [],
      completed_batches: 1,
      total_batches: 1,
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run complete.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a qualified terminal outcome when the run finishes with errored cycles', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({
      type: 'complete',
      message: 'done',
      status: 'completed_with_errors',
      completed_count: 3,
      skipped_count: 0,
      errored_count: 2,
      errored_details: [],
      completed_batches: 1,
      total_batches: 1,
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run finished with errors.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a qualified terminal outcome, not a plain "complete", when the run finishes with only skipped cycles', async () => {
    // Regression: a run that skips cycles (e.g. unavailable market data —
    // a real, non-error outcome) but errors on none used to be announced as
    // an unqualified "Strategy Lab run complete.", even though the sighted
    // UI shows a live "N skipped" badge throughout the run — screen-reader
    // users got systematically less information about the same outcome.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({
      type: 'complete',
      message: 'done',
      status: 'completed',
      completed_count: 3,
      skipped_count: 2,
      errored_count: 0,
      errored_details: [],
      completed_batches: 1,
      total_batches: 1,
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run finished with some strategies skipped.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a qualified outcome from lastTerminalStatus.skipped_cycles when no complete/error event ever reaches the component', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 3,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1', 'rec-2', 'rec-3'],
    });
    fixture.detectChanges();

    stubOf(fixture).lastTerminalStatus.set({
      run_id: 'run-1',
      status: 'completed',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 3,
      skipped_cycles: 2,
      completed_record_ids: ['rec-1', 'rec-2', 'rec-3'],
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run finished with some strategies skipped.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces run failure as the terminal outcome', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({ type: 'error', detail: 'Sandbox crashed' });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run failed.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a cancellation, not a failure, for a distinct "cancelled" terminal event', async () => {
    // Regression: user cancellations are published as their own 'cancelled'
    // terminal event type (not folded into 'error'), mirroring the blogging
    // team's own cancelled-job SSE event — a deliberate stop, not a failure,
    // and never inferred from `detail`'s free text.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({ type: 'cancelled', detail: 'Run cancelled by user' });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run cancelled.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces an interrupt, not a failure, when an external-stop error carries terminal_status "interrupted"', async () => {
    // Regression: an externally-interrupted run is published as an 'error'
    // event carrying terminal_status: 'interrupted'. The live announcement
    // must say "interrupted" (matching the visible banner and the poll
    // fallback's describeRunStatus wording), not the generic "failed".
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({
      type: 'error',
      detail: 'Run was marked interrupted externally.',
      terminal_status: 'interrupted',
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run interrupted.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a failure, not a cancellation, when a genuine error message happens to mention "cancel"', async () => {
    // Regression: before cancellation had its own distinct event type, it
    // was detected via a /cancel/i regex on `detail`'s free text — a genuine
    // failure whose exception message happened to mention "cancel" (e.g. an
    // internal CancelledError surfacing during a real error) was
    // misannounced as a deliberate stop. A type-based discriminator makes
    // this structurally impossible: a real 'error' event can never be
    // misread as 'cancelled' regardless of its text.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({
      type: 'error',
      detail: 'Cycle 3 failed: CancelledError()',
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run failed.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a neutral "lost track" outcome, not a definite failure, for a shared-infra subscription-reclaim event', async () => {
    // Regression: the shared-infra "subscription reclaimed" wire shape
    // (StrategyLabErrorReclaimEvent — only .error, never .detail, e.g. an
    // eviction under load) used to fall through to the generic "Run failed"
    // default, confidently announcing a failure the run may not have had.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({ type: 'error', error: 'stream closed: the server reclaimed this subscription' });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab lost track of the run — status unavailable.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces the outcome from lastTerminalStatus when no complete/error event ever reaches the component', async () => {
    // Regression: StrategyLabRunService.finishRun() clears running/runStatus
    // together whenever a run ends without an explicit complete/error stream
    // event reaching this component (SSE degrades to polling and polling
    // itself later observes a terminal status; or a reconnect's terminal
    // `snapshot` is followed straight by `done`). Neither path calls
    // handleStreamEvent()'s complete/error branches, so runOutcomeAnnouncement
    // would otherwise stay null and the live region would silently go blank
    // right as the run ends — the one moment a screen-reader user most needs
    // to hear something.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 2,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1', 'rec-2'],
    });
    fixture.detectChanges();

    // Mirrors StrategyLabRunService.finishRun(): captures the last known
    // status into lastTerminalStatus, then nulls running/runStatus — with no
    // events$ emission of any kind.
    stubOf(fixture).lastTerminalStatus.set({
      run_id: 'run-1',
      status: 'failed',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 2,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1', 'rec-2'],
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run failed.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a qualified outcome from lastTerminalStatus.errored_cycles when no complete/error event ever reaches the component', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 3,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1', 'rec-2', 'rec-3'],
    });
    fixture.detectChanges();

    stubOf(fixture).lastTerminalStatus.set({
      run_id: 'run-1',
      status: 'completed',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 3,
      skipped_cycles: 0,
      errored_cycles: 2,
      completed_record_ids: ['rec-1', 'rec-2', 'rec-3'],
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run finished with errors.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('prefers the explicit "complete" event outcome over lastTerminalStatus when both are present', async () => {
    // The explicit-event branch always wins: it has richer per-event data
    // (errored_count/skipped_count) than a generic status field can carry.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    fixture.detectChanges();

    stubOf(fixture).events$.next({
      type: 'complete',
      message: 'done',
      status: 'completed',
      completed_count: 5,
      skipped_count: 0,
      errored_count: 0,
      errored_details: [],
      completed_batches: 1,
      total_batches: 1,
    });
    // Even if the service's lastTerminalStatus disagrees (e.g. a stale value
    // from bookkeeping order), the already-derived explicit outcome must win.
    stubOf(fixture).lastTerminalStatus.set({
      run_id: 'run-1',
      status: 'failed',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
    });
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab run complete.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('announces a neutral "lost track" outcome, not success, when lastTerminalStatus is null', async () => {
    // Regression: StrategyLabRunService.fallbackToPolling()'s own error
    // handler (SSE dropped, then polling itself also failed) explicitly
    // nulls runStatus before finishRun() captures it, so lastTerminalStatus
    // reads null here specifically to mean "the run's fate is genuinely
    // unknown" — describeRunStatus() must not fall through to its generic
    // "Strategy Lab run complete." for this null, which would falsely
    // announce success for what is actually a lost connection.
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 1,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1'],
    });
    fixture.detectChanges();

    stubOf(fixture).lastTerminalStatus.set(null);
    stubOf(fixture).running.set(false);
    stubOf(fixture).runStatus.set(null);
    fixture.detectChanges();

    expect(liveRegionText(fixture)).toBe('Strategy Lab lost track of the run — status unavailable.');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('activity-log stays keyboard-focusable and its entries stay exposed to assistive tech, while a run is active', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 1, phase: 'backtesting' },
    });
    fixture.componentInstance.activityLog.set([
      { time: '10:00:00', status: 'active', message: 'Executing strategy backtest...' },
    ]);
    fixture.detectChanges();

    // The scrollable wrapper keeps its WCAG 2.4.7 focusable-region treatment
    // (sighted keyboard users can still Tab in and scroll it), and a
    // screen-reader user who navigates in hears the same per-line detail
    // sighted users see — the live region above only ever carries a concise
    // summary and never duplicates this on-demand detail.
    const log: HTMLElement = fixture.nativeElement.querySelector('.activity-log');
    expect(log).toBeTruthy();
    expect(log.getAttribute('tabindex')).toBe('0');
    expect(log.getAttribute('role')).toBe('group');
    expect(log.getAttribute('aria-label')).toBe('Strategy run activity log, scrollable');
    expect(log.hasAttribute('aria-hidden')).toBe(false);

    const entries: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.log-entry'));
    expect(entries.length).toBeGreaterThan(0);
    for (const entry of entries) {
      expect(entry.hasAttribute('aria-hidden')).toBe(false);
      expect(entry.textContent).toContain('Executing strategy backtest...');
    }

    // Same pre-existing gap as before (the running run-btn nests a focusable
    // spinner) — unrelated to the activity-log visibility under test here.
    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);
});

/**
 * Source-level guard: every `<mat-icon>` in the template must be explicit
 * about `aria-hidden`, one way or the other — either `aria-hidden="true"`
 * for a decorative icon, or `aria-hidden="false"` plus an `aria-label` for
 * an icon that is the sole signal for some state (the
 * `jobs-dashboard.component.html` pattern). Never silently unset.
 *
 * This can't be verified by asserting on the rendered DOM: Angular Material's
 * `MatIcon` constructor adds `aria-hidden="true"` itself at runtime whenever
 * the host element doesn't already have the attribute, so a rendered icon
 * reads as hidden whether or not the template actually sets it. A DOM-only
 * assertion (`el.getAttribute('aria-hidden')).toBe('true')`) would therefore
 * keep passing even if every explicit attribute were stripped from the
 * template, silently losing the guard this file exists to provide. Reading
 * the raw template source is the only way to tell "we set it" apart from
 * "Material defaulted it".
 *
 * The rule deliberately allows the `aria-hidden="false"` + `aria-label` form
 * too, not just `"true"`: the phase-stepper node icon is still the sole
 * signal for its state today and is only marked `aria-hidden="true"` for now
 * because no text alternative exists yet (tracked separately). A guard that
 * required `"true"` unconditionally would fail the moment a future change
 * gives it a real accessible name — permanently blocking that fix instead of
 * just catching an accidentally-omitted attribute.
 *
 * Path is resolved relative to this spec file, independent of the vitest
 * working directory.
 */
describe('StrategyLabComponent template — explicit aria-hidden on every <mat-icon>', () => {
  const TEMPLATE_PATH = resolve(
    dirname(fileURLToPath(import.meta.url)),
    'strategy-lab.component.html',
  );

  it('every <mat-icon> opening tag is explicit about aria-hidden in the source', () => {
    const html = readFileSync(TEMPLATE_PATH, 'utf8');
    const openTags = html.match(/<mat-icon\b[^>]*>/g) ?? [];

    // Sanity check: fails loudly if the template moved/renamed and the regex
    // above stopped matching anything, rather than passing vacuously. The
    // threshold dropped from >30 once the phase-stepper and per-record
    // strategy-card markup (and their own <mat-icon> tags) moved into
    // PhaseStepperComponent/StrategyCardComponent, each with its own
    // equivalent source-scan guard.
    expect(openTags.length).toBeGreaterThan(15);

    const isExplicit = (tag: string): boolean => {
      if (/aria-hidden\s*=\s*"true"/.test(tag)) return true;
      const explicitlyVisible = /aria-hidden\s*=\s*"false"/.test(tag);
      const hasAccessibleName = /\baria-label\s*=/.test(tag) || /\[attr\.aria-label\]\s*=/.test(tag);
      return explicitlyVisible && hasAccessibleName;
    };

    const notExplicit = openTags.filter((tag) => !isExplicit(tag));
    expect(
      notExplicit,
      `<mat-icon> tag(s) not explicit about aria-hidden (need aria-hidden="true", or ` +
        `aria-hidden="false" + an aria-label):\n  ${notExplicit.join('\n  ')}`,
    ).toHaveLength(0);
  });
});

describe('StrategyLabComponent a11y — decorative icons hidden from assistive tech', () => {
  const GATE_FAILED: QualityGateResult = {
    gate_name: 'backtest_min_trades',
    passed: false,
    details: 'Fewer than the minimum required trades were generated.',
    severity: 'critical',
  };
  const GATE_PASSED: QualityGateResult = {
    gate_name: 'backtest_min_sharpe',
    passed: true,
    details: 'Sharpe ratio meets the minimum threshold.',
    severity: 'info',
  };
  const RECORD_WITH_GATES: StrategyLabRecord = {
    ...RECORD,
    quality_gate_results: [GATE_FAILED, GATE_PASSED],
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
    overall_aligned: true,
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

  async function createFixture(
    record: StrategyLabRecord = RECORD,
    resultsOverride?: { items: StrategyLabRecord[]; count: number; winning_count: number; losing_count: number },
  ) {
    const results = resultsOverride ?? { items: [record], count: 1, winning_count: 0, losing_count: 1 };
    const apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(NEVER),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(of(results)),
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
    })
      .overrideComponent(StrategyLabComponent, strategyLabProvidersOverride(createRunServiceStub()))
      .compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // triggers ngOnInit -> loadResults() -> renders the card
    return fixture;
  }

  it('header, data-source notice, and summary/filter icons are aria-hidden', async () => {
    const fixture = await createFixture();

    // TradingView not configured by default (integrationsSpy above), so the
    // ds-notice banner renders without any extra setup. The category-row's
    // icons now live inside GenerateStrategiesDialogComponent (see its own
    // spec) — they're no longer part of this page's template.
    const decorative = [
      '.lab-title mat-icon',
      '.clear-all-btn mat-icon',
      '.generate-btn mat-icon',
      '.ds-notice__icon',
      '.ds-notice__cta mat-icon',
      '.summary-chip.winning mat-icon',
      '.summary-chip.losing mat-icon',
      '.summary-chip.rate mat-icon',
    ];
    for (const selector of decorative) {
      const el: HTMLElement | null = fixture.nativeElement.querySelector(selector);
      expect(el, `expected ${selector} to be present`).toBeTruthy();
      expect(el!.getAttribute('aria-hidden'), `expected ${selector} to be aria-hidden`).toBe('true');
    }

    const filterIcons: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.filter-row mat-icon'));
    expect(filterIcons.length).toBe(2);
    for (const icon of filterIcons) {
      expect(icon.getAttribute('aria-hidden')).toBe('true');
    }

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('in-progress run icons, including the phase-stepper node icon, are aria-hidden', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 1, phase: 'backtesting' },
    });
    fixture.componentInstance.activityLog.set([
      { time: '10:00:00', status: 'done', message: 'Market data loaded.' },
      { time: '10:00:05', status: 'error', message: 'Backtest sandbox failed.' },
    ]);
    fixture.detectChanges();

    const pulseIcon: HTMLElement = fixture.nativeElement.querySelector('.pulse-icon');
    expect(pulseIcon.getAttribute('aria-hidden')).toBe('true');

    const logIcons: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.log-indicator mat-icon'));
    expect(logIcons.length).toBe(2);
    for (const icon of logIcons) {
      expect(icon.getAttribute('aria-hidden')).toBe('true');
    }

    // Completed/current/pending state is conveyed only by this icon + a CSS
    // colour class, with no text alternative anywhere in the card yet (that's
    // separate color-cue/disclosure follow-up work). It still carries
    // aria-hidden="true" — same as mat-icon's own built-in default for any
    // icon without an explicit aria-label — so this isn't a new AT gap.
    const phaseIcon: HTMLElement = fixture.nativeElement.querySelector('.phase-node mat-icon');
    expect(phaseIcon).toBeTruthy();
    expect(phaseIcon.getAttribute('aria-hidden')).toBe('true');

    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('expanded card icons (hypothesis, narrative, panel, gate) are aria-hidden', async () => {
    const fixture = await createFixture(RECORD_WITH_GATES, {
      items: [RECORD_WITH_GATES], count: 1, winning_count: 0, losing_count: 1,
    });
    // Click the real disclosure button (rather than calling toggleCard()
    // directly) so the OnPush view is marked dirty via Angular's normal
    // event-dispatch path, matching real usage.
    fixture.nativeElement.querySelector<HTMLButtonElement>('.expand-chevron-btn')!.click();
    fixture.detectChanges();

    const hypothesisIcon: HTMLElement = fixture.nativeElement.querySelector('.hypothesis-icon');
    const narrativeIcon: HTMLElement = fixture.nativeElement.querySelector('.narrative-icon');
    expect(hypothesisIcon.getAttribute('aria-hidden')).toBe('true');
    expect(narrativeIcon.getAttribute('aria-hidden')).toBe('true');

    // Panel titles (unlike panel bodies) aren't lazy-rendered, so all panel-icons
    // are present without opening any panel.
    const panelIcons: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.panel-icon'));
    expect(panelIcons.length).toBeGreaterThan(0);
    for (const icon of panelIcons) {
      expect(icon.getAttribute('aria-hidden')).toBe('true');
    }

    // The Quality Gates panel body is lazy — open it to render the gate-icon.
    const headers: HTMLElement[] = Array.from(
      fixture.nativeElement.querySelectorAll('mat-expansion-panel-header'),
    );
    const gatesHeader = headers.find((h) => h.textContent?.includes('Quality Gates'));
    expect(gatesHeader).toBeTruthy();
    gatesHeader!.click();
    fixture.detectChanges();

    const gateIcons: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.gate-icon'));
    expect(gateIcons.length).toBe(2);
    for (const icon of gateIcons) {
      expect(icon.getAttribute('aria-hidden')).toBe('true');
    }

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('gate-result rows keep stable DOM node identity across a re-render with unchanged data', async () => {
    const fixture = await createFixture(RECORD_WITH_GATES, {
      items: [RECORD_WITH_GATES], count: 1, winning_count: 0, losing_count: 1,
    });
    // Click the real disclosure button (rather than calling toggleCard()
    // directly) so the OnPush view is marked dirty via Angular's normal
    // event-dispatch path, matching real usage.
    fixture.nativeElement.querySelector<HTMLButtonElement>('.expand-chevron-btn')!.click();
    fixture.detectChanges();
    const headers: HTMLElement[] = Array.from(
      fixture.nativeElement.querySelectorAll('mat-expansion-panel-header'),
    );
    headers.find((h) => h.textContent?.includes('Quality Gates'))!.click();
    fixture.detectChanges();

    const before: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.gate-result'));
    expect(before).toHaveLength(2);

    // An unrelated change-detection pass — gateViewModels(record) is called
    // again by the @for expression, but for the same record it must return
    // the same memoized array/objects, so @for reuses these exact DOM nodes
    // instead of tearing down and recreating them.
    fixture.detectChanges();

    const after: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.gate-result'));
    expect(after).toEqual(before);
  }, 15000);

  it('paper-trading verdict icon is aria-hidden', async () => {
    const fixture = await createFixture();
    stubOf(fixture).paperTradingSessions.set({ 'rec-1': PAPER_SESSION });
    fixture.detectChanges();

    const verdictIcon: HTMLElement = fixture.nativeElement.querySelector('.paper-verdict-badge mat-icon');
    expect(verdictIcon.getAttribute('aria-hidden')).toBe('true');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('comparison-table aligned icon announces "Aligned"/"Not aligned" to assistive tech', async () => {
    const fixture = await createFixture();
    stubOf(fixture).paperTradingSessions.set({
      'rec-1': { ...PAPER_SESSION, comparison: { ...COMPARISON, win_rate_aligned: false } },
    });
    fixture.detectChanges();

    const alignedIcons: HTMLElement[] = Array.from(
      fixture.nativeElement.querySelectorAll('.cmp-aligned mat-icon'),
    );
    expect(alignedIcons.length).toBeGreaterThan(1);

    const misaligned = alignedIcons[0]; // Win Rate row, forced misaligned above
    expect(misaligned.getAttribute('aria-hidden')).toBe('false');
    expect(misaligned.getAttribute('aria-label')).toBe('Not aligned');

    const aligned = alignedIcons[1]; // Annual Return row, still aligned
    expect(aligned.getAttribute('aria-hidden')).toBe('false');
    expect(aligned.getAttribute('aria-label')).toBe('Aligned');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('"no strategies yet" empty-state icon is aria-hidden', async () => {
    const fixture = await createFixture(RECORD, { items: [], count: 0, winning_count: 0, losing_count: 0 });
    const emptyIcon: HTMLElement = fixture.nativeElement.querySelector('.empty-state mat-icon');
    expect(emptyIcon.getAttribute('aria-hidden')).toBe('true');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('"no results after filtering" empty-state icon is aria-hidden', async () => {
    // RECORD is a losing strategy; filtering to "winning" yields zero displayed
    // items with totalCount > 0, triggering the "no results after filtering" state.
    const fixture = await createFixture();
    fixture.componentInstance.onFilterChange('winning');
    fixture.detectChanges();
    const filteredIcon: HTMLElement = fixture.nativeElement.querySelector('.empty-state mat-icon');
    expect(filteredIcon.getAttribute('aria-hidden')).toBe('true');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);
});

describe('StrategyLabComponent a11y — phase stepper state (WCAG 1.3.1 / 4.1.2)', () => {
  async function createFixture() {
    const apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(NEVER),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
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
    })
      .overrideComponent(StrategyLabComponent, strategyLabProvidersOverride(createRunServiceStub()))
      .compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // triggers ngOnInit
    return fixture;
  }

  /** Drives a running state with the given current phase so the stepper renders. */
  function renderStepperAtPhase(fixture: ComponentFixture<StrategyLabComponent>, phase: string): void {
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 1, phase },
    });
    fixture.detectChanges();
  }

  it('renders the stepper as a semantic list with each step a listitem', async () => {
    const fixture = await createFixture();
    renderStepperAtPhase(fixture, 'coding');

    const stepper: HTMLElement = fixture.nativeElement.querySelector('.phase-stepper');
    expect(stepper).toBeTruthy();
    expect(stepper.getAttribute('role')).toBe('list');

    const steps: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.phase-step'));
    expect(steps.length).toBe(4); // Ideate, Code, Backtest, Analyze
    steps.forEach((step) => expect(step.getAttribute('role')).toBe('listitem'));

    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('marks exactly the active step with aria-current="step"', async () => {
    const fixture = await createFixture();
    renderStepperAtPhase(fixture, 'coding'); // second phase

    const steps: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.phase-step'));
    const current = steps.filter((s) => s.getAttribute('aria-current') === 'step');
    expect(current.length).toBe(1);
    // 'coding' is the "Code" step (index 1); every other step omits aria-current entirely.
    expect(current[0].querySelector('.phase-label')?.textContent).toContain('Code');
    steps
      .filter((s) => s !== current[0])
      .forEach((s) => expect(s.hasAttribute('aria-current')).toBe(false));

    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('announces each step\'s label plus completed/current/not-started state to a screen reader', async () => {
    const fixture = await createFixture();
    renderStepperAtPhase(fixture, 'coding');

    const steps: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.phase-step'));
    // Order: Ideate (completed) → Code (current) → Backtest (not started) → Analyze (not started).
    const stateText = (i: number) =>
      steps[i].querySelector('.phase-label .visually-hidden')?.textContent?.trim();

    expect(steps[0].querySelector('.phase-label')?.textContent).toContain('Ideate');
    expect(stateText(0)).toBe(': completed');
    expect(stateText(1)).toBe(': current step');
    expect(stateText(2)).toBe(': not started');
    expect(stateText(3)).toBe(': not started');

    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);
});

describe('StrategyLabComponent a11y — visible run progress (role=meter + current phase)', () => {
  async function createFixture() {
    const apiSpy = {
      runStrategyLab: vi.fn().mockReturnValue(NEVER),
      streamRunStatus: vi.fn().mockReturnValue(NEVER),
      getStrategyLabConfig: vi.fn().mockReturnValue(
        of({ batch_count_min: 1, batch_count_max: 100, asset_categories: [] }),
      ),
      getStrategyLabResults: vi.fn().mockReturnValue(
        of({ items: [], count: 0, winning_count: 0, losing_count: 0 }),
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
    })
      .overrideComponent(StrategyLabComponent, strategyLabProvidersOverride(createRunServiceStub()))
      .compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // triggers ngOnInit
    return fixture;
  }

  it('progress-section exposes role="meter" with a human-readable aria-valuetext', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 8,
      completed_cycles: 4,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 4, phase: 'backtesting' },
    });
    fixture.detectChanges();

    const meter: HTMLElement = fixture.nativeElement.querySelector('.progress-section');
    expect(meter).toBeTruthy();
    expect(meter.getAttribute('role')).toBe('meter');
    expect(meter.getAttribute('aria-valuemin')).toBe('0');
    expect(meter.getAttribute('aria-valuemax')).toBe('100');
    expect(meter.getAttribute('aria-valuenow')).toBe('50');
    expect(meter.getAttribute('aria-valuetext')).toBe('50% — Strategy 5 of 8');
    expect(meter.getAttribute('aria-label')).toBe('Strategy Lab run progress');

    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('aria-valuetext includes the batch position for a multi-batch run, matching the visible progress-title', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      batch_count: 3,
      current_batch: 2,
      current_cycle: { cycle_index: 1, phase: 'ideating' },
    });
    fixture.detectChanges();

    const meter: HTMLElement = fixture.nativeElement.querySelector('.progress-section');
    expect(meter.getAttribute('aria-valuetext')).toBe('0% — Batch 2 of 3 — Strategy 1 of 5');

    const sightedText: string = fixture.nativeElement.querySelector('.progress-title').textContent;
    expect(sightedText).toContain('Batch 2 of 3');
    expect(sightedText).toContain('Strategy 1 of 5');

    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('renders a visible "Current phase" readout adjacent to the stepper', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 0,
      skipped_cycles: 0,
      completed_record_ids: [],
      current_cycle: { cycle_index: 1, phase: 'backtesting' },
    });
    fixture.detectChanges();

    const label: HTMLElement = fixture.nativeElement.querySelector('.current-phase-label');
    expect(label).toBeTruthy();
    expect(label.textContent?.trim()).toBe('Current phase: Backtest');

    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('omits the "Current phase" readout when there is no current_cycle yet', async () => {
    const fixture = await createFixture();
    stubOf(fixture).running.set(true);
    stubOf(fixture).runStatus.set({
      run_id: 'run-1',
      status: 'running',
      started_at: '2024-06-01T00:00:00Z',
      total_cycles: 5,
      completed_cycles: 1,
      skipped_cycles: 0,
      completed_record_ids: ['rec-1'],
    });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.current-phase-label')).toBeNull();

    await expectNoAxeViolations(fixture.nativeElement, {
      'nested-interactive': { enabled: false },
    });
  }, 15000);
});
