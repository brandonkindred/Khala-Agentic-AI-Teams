import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import type { GateViewModel } from '../strategy-lab.formatters';
import { QualityGateListComponent } from './quality-gate-list.component';

function makeViewModel(overrides: Partial<GateViewModel> = {}): GateViewModel {
  return {
    gate: { gate_name: 'realism_check', passed: true, details: 'Within realistic bounds.', severity: 'info' },
    icon: 'check_circle',
    severityClass: 'gate-info',
    isRemedied: false,
    ...overrides,
  };
}

describe('QualityGateListComponent', () => {
  let fixture: ComponentFixture<QualityGateListComponent>;
  let component: QualityGateListComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [QualityGateListComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(QualityGateListComponent);
    component = fixture.componentInstance;
    component.gateViewModels = [makeViewModel()];
  });

  describe('rendered template', () => {
    it('does not render the panel when gateViewModels is empty', () => {
      component.gateViewModels = [];
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.gate-results')).toBeNull();
    });

    it('renders one .gate-result per view model with the mapped icon/severity/name/details', () => {
      component.gateViewModels = [
        makeViewModel({
          gate: { gate_name: 'realism_check', passed: true, details: 'Within realistic bounds.', severity: 'info' },
          icon: 'check_circle',
          severityClass: 'gate-info',
          isRemedied: false,
        }),
        makeViewModel({
          gate: { gate_name: 'zero_trades', passed: false, details: 'No trades generated.', severity: 'critical', refinement_round: 0 },
          icon: 'build_circle',
          severityClass: 'gate-remedied',
          isRemedied: true,
        }),
      ];
      fixture.detectChanges();

      const results: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.gate-result'));
      expect(results.length).toBe(2);

      expect(results[0].classList.contains('gate-info')).toBe(true);
      expect(results[0].querySelector('.gate-name')?.textContent).toBe('realism_check');
      expect(results[0].querySelector('mat-icon')?.textContent).toBe('check_circle');
      expect(results[0].querySelector('.gate-remedied-label')).toBeNull();

      expect(results[1].classList.contains('gate-remedied')).toBe(true);
      expect(results[1].querySelector('.gate-name')?.textContent).toBe('zero_trades');
      expect(results[1].querySelector('.gate-detail')?.textContent).toContain('No trades generated.');
      expect(results[1].querySelector('.gate-remedied-label')?.textContent).toContain('(remedied in round 1)');
    });

    it('shows the check count, and the refinement round count only when set', () => {
      fixture.componentRef.setInput('gateViewModels', [makeViewModel(), makeViewModel({ gate: { ...makeViewModel().gate, gate_name: 'g2' } })]);
      fixture.componentRef.setInput('refinementRounds', undefined);
      fixture.detectChanges();
      let description = fixture.nativeElement.querySelector('mat-panel-description');
      expect(description.textContent).toContain('2 check(s)');
      expect(description.textContent).not.toContain('refinement round');

      fixture.componentRef.setInput('refinementRounds', 3);
      fixture.detectChanges();
      description = fixture.nativeElement.querySelector('mat-panel-description');
      expect(description.textContent).toContain('3 refinement round(s)');
    });

    it('treats refinementRounds = 0 as "not set" (0 rounds means no refinement ran)', () => {
      fixture.componentRef.setInput('refinementRounds', 0);
      fixture.detectChanges();
      const description = fixture.nativeElement.querySelector('mat-panel-description');
      expect(description.textContent).toContain('1 check(s)');
      expect(description.textContent).not.toContain('refinement round');
    });

    it('renders correctly with two view models sharing a gate_name across different refinement rounds (track-key regression)', () => {
      component.gateViewModels = [
        makeViewModel({
          gate: { gate_name: 'realism_check', passed: false, details: 'Failed round 0.', severity: 'warning', refinement_round: 0 },
          icon: 'warning',
          severityClass: 'gate-warning',
          isRemedied: false,
        }),
        makeViewModel({
          gate: { gate_name: 'realism_check', passed: true, details: 'Passed round 1.', severity: 'info', refinement_round: 1 },
          icon: 'check_circle',
          severityClass: 'gate-info',
          isRemedied: false,
        }),
      ];
      fixture.detectChanges();

      const results: HTMLElement[] = Array.from(fixture.nativeElement.querySelectorAll('.gate-result'));
      expect(results.length).toBe(2);
      expect(results[0].querySelector('.gate-detail')?.textContent).toContain('Failed round 0.');
      expect(results[1].querySelector('.gate-detail')?.textContent).toContain('Passed round 1.');
    });
  });
});
