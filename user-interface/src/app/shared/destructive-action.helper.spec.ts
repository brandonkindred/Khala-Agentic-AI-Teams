import { DestroyRef, EnvironmentInjector, createEnvironmentInjector } from '@angular/core';
import { MatDialog } from '@angular/material/dialog';
import { of, Subject, throwError } from 'rxjs';
import { vi } from 'vitest';
import { DestructiveActionHelper, DestructiveActionOptions } from './destructive-action.helper';
import { NotificationService } from '../core/notification.service';

describe('DestructiveActionHelper', () => {
  let dialog: { open: ReturnType<typeof vi.fn> };
  let notify: { saved: ReturnType<typeof vi.fn> };
  let destroyRef: DestroyRef;
  let onError: ReturnType<typeof vi.fn>;
  let helper: DestructiveActionHelper;
  let injector: EnvironmentInjector;

  beforeEach(() => {
    dialog = { open: vi.fn() };
    notify = { saved: vi.fn() };
    // Create a real EnvironmentInjector to get a working DestroyRef.
    injector = createEnvironmentInjector([], undefined as unknown as EnvironmentInjector);
    destroyRef = injector.get(DestroyRef);
    onError = vi.fn();

    helper = new DestructiveActionHelper(
      dialog as unknown as MatDialog,
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

  function mockDialogResult(result: boolean | undefined) {
    dialog.open.mockReturnValue({ afterClosed: () => of(result) });
  }

  describe('execute — confirmation', () => {
    it('opens the confirm dialog with supplied data', () => {
      mockDialogResult(false);
      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => of(null),
        onSuccess: vi.fn(),
        errorFallback: 'fail',
      };

      helper.execute(opts, 'Done.');

      expect(dialog.open).toHaveBeenCalledWith(
        expect.anything(),
        { data: dialogData },
      );
    });

    it('does not call apiCall when the user cancels', () => {
      mockDialogResult(false);
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

    it('does not call apiCall when the dialog is dismissed (undefined)', () => {
      mockDialogResult(undefined);
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
    it('blocks a second dialog while the first is still open', () => {
      const dialogClose$ = new Subject<boolean | undefined>();
      dialog.open.mockReturnValue({ afterClosed: () => dialogClose$.asObservable() });

      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => of(null),
        onSuccess: vi.fn(),
        errorFallback: 'fail',
      };

      helper.execute(opts, 'Done.');
      helper.execute(opts, 'Done.');

      // Only one dialog opened despite two calls.
      expect(dialog.open).toHaveBeenCalledTimes(1);
    });

    it('releases the guard after the dialog closes', () => {
      const dialogClose$ = new Subject<boolean | undefined>();
      dialog.open.mockReturnValue({ afterClosed: () => dialogClose$.asObservable() });

      const opts: DestructiveActionOptions = {
        dialogData,
        apiCall: () => of(null),
        onSuccess: vi.fn(),
        errorFallback: 'fail',
      };

      helper.execute(opts, 'Done.');

      // Close dialog with cancel — emits false then completes.
      dialogClose$.next(false);
      dialogClose$.complete();

      // Guard released — new dialog can open.
      const dialogClose2$ = new Subject<boolean | undefined>();
      dialog.open.mockReturnValue({ afterClosed: () => dialogClose2$.asObservable() });
      helper.execute(opts, 'Done.');
      expect(dialog.open).toHaveBeenCalledTimes(2);

      dialogClose2$.next(false);
      dialogClose2$.complete();
    });
  });

  describe('execute — success path', () => {
    it('calls onStart, apiCall, onSuccess, toast, and onFinally in order', () => {
      mockDialogResult(true);
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
      mockDialogResult(true);
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
      mockDialogResult(true);
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
      mockDialogResult(true);
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
      mockDialogResult(true);
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
