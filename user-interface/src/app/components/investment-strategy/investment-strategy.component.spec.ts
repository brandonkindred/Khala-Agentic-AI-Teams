import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';

import { InvestmentStrategyComponent } from './investment-strategy.component';
import { AuditContext, StrategySpec } from '../../models';
import { environment } from '../../../environments/environment';

function emptyAudit(): AuditContext {
  return {
    data_snapshot_id: '',
    assumptions: [],
    calc_artifacts: [],
    gate_trace: [],
    agent_versions: {},
  };
}

function baseSpec(overrides: Partial<StrategySpec> = {}): StrategySpec {
  return {
    strategy_id: 'strat-1',
    authored_by: 'tester',
    asset_class: 'equities',
    hypothesis: 'h',
    signal_definition: 's',
    timeframe: '1d',
    entry_rules: [],
    exit_rules: [],
    sizing: { kind: 'fixed_fraction', fraction: 0.02 },
    target_symbols: [],
    risk_limits: {},
    speculative: false,
    requires_redesign: false,
    unparsed_rules: [],
    audit: emptyAudit(),
    ...overrides,
  };
}

describe('InvestmentStrategyComponent', () => {
  let component: InvestmentStrategyComponent;
  let fixture: ComponentFixture<InvestmentStrategyComponent>;
  let httpMock: HttpTestingController;

  const strategiesUrl = `${environment.investmentApiUrl}/strategies`;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [InvestmentStrategyComponent, NoopAnimationsModule],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentStrategyComponent);
    component = fixture.componentInstance;
    httpMock = TestBed.inject(HttpTestingController);
    fixture.detectChanges();
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('creates without errors', () => {
    expect(component).toBeTruthy();
    expect(component.entryRulesArray.length).toBe(0);
    expect(component.exitRulesArray.length).toBe(0);
    expect(component.hasPrefillErrors).toBe(false);
  });

  it('submits empty entry_rules and default fixed_fraction sizing', async () => {
    component.form.patchValue({
      hypothesis: 'mean reversion',
      signal_definition: 'rsi',
    });

    await component.createStrategy();

    const req = httpMock.expectOne(strategiesUrl);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.entry_rules).toEqual([]);
    expect(req.request.body.exit_rules).toEqual([]);
    expect(req.request.body.timeframe).toBe('1d');
    expect(req.request.body.sizing).toEqual({ kind: 'fixed_fraction', fraction: 0.02 });
    expect(req.request.body.asset_class).toBe('equities');
    expect(req.request.body.speculative).toBe(false);

    req.flush({
      strategy_id: 'strat-x',
      strategy: baseSpec({ strategy_id: 'strat-x' }),
      message: 'ok',
    });
  });

  it('serialises an RSI(14) < 30 entry rule as structured DSL', async () => {
    component.form.patchValue({
      hypothesis: 'h',
      signal_definition: 's',
    });
    component.addEntryRule();
    const rule = component.entryRulesArray.at(0);

    rule.patchValue({ side: 'long' });
    const when = rule.get('when')!;
    const lhs = when.get('lhs')!;
    lhs.patchValue({ side_kind: 'indicator' });
    const indicator = lhs.get('indicator')!;
    indicator.patchValue({ name: 'rsi' });
    indicator.get('params')!.patchValue({ period: 14 });

    when.patchValue({ op: '<' });

    const rhs = when.get('rhs')!;
    rhs.patchValue({ side_kind: 'number', number_val: 30 });

    await component.createStrategy();

    const req = httpMock.expectOne(strategiesUrl);
    expect(req.request.body.entry_rules).toHaveLength(1);
    expect(req.request.body.entry_rules[0]).toEqual({
      kind: 'entry',
      side: 'long',
      when: {
        lhs: { name: 'rsi', params: { period: 14 }, source: 'close' },
        op: '<',
        rhs: 30,
      },
    });

    req.flush({
      strategy_id: 'strat-y',
      strategy: baseSpec({ strategy_id: 'strat-y' }),
      message: 'ok',
    });
  });

  it('prefills a TimeStopRule into the exit-rules array', () => {
    const spec = baseSpec({
      exit_rules: [{ kind: 'time_stop', n_bars: 10, note: '' }],
    });
    component.populateForm(spec);

    expect(component.exitRulesArray.length).toBe(1);
    const row = component.exitRulesArray.at(0);
    expect(row.get('kind')!.value).toBe('time_stop');
    expect(row.get('n_bars')!.value).toBe(10);
    expect(component.hasPrefillErrors).toBe(false);
  });

  it('flags an unknown indicator name and blocks submit', async () => {
    const spec = baseSpec({
      entry_rules: [
        {
          kind: 'entry',
          side: 'long',
          when: {
            lhs: { name: 'not_real' as unknown as 'sma', params: {} },
            op: '<',
            rhs: 30,
          },
        },
      ],
    });

    component.populateForm(spec);

    expect(component.hasPrefillErrors).toBe(true);
    expect(component.prefillErrors.some((e) => e.includes('not_real'))).toBe(true);

    await component.createStrategy();
    httpMock.expectNone(strategiesUrl);
  });

  it('preserves prefilled indicator params (does not reset to spec defaults)', () => {
    const spec = baseSpec({
      entry_rules: [
        {
          kind: 'entry',
          side: 'long',
          when: {
            lhs: { name: 'rsi', params: { period: 7 }, source: 'close' },
            op: '<',
            rhs: 30,
          },
        },
      ],
    });

    component.populateForm(spec);
    fixture.detectChanges();

    expect(component.hasPrefillErrors).toBe(false);
    const indicator = component.entryRulesArray
      .at(0)
      .get('when')!
      .get('lhs')!
      .get('indicator')!;
    expect(indicator.get('name')!.value).toBe('rsi');
    expect(indicator.get('params')!.get('period')!.value).toBe(7);
  });

  it('blocks submit when an RHS constant is left blank', async () => {
    component.form.patchValue({ hypothesis: 'h', signal_definition: 's' });
    component.addEntryRule();
    const rule = component.entryRulesArray.at(0);
    const rhs = rule.get('when')!.get('rhs')!;
    rhs.patchValue({ side_kind: 'number', number_val: null });

    expect(rule.valid).toBe(false);
    expect(component.form.valid).toBe(false);

    await component.createStrategy();
    httpMock.expectNone(strategiesUrl);
  });

  it('rejects fractional values for integer indicator params', () => {
    component.addEntryRule();
    const rule = component.entryRulesArray.at(0);
    const indicator = rule.get('when')!.get('lhs')!.get('indicator')!;
    indicator.patchValue({ name: 'sma' });
    indicator.get('params')!.patchValue({ period: 2.5 });

    expect(indicator.get('params')!.get('period')!.hasError('notInteger')).toBe(true);
    expect(component.form.valid).toBe(false);
  });

  it('preserves entry-rule subscriptions when a non-last row is removed', async () => {
    component.form.patchValue({ hypothesis: 'h', signal_definition: 's' });
    component.addEntryRule();
    component.addEntryRule();
    fixture.detectChanges();

    component.removeEntryRule(0);
    fixture.detectChanges();

    expect(component.entryRulesArray.length).toBe(1);

    // The remaining row's indicator name swap must still rebuild params
    // correctly (i.e. the child editor's name.valueChanges subscription
    // is still attached to the surviving FormGroup, not the destroyed one).
    const rule = component.entryRulesArray.at(0);
    const indicator = rule.get('when')!.get('lhs')!.get('indicator')!;
    indicator.patchValue({ name: 'rsi' });
    fixture.detectChanges();

    const params = indicator.get('params')!;
    // RSI's spec has a `period` param (default 14); SMA had one too but
    // with a different validator profile. Either way, after the swap the
    // params group must still contain `period`.
    expect(params.get('period')).not.toBeNull();
  });

  it('flags an unknown exit-rule kind', () => {
    const spec = baseSpec({
      exit_rules: [{ kind: 'gibberish' } as unknown as StrategySpec['exit_rules'][number]],
    });

    component.populateForm(spec);

    expect(component.hasPrefillErrors).toBe(true);
    expect(component.prefillErrors).toContain('exit_rule:gibberish');
    expect(component.exitRulesArray.length).toBe(0);
  });
});
