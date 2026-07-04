import { TestBed } from '@angular/core/testing';
import { MatSnackBar } from '@angular/material/snack-bar';
import { vi } from 'vitest';
import { NotificationService } from './notification.service';

describe('NotificationService', () => {
  let snackBar: { open: ReturnType<typeof vi.fn> };
  let service: NotificationService;

  beforeEach(() => {
    snackBar = { open: vi.fn() };
    TestBed.configureTestingModule({
      providers: [NotificationService, { provide: MatSnackBar, useValue: snackBar }],
    });
    service = TestBed.inject(NotificationService);
  });

  afterEach(() => TestBed.resetTestingModule());

  it('opens a dismissible 3s snackbar with the message', () => {
    service.saved('Saved.');
    expect(snackBar.open).toHaveBeenCalledWith('Saved.', 'Dismiss', { duration: 3000 });
  });
});
