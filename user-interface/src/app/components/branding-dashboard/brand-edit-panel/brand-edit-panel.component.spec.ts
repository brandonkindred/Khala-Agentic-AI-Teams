import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { BrandEditPanelComponent } from './brand-edit-panel.component';
import type { BrandingMissionSnapshot, Brand } from '../../../models';
import { MatChipInputEvent } from '@angular/material/chips';

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
    expect(component.values).toEqual(['integrity', 'quality']);
    expect(component.differentiators).toEqual(['speed']);
  });

  it('patches form from brand.mission when brand is provided', () => {
    component.brand = { id: 'b1', mission } as Brand;
    component.mission = null;
    component.open = true;
    component.ngOnChanges({
      brand: { currentValue: component.brand, previousValue: null, firstChange: false, isFirstChange: () => false },
    });
    expect(component.form.value.company_name).toBe('Acme');
    expect(component.values).toEqual(['integrity', 'quality']);
  });

  it('onApply emits missionUpdate with values and differentiators arrays', () => {
    const spy = vi.fn();
    component.missionUpdate.subscribe(spy);
    component.form.setValue({
      company_name: 'NewCo',
      company_description: 'New desc here',
      target_audience: 'developers',
      desired_voice: 'bold',
    });
    component.values = ['honesty', 'trust'];
    component.differentiators = ['reliability'];
    component.onApply();
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        company_name: 'NewCo',
        values: ['honesty', 'trust'],
        differentiators: ['reliability'],
      }),
    );
  });

  it('onApply emits empty arrays when values/differentiators are cleared', () => {
    const spy = vi.fn();
    component.missionUpdate.subscribe(spy);
    component.form.setValue({
      company_name: 'ClearCo',
      company_description: 'Clearing values test',
      target_audience: 'testers',
      desired_voice: '',
    });
    component.values = [];
    component.differentiators = [];
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
    expect(component.values).toEqual(['integrity', 'quality']);

    component.mission = null;
    component.brand = null;
    component.ngOnChanges({
      mission: { currentValue: null, previousValue: mission, firstChange: false, isFirstChange: () => false },
    });
    expect(component.form.value.company_name).toBe('');
    expect(component.values).toEqual([]);
    expect(component.differentiators).toEqual([]);
  });

  describe('chip input methods', () => {
    const makeChipEvent = (value: string): MatChipInputEvent => ({
      value,
      chipInput: { clear: vi.fn() } as unknown as MatChipInputEvent['chipInput'],
      input: document.createElement('input'),
    });

    it('addValue adds a trimmed value and clears the input', () => {
      const event = makeChipEvent('  innovation  ');
      component.addValue(event);
      expect(component.values).toEqual(['innovation']);
      expect(event.chipInput.clear).toHaveBeenCalled();
    });

    it('addValue ignores empty strings', () => {
      const event = makeChipEvent('   ');
      component.addValue(event);
      expect(component.values).toEqual([]);
    });

    it('removeValue removes the matching value', () => {
      component.values = ['a', 'b', 'c'];
      component.removeValue('b');
      expect(component.values).toEqual(['a', 'c']);
    });

    it('removeValue does nothing for non-existent value', () => {
      component.values = ['a', 'b'];
      component.removeValue('z');
      expect(component.values).toEqual(['a', 'b']);
    });

    it('addDifferentiator adds a trimmed value and clears the input', () => {
      const event = makeChipEvent('scalability');
      component.addDifferentiator(event);
      expect(component.differentiators).toEqual(['scalability']);
      expect(event.chipInput.clear).toHaveBeenCalled();
    });

    it('addDifferentiator ignores empty strings', () => {
      const event = makeChipEvent('');
      component.addDifferentiator(event);
      expect(component.differentiators).toEqual([]);
    });

    it('removeDifferentiator removes the matching value', () => {
      component.differentiators = ['fast', 'reliable'];
      component.removeDifferentiator('fast');
      expect(component.differentiators).toEqual(['reliable']);
    });

    it('removeDifferentiator does nothing for non-existent value', () => {
      component.differentiators = ['fast'];
      component.removeDifferentiator('slow');
      expect(component.differentiators).toEqual(['fast']);
    });
  });
});
