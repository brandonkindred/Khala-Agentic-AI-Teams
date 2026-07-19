import { ComponentFixture, TestBed } from '@angular/core/testing';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { PhaseStepperComponent } from './phase-stepper.component';

describe('PhaseStepperComponent', () => {
  let fixture: ComponentFixture<PhaseStepperComponent>;
  let component: PhaseStepperComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [PhaseStepperComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(PhaseStepperComponent);
    component = fixture.componentInstance;
  });

  describe('isPhaseCompleted / isCurrentPhase / isPhasePending', () => {
    it('treats every phase as pending when there is no current phase', () => {
      component.currentPhase = undefined;
      expect(component.isPhaseCompleted('ideating')).toBe(false);
      expect(component.isCurrentPhase('ideating')).toBe(false);
      expect(component.isPhasePending('ideating')).toBe(true);
    });

    it('treats every phase as pending for a null current phase', () => {
      component.currentPhase = null;
      expect(component.isPhasePending('backtesting')).toBe(true);
    });

    it('treats every phase as pending for an unrecognized phase id', () => {
      component.currentPhase = 'not-a-real-phase';
      expect(component.isPhaseCompleted('ideating')).toBe(false);
      expect(component.isCurrentPhase('ideating')).toBe(false);
      expect(component.isPhasePending('ideating')).toBe(true);
    });

    it('marks earlier phases completed, the active one current, and later ones pending', () => {
      component.currentPhase = 'backtesting';
      expect(component.isPhaseCompleted('ideating')).toBe(true);
      expect(component.isPhaseCompleted('coding')).toBe(true);
      expect(component.isCurrentPhase('backtesting')).toBe(true);
      expect(component.isPhaseCompleted('backtesting')).toBe(false);
      expect(component.isPhasePending('analyzing')).toBe(true);
      expect(component.isPhaseCompleted('analyzing')).toBe(false);
    });

    it('marks every other phase completed once the last phase is current', () => {
      component.currentPhase = 'analyzing';
      expect(component.isPhaseCompleted('ideating')).toBe(true);
      expect(component.isPhaseCompleted('coding')).toBe(true);
      expect(component.isPhaseCompleted('backtesting')).toBe(true);
      expect(component.isCurrentPhase('analyzing')).toBe(true);
    });
  });

  describe('rendered template', () => {
    it('renders one .phase-step per phase with the correct completed/current/pending classes', () => {
      component.currentPhase = 'backtesting';
      fixture.detectChanges();

      const steps: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.phase-step'));
      expect(steps).toHaveLength(4);
      expect(steps[0].classList.contains('completed')).toBe(true); // ideating
      expect(steps[1].classList.contains('completed')).toBe(true); // coding
      expect(steps[2].classList.contains('current')).toBe(true); // backtesting
      expect(steps[3].classList.contains('pending')).toBe(true); // analyzing

      const labels = steps.map((s) => s.querySelector('.phase-label')?.textContent?.trim());
      expect(labels).toEqual(['Ideate', 'Code', 'Backtest', 'Analyze']);

      // Completed phases swap their icon for a checkmark.
      expect(steps[0].querySelector('mat-icon')?.textContent?.trim()).toBe('check');
      expect(steps[2].querySelector('mat-icon')?.textContent?.trim()).toBe('play_circle');
    });

    it('renders every phase-node icon as aria-hidden (decorative — state is conveyed by CSS + label)', () => {
      component.currentPhase = 'ideating';
      fixture.detectChanges();

      const icons: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.phase-node mat-icon'));
      expect(icons).toHaveLength(4);
      for (const icon of icons) {
        expect(icon.getAttribute('aria-hidden')).toBe('true');
      }
    });
  });

  it('every <mat-icon> opening tag in the template source is explicit about aria-hidden', () => {
    const templatePath = resolve(dirname(fileURLToPath(import.meta.url)), 'phase-stepper.component.html');
    const html = readFileSync(templatePath, 'utf8');
    const openTags = html.match(/<mat-icon\b[^>]*>/g) ?? [];

    expect(openTags.length).toBe(1);
    for (const tag of openTags) {
      expect(tag).toMatch(/aria-hidden\s*=\s*"true"/);
    }
  });
});
