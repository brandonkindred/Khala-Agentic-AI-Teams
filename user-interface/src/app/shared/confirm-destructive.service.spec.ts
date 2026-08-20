import { TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { Subject, of } from 'rxjs';
import { vi } from 'vitest';
import { ConfirmDestructiveService } from './confirm-destructive.service';
import { ConfirmDialogComponent } from './confirm-dialog/confirm-dialog.component';

describe('ConfirmDestructiveService', () => {
  let service: ConfirmDestructiveService;
  let dialogOpen: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    dialogOpen = vi.fn();
    TestBed.configureTestingModule({
      providers: [
        ConfirmDestructiveService,
        { provide: MatDialog, useValue: { open: dialogOpen } },
      ],
    });
    service = TestBed.inject(ConfirmDestructiveService);
  });

  it('opens the dialog with the provided data and emits true on confirm', () => {
    dialogOpen.mockReturnValue({ afterClosed: () => of(true) });
    let result: boolean | undefined;
    service
      .confirm({ title: 'Delete?', message: 'Really?', variant: 'danger' })
      .subscribe((v) => (result = v));

    expect(dialogOpen).toHaveBeenCalledWith(ConfirmDialogComponent, {
      data: { title: 'Delete?', message: 'Really?', variant: 'danger' },
    });
    expect(result).toBe(true);
  });

  it('forwards confirmLabel and cancelLabel to the dialog data', () => {
    dialogOpen.mockReturnValue({ afterClosed: () => of(false) });
    service
      .confirm({
        title: 'Tear Down',
        message: 'Destroy sandbox?',
        confirmLabel: 'Tear Down',
        cancelLabel: 'Keep',
        variant: 'danger',
      })
      .subscribe(() => { /* no-op */ });

    expect(dialogOpen).toHaveBeenCalledWith(ConfirmDialogComponent, {
      data: {
        title: 'Tear Down',
        message: 'Destroy sandbox?',
        confirmLabel: 'Tear Down',
        cancelLabel: 'Keep',
        variant: 'danger',
      },
    });
  });

  it('emits false on cancel/dismiss', () => {
    dialogOpen.mockReturnValue({ afterClosed: () => of(undefined) });
    let result: boolean | undefined;
    service
      .confirm({ title: 'Delete?', message: 'Really?', variant: 'danger' })
      .subscribe((v) => (result = v));
    expect(result).toBe(false);
  });

  it('blocks re-entrant opens while the dialog is still open', () => {
    const afterClosed$ = new Subject<boolean | undefined>();
    dialogOpen.mockReturnValue({ afterClosed: () => afterClosed$.asObservable() });

    // First call — dialog stays open (afterClosed has not emitted yet).
    let firstResult: boolean | undefined;
    service
      .confirm({ title: 'A', message: 'First', variant: 'danger' })
      .subscribe((v) => (firstResult = v));
    expect(dialogOpen).toHaveBeenCalledTimes(1);

    // Second call while first is still open — should be blocked.
    dialogOpen.mockClear();
    let secondResult: boolean | undefined;
    service
      .confirm({ title: 'B', message: 'Second', variant: 'danger' })
      .subscribe((v) => (secondResult = v));

    // Dialog was NOT opened again; second call emits false immediately.
    expect(dialogOpen).not.toHaveBeenCalled();
    expect(secondResult).toBe(false);

    // First dialog still pending — no result yet.
    expect(firstResult).toBeUndefined();

    // Now the first dialog closes — guard is released, first result arrives.
    afterClosed$.next(true);
    afterClosed$.complete();
    expect(firstResult).toBe(true);

    // Third call after guard is released — should open the dialog again.
    dialogOpen.mockClear();
    dialogOpen.mockReturnValue({ afterClosed: () => of(true) });
    let thirdResult: boolean | undefined;
    service
      .confirm({ title: 'C', message: 'Third', variant: 'danger' })
      .subscribe((v) => (thirdResult = v));
    expect(dialogOpen).toHaveBeenCalledTimes(1);
    expect(thirdResult).toBe(true);
  });

  it('releases the guard and emits an error if dialog.open() throws synchronously', () => {
    const openError = new Error('Missing component');
    dialogOpen.mockImplementation(() => { throw openError; });

    let emittedError: unknown;
    service
      .confirm({ title: 'X', message: 'Boom', variant: 'danger' })
      .subscribe({ error: (e) => (emittedError = e) });

    expect(emittedError).toBe(openError);

    // Guard was released — a subsequent call should open the dialog normally.
    dialogOpen.mockReset();
    dialogOpen.mockReturnValue({ afterClosed: () => of(true) });
    let result: boolean | undefined;
    service
      .confirm({ title: 'Y', message: 'Retry', variant: 'danger' })
      .subscribe((v) => (result = v));
    expect(dialogOpen).toHaveBeenCalledTimes(1);
    expect(result).toBe(true);
  });
});
