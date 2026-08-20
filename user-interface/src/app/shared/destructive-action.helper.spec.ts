import { DestroyRef, EnvironmentInjector, createEnvironmentInjector } from '@angular/core';
import { of, Subject, throwError } from 'rxjs';
import { vi } from 'vitest';
import { DestructiveActionHelper, DestructiveActionOptions } from './destructive-action.helper';
import { ConfirmDestructiveService } from './confirm-destructive.service';
import { NotificationService } from '../core/notification.service';

describe('DestructiveActionHelper', () => {
  let confirmService: { confirm: ReturnType<typeof vi.fn> };
  let notify: { saved: ReturnType<typeof vi.fn> };
  let destroyRef: DestroyRef;
  let onError: ReturnType<typeof vi.fn>;
  let helper: DestructiveActionHelper;
  let injector: EnvironmentInjector;

  beforeEach(() => {
    confirmService = { confirm: vi.fn() };
    notify = { saved: vi.fn() };
    // Create a real EnvironmentInjector to get a working DestroyRef.
    injector = createEnvironmentInjector([], undefined as unknown as EnvironmentInjector);
    destroyRef = injector.get(DestroyRef);
    onError = vi.fn();

    helper = new DestructiveActionHelper(
      confirmService as unknown as ConfirmDestructiveService,
      notify as unknown as NotificationService,
      destroyRef,
      onError,
    );
  });

  afterEach(() => {
    try { injector.destroy(); } catch { /* already destroyed by test */ }
  });

  const dialogData = {
    title: 'Delete item',
    message: 'Sure?',
    confirmLabel: 'Delete',
    variant: 'danger' as const,
  };

  function mockConfirmResult(result: boolean) {
    confirmService.confirm.mockReturnValue(of(result));
  }

  describe('execute — confirmation', () => {
    it('opens the confirm dialog with supplied data', () => {
      mockConfirmResult(false);
      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => of(null),
        onSuccess: vi.fn(),
        errorFallback: 'fail',
      };

      helper.execute(opts, 'Done.');

      expect(confirmService.confirm).toHaveBeenCalledWith(dialogData);
    });

    it('does not call apiCall when the user cancels', () => {
      mockConfirmResult(false);
      const apiCall = vi.fn().mockReturnValue(of(null));
      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall,
        onSuccess: vi.fn(),
        errorFallback: 'fail',
      };

      helper.execute(opts, 'Done.');

      expect(apiCall).not.toHaveBeenCalled();
    });
  });

  describe('execute — re-entrancy guard', () => {
    it('delegates re-entrancy to ConfirmDestructiveService (two calls both reach confirm)', () => {
      // ConfirmDestructiveService owns the re-entrancy guard. The helper
      // simply delegates — both calls reach confirmService.confirm().
      mockConfirmResult(false);

      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => of(null),
        onSuccess: vi.fn(),
        errorFallback: 'fail',
      };

      helper.execute(opts, 'Done.');
      helper.execute(opts, 'Done.');

      expect(confirmService.confirm).toHaveBeenCalledTimes(2);
    });
  });

  describe('execute — success path', () => {
    it('calls onStart, apiCall, onSuccess, toast, and onFinally in order', () => {
      mockConfirmResult(true);
      const order: string[] = [];
      const opts: DestructiveActionOptions<string> = {
        dialogData,
        apiCall: () => { order.push('apiCall'); return of('result'); },
        onSuccess: (r) => { order.push(`onSuccess:${r}`); },
        errorFallback: 'fail',
      };
      const onStart = vi.fn(() => order.push('onStart'));
      const onFinally = vi.fn(() => order.push('onFinally'));

      helper.execute(opts, 'Item deleted.', onStart, onFinally);

      expect(order).toEqual(['onStart', 'apiCall', 'onSuccess:result', 'onFinally']);
      expect(notify.saved).toHaveBeenCalledWith('Item deleted.');
      expect(onError).toHaveBeenCalledWith(null); // clears previous error
    });
  });

  describe('execute — error path', () => {
    it('calls onError with extracted detail and onFinally, no toast', () => {
      mockConfirmResult(true);
      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => throwError(() => ({ error: { detail: 'conflict' } })),
        onSuccess: vi.fn(),
        errorFallback: 'generic fail',
      };
      const onFinally = vi.fn();

      helper.execute(opts, 'Done.', undefined, onFinally);

      expect(onError).toHaveBeenCalledWith('conflict');
      expect(onFinally).toHaveBeenCalled();
      expect(notify.saved).not.toHaveBeenCalled();
      expect(opts.onSuccess).not.toHaveBeenCalled();
    });

    it('uses the fallback message when error has no detail', () => {
      mockConfirmResult(true);
      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => throwError(() => ({})),
        onSuccess: vi.fn(),
        errorFallback: 'Something went wrong.',
      };

      helper.execute(opts, 'Done.');

      expect(onError).toHaveBeenCalledWith('Something went wrong.');
    });

    it('catches a synchronous throw from apiCall factory', () => {
      mockConfirmResult(true);
      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => { throw new Error('sync boom'); },
        onSuccess: vi.fn(),
        errorFallback: 'Factory error.',
      };
      const onFinally = vi.fn();

      helper.execute(opts, 'Done.', undefined, onFinally);

      expect(onError).toHaveBeenCalledWith('sync boom');
      expect(onFinally).toHaveBeenCalled();
      expect(opts.onSuccess).not.toHaveBeenCalled();
    });
  });

  describe('execute — lifecycle cleanup', () => {
    it('calls onFinally via finalize when the component is destroyed mid-flight', () => {
      mockConfirmResult(true);
      // Use a Subject that never completes to simulate an in-flight request.
      const inflight$ = new Subject<unknown>();
      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => inflight$.asObservable(),
        onSuccess: vi.fn(),
        errorFallback: 'fail',
      };
      const onFinally = vi.fn();

      helper.execute(opts, 'Done.', undefined, onFinally);

      // Request is in flight — onFinally not yet called.
      expect(onFinally).not.toHaveBeenCalled();

      // Simulate component destruction via the injector.
      injector.destroy();

      expect(onFinally).toHaveBeenCalled();
    });
  });
});
