import { TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { unsavedChangesGuard, HasUnsavedChanges } from './unsaved-changes.guard';

describe('unsavedChangesGuard', () => {
  let dialog: { open: ReturnType<typeof vi.fn> };

  function run(component: HasUnsavedChanges) {
    return TestBed.runInInjectionContext(() =>
      unsavedChangesGuard(component, null as never, null as never, null as never),
    );
  }

  beforeEach(() => {
    dialog = { open: vi.fn() };
    TestBed.configureTestingModule({
      providers: [{ provide: MatDialog, useValue: dialog }],
    });
  });

  it('allows navigation with no unsaved changes and never opens a dialog', () => {
    expect(run({ hasUnsavedChanges: () => false })).toBe(true);
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('opens the confirm dialog and resolves true when the user discards', async () => {
    dialog.open.mockReturnValue({ afterClosed: () => of(true) });
    const result = run({ hasUnsavedChanges: () => true });
    expect(dialog.open).toHaveBeenCalled();
    await expect(firstValue(result)).resolves.toBe(true);
  });

  it('resolves false when the user keeps editing (cancel/backdrop)', async () => {
    dialog.open.mockReturnValue({ afterClosed: () => of(undefined) });
    const result = run({ hasUnsavedChanges: () => true });
    await expect(firstValue(result)).resolves.toBe(false);
  });

  it('tolerates a component without the method', () => {
    expect(run({} as HasUnsavedChanges)).toBe(true);
  });
});

/** Resolve the guard's boolean | Observable<boolean> to a promise. */
function firstValue(result: boolean | { subscribe: (o: (v: boolean) => void) => void }): Promise<boolean> {
  if (typeof result === 'boolean') return Promise.resolve(result);
  return new Promise((resolve) => result.subscribe(resolve));
}
