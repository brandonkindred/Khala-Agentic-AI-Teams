import { TestBed } from '@angular/core/testing';
import { MatDialogRef } from '@angular/material/dialog';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DraftConflictDialogComponent } from './draft-conflict-dialog.component';

function configure() {
  const ref = { close: vi.fn() };
  TestBed.configureTestingModule({
    imports: [DraftConflictDialogComponent],
    providers: [{ provide: MatDialogRef, useValue: ref }],
  });
  const fixture = TestBed.createComponent(DraftConflictDialogComponent);
  fixture.detectChanges();
  return { fixture, ref };
}

describe('DraftConflictDialogComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('Save first closes with save', () => {
    const { fixture, ref } = configure();
    fixture.componentInstance.save();
    expect(ref.close).toHaveBeenCalledWith('save');
  });

  it('Discard closes with discard', () => {
    const { fixture, ref } = configure();
    fixture.componentInstance.discard();
    expect(ref.close).toHaveBeenCalledWith('discard');
  });

  it('Cancel closes with no result', () => {
    const { fixture, ref } = configure();
    fixture.componentInstance.cancel();
    expect(ref.close).toHaveBeenCalledWith();
  });

  it('renders the spec copy and three actions', () => {
    const { fixture } = configure();
    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Unsaved changes');
    expect(text).toContain('You have unsaved changes — save them first, or discard?');
    expect(text).toContain('Save first');
    expect(text).toContain('Discard');
    expect(text).toContain('Cancel');
  });
});
