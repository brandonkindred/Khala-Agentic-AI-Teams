import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentPromotionComponent } from './investment-promotion.component';

describe('InvestmentPromotionComponent', () => {
  let component: InvestmentPromotionComponent;
  let fixture: ComponentFixture<InvestmentPromotionComponent>;
  let apiSpy: { promotionDecision: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = { promotionDecision: vi.fn().mockReturnValue(of({ decision: { outcome: 'paper' } })) };
    await TestBed.configureTestingModule({
      imports: [InvestmentPromotionComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentPromotionComponent);
    component = fixture.componentInstance;
  });

  it('creates with form defaults', () => {
    expect(component).toBeTruthy();
    expect(component.form.get('proposer_agent_id')?.value).toBe('strategy_agent');
  });

  it('runPromotion exits early without ips/strategy', () => {
    component.runPromotion();
    expect(apiSpy.promotionDecision).not.toHaveBeenCalled();
  });

  it('runPromotion exits early when form invalid', () => {
    component.ips = { profile: { user_id: 'u1' } } as never;
    component.strategy = { strategy_id: 's1' } as never;
    component.form.patchValue({ proposer_agent_id: '' });
    component.runPromotion();
    expect(apiSpy.promotionDecision).not.toHaveBeenCalled();
  });

  it('runPromotion emits and sets decision', () => {
    component.ips = { profile: { user_id: 'u1' } } as never;
    component.strategy = { strategy_id: 's1' } as never;
    const spy = vi.fn();
    component.decisionMade.subscribe(spy);
    component.runPromotion();
    expect(apiSpy.promotionDecision).toHaveBeenCalled();
    expect(spy).toHaveBeenCalledWith({ outcome: 'paper' });
    expect(component.loading).toBe(false);
  });

  it('runPromotion error path', () => {
    apiSpy.promotionDecision.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.ips = { profile: { user_id: 'u1' } } as never;
    component.strategy = { strategy_id: 's1' } as never;
    component.runPromotion();
    expect(component.error).toBe('oops');
    expect(component.loading).toBe(false);
  });

  it('getGateIcon maps result types', () => {
    expect(component.getGateIcon('pass')).toBe('check_circle');
    expect(component.getGateIcon('warn')).toBe('warning');
    expect(component.getGateIcon('fail')).toBe('cancel');
    expect(component.getGateIcon('?')).toBe('help');
  });

  it('getGateClass returns prefixed string', () => {
    expect(component.getGateClass('pass')).toBe('gate-pass');
  });

  it('getOutcomeConfig returns mapped config or reject fallback', () => {
    expect(component.getOutcomeConfig('live')).toEqual(component.outcomeConfig.live);
    expect(component.getOutcomeConfig('reject')).toEqual(component.outcomeConfig.reject);
    expect(component.getOutcomeConfig('unknown' as never)).toEqual(component.outcomeConfig.reject);
  });
});
