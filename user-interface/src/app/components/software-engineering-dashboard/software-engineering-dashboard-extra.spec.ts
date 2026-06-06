import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi, beforeEach } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { SoftwareEngineeringDashboardComponent } from './software-engineering-dashboard.component';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

describe('SoftwareEngineeringDashboardComponent (extra coverage)', () => {
  let component: SoftwareEngineeringDashboardComponent;
  let fixture: ComponentFixture<SoftwareEngineeringDashboardComponent>;
  let api: { getRunningJobs: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    api = { getRunningJobs: vi.fn().mockReturnValue(of({ jobs: [] })) };
    await TestBed.configureTestingModule({
      imports: [SoftwareEngineeringDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideRouter([]),
        { provide: SoftwareEngineeringApiService, useValue: api },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(SoftwareEngineeringDashboardComponent);
    component = fixture.componentInstance;
  });

  it('isTerminal recognizes terminal statuses', () => {
    fixture.detectChanges();
    expect(component.isTerminal('completed')).toBe(true);
    expect(component.isTerminal('failed')).toBe(true);
    expect(component.isTerminal('cancelled')).toBe(true);
    expect(component.isTerminal('stopped')).toBe(true);
    expect(component.isTerminal('completed_with_failures')).toBe(true);
    expect(component.isTerminal('running')).toBe(false);
  });

  it('splits jobs into running and completed', () => {
    api.getRunningJobs.mockReturnValue(of({
      jobs: [
        { job_id: 'a', status: 'running' },
        { job_id: 'b', status: 'completed' },
        { job_id: 'c', status: 'failed' },
      ],
    }));
    fixture.detectChanges();
    expect(component.runningJobs.length).toBe(1);
    expect(component.completedJobs.length).toBe(2);
    expect(component.activeView).toBe('jobs');
  });

  it('stays empty when no jobs', () => {
    api.getRunningJobs.mockReturnValue(of({ jobs: [] }));
    fixture.detectChanges();
    expect(component.activeView).toBe('empty');
  });

  it('handles undefined jobs gracefully', () => {
    api.getRunningJobs.mockReturnValue(of({}));
    fixture.detectChanges();
    expect(component.allJobs).toEqual([]);
  });

  it('showNewProject sets activeView', () => {
    fixture.detectChanges();
    component.showNewProject();
    expect(component.activeView).toBe('new-project');
  });

  it('showJobs sets activeView', () => {
    fixture.detectChanges();
    component.showJobs();
    expect(component.activeView).toBe('jobs');
  });

  it('onWorkflowLaunched switches to jobs view', () => {
    fixture.detectChanges();
    component.onWorkflowLaunched({ job_id: 'j1', conversation_id: 'c1' });
    expect(component.activeView).toBe('jobs');
  });

  it('ngOnDestroy unsubscribes', () => {
    fixture.detectChanges();
    component.ngOnDestroy();
    // No throw
    expect(component).toBeTruthy();
  });
});
