import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import type { StrategyLabRecord } from '../../../models';
import { expectNoAxeViolations } from '../../../testing/a11y';
import { StrategyCardComponent } from './strategy-card.component';

const RECORD: StrategyLabRecord = {
  lab_record_id: 'rec-1',
  is_winning: true,
  is_publishable: true,
  strategy_rationale: 'Momentum persists after a volume breakout.',
  analysis_narrative: 'This strategy performed well across the backtest window.',
  created_at: '2026-01-01T00:00:00Z',
  refinement_rounds: 1,
  quality_gate_results: [
    { gate_name: 'realism_check', passed: true, details: 'Within realistic bounds.', severity: 'info' },
    { gate_name: 'zero_trades', passed: false, details: 'No trades generated.', severity: 'critical', refinement_round: 0 },
  ],
  strategy: {
    strategy_id: 'strat-1',
    authored_by: 'design-agent',
    asset_class: 'stocks',
    hypothesis: 'Stocks with rising volume tend to continue trending.',
    signal_definition: 'volume_zscore > 2',
    timeframe: 'daily',
    entry_rules: [{ indicator: 'volume_zscore', operator: '>', value: 2 }],
    exit_rules: [{ indicator: 'days_held', operator: '>', value: 10 }],
    sizing: { method: 'fixed_fraction', value: 0.1 },
    target_symbols: ['AAPL'],
    risk_limits: {},
    speculative: false,
    requires_redesign: false,
    unparsed_rules: [],
    audit: {},
  } as unknown as StrategyLabRecord['strategy'],
  backtest: {
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
    trades: [
      {
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
      },
    ],
  } as unknown as StrategyLabRecord['backtest'],
};

describe('StrategyCardComponent a11y', () => {
  async function createFixture() {
    await TestBed.configureTestingModule({
      imports: [StrategyCardComponent, NoopAnimationsModule],
    }).compileComponents();
    const fixture = TestBed.createComponent(StrategyCardComponent);
    fixture.componentInstance.record = RECORD;
    fixture.detectChanges();
    return fixture;
  }

  it('has no axe violations collapsed', async () => {
    const fixture = await createFixture();
    expect(fixture.nativeElement.querySelector('.strategy-card')).toBeTruthy();
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('has no axe violations expanded, with quality gates, ledger, and a paper-trade CTA rendered', async () => {
    const fixture = await createFixture();
    fixture.componentRef.setInput('expanded', true);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.card-expanded-region')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.gate-results')).toBeTruthy();
    const table: HTMLElement | null = fixture.nativeElement.querySelector('.trade-table');
    expect(table).toBeTruthy();
    // The <table> itself needs its own accessible name — table-navigation
    // screen-reader commands read the <table>'s name, not the wrapper's.
    expect(table?.getAttribute('aria-label')).toBe('Trade ledger, 1 trades');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('strategy-code panel: shows a "read only" caption and an accessibly-named copy button, axe-clean', async () => {
    const fixture = await createFixture();
    fixture.componentRef.setInput('record', { ...RECORD, strategy_code: 'print("hello")' });
    fixture.componentRef.setInput('expanded', true);
    fixture.detectChanges();

    const caption: HTMLElement | null = fixture.nativeElement.querySelector('.strategy-code-caption-text');
    expect(caption?.textContent?.trim()).toBe('Generated Python — read only');

    const copyBtn: HTMLButtonElement | null = fixture.nativeElement.querySelector('.strategy-code-copy-btn');
    expect(copyBtn).toBeTruthy();
    expect(copyBtn?.getAttribute('aria-label')).toBe('Copy strategy code');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('collapsed: surfaces only the primary metrics (Annual Return, Sharpe, Max DD); secondary metrics stay behind the disclosure', async () => {
    const fixture = await createFixture();

    const metricsRow: HTMLElement = fixture.nativeElement.querySelector('.metrics-row');
    const primaryLabels = Array.from(metricsRow.querySelectorAll('.metric-label')).map((el) => el.textContent?.trim());
    expect(primaryLabels).toEqual(['Annual Return', 'Sharpe', 'Max DD']);

    // Secondary metrics render only once the card is expanded — not present at all while collapsed.
    expect(fixture.nativeElement.querySelector('.secondary-metrics-row')).toBeNull();

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('expanded: surfaces the secondary metrics (Win Rate, Profit Factor, Volatility) without duplicating the primary row', async () => {
    const fixture = await createFixture();
    fixture.componentRef.setInput('expanded', true);
    fixture.detectChanges();

    const secondaryRow: HTMLElement = fixture.nativeElement.querySelector('.secondary-metrics-row');
    expect(secondaryRow).toBeTruthy();
    const secondaryLabels = Array.from(secondaryRow.querySelectorAll('.metric-label')).map((el) => el.textContent?.trim());
    expect(secondaryLabels).toEqual(['Win Rate', 'Profit Factor', 'Volatility']);

    // The primary row is still present and unchanged, not duplicated into the secondary row.
    const primaryLabels = Array.from(
      fixture.nativeElement.querySelector('.metrics-row')!.querySelectorAll('.metric-label'),
    ).map((el) => el.textContent?.trim());
    expect(primaryLabels).toEqual(['Annual Return', 'Sharpe', 'Max DD']);

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('ledger-summary bar is trimmed to Trades/Wins/Losses/Net P&L; Start/End/Capital move into the panel description', async () => {
    const fixture = await createFixture();
    fixture.componentRef.setInput('expanded', true);
    fixture.detectChanges();

    const summary: HTMLElement = fixture.nativeElement.querySelector('.ledger-summary');
    expect(summary).toBeTruthy();
    const summaryLabels = Array.from(summary.querySelectorAll('.ls-label')).map((el) => el.textContent?.trim());
    expect(summaryLabels).toEqual(['Trades', 'Wins', 'Losses', 'Net P&L']);

    // Start/End/Capital are still reachable — relocated into the panel description,
    // visible even before the panel itself is opened.
    const description: HTMLElement = fixture.nativeElement.querySelector('.ledger-panel mat-panel-description');
    expect(description.textContent).toContain('2025-01-01–2025-12-31');
    expect(description.textContent).toContain('Capital: $100,000');

    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('every mat-icon in the card carries an explicit aria-hidden or aria-label', async () => {
    const fixture = await createFixture();
    fixture.componentRef.setInput('expanded', true);
    fixture.detectChanges();

    const icons: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('mat-icon'));
    expect(icons.length).toBeGreaterThan(10);
    for (const icon of icons) {
      const hasAriaHidden = icon.hasAttribute('aria-hidden');
      const hasAriaLabel = icon.hasAttribute('aria-label');
      expect(hasAriaHidden || hasAriaLabel, `expected ${icon.outerHTML} to declare aria-hidden or aria-label`).toBe(true);
    }
  });

  it('every <mat-icon> opening tag in the template source is explicit about aria-hidden', () => {
    const templatePath = resolve(dirname(fileURLToPath(import.meta.url)), 'strategy-card.component.html');
    const html = readFileSync(templatePath, 'utf8');
    const openTags = html.match(/<mat-icon\b[^>]*>/g) ?? [];

    // Sanity check: fails loudly if the template moved/renamed and the regex
    // above stopped matching anything, rather than passing vacuously. Lower
    // than before extraction — the paper-trading section's icons moved to
    // PaperTradingPanelComponent's own template (and its own equivalent
    // check), see paper-trading-panel.component.a11y.spec.ts.
    expect(openTags.length).toBeGreaterThan(5);

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
