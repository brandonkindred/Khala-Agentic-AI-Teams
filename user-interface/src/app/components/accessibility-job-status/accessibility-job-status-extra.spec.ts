import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { vi, beforeEach } from 'vitest';
import { AccessibilityApiService } from '../../services/accessibility-api.service';
import { AccessibilityJobStatusComponent } from './accessibility-job-status.component';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

describe('AccessibilityJobStatusComponent (extra coverage)', () => {
  let component: AccessibilityJobStatusComponent;
  let fixture: ComponentFixture<AccessibilityJobStatusComponent>;
  let api: { getJobStatus: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    api = { getJobStatus: vi.fn().mockReturnValue(of({ job_id: 'j1', status: 'running', audit_id: 'a1', current_phase: 'crawl', completed_phases: ['discovery'] })) };
    await TestBed.configureTestingModule({
      imports: [AccessibilityJobStatusComponent],
      providers: [{ provide: AccessibilityApiService, useValue: api }],
    }).compileComponents();
    fixture = TestBed.createComponent(AccessibilityJobStatusComponent);
    component = fixture.componentInstance;
  });

  it('does not poll without jobId', () => {
    fixture.detectChanges();
    expect(api.getJobStatus).not.toHaveBeenCalled();
  });

  it('emits statusChange + auditComplete on complete', () => {
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'complete', audit_id: 'a1', current_phase: null, completed_phases: [] }));
    let statusEmitted = false;
    let completeEmitted = false;
    component.statusChange.subscribe(() => { statusEmitted = true; });
    component.auditComplete.subscribe(() => { completeEmitted = true; });
    fixture.componentRef.setInput('jobId', 'j1');
    fixture.detectChanges();
    expect(statusEmitted).toBe(true);
    expect(completeEmitted).toBe(true);
  });

  it('does not emit auditComplete on failed', () => {
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'failed', audit_id: 'a1' }));
    let completeEmitted = false;
    component.auditComplete.subscribe(() => { completeEmitted = true; });
    fixture.componentRef.setInput('jobId', 'j1');
    fixture.detectChanges();
    expect(completeEmitted).toBe(false);
  });

  it('handles polling error', () => {
    api.getJobStatus.mockReturnValue(throwError(() => ({ message: 'oops' })));
    fixture.componentRef.setInput('jobId', 'j1');
    fixture.detectChanges();
    expect(component.error).toBe('oops');
  });

  it('getPhaseStatus returns "current" / "completed" / "pending"', () => {
    fixture.componentRef.setInput('jobId', 'j1');
    fixture.detectChanges();
    expect(component.getPhaseStatus('crawl' as never)).toBe('current');
    expect(component.getPhaseStatus('discovery' as never)).toBe('completed');
    expect(component.getPhaseStatus('analysis' as never)).toBe('pending');
  });

  it('getPhaseStatus pending when no current_phase', () => {
    component.status = null;
    expect(component.getPhaseStatus('discovery' as never)).toBe('pending');
  });

  it('getPhaseIcon falls back when unknown phase', () => {
    expect(component.getPhaseIcon('unknown' as never)).toBe('radio_button_unchecked');
  });

  it('getStatusIcon and getStatusColor map status values', () => {
    expect(component.getStatusIcon('complete')).toBe('check_circle');
    expect(component.getStatusIcon('failed')).toBe('error');
    expect(component.getStatusIcon('cancelled')).toBe('cancel');
    expect(component.getStatusIcon('running')).toBe('play_circle');
    expect(component.getStatusIcon('other')).toBe('hourglass_empty');

    expect(component.getStatusColor('complete')).toBe('primary');
    expect(component.getStatusColor('failed')).toBe('warn');
    expect(component.getStatusColor('cancelled')).toBe('accent');
    expect(component.getStatusColor('running')).toBe('accent');
  });

  it('getSeverityClass lowercases', () => {
    expect(component.getSeverityClass('HIGH')).toBe('high');
  });

  it('onViewFindings/onViewReport emit when audit_id present', () => {
    api.getJobStatus.mockReturnValue(of({ job_id: 'j1', status: 'complete', audit_id: 'a1' }));
    let findingsId = '';
    let reportId = '';
    component.viewFindings.subscribe((id) => { findingsId = id; });
    component.viewReport.subscribe((id) => { reportId = id; });
    fixture.componentRef.setInput('jobId', 'j1');
    fixture.detectChanges();
    component.onViewFindings();
    component.onViewReport();
    expect(findingsId).toBe('a1');
    expect(reportId).toBe('a1');
  });

  it('onViewFindings/onViewReport silent when no audit_id', () => {
    component.status = null;
    let findingsId = '';
    component.viewFindings.subscribe((id) => { findingsId = id; });
    component.onViewFindings();
    expect(findingsId).toBe('');
  });
});
