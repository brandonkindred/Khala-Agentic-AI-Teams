import { TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentRunnerDestructiveActionsService } from './agent-runner-destructive-actions.service';
import { AgentRunnerApiService } from './agent-runner-api.service';
import { NotificationService } from '../core/notification.service';

describe('AgentRunnerDestructiveActionsService', () => {
  let service: AgentRunnerDestructiveActionsService;
  let dialogOpen: ReturnType<typeof vi.fn>;
  let runnerApi: {
    deleteSavedInput: ReturnType<typeof vi.fn>;
    teardown: ReturnType<typeof vi.fn>;
  };
  let notify: { saved: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    dialogOpen = vi.fn();
    runnerApi = {
      deleteSavedInput: vi.fn(),
      teardown: vi.fn(),
    };
    notify = { saved: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        AgentRunnerDestructiveActionsService,
        { provide: MatDialog, useValue: { open: dialogOpen } },
        { provide: AgentRunnerApiService, useValue: runnerApi },
        { provide: NotificationService, useValue: notify },
      ],
    });

    service = TestBed.inject(AgentRunnerDestructiveActionsService);
  });

  function mockDialogResult(result: boolean | undefined) {
    dialogOpen.mockReturnValue({ afterClosed: () => of(result) });
  }

  describe('deleteSavedInput', () => {
    it('opens a danger confirm dialog with the saved input name', () => {
      mockDialogResult(false);
      service.deleteSavedInput('id-1', 'My Input');

      expect(dialogOpen).toHaveBeenCalledWith(
        expect.anything(),
        {
          data: expect.objectContaining({
            title: 'Delete saved input',
            confirmLabel: 'Delete',
            variant: 'danger',
          }),
        },
      );
    });

    it('does not call the API when the user cancels', () => {
      mockDialogResult(false);
      service.deleteSavedInput('id-1', 'My Input');
      expect(runnerApi.deleteSavedInput).not.toHaveBeenCalled();
    });

    it('sets deletingSavedInputId during the API call and clears it on success', () => {
      mockDialogResult(true);
      runnerApi.deleteSavedInput.mockReturnValue(of({ id: 'id-1', status: 'deleted' }));

      // Signal starts null.
      expect(service.deletingSavedInputId()).toBeNull();

      service.deleteSavedInput('id-1', 'My Input');

      // After success, loading signal is cleared.
      expect(service.deletingSavedInputId()).toBeNull();
    });

    it('emits the saved id through savedInputDeleted$ on success', () => {
      mockDialogResult(true);
      runnerApi.deleteSavedInput.mockReturnValue(of({ id: 'id-1', status: 'deleted' }));

      const emitted: string[] = [];
      service.savedInputDeleted$.subscribe((id) => emitted.push(id));

      service.deleteSavedInput('id-1', 'My Input');

      expect(emitted).toEqual(['id-1']);
    });

    it('shows a success toast on success', () => {
      mockDialogResult(true);
      runnerApi.deleteSavedInput.mockReturnValue(of({ id: 'id-1', status: 'deleted' }));

      service.deleteSavedInput('id-1', 'My Input');

      expect(notify.saved).toHaveBeenCalledWith('Saved input deleted.');
    });

    it('emits the error message through errors$ on failure and clears loading', () => {
      mockDialogResult(true);
      runnerApi.deleteSavedInput.mockReturnValue(
        throwError(() => ({ error: { detail: 'not found' } })),
      );

      const errors: Array<string | null> = [];
      service.errors$.subscribe((msg) => errors.push(msg));

      service.deleteSavedInput('id-1', 'My Input');

      // First emission is null (clearing previous error), second is the failure.
      expect(errors).toEqual([null, 'not found']);
      expect(service.deletingSavedInputId()).toBeNull();
      expect(notify.saved).not.toHaveBeenCalled();
    });
  });

  describe('tearDownSandbox', () => {
    it('opens a danger confirm dialog with the agent label', () => {
      mockDialogResult(false);
      service.tearDownSandbox('agent-1', 'Writer');

      expect(dialogOpen).toHaveBeenCalledWith(
        expect.anything(),
        {
          data: expect.objectContaining({
            title: 'Tear down sandbox',
            confirmLabel: 'Tear down',
            variant: 'danger',
          }),
        },
      );
    });

    it('does not call the API when the user cancels', () => {
      mockDialogResult(false);
      service.tearDownSandbox('agent-1', 'Writer');
      expect(runnerApi.teardown).not.toHaveBeenCalled();
    });

    it('sets tearingDown during the API call and clears it on success', () => {
      mockDialogResult(true);
      runnerApi.teardown.mockReturnValue(of({ agent_id: 'agent-1', status: 'stopped' }));

      expect(service.tearingDown()).toBe(false);

      service.tearDownSandbox('agent-1', 'Writer');

      expect(service.tearingDown()).toBe(false);
    });

    it('emits through sandboxTornDown$ on success', () => {
      mockDialogResult(true);
      runnerApi.teardown.mockReturnValue(of({ agent_id: 'agent-1', status: 'stopped' }));

      let emitted = false;
      service.sandboxTornDown$.subscribe(() => { emitted = true; });

      service.tearDownSandbox('agent-1', 'Writer');

      expect(emitted).toBe(true);
    });

    it('shows a success toast on success', () => {
      mockDialogResult(true);
      runnerApi.teardown.mockReturnValue(of({ agent_id: 'agent-1', status: 'stopped' }));

      service.tearDownSandbox('agent-1', 'Writer');

      expect(notify.saved).toHaveBeenCalledWith('Sandbox torn down.');
    });

    it('emits the error message through errors$ on failure and clears loading', () => {
      mockDialogResult(true);
      runnerApi.teardown.mockReturnValue(
        throwError(() => ({ error: { detail: 'teardown refused' } })),
      );

      const errors: Array<string | null> = [];
      service.errors$.subscribe((msg) => errors.push(msg));

      service.tearDownSandbox('agent-1', 'Writer');

      expect(errors).toEqual([null, 'teardown refused']);
      expect(service.tearingDown()).toBe(false);
      expect(notify.saved).not.toHaveBeenCalled();
    });
  });
});
