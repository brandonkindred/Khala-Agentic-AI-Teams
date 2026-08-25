import { EnvironmentInjector, createEnvironmentInjector } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Observable, of, Subject, throwError } from 'rxjs';
import { vi } from 'vitest';
import {
  AgentRunnerDestructiveActionsService,
  AgentTaggedError,
  AgentTaggedEvent,
} from './agent-runner-destructive-actions.service';
import { AgentConsoleApiService } from './agent-console-api.service';
import { NotificationService } from '../core/notification.service';
import { ConfirmDestructiveService } from '../shared/confirm-destructive.service';

describe('AgentRunnerDestructiveActionsService', () => {
  let service: AgentRunnerDestructiveActionsService;
  let confirmFn: ReturnType<typeof vi.fn>;
  let runnerApi: {
    deleteSavedInput: ReturnType<typeof vi.fn>;
    teardown: ReturnType<typeof vi.fn>;
  };
  let notify: { saved: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    confirmFn = vi.fn();
    runnerApi = {
      deleteSavedInput: vi.fn(),
      teardown: vi.fn(),
    };
    notify = { saved: vi.fn() };

    TestBed.configureTestingModule({
      providers: [
        AgentRunnerDestructiveActionsService,
        { provide: ConfirmDestructiveService, useValue: { confirm: confirmFn } },
        { provide: AgentConsoleApiService, useValue: runnerApi },
        { provide: NotificationService, useValue: notify },
      ],
    });

    service = TestBed.inject(AgentRunnerDestructiveActionsService);
  });

  function mockDialogResult(result: boolean) {
    confirmFn.mockReturnValue(of(result));
  }

  describe('deleteSavedInput', () => {
    it('opens a danger confirm dialog with the saved input name', () => {
      mockDialogResult(false);
      service.deleteSavedInput('blogging.writer', 'id-1', 'My Input');

      expect(confirmFn).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Delete saved input',
          message: expect.stringContaining('My Input'),
          confirmLabel: 'Delete',
          variant: 'danger',
        }),
      );
    });

    it('does not call the API when the user cancels', () => {
      mockDialogResult(false);
      service.deleteSavedInput('blogging.writer', 'id-1', 'My Input');
      expect(runnerApi.deleteSavedInput).not.toHaveBeenCalled();
    });

    it('sets deletingSavedInputId during the API call and clears it on success', () => {
      mockDialogResult(true);
      runnerApi.deleteSavedInput.mockReturnValue(
        new Observable((subscriber) => {
          expect(service.deletingSavedInputId()).toBe('id-1');
          subscriber.next({ id: 'id-1', status: 'deleted' });
          subscriber.complete();
        }),
      );

      expect(service.deletingSavedInputId()).toBeNull();
      service.deleteSavedInput('blogging.writer', 'id-1', 'My Input');
      expect(service.deletingSavedInputId()).toBeNull();
    });

    it('emits the saved id tagged with the agent through savedInputDeleted$ on success', () => {
      mockDialogResult(true);
      runnerApi.deleteSavedInput.mockReturnValue(of({ id: 'id-1', status: 'deleted' }));

      const emitted: AgentTaggedEvent<string>[] = [];
      service.savedInputDeleted$.subscribe((e) => emitted.push(e));

      service.deleteSavedInput('blogging.writer', 'id-1', 'My Input');

      expect(runnerApi.deleteSavedInput).toHaveBeenCalledWith('id-1');
      expect(emitted).toEqual([{ agentId: 'blogging.writer', payload: 'id-1' }]);
    });

    it('shows a success toast on success', () => {
      mockDialogResult(true);
      runnerApi.deleteSavedInput.mockReturnValue(of({ id: 'id-1', status: 'deleted' }));

      service.deleteSavedInput('blogging.writer', 'id-1', 'My Input');

      expect(notify.saved).toHaveBeenCalledWith('Saved input deleted.');
    });

    it('emits the error tagged with the agent through errors$ on failure', () => {
      mockDialogResult(true);
      runnerApi.deleteSavedInput.mockReturnValue(
        throwError(() => ({ error: { detail: 'not found' } })),
      );

      const errors: AgentTaggedError[] = [];
      service.errors$.subscribe((e) => errors.push(e));
      const deleted: AgentTaggedEvent<string>[] = [];
      service.savedInputDeleted$.subscribe((e) => deleted.push(e));

      service.deleteSavedInput('blogging.writer', 'id-1', 'My Input');

      expect(errors).toEqual([
        { agentId: 'blogging.writer', message: null },
        { agentId: 'blogging.writer', message: 'not found' },
      ]);
      expect(deleted).toEqual([]);
      expect(service.deletingSavedInputId()).toBeNull();
      expect(notify.saved).not.toHaveBeenCalled();
    });

    it('does not double-fire the API when called rapidly while a confirm is in flight', () => {
      const confirm$ = new Subject<boolean>();
      confirmFn.mockReturnValueOnce(confirm$.asObservable()); // first call: dialog still open
      confirmFn.mockReturnValueOnce(of(false)); // rapid second call: guard short-circuits, matching ConfirmDestructiveService
      runnerApi.deleteSavedInput.mockReturnValue(of({ id: 'id-1', status: 'deleted' }));

      service.deleteSavedInput('blogging.writer', 'id-1', 'My Input');
      service.deleteSavedInput('blogging.writer', 'id-1', 'My Input');

      expect(confirmFn).toHaveBeenCalledTimes(2);
      expect(runnerApi.deleteSavedInput).not.toHaveBeenCalled();

      confirm$.next(true);
      confirm$.complete();

      expect(runnerApi.deleteSavedInput).toHaveBeenCalledTimes(1);
    });
  });

  describe('tearDownSandbox', () => {
    it('opens a danger confirm dialog with the agent label', () => {
      mockDialogResult(false);
      service.tearDownSandbox('agent-1', 'Writer');

      expect(confirmFn).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Tear down sandbox',
          message: expect.stringContaining('Writer'),
          confirmLabel: 'Tear down',
          variant: 'danger',
        }),
      );
    });

    it('does not call the API when the user cancels', () => {
      mockDialogResult(false);
      service.tearDownSandbox('agent-1', 'Writer');
      expect(runnerApi.teardown).not.toHaveBeenCalled();
    });

    it('sets tearingDown during the API call and clears it on success', () => {
      mockDialogResult(true);
      runnerApi.teardown.mockReturnValue(
        new Observable((subscriber) => {
          expect(service.tearingDown()).toBe(true);
          subscriber.next({ agent_id: 'agent-1', status: 'stopped' });
          subscriber.complete();
        }),
      );

      expect(service.tearingDown()).toBe(false);
      service.tearDownSandbox('agent-1', 'Writer');
      expect(service.tearingDown()).toBe(false);
    });

    it('emits through sandboxTornDown$ tagged with the agent on success', () => {
      mockDialogResult(true);
      runnerApi.teardown.mockReturnValue(of({ agent_id: 'agent-1', status: 'stopped' }));

      const emitted: AgentTaggedEvent[] = [];
      service.sandboxTornDown$.subscribe((e) => emitted.push(e));

      service.tearDownSandbox('agent-1', 'Writer');

      expect(runnerApi.teardown).toHaveBeenCalledWith('agent-1');
      expect(emitted).toEqual([{ agentId: 'agent-1', payload: undefined }]);
    });

    it('shows a success toast on success', () => {
      mockDialogResult(true);
      runnerApi.teardown.mockReturnValue(of({ agent_id: 'agent-1', status: 'stopped' }));

      service.tearDownSandbox('agent-1', 'Writer');

      expect(notify.saved).toHaveBeenCalledWith('Sandbox torn down.');
    });

    it('emits the error tagged with the agent through errors$ on failure', () => {
      mockDialogResult(true);
      runnerApi.teardown.mockReturnValue(
        throwError(() => ({ error: { detail: 'teardown refused' } })),
      );

      const errors: AgentTaggedError[] = [];
      service.errors$.subscribe((e) => errors.push(e));
      let tornDown = false;
      service.sandboxTornDown$.subscribe(() => { tornDown = true; });

      service.tearDownSandbox('agent-1', 'Writer');

      expect(errors).toEqual([
        { agentId: 'agent-1', message: null },
        { agentId: 'agent-1', message: 'teardown refused' },
      ]);
      expect(tornDown).toBe(false);
      expect(service.tearingDown()).toBe(false);
      expect(notify.saved).not.toHaveBeenCalled();
    });

    it('does not double-fire the API when called rapidly while a confirm is in flight', () => {
      const confirm$ = new Subject<boolean>();
      confirmFn.mockReturnValueOnce(confirm$.asObservable()); // first call: dialog still open
      confirmFn.mockReturnValueOnce(of(false)); // rapid second call: guard short-circuits, matching ConfirmDestructiveService
      runnerApi.teardown.mockReturnValue(of({ agent_id: 'agent-1', status: 'stopped' }));

      service.tearDownSandbox('agent-1', 'Writer');
      service.tearDownSandbox('agent-1', 'Writer');

      expect(confirmFn).toHaveBeenCalledTimes(2);
      expect(runnerApi.teardown).not.toHaveBeenCalled();

      confirm$.next(true);
      confirm$.complete();

      expect(runnerApi.teardown).toHaveBeenCalledTimes(1);
    });
  });

  describe('lifecycle cleanup', () => {
    let childInjector: EnvironmentInjector;

    afterEach(() => {
      try {
        childInjector?.destroy();
      } catch {
        /* already destroyed by test */
      }
    });

    // Provides the service on its own child EnvironmentInjector (mirroring a
    // component-level provider) so destroying that injector fires the
    // service's own DestroyRef, exercising takeUntilDestroyed independently
    // of the shared TestBed module injector used by the other tests.
    function createScopedService(): AgentRunnerDestructiveActionsService {
      childInjector = createEnvironmentInjector(
        [
          AgentRunnerDestructiveActionsService,
          { provide: ConfirmDestructiveService, useValue: { confirm: confirmFn } },
          { provide: AgentConsoleApiService, useValue: runnerApi },
          { provide: NotificationService, useValue: notify },
        ],
        TestBed.inject(EnvironmentInjector),
      );
      return childInjector.get(AgentRunnerDestructiveActionsService);
    }

    it('does not emit through savedInputDeleted$ or leave deletingSavedInputId stuck when destroyed before deleteSavedInput responds', () => {
      const scoped = createScopedService();
      mockDialogResult(true);
      const inflight$ = new Subject<{ id: string; status: string }>();
      runnerApi.deleteSavedInput.mockReturnValue(inflight$.asObservable());

      const emitted: AgentTaggedEvent<string>[] = [];
      scoped.savedInputDeleted$.subscribe((e) => emitted.push(e));

      scoped.deleteSavedInput('blogging.writer', 'id-1', 'My Input');
      expect(scoped.deletingSavedInputId()).toBe('id-1');

      childInjector.destroy();

      // finalize() still runs on teardown, so the loading signal resets
      // even though the API call never emitted.
      expect(scoped.deletingSavedInputId()).toBeNull();
      expect(emitted).toEqual([]);
      expect(notify.saved).not.toHaveBeenCalled();

      // A late emission after destroy must have no observable effect —
      // proves the subscription was actually torn down, not just pending.
      inflight$.next({ id: 'id-1', status: 'deleted' });
      expect(emitted).toEqual([]);
      expect(notify.saved).not.toHaveBeenCalled();
    });

    it('does not emit through sandboxTornDown$ or leave tearingDown stuck when destroyed before tearDownSandbox responds', () => {
      const scoped = createScopedService();
      mockDialogResult(true);
      const inflight$ = new Subject<{ agent_id: string; status: string }>();
      runnerApi.teardown.mockReturnValue(inflight$.asObservable());

      const emitted: AgentTaggedEvent[] = [];
      scoped.sandboxTornDown$.subscribe((e) => emitted.push(e));

      scoped.tearDownSandbox('agent-1', 'Writer');
      expect(scoped.tearingDown()).toBe(true);

      childInjector.destroy();

      expect(scoped.tearingDown()).toBe(false);
      expect(emitted).toEqual([]);
      expect(notify.saved).not.toHaveBeenCalled();

      inflight$.next({ agent_id: 'agent-1', status: 'stopped' });
      expect(emitted).toEqual([]);
      expect(notify.saved).not.toHaveBeenCalled();
    });
  });
});
