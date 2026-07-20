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
      expect(labels).toEqual(['Ideate: completed', 'Code: completed', 'Backtest: current step', 'Analyze: not started']);

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

    it('renders as a semantic list, with aria-current="step" on exactly the active step (WCAG 1.3.1 / 4.1.2)', () => {
      component.currentPhase = 'coding';
      fixture.detectChanges();

      const stepper: HTMLElement = fixture.nativeElement.querySelector('.phase-stepper');
      expect(stepper.getAttribute('role')).toBe('list');

      const steps: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.phase-step'));
      steps.forEach((step) => expect(step.getAttribute('role')).toBe('listitem'));

      const current = steps.filter((s) => s.getAttribute('aria-current') === 'step');
      expect(current).toHaveLength(1);
      expect(current[0].querySelector('.phase-label')?.textContent).toContain('Code');
      steps.filter((s) => s !== current[0]).forEach((s) => expect(s.hasAttribute('aria-current')).toBe(false));
    });
  });

  describe('phaseStateLabel', () => {
    it('returns "completed" for a phase before the current one', () => {
      component.currentPhase = 'backtesting';
      expect(component.phaseStateLabel('ideating')).toBe('completed');
    });

    it('returns "current step" for the active phase', () => {
      component.currentPhase = 'backtesting';
      expect(component.phaseStateLabel('backtesting')).toBe('current step');
    });

    it('returns "not started" for a phase after the current one', () => {
      component.currentPhase = 'backtesting';
      expect(component.phaseStateLabel('analyzing')).toBe('not started');
    });

    it('returns "not started" for every phase when there is no current phase', () => {
      component.currentPhase = undefined;
      expect(component.phaseStateLabel('ideating')).toBe('not started');
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
