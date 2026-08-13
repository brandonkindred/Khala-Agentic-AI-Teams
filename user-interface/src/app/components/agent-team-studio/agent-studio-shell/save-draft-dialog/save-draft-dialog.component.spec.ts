import { TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { of, throwError } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  SaveDraftDialogComponent,
  type SaveDraftDialogData,
} from './save-draft-dialog.component';
import { AgentStudioFacade } from '../../../../services/agent-studio.facade';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

const summary = (id: string, name: string): AgentStudioDraftSummary => ({
  draft_id: id,
  name,
  updated_at: '2026-01-01T00:00:00Z',
});

function configure(
  data: SaveDraftDialogData,
  saveDraft = vi.fn().mockReturnValue(of(summary('new-1', 'x'))),
) {
  const ref = { close: vi.fn() };
  const facade = { saveDraft };
  TestBed.configureTestingModule({
    imports: [SaveDraftDialogComponent],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data },
      { provide: MatDialogRef, useValue: ref },
      { provide: AgentStudioFacade, useValue: facade },
    ],
  });
  const fixture = TestBed.createComponent(SaveDraftDialogComponent);
  return { fixture, ref, facade };
}

describe('SaveDraftDialogComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('pre-fills the name from initialName when re-saving an existing draft', () => {
    const { fixture } = configure({ draftId: 'd-1', initialName: 'My draft', payload: {} });
    expect(fixture.componentInstance.name()).toBe('My draft');
  });

  it('falls back to a non-empty timestamp default when initialName is null', () => {
    const { fixture } = configure({ draftId: null, initialName: null, payload: {} });
    expect(fixture.componentInstance.name().length).toBeGreaterThan(0);
  });

  it('submit creates a new draft when draftId is null', () => {
    const saveDraft = vi.fn().mockReturnValue(of(summary('new-1', 'My draft')));
    const payload = { registryAgentId: 'reg-1' };
    const { fixture, ref, facade } = configure(
      { draftId: null, initialName: 'My draft', payload },
      saveDraft,
    );
    fixture.componentInstance.submit();
    expect(saveDraft).toHaveBeenCalledWith({ name: 'My draft', payload }, null);
    expect(ref.close).toHaveBeenCalledWith(summary('new-1', 'My draft'));
    expect(facade.saveDraft).toHaveBeenCalledTimes(1);
  });

  it('submit updates the existing draft when draftId is set', () => {
    const saveDraft = vi.fn().mockReturnValue(of(summary('d-1', 'Renamed')));
    const payload = { teamId: 'team-1' };
    const { fixture, ref } = configure(
      { draftId: 'd-1', initialName: 'Old name', payload },
      saveDraft,
    );
    fixture.componentInstance.name.set('Renamed');
    fixture.componentInstance.submit();
    expect(saveDraft).toHaveBeenCalledWith({ name: 'Renamed', payload }, 'd-1');
    expect(ref.close).toHaveBeenCalledWith(summary('d-1', 'Renamed'));
  });

  it('a blank name sets a server error and does not call the API', () => {
    const { fixture, ref, facade } = configure({ draftId: null, initialName: null, payload: {} });
    fixture.componentInstance.name.set('   ');
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('Name is required.');
    expect(facade.saveDraft).not.toHaveBeenCalled();
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('an API error surfaces serverError, resets busy, and keeps the dialog open', () => {
    const saveDraft = vi.fn().mockReturnValue(
      throwError(() => ({ error: { detail: 'Name already taken' } })),
    );
    const { fixture, ref } = configure(
      { draftId: null, initialName: 'My draft', payload: {} },
      saveDraft,
    );
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.busy()).toBe(false);
    expect(fixture.componentInstance.serverError()).toBe('Name already taken');
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('falls back to err.message, then a generic message, when no detail is present', () => {
    const saveDraft = vi.fn().mockReturnValue(throwError(() => ({ message: 'network down' })));
    const { fixture } = configure(
      { draftId: null, initialName: 'My draft', payload: {} },
      saveDraft,
    );
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('network down');
  });

  it('falls back to a generic message when the error has neither detail nor message', () => {
    const saveDraft = vi.fn().mockReturnValue(throwError(() => ({})));
    const { fixture } = configure(
      { draftId: null, initialName: 'My draft', payload: {} },
      saveDraft,
    );
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('Failed to save draft.');
  });

  it('cancel closes with no result', () => {
    const { fixture, ref } = configure({ draftId: null, initialName: null, payload: {} });
    fixture.componentInstance.cancel();
    expect(ref.close).toHaveBeenCalledWith();
  });

  it('cancel is a no-op while a save is in flight', () => {
    // A call that never emits keeps busy() true, simulating an in-flight request.
    const saveDraft = vi.fn().mockReturnValue({ subscribe: () => undefined });
    const { fixture, ref } = configure(
      { draftId: null, initialName: 'My draft', payload: {} },
      saveDraft,
    );
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.busy()).toBe(true);
    fixture.componentInstance.cancel();
    expect(ref.close).not.toHaveBeenCalled();
  });
});
