import { of } from 'rxjs';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { SoftwareEngineeringDashboardComponent } from './software-engineering-dashboard.component';
import { expectNoAxeViolations } from '../../testing/a11y';
import { renderDashboardShellA11y } from '../../testing/dashboard-a11y';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

function apiStub(jobs: { job_id: string; status: string }[] = []) {
  return {
    provide: SoftwareEngineeringApiService,
    useValue: { getRunningJobs: vi.fn().mockReturnValue(of({ jobs })) },
  };
}

describe('SoftwareEngineeringDashboardComponent a11y', () => {
  it('has no axe violations on the empty view', async () => {
    const fixture = await renderDashboardShellA11y(SoftwareEngineeringDashboardComponent, [apiStub([])]);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.empty-state')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the new-project view', async () => {
    const fixture = await renderDashboardShellA11y(SoftwareEngineeringDashboardComponent, [apiStub([])]);
    fixture.componentInstance.showNewProject();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.new-project-view')).not.toBeNull();
    // Guard: the embedded chat actually loaded a conversation (an assistant
    // message painted) — so axe audits the real, populated surface, not a
    // bare-mounted or errored chat.
    expect(el.querySelector('app-team-assistant-chat .message.assistant')).toBeTruthy();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the jobs view', async () => {
    const fixture = await renderDashboardShellA11y(SoftwareEngineeringDashboardComponent, [
      apiStub([
        { job_id: 'a', status: 'running' },
        { job_id: 'b', status: 'completed' },
      ]),
    ]);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.jobs-list-view')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);
});
