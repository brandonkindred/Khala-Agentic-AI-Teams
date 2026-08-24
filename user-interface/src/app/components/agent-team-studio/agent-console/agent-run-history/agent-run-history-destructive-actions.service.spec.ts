import { TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { Observable, Subject, of, throwError } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentRunHistoryDestructiveActionsService } from './agent-run-history-destructive-actions.service';
import { AgentConsoleApiService } from '../../../../services/agent-console-api.service';
import { NotificationService } from '../../../../core/notification.service';
import { ConfirmDestructiveService } from '../../../../shared/confirm-destructive.service';
import type { RunSummary } from '../../../../models/agent-history.model';

describe('AgentRunHistoryDestructiveActionsService', () => {
  let service: AgentRunHistoryDestructiveActionsService;
  let apiSpy: { deleteRun: ReturnType<typeof vi.fn> };
  let notifySpy: { saved: ReturnType<typeof vi.fn> };
  let dialogSpy: { open: ReturnType<typeof vi.fn> };
  // Read lazily by the dialog stub's afterClosed(), so a test can set the
  // outcome before invoking the action under test.
  let confirmResult: boolean;

  const run = {
    id: 'run-1',
    agent_id: 'blogging.writer',
    trace_id: 'abcdef1234567890',
  } as unknown as RunSummary;

  const otherRun = {
    id: 'run-2',
    agent_id: 'blogging.writer',
    trace_id: 'fedcba9876543210',
  } as unknown as RunSummary;

  beforeEach(() => {
    confirmResult = true;
    apiSpy = { deleteRun: vi.fn().mockReturnValue(of({ id: 'run-1', status: 'deleted' })) };
    notifySpy = { saved: vi.fn() };
    dialogSpy = {
      open: vi.fn().mockReturnValue({ afterClosed: () => of(confirmResult) }),
    };

    TestBed.configureTestingModule({
      providers: [
        AgentRunHistoryDestructiveActionsService,
        ConfirmDestructiveService,
        { provide: AgentConsoleApiService, useValue: apiSpy },
        { provide: NotificationService, useValue: notifySpy },
        { provide: MatDialog, useValue: dialogSpy },
      ],
    });
    service = TestBed.inject(AgentRunHistoryDestructiveActionsService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  describe('deleteRun', () => {
    it('opens a danger confirm dialog and deletes when confirmed', () => {
      confirmResult = true;
      const deleted: { agentId: string; payload: string }[] = [];
      service.runDeleted$.subscribe((e) => deleted.push(e));

      service.deleteRun(run);

      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      expect(dialogSpy.open.mock.calls[0][1].data).toMatchObject({
        title: 'Delete run',
        message: "Delete run abcdef12? This can't be undone.",
        confirmLabel: 'Delete',
        variant: 'danger',
      });
      expect(apiSpy.deleteRun).toHaveBeenCalledWith('run-1');
      expect(deleted).toEqual([{ agentId: 'blogging.writer', payload: 'run-1' }]);
      expect(notifySpy.saved).toHaveBeenCalledWith('Run deleted.');
      expect(service.isDeleting('run-1')).toBe(false);
    });

    it('does not delete when the dialog is cancelled', () => {
      confirmResult = false;

      service.deleteRun(run);

      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      expect(apiSpy.deleteRun).not.toHaveBeenCalled();
      expect(notifySpy.saved).not.toHaveBeenCalled();
    });

    it('sets isDeleting during the API call and clears it on success', () => {
      confirmResult = true;
      apiSpy.deleteRun.mockReturnValue(
        new Observable((subscriber) => {
          expect(service.isDeleting('run-1')).toBe(true);
          subscriber.next({ id: 'run-1', status: 'deleted' });
          subscriber.complete();
        }),
      );

      service.deleteRun(run);

      expect(service.isDeleting('run-1')).toBe(false);
    });

    it('surfaces an error on errors$ tagged with the agent and skips the toast when delete fails after confirm', () => {
      confirmResult = true;
      apiSpy.deleteRun.mockReturnValueOnce(throwError(() => ({ error: { detail: 'boom' } })));
      const messages: { agentId: string; message: string | null }[] = [];
      service.errors$.subscribe((m) => messages.push(m));
      const deleted: { agentId: string; payload: string }[] = [];
      service.runDeleted$.subscribe((e) => deleted.push(e));

      service.deleteRun(run);

      expect(messages).toEqual([
        { agentId: 'blogging.writer', message: null },
        { agentId: 'blogging.writer', message: 'boom' },
      ]);
      expect(deleted).toEqual([]);
      expect(service.isDeleting('run-1')).toBe(false);
      expect(notifySpy.saved).not.toHaveBeenCalled();
    });

    it('does not open a second confirmation while one dialog is still pending', () => {
      // A dialog that has not resolved yet (afterClosed has not emitted), so the
      // re-entrancy guard should stay engaged across a rapid second activation.
      const closed$ = new Subject<boolean>();
      dialogSpy.open.mockReturnValue({ afterClosed: () => closed$.asObservable() });

      service.deleteRun(run);
      service.deleteRun(run); // rapid second activation before the first closes

      expect(dialogSpy.open).toHaveBeenCalledTimes(1);
      expect(apiSpy.deleteRun).not.toHaveBeenCalled();

      // Closing the first dialog releases the guard so later actions work again.
      closed$.next(false);
      closed$.complete();
      service.deleteRun(run);
      expect(dialogSpy.open).toHaveBeenCalledTimes(2);
    });

    it('tracks each in-flight run independently so one finishing does not clear another', () => {
      // Two distinct runs confirmed back-to-back — each opens its own dialog
      // (the re-entrancy guard only blocks a *second* concurrent dialog, and
      // by the time run-2's delete is confirmed, run-1's dialog has already
      // closed), so both API calls can be genuinely in flight at once.
      const runOneResponse$ = new Subject<{ id: string; status: string }>();
      apiSpy.deleteRun.mockImplementation((id: string) =>
        id === 'run-1' ? runOneResponse$.asObservable() : of({ id: 'run-2', status: 'deleted' }),
      );

      service.deleteRun(run);
      service.deleteRun(otherRun);

      expect(service.isDeleting('run-1')).toBe(true);
      expect(service.isDeleting('run-2')).toBe(false);

      runOneResponse$.next({ id: 'run-1', status: 'deleted' });
      runOneResponse$.complete();

      expect(service.isDeleting('run-1')).toBe(false);
    });

    it('tags emissions with the run’s own agent_id, not a shared/global agent', () => {
      const runForAgentB = { ...otherRun, agent_id: 'blogging.editor' } as unknown as RunSummary;
      apiSpy.deleteRun.mockReturnValue(of({ id: 'run-2', status: 'deleted' }));
      const deleted: { agentId: string; payload: string }[] = [];
      service.runDeleted$.subscribe((e) => deleted.push(e));

      service.deleteRun(run); // agent_id: blogging.writer
      service.deleteRun(runForAgentB); // agent_id: blogging.editor

      expect(deleted).toEqual([
        { agentId: 'blogging.writer', payload: 'run-1' },
        { agentId: 'blogging.editor', payload: 'run-2' },
      ]);
    });
  });
});
