import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { InvestmentApiService } from '../../services/investment-api.service';
import { InvestmentProfileFormComponent } from './investment-profile-form.component';

describe('InvestmentProfileFormComponent', () => {
  let component: InvestmentProfileFormComponent;
  let fixture: ComponentFixture<InvestmentProfileFormComponent>;
  let apiSpy: { createProfile: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    apiSpy = { createProfile: vi.fn().mockReturnValue(of({ ips: { profile: { user_id: 'u1' } } })) };
    await TestBed.configureTestingModule({
      imports: [InvestmentProfileFormComponent, NoopAnimationsModule],
      providers: [{ provide: InvestmentApiService, useValue: apiSpy }],
    }).compileComponents();

    fixture = TestBed.createComponent(InvestmentProfileFormComponent);
    component = fixture.componentInstance;
  });

  it('creates with form defaults', () => {
    expect(component).toBeTruthy();
    expect(component.form.get('risk_tolerance')?.value).toBe('medium');
  });

  it('goalsArray addGoal/removeGoal', () => {
    component.addGoal();
    expect(component.goalsArray.length).toBe(1);
    component.removeGoal(0);
    expect(component.goalsArray.length).toBe(0);
  });

  it('submit exits early on invalid form', async () => {
    await component.submit();
    expect(apiSpy.createProfile).not.toHaveBeenCalled();
  });

  it('submit posts and emits profileCreated on success', async () => {
    component.form.patchValue({
      user_id: 'u1abc',
      annual_gross_income: 100000,
      total_net_worth: 500000,
      investable_assets: 200000,
    });
    const spy = vi.fn();
    component.profileCreated.subscribe(spy);
    await component.submit();
    expect(apiSpy.createProfile).toHaveBeenCalled();
    expect(spy).toHaveBeenCalled();
    expect(component.loading).toBe(false);
  });

  it('submit error path sets error', async () => {
    apiSpy.createProfile.mockReturnValue(throwError(() => ({ error: { detail: 'oops' } })));
    component.form.patchValue({
      user_id: 'u1abc',
      annual_gross_income: 100000,
      total_net_worth: 500000,
      investable_assets: 200000,
    });
    await component.submit();
    expect(component.error).toBe('oops');
    expect(component.loading).toBe(false);
  });

  it('submit includes notes when provided', async () => {
    component.form.patchValue({
      user_id: 'u1abc',
      annual_gross_income: 100000,
      total_net_worth: 500000,
      investable_assets: 200000,
      notes: 'something',
    });
    await component.submit();
    const body = apiSpy.createProfile.mock.calls[0][0];
    expect(body.notes).toEqual(['something']);
  });

  it('submit notes empty -> [] ', async () => {
    component.form.patchValue({
      user_id: 'u1abc',
      annual_gross_income: 100000,
      total_net_worth: 500000,
      investable_assets: 200000,
    });
    await component.submit();
    const body = apiSpy.createProfile.mock.calls[0][0];
    expect(body.notes).toEqual([]);
  });

  it('cancel emits', () => {
    const spy = vi.fn();
    component.cancelled.subscribe(spy);
    component.cancel();
    expect(spy).toHaveBeenCalled();
  });
});
