import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { vi } from 'vitest';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import { SoftwareEngineeringDashboardComponent } from './software-engineering-dashboard.component';
import { expectNoAxeViolations } from '../../testing/a11y';

vi.mock('rxjs', async (importOriginal) => {
  const rxjs = await importOriginal<typeof import('rxjs')>();
  return { ...rxjs, timer: vi.fn(() => rxjs.of(0)) };
});

describe('SoftwareEngineeringDashboardComponent a11y', () => {
  let fixture: ComponentFixture<SoftwareEngineeringDashboardComponent>;
  let api: { getRunningJobs: ReturnType<typeof vi.fn> };

  async function setup(jobs: { job_id: string; status: string }[] = []): Promise<void> {
    api = { getRunningJobs: vi.fn().mockReturnValue(of({ jobs })) };
    await TestBed.configureTestingModule({
      imports: [SoftwareEngineeringDashboardComponent, NoopAnimationsModule],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: SoftwareEngineeringApiService, useValue: api },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(SoftwareEngineeringDashboardComponent);
    fixture.detectChanges();
  }

  it('has no axe violations on the empty view', async () => {
    await setup([]);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.empty-state')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the new-project view', async () => {
    await setup([]);
    fixture.componentInstance.showNewProject();
    fixture.detectChanges();
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.new-project-view')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);

  it('has no axe violations on the jobs view', async () => {
    await setup([
      { job_id: 'a', status: 'running' },
      { job_id: 'b', status: 'completed' },
    ]);
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.jobs-list-view')).not.toBeNull();
    await expectNoAxeViolations(el);
  }, 15000);
});
