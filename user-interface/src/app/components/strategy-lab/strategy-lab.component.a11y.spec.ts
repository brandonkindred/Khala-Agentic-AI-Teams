import { TestBed } from '@angular/core/testing';
import { of, NEVER } from 'rxjs';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { IntegrationsApiService } from '../../services/integrations-api.service';
import { StrategyLabComponent } from './strategy-lab.component';
import type { StrategyLabRecord, StrategySpec } from '../../models';
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
