import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentProposalComponent } from './investment-proposal.component';
import type { PortfolioProposal } from '../../models';

describe('InvestmentProposalComponent', () => {
  let component: InvestmentProposalComponent;
  let fixture: ComponentFixture<InvestmentProposalComponent>;
  let apiSpy: {
    createProposal: ReturnType<typeof vi.fn>;
    validateProposal: ReturnType<typeof vi.fn>;
  };

  const makeProposal = (): PortfolioProposal => ({
    proposal_id: 'p1',
    objective: 'growth',
    expected_return_pct: 8,
    expected_volatility_pct: 12,
    expected_max_drawdown_pct: 20,
    assumptions: ['a', 'b'],
    positions: [
      { symbol: 'AAPL', asset_class: 'equities', weight_pct: 50, rationale: 'tech' },
      { symbol: 'BND', asset_class: 'bonds', weight_pct: 50, rationale: 'safety' },
    ],
  } as never);

  beforeEach(async () => {
    apiSpy = {
      createProposal: vi.fn().mockReturnValue(of({ proposal: makeProposal() })),
      validateProposal: vi.fn().mockReturnValue(of({ approved: true })),
    };
    await TestBed.configureTestingModule({
      imports: [InvestmentProposalComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentProposalComponent);
    component = fixture.componentInstance;
  });

  it('creates with empty form', () => {
    fixture.detectChanges();
    expect(component).toBeTruthy();
    expect(component.positionsArray.length).toBe(0);
  });

  it('ngOnInit populates from existing proposal', () => {
    component.existingProposal = makeProposal();
    component.ngOnInit();
    expect(component.currentProposal).toBeTruthy();
    expect(component.positionsArray.length).toBe(2);
  });

  it('addPosition appends a row with defaults', () => {
    component.addPosition();
    expect(component.positionsArray.length).toBe(1);
    expect((component.positionsArray.at(0) as { get: (k: string) => { value: string } | null }).get('asset_class')?.value).toBe(
      'equities',
    );
  });

  it('addPosition with partial values', () => {
    component.addPosition({ symbol: 'X', asset_class: 'crypto', weight_pct: 10 } as never);
    expect((component.positionsArray.at(0) as { get: (k: string) => { value: unknown } | null }).get('symbol')?.value).toBe('X');
  });

  it('removePosition removes at index', () => {
    component.addPosition();
    component.addPosition();
    component.removePosition(0);
    expect(component.positionsArray.length).toBe(1);
  });

  it('totalWeight sums position weights', () => {
    component.addPosition({ symbol: 'A', asset_class: 'equities', weight_pct: 30 } as never);
    component.addPosition({ symbol: 'B', asset_class: 'bonds', weight_pct: 70 } as never);
    expect(component.totalWeight).toBe(100);
  });

  it('allocationByClass aggregates by class sorted desc', () => {
    component.addPosition({ symbol: 'A', asset_class: 'equities', weight_pct: 30 } as never);
    component.addPosition({ symbol: 'B', asset_class: 'equities', weight_pct: 30 } as never);
    component.addPosition({ symbol: 'C', asset_class: 'bonds', weight_pct: 40 } as never);
    expect(component.allocationByClass[0]).toEqual({ assetClass: 'equities', weight: 60 });
    expect(component.allocationByClass[1]).toEqual({ assetClass: 'bonds', weight: 40 });
  });

  it('createProposal early-exits without ips', async () => {
    component.ips = null;
    await component.createProposal();
    expect(apiSpy.createProposal).not.toHaveBeenCalled();
  });

  it('createProposal early-exits when form invalid', async () => {
    component.ips = { profile: { user_id: 'u1' } } as never;
    await component.createProposal();
    expect(apiSpy.createProposal).not.toHaveBeenCalled();
  });

  it('createProposal success emits proposalCreated', async () => {
    component.ips = { profile: { user_id: 'u1' } } as never;
    component.form.patchValue({ objective: 'growth' });
    component.addPosition({ symbol: 'AAPL', asset_class: 'equities', weight_pct: 50 } as never);
    component.addPosition({ symbol: 'BND', asset_class: 'bonds', weight_pct: 50 } as never);
    const spy = vi.fn();
    component.proposalCreated.subscribe(spy);
    await component.createProposal();
    expect(apiSpy.createProposal).toHaveBeenCalled();
    expect(spy).toHaveBeenCalled();
    expect(component.currentProposal).toBeTruthy();
  });

  it('createProposal error path sets error', async () => {
    apiSpy.createProposal.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.ips = { profile: { user_id: 'u1' } } as never;
    component.form.patchValue({ objective: 'growth' });
    component.addPosition({ symbol: 'AAPL', asset_class: 'equities', weight_pct: 50 } as never);
    component.addPosition({ symbol: 'BND', asset_class: 'bonds', weight_pct: 50 } as never);
    await component.createProposal();
    expect(component.error).toBe('oops');
    expect(component.loading).toBe(false);
  });

  it('createProposal handles assumptions multi-line', async () => {
    component.ips = { profile: { user_id: 'u1' } } as never;
    component.form.patchValue({ objective: 'growth', assumptions: 'a1\na2\n' });
    component.addPosition({ symbol: 'AAPL', asset_class: 'equities', weight_pct: 100 } as never);
    await component.createProposal();
    const body = apiSpy.createProposal.mock.calls[0][0];
    expect(body.assumptions).toEqual(['a1', 'a2']);
  });

  it('validateProposal early-exits without proposal', () => {
    component.validateProposal();
    expect(apiSpy.validateProposal).not.toHaveBeenCalled();
  });

  it('validateProposal early-exits without ips', () => {
    component.currentProposal = makeProposal();
    component.ips = null;
    component.validateProposal();
    expect(apiSpy.validateProposal).not.toHaveBeenCalled();
  });

  it('validateProposal success sets validationResult', () => {
    component.currentProposal = makeProposal();
    component.ips = { profile: { user_id: 'u1' } } as never;
    component.validateProposal();
    expect(apiSpy.validateProposal).toHaveBeenCalledWith('p1', { user_id: 'u1' });
    expect(component.validationResult).toBeTruthy();
  });

  it('validateProposal error path sets error', () => {
    apiSpy.validateProposal.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.currentProposal = makeProposal();
    component.ips = { profile: { user_id: 'u1' } } as never;
    component.validateProposal();
    expect(component.error).toBe('oops');
    expect(component.validating).toBe(false);
  });
});
