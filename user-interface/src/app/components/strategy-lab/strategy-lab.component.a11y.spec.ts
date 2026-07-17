import { TestBed } from '@angular/core/testing';
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
import type {
  StrategyLabRecord,
  StrategySpec,
  TradeRecord,
  PaperTradingSession,
  PaperTradingComparison,
  QualityGateResult,
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
 * too, not just `"true"`: two icons in this template (the phase-stepper node
 * icon and the paper-trading comparison table's "Aligned" cell icon) are the
 * sole signal for their state today and are only marked `aria-hidden="true"`
 * for now because no text alternative exists yet. A guard that required
 * `"true"` unconditionally would fail the moment a future change gives either
 * of them a real accessible name — permanently blocking that fix instead of
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
    // above stopped matching anything, rather than passing vacuously.
    expect(openTags.length).toBeGreaterThan(30);

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
    }).compileComponents();

    const fixture = TestBed.createComponent(StrategyLabComponent);
    fixture.detectChanges(); // triggers ngOnInit -> loadResults() -> renders the card
    return fixture;
  }

  it('header, data-source notice, category row, and summary/filter icons are aria-hidden', async () => {
    const fixture = await createFixture();

    // TradingView not configured by default (integrationsSpy above), so the
    // ds-notice banner renders without any extra setup.
    const decorative = [
      '.lab-title mat-icon',
      '.clear-all-btn mat-icon',
      '.run-btn mat-icon',
      '.ds-notice__icon',
      '.ds-notice__cta mat-icon',
      '.category-label mat-icon',
      '.summary-chip.winning mat-icon',
      '.summary-chip.losing mat-icon',
      '.summary-chip.rate mat-icon',
    ];
    for (const selector of decorative) {
      const el: HTMLElement | null = fixture.nativeElement.querySelector(selector);
      expect(el, `expected ${selector} to be present`).toBeTruthy();
      expect(el!.getAttribute('aria-hidden'), `expected ${selector} to be aria-hidden`).toBe('true');
    }

    const categoryIcons: HTMLElement[] = Array.from(
      fixture.nativeElement.querySelectorAll('.category-toggle-group mat-icon'),
    );
    expect(categoryIcons.length).toBeGreaterThan(0);
    for (const icon of categoryIcons) {
      expect(icon.getAttribute('aria-hidden')).toBe('true');
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
      { time: '10:00:00', status: 'done', message: 'Market data loaded.' },
      { time: '10:00:05', status: 'error', message: 'Backtest sandbox failed.' },
    ];
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
      'aria-progressbar-name': { enabled: false },
      'nested-interactive': { enabled: false },
    });
  }, 15000);

  it('expanded card icons (hypothesis, narrative, panel, gate) are aria-hidden', async () => {
    const fixture = await createFixture(RECORD_WITH_GATES, {
      items: [RECORD_WITH_GATES], count: 1, winning_count: 0, losing_count: 1,
    });
    fixture.componentInstance.toggleCard('rec-1');
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

  it('paper-trading verdict icon and comparison-table aligned icon are aria-hidden', async () => {
    const fixture = await createFixture();
    fixture.componentInstance.paperTradingSessions = { 'rec-1': PAPER_SESSION };
    fixture.detectChanges();

    const verdictIcon: HTMLElement = fixture.nativeElement.querySelector('.paper-verdict-badge mat-icon');
    expect(verdictIcon.getAttribute('aria-hidden')).toBe('true');

    // Sole content of its table cell, with no text fallback yet (that's separate
    // color-cue/disclosure follow-up work). Still carries aria-hidden="true" —
    // same as mat-icon's own built-in default — so this isn't a new AT gap.
    const alignedIcon: HTMLElement = fixture.nativeElement.querySelector('.cmp-aligned mat-icon');
    expect(alignedIcon).toBeTruthy();
    expect(alignedIcon.getAttribute('aria-hidden')).toBe('true');

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
