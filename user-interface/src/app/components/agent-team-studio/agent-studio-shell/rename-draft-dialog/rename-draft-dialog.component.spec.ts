import { TestBed } from '@angular/core/testing';
import { MAT_DIALOG_DATA, MatDialogRef } from '@angular/material/dialog';
import { NEVER, of, throwError } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  RenameDraftDialogComponent,
  type RenameDraftDialogData,
} from './rename-draft-dialog.component';
import { AgentStudioFacade } from '../../../../services/agent-studio.facade';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

const summary = (id: string, name: string): AgentStudioDraftSummary => ({
  draft_id: id,
  name,
  updated_at: '2026-01-01T00:00:00Z',
});

function configure(
  data: RenameDraftDialogData,
  renameDraft = vi.fn().mockReturnValue(of(summary(data.draftId, data.initialName))),
) {
  const ref = { close: vi.fn() };
  const facade = { renameDraft };
  TestBed.configureTestingModule({
    imports: [RenameDraftDialogComponent],
    providers: [
      { provide: MAT_DIALOG_DATA, useValue: data },
      { provide: MatDialogRef, useValue: ref },
      { provide: AgentStudioFacade, useValue: facade },
    ],
  });
  const fixture = TestBed.createComponent(RenameDraftDialogComponent);
  return { fixture, ref, facade };
}

describe('RenameDraftDialogComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  it('pre-fills the name from initialName', () => {
    const { fixture } = configure({ draftId: 'd-1', initialName: 'My draft' });
    expect(fixture.componentInstance.name()).toBe('My draft');
  });

  it('submit PATCHes the name and closes with the summary', () => {
    const renameDraft = vi.fn().mockReturnValue(of(summary('d-1', 'Renamed')));
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.name.set('Renamed');
    fixture.componentInstance.submit();
    expect(renameDraft).toHaveBeenCalledWith('d-1', 'Renamed');
    expect(ref.close).toHaveBeenCalledWith(summary('d-1', 'Renamed'));
  });

  it('trims whitespace before sending the new name', () => {
    const renameDraft = vi.fn().mockReturnValue(of(summary('d-1', 'Renamed')));
    const { fixture } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.name.set('  Renamed  ');
    fixture.componentInstance.submit();
    expect(renameDraft).toHaveBeenCalledWith('d-1', 'Renamed');
  });

  it('a blank name sets a server error and does not call the API', () => {
    const { fixture, ref, facade } = configure({ draftId: 'd-1', initialName: 'Old' });
    fixture.componentInstance.name.set('   ');
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('Name is required.');
    expect(facade.renameDraft).not.toHaveBeenCalled();
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('an API error surfaces serverError, resets busy, and keeps the dialog open', () => {
    const renameDraft = vi.fn().mockReturnValue(
      throwError(() => ({ error: { detail: 'Name already taken' } })),
    );
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.busy()).toBe(false);
    expect(fixture.componentInstance.serverError()).toBe('Name already taken');
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('falls back to err.message, then a generic message, when no detail is present', () => {
    const renameDraft = vi.fn().mockReturnValue(throwError(() => ({ message: 'network down' })));
    const { fixture } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('network down');
  });

  it('falls back to a generic message when the error has neither detail nor message', () => {
    const renameDraft = vi.fn().mockReturnValue(throwError(() => ({})));
    const { fixture } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('Failed to rename draft.');
  });

  it('handles a primitive-string error without crashing and shows the generic message', () => {
    const renameDraft = vi.fn().mockReturnValue(throwError(() => 'Simple Error String'));
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('Failed to rename draft.');
    expect(fixture.componentInstance.busy()).toBe(false);
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('handles a null error without crashing and shows the generic message', () => {
    const renameDraft = vi.fn().mockReturnValue(throwError(() => null));
    const { fixture } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.serverError()).toBe('Failed to rename draft.');
    expect(fixture.componentInstance.busy()).toBe(false);
  });

  it('cancel closes with no result', () => {
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' });
    fixture.componentInstance.cancel();
    expect(ref.close).toHaveBeenCalledWith();
  });

  it('cancel is a no-op while a rename is in flight', () => {
    const renameDraft = vi.fn().mockReturnValue(NEVER);
    const { fixture, ref } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    expect(fixture.componentInstance.busy()).toBe(true);
    fixture.componentInstance.cancel();
    expect(ref.close).not.toHaveBeenCalled();
  });

  it('a second submit while a rename is in flight is a no-op', () => {
    const renameDraft = vi.fn().mockReturnValue(NEVER);
    const { fixture } = configure({ draftId: 'd-1', initialName: 'Old' }, renameDraft);
    fixture.componentInstance.submit();
    fixture.componentInstance.submit();
    expect(renameDraft).toHaveBeenCalledTimes(1);
    expect(fixture.componentInstance.busy()).toBe(true);
  });
});
