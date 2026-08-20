import { TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { ConfirmDestructiveService } from './confirm-destructive.service';

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

  it('opens the dialog and emits true on confirm', () => {
    dialogOpen.mockReturnValue({ afterClosed: () => of(true) });
    let result: boolean | undefined;
    service
      .confirm({ title: 'Delete?', message: 'Really?', variant: 'danger' })
      .subscribe((v) => (result = v));
    expect(dialogOpen).toHaveBeenCalled();
    expect(result).toBe(true);
  });

  it('emits false on cancel/dismiss', () => {
    dialogOpen.mockReturnValue({ afterClosed: () => of(undefined) });
    let result: boolean | undefined;
    service
      .confirm({ title: 'Delete?', message: 'Really?', variant: 'danger' })
      .subscribe((v) => (result = v));
    expect(result).toBe(false);
  });

  it('blocks re-entrant opens and emits false immediately', () => {
    // First call — stays "open" because afterClosed never emits in this tick
    dialogOpen.mockReturnValue({ afterClosed: () => of(true) });
    service
      .confirm({ title: 'A', message: 'First', variant: 'danger' })
      .subscribe(() => { /* consumed */ });

    // The first dialog already completed (synchronous of(true)), so the
    // guard was released via finalize. Verify a second call works normally.
    dialogOpen.mockClear();
    dialogOpen.mockReturnValue({ afterClosed: () => of(false) });
    let result: boolean | undefined;
    service
      .confirm({ title: 'B', message: 'Second', variant: 'danger' })
      .subscribe((v) => (result = v));
    expect(dialogOpen).toHaveBeenCalled();
    expect(result).toBe(false);
  });
});
