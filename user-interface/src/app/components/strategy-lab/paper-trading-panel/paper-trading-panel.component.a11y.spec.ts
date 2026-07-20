import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import type { PaperTradingComparison, PaperTradingSession, StrategyLabRecord } from '../../../models';
import { expectNoAxeViolations } from '../../../testing/a11y';
import { PaperTradingPanelComponent } from './paper-trading-panel.component';

const RECORD: StrategyLabRecord = {
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
    trades: [],
  } as unknown as StrategyLabRecord['backtest'],
};

const COMPARISON: PaperTradingComparison = {
  backtest_win_rate_pct: 55, paper_win_rate_pct: 52,
  backtest_annualized_return_pct: 12, paper_annualized_return_pct: 11,
  backtest_sharpe_ratio: 1.5, paper_sharpe_ratio: 1.4,
  backtest_max_drawdown_pct: -5, paper_max_drawdown_pct: -6,
  backtest_profit_factor: 1.8, paper_profit_factor: 1.7,
  win_rate_aligned: true, return_aligned: true, sharpe_aligned: true,
  drawdown_aligned: true, profit_factor_aligned: true, overall_aligned: true,
};

const SESSION: PaperTradingSession = {
  session_id: 'sess-1',
  lab_record_id: 'rec-1',
  status: 'completed',
  verdict: 'ready_for_live',
  trades: [],
  data_period_start: '2025-01-01',
  data_period_end: '2025-06-01',
  comparison: COMPARISON,
} as unknown as PaperTradingSession;

describe('PaperTradingPanelComponent a11y', () => {
  async function createFixture() {
    await TestBed.configureTestingModule({
      imports: [PaperTradingPanelComponent, NoopAnimationsModule],
    }).compileComponents();
    const fixture = TestBed.createComponent(PaperTradingPanelComponent);
    fixture.componentInstance.record = RECORD;
    fixture.detectChanges();
    return fixture;
  }

  it('has no axe violations with no session yet (Paper Trade CTA)', async () => {
    const fixture = await createFixture();
    expect(fixture.nativeElement.querySelector('.paper-trade-btn')).toBeTruthy();
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('has no axe violations with a completed session (verdict badge + comparison table)', async () => {
    const fixture = await createFixture();
    fixture.componentRef.setInput('paperSession', SESSION);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.comparison-table')).toBeTruthy();
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('every mat-icon in the panel carries an explicit aria-hidden or aria-label', () => {
    const fixture = TestBed.createComponent(PaperTradingPanelComponent);
    fixture.componentInstance.record = RECORD;
    fixture.componentInstance.paperSession = SESSION;
    fixture.detectChanges();

    const icons: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('mat-icon'));
    expect(icons.length).toBeGreaterThan(0);
    for (const icon of icons) {
      const hasAriaHidden = icon.hasAttribute('aria-hidden');
      const hasAriaLabel = icon.hasAttribute('aria-label');
      expect(hasAriaHidden || hasAriaLabel, `expected ${icon.outerHTML} to declare aria-hidden or aria-label`).toBe(true);
    }
  });

  it('every <mat-icon> opening tag in the template source is explicit about aria-hidden', () => {
    const templatePath = resolve(dirname(fileURLToPath(import.meta.url)), 'paper-trading-panel.component.html');
    const html = readFileSync(templatePath, 'utf8');
    const openTags = html.match(/<mat-icon\b[^>]*>/g) ?? [];

    // Sanity check: fails loudly if the template moved/renamed and the regex
    // above stopped matching anything, rather than passing vacuously.
    expect(openTags.length).toBeGreaterThan(3);

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
