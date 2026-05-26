import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { BrandEditPanelComponent } from './brand-edit-panel.component';
import type { BrandingMissionSnapshot, Brand } from '../../../models';

describe('BrandEditPanelComponent', () => {
  let component: BrandEditPanelComponent;
  let fixture: ComponentFixture<BrandEditPanelComponent>;

  const mission: BrandingMissionSnapshot = {
    company_name: 'Acme',
    company_description: 'Widgets for all',
    target_audience: 'Companies',
    values: ['integrity', 'quality'],
    differentiators: ['speed'],
    desired_voice: 'clear, confident',
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [BrandEditPanelComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(BrandEditPanelComponent);
    component = fixture.componentInstance;
  });

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('patches form from mission when opened', () => {
    component.mission = mission;
    component.open = true;
    component.ngOnChanges({
      open: { currentValue: true, previousValue: false, firstChange: false, isFirstChange: () => false },
    });
    expect(component.form.value.company_name).toBe('Acme');
    expect(component.form.value.values_csv).toBe('integrity, quality');
    expect(component.form.value.differentiators_csv).toBe('speed');
  });

  it('patches form from brand.mission when brand is provided', () => {
    component.brand = { id: 'b1', mission } as Brand;
    component.mission = null;
    component.open = true;
    component.ngOnChanges({
      brand: { currentValue: component.brand, previousValue: null, firstChange: false, isFirstChange: () => false },
    });
    expect(component.form.value.company_name).toBe('Acme');
  });

  it('onApply emits missionUpdate with parsed values', () => {
    const spy = vi.fn();
    component.missionUpdate.subscribe(spy);
    component.form.setValue({
      company_name: 'NewCo',
      company_description: 'New desc here',
      target_audience: 'developers',
      desired_voice: 'bold',
      values_csv: 'honesty, trust',
      differentiators_csv: 'reliability',
    });
    component.onApply();
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        company_name: 'NewCo',
        values: ['honesty', 'trust'],
        differentiators: ['reliability'],
      }),
    );
  });

  it('onApply emits empty arrays (not undefined) when values/differentiators are cleared', () => {
    const spy = vi.fn();
    component.missionUpdate.subscribe(spy);
    component.form.setValue({
      company_name: 'ClearCo',
      company_description: 'Clearing values test',
      target_audience: 'testers',
      desired_voice: '',
      values_csv: '',
      differentiators_csv: '',
    });
    component.onApply();
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        values: [],
        differentiators: [],
      }),
    );
  });

  it('onApply does not emit when form is invalid', () => {
    const spy = vi.fn();
    component.missionUpdate.subscribe(spy);
    component.form.setValue({
      company_name: '',
      company_description: '',
      target_audience: '',
      desired_voice: '',
      values_csv: '',
      differentiators_csv: '',
    });
    component.onApply();
    expect(spy).not.toHaveBeenCalled();
  });

  it('does not clobber form when mission changes while panel is open', () => {
    component.mission = mission;
    component.open = true;
    component.ngOnChanges({
      open: { currentValue: true, previousValue: false, firstChange: false, isFirstChange: () => false },
    });
    expect(component.form.value.company_name).toBe('Acme');

    component.form.patchValue({ company_name: 'UserEdit' });

    component.mission = { ...mission, company_name: 'ServerUpdate' };
    component.ngOnChanges({
      mission: { currentValue: component.mission, previousValue: mission, firstChange: false, isFirstChange: () => false },
    });
    expect(component.form.value.company_name).toBe('UserEdit');
  });

  it('onSkipSaveToggle emits skipSaveChange', () => {
    const spy = vi.fn();
    component.skipSaveChange.subscribe(spy);
    component.onSkipSaveToggle(true);
    expect(spy).toHaveBeenCalledWith(true);
  });

  it('onClose emits closePanel', () => {
    const spy = vi.fn();
    component.closePanel.subscribe(spy);
    component.onClose();
    expect(spy).toHaveBeenCalled();
  });

  it('clears form when no mission is available (prevents stale data)', () => {
    component.mission = mission;
    component.open = true;
    component.ngOnChanges({
      open: { currentValue: true, previousValue: false, firstChange: false, isFirstChange: () => false },
    });
    expect(component.form.value.company_name).toBe('Acme');

    component.mission = null;
    component.brand = null;
    component.ngOnChanges({
      mission: { currentValue: null, previousValue: mission, firstChange: false, isFirstChange: () => false },
    });
    expect(component.form.value.company_name).toBe('');
    expect(component.form.value.values_csv).toBe('');
  });
});
