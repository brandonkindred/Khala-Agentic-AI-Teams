import { TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { vi } from 'vitest';
import {
  ConfirmDialogComponent,
  type ConfirmDialogData,
} from './confirm-dialog.component';

function configure(data: ConfirmDialogData) {
  const ref = { close: vi.fn() };
  TestBed.configureTestingModule({
    imports: [ConfirmDialogComponent],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data },
      { provide: MatDialogRef, useValue: ref },
    ],
  });
  const fixture = TestBed.createComponent(ConfirmDialogComponent);
  fixture.detectChanges();
  return { fixture, ref };
}

describe('ConfirmDialogComponent', () => {
  beforeEach(() => TestBed.resetTestingModule());

  it('renders title and message from MAT_DIALOG_DATA', () => {
    const { fixture } = configure({
      title: 'Delete job',
      message: 'Permanently delete this job?',
    });
    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Delete job');
    expect(text).toContain('Permanently delete this job?');
  });

  it('uses default Cancel / Confirm labels when none are provided', () => {
    const { fixture } = configure({ title: 't', message: 'm' });
    const c = fixture.componentInstance;
    expect(c.cancelLabel).toBe('Cancel');
    expect(c.confirmLabel).toBe('Confirm');
  });

  it('honors custom confirm and cancel labels', () => {
    const { fixture } = configure({
      title: 't',
      message: 'm',
      confirmLabel: 'Delete',
      cancelLabel: 'Keep',
    });
    const text = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Delete');
    expect(text).toContain('Keep');
  });

  it('closes with true on confirm()', () => {
    const { fixture, ref } = configure({ title: 't', message: 'm' });
    fixture.componentInstance.confirm();
    expect(ref.close).toHaveBeenCalledWith(true);
  });

  it('closes with false on cancel()', () => {
    const { fixture, ref } = configure({ title: 't', message: 'm' });
    fixture.componentInstance.cancel();
    expect(ref.close).toHaveBeenCalledWith(false);
  });

  it('selects warn color and danger class for the danger variant', () => {
    const { fixture } = configure({ title: 't', message: 'm', variant: 'danger' });
    expect(fixture.componentInstance.confirmColor).toBe('warn');
    const danger = fixture.nativeElement.querySelector(
      '.confirm-dialog__confirm--danger',
    );
    expect(danger).not.toBeNull();
  });

  it('selects primary color and no danger class for the default variant', () => {
    const { fixture } = configure({ title: 't', message: 'm' });
    expect(fixture.componentInstance.confirmColor).toBe('primary');
    const danger = fixture.nativeElement.querySelector(
      '.confirm-dialog__confirm--danger',
    );
    expect(danger).toBeNull();
  });

  it('applies the warn class for the warn variant', () => {
    const { fixture } = configure({ title: 't', message: 'm', variant: 'warn' });
    expect(fixture.componentInstance.confirmColor).toBe('primary');
    const warn = fixture.nativeElement.querySelector(
      '.confirm-dialog__confirm--warn',
    );
    expect(warn).not.toBeNull();
  });

  it('focuses the Cancel button initially for the danger variant', () => {
    const { fixture } = configure({ title: 't', message: 'm', variant: 'danger' });
    expect(fixture.componentInstance.cancelIsInitiallyFocused).toBe(true);
    const buttons = fixture.nativeElement.querySelectorAll('button');
    expect(buttons[0].hasAttribute('cdkFocusInitial')).toBe(true);
    expect(buttons[1].hasAttribute('cdkFocusInitial')).toBe(false);
  });

  it('focuses the Confirm button initially for non-danger variants', () => {
    const { fixture } = configure({ title: 't', message: 'm', variant: 'warn' });
    expect(fixture.componentInstance.cancelIsInitiallyFocused).toBe(false);
    const buttons = fixture.nativeElement.querySelectorAll('button');
    expect(buttons[0].hasAttribute('cdkFocusInitial')).toBe(false);
    expect(buttons[1].hasAttribute('cdkFocusInitial')).toBe(true);
  });
});
