import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { AgentConsoleApiService } from '../../../../services/agent-console-api.service';
import { NotificationService } from '../../../../core/notification.service';
import type { RunSummary } from '../../../../models/agent-history.model';
import { AgentRunHistoryComponent } from './agent-run-history.component';

describe('AgentRunHistoryComponent', () => {
  const runA: RunSummary = {
    id: 'run-a',
    agent_id: 'blogging.writer',
    team: 'blogging',
    saved_input_id: null,
    status: 'ok',
    duration_ms: 120,
    trace_id: 'trace-aaaaaaaa',
    author: 'brandon',
    created_at: '2026-08-01T00:00:00Z',
  };

  const runB: RunSummary = { ...runA, id: 'run-b', trace_id: 'trace-bbbbbbbb' };

  let fixture: ComponentFixture<AgentRunHistoryComponent>;
  let component: AgentRunHistoryComponent;
  let apiMock: { listRuns: ReturnType<typeof vi.fn>; deleteRun: ReturnType<typeof vi.fn> };
  let notifyMock: { saved: ReturnType<typeof vi.fn> };
  let dialogOpen: ReturnType<typeof vi.fn>;
  let confirmResult: boolean;

  beforeEach(async () => {
    confirmResult = true;
    apiMock = {
      listRuns: vi.fn().mockReturnValue(of([runA, runB])),
      deleteRun: vi.fn().mockReturnValue(of({ id: 'run-a', status: 'deleted' })),
    };
    notifyMock = { saved: vi.fn() };
    dialogOpen = vi.fn().mockImplementation(() => ({ afterClosed: () => of(confirmResult) }));

    await TestBed.configureTestingModule({
      imports: [AgentRunHistoryComponent, NoopAnimationsModule],
      providers: [
        { provide: AgentConsoleApiService, useValue: apiMock },
        { provide: NotificationService, useValue: notifyMock },
      ],
    }).compileComponents();
    TestBed.overrideProvider(MatDialog, { useValue: { open: dialogOpen } });

    fixture = TestBed.createComponent(AgentRunHistoryComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('agentId', 'blogging.writer');
    fixture.detectChanges();
  });

  it('does not call the native confirm() dialog', () => {
    const confirmSpy = vi.spyOn(window, 'confirm');

    component.deleteRun(runA, { stopPropagation: vi.fn() } as unknown as Event);

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(dialogOpen).toHaveBeenCalledTimes(1);
  });

  it('stops event propagation and opens the Material confirm dialog with danger styling', () => {
    const event = { stopPropagation: vi.fn() } as unknown as Event;

    component.deleteRun(runA, event);

    expect(event.stopPropagation).toHaveBeenCalled();
    expect(dialogOpen.mock.calls[0][1].data).toMatchObject({ variant: 'danger' });
  });

  it('deletes the run and removes it from the list on confirm', () => {
    confirmResult = true;
    expect(component.runs().map((r) => r.id)).toEqual(['run-a', 'run-b']);

    component.deleteRun(runA, { stopPropagation: vi.fn() } as unknown as Event);

    expect(apiMock.deleteRun).toHaveBeenCalledWith('run-a');
    expect(component.runs().map((r) => r.id)).toEqual(['run-b']);
    expect(notifyMock.saved).toHaveBeenCalledWith('Run deleted.');
  });

  it('does not delete or mutate the list when the dialog is cancelled', () => {
    confirmResult = false;

    component.deleteRun(runA, { stopPropagation: vi.fn() } as unknown as Event);

    expect(apiMock.deleteRun).not.toHaveBeenCalled();
    expect(component.runs().map((r) => r.id)).toEqual(['run-a', 'run-b']);
  });
});
