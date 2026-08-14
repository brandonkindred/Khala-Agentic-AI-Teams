import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { ActivatedRoute, provideRouter } from '@angular/router';
import { vi } from 'vitest';
import { PersonaTestingApiService } from '../../../services/persona-testing-api.service';
import type { RunArtifacts } from '../../../models';
import { PersonaTestAuditPanelComponent } from './persona-test-audit-panel.component';

describe('PersonaTestAuditPanelComponent', () => {
  let component: PersonaTestAuditPanelComponent;
  let fixture: ComponentFixture<PersonaTestAuditPanelComponent>;
  let apiSpy: {
    getRunStatus: ReturnType<typeof vi.fn>;
    getRunArtifacts: ReturnType<typeof vi.fn>;
  };

  const buildFixture = (
    runId: string | null = 'r1',
    statusObs = of({ status: 'completed', decisions: [] }) as never,
  ) => {
    const routeStub = {
      snapshot: { paramMap: { get: vi.fn(() => runId) } },
    } as unknown as ActivatedRoute;
    apiSpy = {
      getRunStatus: vi.fn().mockReturnValue(statusObs),
      getRunArtifacts: vi.fn().mockReturnValue(
        of({
          se_job_status: { progress: 50, task_states: { t1: { status: 'done', title: 'X' } } },
        }),
      ),
    };

    TestBed.configureTestingModule({
      imports: [PersonaTestAuditPanelComponent],
      providers: [
        provideRouter([]),
        { provide: ActivatedRoute, useValue: routeStub },
        { provide: PersonaTestingApiService, useValue: apiSpy },
      ],
    });

    fixture = TestBed.createComponent(PersonaTestAuditPanelComponent);
    component = fixture.componentInstance;
  };

  afterEach(() => {
    component?.ngOnDestroy();
    TestBed.resetTestingModule();
  });

  it('creates and loads status on init', async () => {
    buildFixture();
    component.ngOnInit();
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(apiSpy.getRunStatus).toHaveBeenCalledWith('r1');
    expect(component.run).toBeTruthy();
  });

  it('sets error when no runId', () => {
    buildFixture(null);
    component.ngOnInit();
    expect(component.error).toBe('No run ID provided');
    expect(component.loading).toBe(false);
  });

  it('error path from getRunStatus sets error', async () => {
    buildFixture('r1', throwError(() => ({ error: { detail: 'oops' } })) as never);
    component.ngOnInit();
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(component.error).toBe('oops');
    expect(component.loading).toBe(false);
  });

  it('isTerminal reflects status', () => {
    buildFixture();
    expect(component.isTerminal).toBe(false);
    component.run = { status: 'completed' } as never;
    expect(component.isTerminal).toBe(true);
    component.run = { status: 'failed' } as never;
    expect(component.isTerminal).toBe(true);
    component.run = { status: 'running' } as never;
    expect(component.isTerminal).toBe(false);
  });

  it('decisions returns array or empty', () => {
    buildFixture();
    expect(component.decisions).toEqual([]);
    component.run = { status: 'x', decisions: [{ id: 'd1' }] } as never;
    expect(component.decisions.length).toBe(1);
  });

  it('statusClass returns prefix or empty', () => {
    buildFixture();
    expect(component.statusClass).toBe('');
    component.run = { status: 'running' } as never;
    expect(component.statusClass).toBe('status-running');
  });

  it('formatStatus replaces underscores', () => {
    buildFixture();
    expect(component.formatStatus('in_progress')).toBe('in progress');
  });

  it('seJobProgress/seJobTaskStates/seJobTaskIds + getTaskStatus/Title with artifacts', () => {
    buildFixture();
    component.artifacts = {
      se_job_status: { progress: 50, task_states: { t1: { status: 'done', title: 'X' } } },
    } as never;
    expect(component.seJobProgress).toBe(50);
    expect(component.seJobTaskIds).toEqual(['t1']);
    expect(component.getTaskStatus('t1')).toBe('done');
    expect(component.getTaskTitle('t1')).toBe('X');
    expect(component.getTaskTitle('nope')).toBe('nope');
  });

  it('handles missing artifacts safely', () => {
    buildFixture();
    component.artifacts = null;
    expect(component.seJobProgress).toBeNull();
    expect(component.seJobTaskStates).toBeNull();
    expect(component.seJobTaskIds).toEqual([]);
    expect(component.getTaskStatus('t1')).toBe('');
    expect(component.getTaskTitle('t1')).toBe('t1');
  });

  it('handles missing artifact fields', () => {
    buildFixture();
    component.artifacts = { se_job_status: {} } as RunArtifacts;
    expect(component.seJobProgress).toBeNull();
    expect(component.seJobTaskStates).toBeNull();
    expect(component.getTaskStatus('t1')).toBe('');
  });

  it('defaults the back link to /persona-testing', () => {
    buildFixture();
    fixture.detectChanges();
    const link = fixture.nativeElement.querySelector('a.back-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/persona-testing');
    expect(link.textContent).toContain('Back to Testing Personas');
  });

  it('renders a custom backLink and backLabel when provided', () => {
    buildFixture();
    component.backLink = '/agent-studio';
    component.backLabel = 'Back to Agent Studio';
    fixture.detectChanges();
    const link = fixture.nativeElement.querySelector('a.back-link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('/agent-studio');
    expect(link.textContent).toContain('Back to Agent Studio');
    expect(link.textContent).not.toContain('Testing Personas');
  });
});
