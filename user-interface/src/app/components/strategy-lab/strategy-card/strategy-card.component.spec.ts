import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import type { QualityGateResult, StrategyLabRecord, TradeRecord } from '../../../models';
import { StrategyCardComponent } from './strategy-card.component';

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

describe('StrategyCardComponent', () => {
  let fixture: ComponentFixture<StrategyCardComponent>;
  let component: StrategyCardComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [StrategyCardComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(StrategyCardComponent);
    component = fixture.componentInstance;
    component.record = makeRecord();
  });

  describe('hasSignalBrief', () => {
    it('returns false when signal_intelligence_brief is unset', () => {
      component.record = makeRecord({ signal_intelligence_brief: undefined });
      expect(component.hasSignalBrief()).toBe(false);
    });

    it('returns false when signal_intelligence_brief is an empty object', () => {
      component.record = makeRecord({ signal_intelligence_brief: {} as StrategyLabRecord['signal_intelligence_brief'] });
      expect(component.hasSignalBrief()).toBe(false);
    });

    it('returns true when signal_intelligence_brief has at least one key', () => {
      component.record = makeRecord({
        signal_intelligence_brief: { summary: 'Momentum confirmed by volume.' } as unknown as StrategyLabRecord['signal_intelligence_brief'],
      });
      expect(component.hasSignalBrief()).toBe(true);
    });
  });

  describe('isRemedied / gateIcon / gateSeverityClass', () => {
    beforeEach(() => {
      component.record = makeRecord({ refinement_rounds: 2, quality_gate_results: [] });
    });

    it('a passed gate is never remedied', () => {
      const gate: QualityGateResult = { gate_name: 'g', passed: true, details: '', severity: 'info' };
      expect(component.isRemedied(gate)).toBe(false);
      expect(component.gateIcon(gate)).toBe('check_circle');
    });

    it('a standard failed gate is remedied when a later round exists', () => {
      const gate: QualityGateResult = { gate_name: 'g', passed: false, details: '', severity: 'critical', refinement_round: 0 };
      expect(component.isRemedied(gate)).toBe(true);
      expect(component.gateIcon(gate)).toBe('build_circle');
      expect(component.gateSeverityClass(gate)).toBe('gate-remedied');
    });

    it('a standard failed gate with no later round is not remedied', () => {
      const gate: QualityGateResult = { gate_name: 'g', passed: false, details: '', severity: 'critical', refinement_round: 0 };
      component.record = makeRecord({ refinement_rounds: 0, quality_gate_results: [] });
      expect(component.isRemedied(gate)).toBe(false);
      expect(component.gateIcon(gate)).toBe('cancel');
      expect(component.gateSeverityClass(gate)).toBe('gate-critical');
    });

    // Boundary coverage for the gateRound < maxRound comparison. Backend
    // semantics: refinement_rounds is the 0-indexed round the refinement loop
    // actually reached (len(refinement_attempts) — an entry is only appended
    // once the loop advances past a round with an applied fix), not a count
    // with valid indices 0..refinement_rounds-1. So a gate whose round is
    // strictly less than refinement_rounds had a later round genuinely run
    // after it; a gate whose round equals refinement_rounds failed in the
    // last round the loop reached, with nothing after it.
    it('a gate failing strictly before the last-reached round is remedied', () => {
      const gate: QualityGateResult = { gate_name: 'g', passed: false, details: '', severity: 'critical', refinement_round: 1 };
      component.record = makeRecord({ refinement_rounds: 2, quality_gate_results: [] });
      expect(component.isRemedied(gate)).toBe(true);
    });

    it('a gate failing in the last-reached round itself (gateRound === maxRound) is not remedied', () => {
      const gate: QualityGateResult = { gate_name: 'g', passed: false, details: '', severity: 'critical', refinement_round: 1 };
      component.record = makeRecord({ refinement_rounds: 1, quality_gate_results: [] });
      expect(component.isRemedied(gate)).toBe(false);
    });

    it('a non-critical failed, non-remedied gate reports "warning"', () => {
      const gate: QualityGateResult = { gate_name: 'g', passed: false, details: '', severity: 'warning', refinement_round: 0 };
      component.record = makeRecord({ refinement_rounds: 0, quality_gate_results: [] });
      expect(component.gateIcon(gate)).toBe('warning');
    });

    it('a pre-synthesis gate (refinement_round -1) is remedied by a later passing repair pass', () => {
      const gate: QualityGateResult = { gate_name: 'zero_trades', passed: false, details: '', severity: 'critical', refinement_round: -1 };
      component.record = makeRecord({
        refinement_rounds: 1,
        quality_gate_results: [
          { gate_name: 'zero_trade_repair_zero_trades', passed: true, details: '', severity: 'info', refinement_round: 0 },
        ],
      });
      expect(component.isRemedied(gate)).toBe(true);
    });

    it('a pre-synthesis gate with no matching later pass is not remedied', () => {
      const gate: QualityGateResult = { gate_name: 'zero_trades', passed: false, details: '', severity: 'critical', refinement_round: -1 };
      component.record = makeRecord({ refinement_rounds: 0, quality_gate_results: [] });
      expect(component.isRemedied(gate)).toBe(false);
    });
  });

  describe('gateViewModels (precomputed per-gate template data)', () => {
    it('maps each gate to its icon/severityClass/isRemedied, matching the underlying methods', () => {
      const gates: QualityGateResult[] = [
        { gate_name: 'a', passed: true, details: 'ok', severity: 'info' },
        { gate_name: 'b', passed: false, details: 'bad', severity: 'critical', refinement_round: 0 },
      ];
      component.record = makeRecord({ refinement_rounds: 2, quality_gate_results: gates });

      const viewModels = component.gateViewModels();

      expect(viewModels).toEqual([
        { gate: gates[0], icon: component.gateIcon(gates[0]), severityClass: component.gateSeverityClass(gates[0]), isRemedied: false },
        { gate: gates[1], icon: component.gateIcon(gates[1]), severityClass: component.gateSeverityClass(gates[1]), isRemedied: true },
      ]);
    });

    it('returns the same array reference for the same record (memoized), and a new array once the record changes', () => {
      const record1 = makeRecord({
        refinement_rounds: 0,
        quality_gate_results: [{ gate_name: 'a', passed: true, details: '', severity: 'info' }],
      });
      const record2 = makeRecord({
        refinement_rounds: 0,
        quality_gate_results: [{ gate_name: 'b', passed: true, details: '', severity: 'info' }],
      });

      component.record = record1;
      const first = component.gateViewModels();
      expect(component.gateViewModels()).toBe(first);

      component.record = record2;
      expect(component.gateViewModels()).not.toBe(first);
    });

    it('returns an empty array when a record has no quality_gate_results', () => {
      component.record = makeRecord({ refinement_rounds: 0, quality_gate_results: undefined });
      expect(component.gateViewModels()).toEqual([]);
    });
  });

  describe('truncatedHypothesis', () => {
    it('returns the hypothesis unchanged when 70 characters or fewer', () => {
      component.record = makeRecord({
        strategy: { ...makeRecord().strategy, hypothesis: 'Short hypothesis.' },
      });
      expect(component.truncatedHypothesis()).toBe('Short hypothesis.');
    });

    it('truncates to 70 characters with an ellipsis when longer', () => {
      const hypothesis = 'A'.repeat(80);
      component.record = makeRecord({
        strategy: { ...makeRecord().strategy, hypothesis },
      });
      expect(component.truncatedHypothesis()).toBe('A'.repeat(70) + '…');
    });
  });

  describe('strategyCode', () => {
    it('prefers the top-level strategy_code field', () => {
      component.record = makeRecord({
        strategy_code: 'top-level code',
        strategy: { ...makeRecord().strategy, strategy_code: 'nested code' },
      });
      expect(component.strategyCode()).toBe('top-level code');
    });

    it('falls back to strategy.strategy_code when the top-level field is unset', () => {
      component.record = makeRecord({
        strategy_code: undefined,
        strategy: { ...makeRecord().strategy, strategy_code: 'nested code' },
      });
      expect(component.strategyCode()).toBe('nested code');
    });

    it('returns undefined when neither location has strategy code', () => {
      component.record = makeRecord({ strategy_code: undefined });
      expect(component.strategyCode()).toBeUndefined();
    });
  });

  it('verdictColor covers ready_for_live, not_performant, and inconclusive', () => {
    expect(component.verdictColor('ready_for_live')).toBe('winning');
    expect(component.verdictColor('not_performant')).toBe('losing');
    expect(component.verdictColor(undefined)).toBe('neutral');
  });

  it('returnColor/returnColorLabel classify annualized return', () => {
    expect(component.returnColor(12)).toBe('winning');
    expect(component.returnColorLabel(-1)).toBe('Negative');
  });

  describe('outputs', () => {
    it('emits deleteRequested exactly once when the delete button is clicked', () => {
      fixture.detectChanges();
      const spy = vi.fn();
      component.deleteRequested.subscribe(spy);
      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.delete-strategy-btn');
      btn.click();
      expect(spy).toHaveBeenCalledTimes(1);
    });

    it('emits expandToggled exactly once when the chevron is clicked', () => {
      fixture.detectChanges();
      const spy = vi.fn();
      component.expandToggled.subscribe(spy);
      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.expand-chevron-btn');
      btn.click();
      expect(spy).toHaveBeenCalledTimes(1);
    });

    it('emits pageChanged with the paginator event when the ledger page changes', () => {
      component.record = makeRecord({
        backtest: makeBacktest({ trades: [makeTrade(), makeTrade({ trade_num: 2 })] }),
      });
      component.expanded = true;
      fixture.detectChanges();
      const spy = vi.fn();
      component.pageChanged.subscribe(spy);
      const event = { pageIndex: 1, pageSize: 20, length: 2 };
      component.onPageChange(event);
      expect(spy).toHaveBeenCalledWith(event);
    });

    // Rendering/behavior of the paper-trading panel itself is covered by
    // PaperTradingPanelComponent's own spec; this just guards that its
    // paperTradeRequested output is actually forwarded up through
    // StrategyCardComponent's own output (onRunPaperTrading), not dropped.
    it('forwards paperTradeRequested from the paper-trading panel', () => {
      component.record = makeRecord({ is_winning: true, is_publishable: true });
      fixture.detectChanges();
      const spy = vi.fn();
      component.paperTradeRequested.subscribe(spy);
      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.paper-trade-btn');
      expect(btn).toBeTruthy();
      btn.click();
      expect(spy).toHaveBeenCalledTimes(1);
    });
  });

  describe('copyStrategyCode', () => {
    it('writes the code to the clipboard and flashes strategyCodeCopied', () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      const original = (navigator as { clipboard?: unknown }).clipboard;
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
      try {
        expect(component.strategyCodeCopied).toBe(false);
        component.copyStrategyCode('print("hello")');
        expect(writeText).toHaveBeenCalledWith('print("hello")');
        expect(component.strategyCodeCopied).toBe(true);
      } finally {
        Object.defineProperty(navigator, 'clipboard', { value: original, configurable: true });
      }
    });

    it('swallows a rejected clipboard write instead of leaking an unhandled rejection', async () => {
      const writeText = vi.fn().mockRejectedValue(new Error('permission denied'));
      const original = (navigator as { clipboard?: unknown }).clipboard;
      Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
      try {
        expect(() => component.copyStrategyCode('print("hello")')).not.toThrow();
        expect(component.strategyCodeCopied).toBe(true);
        // Let the rejected promise settle; the .catch() must absorb it.
        await new Promise((resolve) => setTimeout(resolve, 0));
      } finally {
        Object.defineProperty(navigator, 'clipboard', { value: original, configurable: true });
      }
    });

    it('cancels the copy-confirmation timer on destroy', () => {
      component.copyStrategyCode('print("hello")');
      expect(component.strategyCodeCopied).toBe(true);
      expect(component['copyResetTimer']).not.toBeNull();
      fixture.destroy();
      expect(component['copyResetTimer']).toBeNull();
    });
  });

  describe('showTitle / expanded gating', () => {
    // OnPush + a decorator `@Input()`: mutating the property directly (bypassing
    // Angular's own input-binding path, which is what a real `[showTitle]="..."`
    // template binding goes through) doesn't mark the view dirty, so a second
    // `detectChanges()` on the same fixture wouldn't re-render — `setInput`
    // exercises the same dirty-marking path a real bound input change would.
    it('renders an h3 title when showTitle is true, and an h2 when false', () => {
      fixture.componentRef.setInput('showTitle', true);
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('h3[mat-card-title]')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('h2[mat-card-title]')).toBeNull();

      fixture.componentRef.setInput('showTitle', false);
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('h2[mat-card-title]')).toBeTruthy();
      expect(fixture.nativeElement.querySelector('h3[mat-card-title]')).toBeNull();
    });

    it('hides the disclosure region when collapsed and shows it when expanded', () => {
      fixture.componentRef.setInput('expanded', false);
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.card-expanded-region')).toBeNull();

      fixture.componentRef.setInput('expanded', true);
      fixture.detectChanges();
      const region: HTMLElement = fixture.nativeElement.querySelector('.card-expanded-region');
      expect(region).toBeTruthy();
      expect(region.getAttribute('aria-label')).toBe('stocks strategy details');
    });
  });

  describe('rendered template', () => {
    it('renders a winning record with WINNING badge and success styling', () => {
      component.record = makeRecord({ is_winning: true });
      fixture.detectChanges();

      const card: HTMLElement = fixture.nativeElement.querySelector('.strategy-card');
      expect(card.classList.contains('is-winning')).toBe(true);
      expect(fixture.nativeElement.querySelector('.outcome-badge').textContent).toContain('WINNING');
    });

    it('renders a losing record with LOSING badge and failure styling', () => {
      component.record = makeRecord({ is_winning: false });
      fixture.detectChanges();

      const card: HTMLElement = fixture.nativeElement.querySelector('.strategy-card');
      expect(card.classList.contains('is-losing')).toBe(true);
      expect(fixture.nativeElement.querySelector('.outcome-badge').textContent).toContain('LOSING');
    });

    it('disables the delete button while deleting and shows a spinner', () => {
      component.isDeleting = true;
      component.deleteDisabled = true;
      fixture.detectChanges();

      const btn: HTMLButtonElement = fixture.nativeElement.querySelector('.delete-strategy-btn');
      expect(btn.disabled).toBe(true);
      expect(fixture.nativeElement.querySelector('.delete-strategy-btn mat-spinner')).toBeTruthy();
    });

    it('renders the Signal Intelligence panel only when hasSignalBrief() is true', () => {
      component.record = makeRecord({ signal_intelligence_brief: undefined });
      component.expanded = true;
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.signal-panel')).toBeNull();

      fixture.componentRef.setInput('record', makeRecord({
        signal_intelligence_brief: { summary: 'Momentum confirmed by volume.' } as unknown as StrategyLabRecord['signal_intelligence_brief'],
      }));
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.signal-panel')).toBeTruthy();
    });

    it('renders entry/exit rules, sizing, and the signal brief as labeled rows, with no raw JSON dump remaining', () => {
      component.record = makeRecord({
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
        signal_intelligence_brief: {
          summary: 'Momentum confirmed by volume.',
        } as unknown as StrategyLabRecord['signal_intelligence_brief'],
      });
      component.expanded = true;
      fixture.detectChanges();

      // No raw JSON dump remains anywhere in the rules/sizing/signal-brief sections.
      expect(fixture.nativeElement.querySelector('.rule-json')).toBeNull();
      expect(fixture.nativeElement.querySelector('.signal-json')).toBeNull();

      const allText = (el: Element) =>
        Array.from(el.querySelectorAll('dt, dd')).map((n) => n.textContent?.trim());

      const detailSections: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.detail-section'));
      const entrySection = detailSections.find((s) => s.querySelector('strong')?.textContent === 'Entry Rules')!;
      const exitSection = detailSections.find((s) => s.querySelector('strong')?.textContent === 'Exit Rules')!;
      const sizingSection = detailSections.find((s) => s.querySelector('strong')?.textContent === 'Sizing')!;

      expect(allText(entrySection)).toEqual(['Indicator', 'volume_zscore', 'Operator', '>', 'Value', '2']);
      expect(allText(exitSection)).toEqual(['Indicator', 'days_held', 'Operator', '>', 'Value', '10']);
      expect(allText(sizingSection)).toEqual(['Method', 'fixed_fraction', 'Value', '0.1']);

      const signalPanel: HTMLElement = fixture.nativeElement.querySelector('.signal-panel');
      expect(allText(signalPanel)).toEqual(['Summary', 'Momentum confirmed by volume.']);
    });
  });
});
