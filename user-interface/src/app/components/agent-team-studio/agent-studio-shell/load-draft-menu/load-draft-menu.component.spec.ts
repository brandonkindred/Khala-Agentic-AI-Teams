import { TestBed } from '@angular/core/testing';
import { MatDialog } from '@angular/material/dialog';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { Subject, of, throwError } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LoadDraftMenuComponent } from './load-draft-menu.component';
import { AgentStudioFacade } from '../../../../services/agent-studio.facade';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

const summary = (id: string, name: string): AgentStudioDraftSummary => ({
  draft_id: id,
  name,
  updated_at: '2026-01-01T00:00:00Z',
});

function configure(
  listDrafts = vi.fn().mockReturnValue(of([])),
  deleteDraft = vi.fn().mockReturnValue(of({ draft_id: 'd-1', status: 'deleted' })),
) {
  const facade = { listDrafts, deleteDraft };
  TestBed.configureTestingModule({
    imports: [LoadDraftMenuComponent, NoopAnimationsModule],
    providers: [{ provide: AgentStudioFacade, useValue: facade }],
  });
  const fixture = TestBed.createComponent(LoadDraftMenuComponent);
  return { fixture, facade };
}

describe('LoadDraftMenuComponent', () => {
  it('onOpened fetches page 1 and populates drafts()', () => {
    const listDrafts = vi.fn().mockReturnValue(of([summary('d-1', 'A'), summary('d-2', 'B')]));
    const { fixture, facade } = configure(listDrafts);
    fixture.componentInstance.onOpened();
    expect(facade.listDrafts).toHaveBeenCalledWith(10, 0);
    expect(fixture.componentInstance.drafts()).toEqual([summary('d-1', 'A'), summary('d-2', 'B')]);
    expect(fixture.componentInstance.loading()).toBe(false);
  });

  it('hasMore is true when a full page is returned', () => {
    const fullPage = Array.from({ length: 10 }, (_, i) => summary(`d-${i}`, `n${i}`));
    const { fixture } = configure(vi.fn().mockReturnValue(of(fullPage)));
    fixture.componentInstance.onOpened();
    expect(fixture.componentInstance.hasMore()).toBe(true);
  });

  it('hasMore is false when fewer than a full page is returned', () => {
    const { fixture } = configure(vi.fn().mockReturnValue(of([summary('d-1', 'A')])));
    fixture.componentInstance.onOpened();
    expect(fixture.componentInstance.hasMore()).toBe(false);
  });

  it('loadMore appends the next page and advances the offset', () => {
    const fullPage = Array.from({ length: 10 }, (_, i) => summary(`d-${i}`, `n${i}`));
    const listDrafts = vi.fn().mockReturnValueOnce(of(fullPage)).mockReturnValueOnce(of([summary('d-10', 'n10')]));
    const { fixture, facade } = configure(listDrafts);
    fixture.componentInstance.onOpened();
    fixture.componentInstance.loadMore();
    expect(facade.listDrafts).toHaveBeenLastCalledWith(10, 10);
    expect(fixture.componentInstance.drafts()).toHaveLength(11);
    expect(fixture.componentInstance.hasMore()).toBe(false);
  });

  it('loadMore is a no-op while loading', () => {
    // A call that never emits keeps loading() true, simulating an in-flight request.
    const listDrafts = vi.fn().mockReturnValue({ subscribe: () => undefined });
    const { fixture, facade } = configure(listDrafts);
    fixture.componentInstance.onOpened();
    expect(fixture.componentInstance.loading()).toBe(true);
    fixture.componentInstance.loadMore();
    expect(facade.listDrafts).toHaveBeenCalledTimes(1);
  });

  it('loadMore is a no-op once hasMore is false', () => {
    const { fixture, facade } = configure(vi.fn().mockReturnValue(of([summary('d-1', 'A')])));
    fixture.componentInstance.onOpened();
    fixture.componentInstance.loadMore();
    expect(facade.listDrafts).toHaveBeenCalledTimes(1);
  });

  it('renders the empty state when the fetch succeeds with no drafts', () => {
    const { fixture } = configure(vi.fn().mockReturnValue(of([])));
    fixture.componentInstance.onOpened();
    expect(fixture.componentInstance.drafts()).toEqual([]);
    expect(fixture.componentInstance.error()).toBeNull();
  });

  it('a list-fetch failure sets error()', () => {
    const listDrafts = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
    const { fixture } = configure(listDrafts);
    fixture.componentInstance.onOpened();
    expect(fixture.componentInstance.error()).toBe('nope');
    expect(fixture.componentInstance.loading()).toBe(false);
  });

  it('re-opening resets drafts/offset and refetches rather than appending', () => {
    const listDrafts = vi
      .fn()
      .mockReturnValueOnce(of([summary('d-1', 'A')]))
      .mockReturnValueOnce(of([summary('d-2', 'B')]));
    const { fixture, facade } = configure(listDrafts);
    fixture.componentInstance.onOpened();
    expect(fixture.componentInstance.drafts()).toEqual([summary('d-1', 'A')]);
    fixture.componentInstance.onOpened();
    expect(facade.listDrafts).toHaveBeenLastCalledWith(10, 0);
    expect(fixture.componentInstance.drafts()).toEqual([summary('d-2', 'B')]);
  });

  it('reopening while the first page is still in flight discards the stale response', () => {
    const firstOpen = new Subject<AgentStudioDraftSummary[]>();
    const listDrafts = vi
      .fn()
      .mockReturnValueOnce(firstOpen.asObservable())
      .mockReturnValueOnce(of([summary('d-2', 'B')]));
    const { fixture } = configure(listDrafts);
    fixture.componentInstance.onOpened(); // first fetch left pending
    fixture.componentInstance.onOpened(); // reopen supersedes it, resolves synchronously

    expect(fixture.componentInstance.drafts()).toEqual([summary('d-2', 'B')]);
    firstOpen.next([summary('d-1', 'A')]);
    firstOpen.complete();
    // The stale first-open response must not append a duplicate/stray row.
    expect(fixture.componentInstance.drafts()).toEqual([summary('d-2', 'B')]);
  });

  it('openDraft emits draftSelected and makes no HTTP call', () => {
    const { fixture, facade } = configure();
    const spy = vi.fn();
    fixture.componentInstance.draftSelected.subscribe(spy);
    fixture.componentInstance.openDraft('d-1');
    expect(spy).toHaveBeenCalledWith('d-1');
    expect(facade.listDrafts).not.toHaveBeenCalled();
  });

  it('the busy input disables the trigger button', () => {
    const { fixture } = configure();
    fixture.componentRef.setInput('busy', true);
    fixture.detectChanges();
    const button: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__draft-btn');
    expect(button.disabled).toBe(true);
  });

  describe('delete', () => {
    let openSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
      openSpy = vi.spyOn(MatDialog.prototype, 'open');
    });
    afterEach(() => {
      openSpy.mockRestore();
    });

    it('confirmDelete cancel does not call deleteDraft or emit draftDeleted', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(false) } as unknown as ReturnType<MatDialog['open']>);
      const { fixture, facade } = configure();
      const spy = vi.fn();
      fixture.componentInstance.draftDeleted.subscribe(spy);
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      expect(facade.deleteDraft).not.toHaveBeenCalled();
      expect(spy).not.toHaveBeenCalled();
    });

    it('confirmDelete confirm deletes, drops the row, and emits draftDeleted', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(true) } as unknown as ReturnType<MatDialog['open']>);
      const deleteDraft = vi.fn().mockReturnValue(of({ draft_id: 'd-1', status: 'deleted' }));
      const { fixture } = configure(
        vi.fn().mockReturnValue(of([summary('d-1', 'A'), summary('d-2', 'B')])),
        deleteDraft,
      );
      fixture.componentInstance.onOpened();
      const spy = vi.fn();
      fixture.componentInstance.draftDeleted.subscribe(spy);
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      expect(deleteDraft).toHaveBeenCalledWith('d-1');
      expect(fixture.componentInstance.drafts().map((d) => d.draft_id)).toEqual(['d-2']);
      expect(spy).toHaveBeenCalledWith('d-1');
    });

    it('confirmDelete recomputes the Show-older offset from the remaining rows', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(true) } as unknown as ReturnType<MatDialog['open']>);
      const fullPage = Array.from({ length: 10 }, (_, i) => summary(`d-${i}`, `n${i}`));
      const listDrafts = vi
        .fn()
        .mockReturnValueOnce(of(fullPage))
        .mockReturnValueOnce(of([summary('d-10', 'n10')]));
      const { fixture, facade } = configure(
        listDrafts,
        vi.fn().mockReturnValue(of({ draft_id: 'd-0', status: 'deleted' })),
      );
      fixture.componentInstance.onOpened();
      fixture.componentInstance.confirmDelete(summary('d-0', 'n0'));
      fixture.componentInstance.loadMore();
      expect(facade.listDrafts).toHaveBeenLastCalledWith(10, 9);
      expect(fixture.componentInstance.drafts().map((d) => d.draft_id)).toEqual([
        ...fullPage.slice(1).map((d) => d.draft_id),
        'd-10',
      ]);
    });

    it('confirmDelete discards an in-flight Show-older page and refetches from the new offset', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(true) } as unknown as ReturnType<MatDialog['open']>);
      const fullPage = Array.from({ length: 10 }, (_, i) => summary(`d-${i}`, `n${i}`));
      const stalePage = new Subject<AgentStudioDraftSummary[]>();
      const listDrafts = vi
        .fn()
        .mockReturnValueOnce(of(fullPage))
        .mockReturnValueOnce(stalePage.asObservable())
        .mockReturnValueOnce(of([summary('d-10', 'n10')]));
      const { fixture, facade } = configure(
        listDrafts,
        vi.fn().mockReturnValue(of({ draft_id: 'd-0', status: 'deleted' })),
      );
      fixture.componentInstance.onOpened();
      fixture.componentInstance.loadMore();
      fixture.componentInstance.confirmDelete(summary('d-0', 'n0'));
      expect(facade.listDrafts).toHaveBeenLastCalledWith(10, 9);
      stalePage.next([summary('stale', 'skip')]);
      stalePage.complete();
      expect(fixture.componentInstance.drafts().map((d) => d.draft_id)).toEqual([
        ...fullPage.slice(1).map((d) => d.draft_id),
        'd-10',
      ]);
    });

    it('confirmDelete API failure sets error() and leaves the row', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(true) } as unknown as ReturnType<MatDialog['open']>);
      const deleteDraft = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
      const { fixture } = configure(vi.fn().mockReturnValue(of([summary('d-1', 'A')])), deleteDraft);
      fixture.componentInstance.onOpened();
      const spy = vi.fn();
      fixture.componentInstance.draftDeleted.subscribe(spy);
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      expect(fixture.componentInstance.error()).toBe('nope');
      expect(fixture.componentInstance.drafts()).toEqual([summary('d-1', 'A')]);
      expect(spy).not.toHaveBeenCalled();
    });

    it('confirmDelete does not emit draftSelected', () => {
      openSpy.mockReturnValue({ afterClosed: () => of(false) } as unknown as ReturnType<MatDialog['open']>);
      const { fixture, facade } = configure();
      const selected = vi.fn();
      fixture.componentInstance.draftSelected.subscribe(selected);
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      expect(selected).not.toHaveBeenCalled();
      expect(facade.deleteDraft).not.toHaveBeenCalled();
    });

    it('confirmDelete of an id already deleting is a no-op', () => {
      const pending = new Subject<{ draft_id: string; status: string }>();
      const deleteDraft = vi.fn().mockReturnValue(pending.asObservable());
      openSpy.mockReturnValue({ afterClosed: () => of(true) } as unknown as ReturnType<MatDialog['open']>);
      const { fixture } = configure(vi.fn().mockReturnValue(of([summary('d-1', 'A')])), deleteDraft);
      fixture.componentInstance.onOpened();
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      expect(deleteDraft).toHaveBeenCalledTimes(1);
      expect(fixture.componentInstance.isDeleting('d-1')).toBe(true);
    });

    it('allows deleting a different draft while another DELETE is in flight', () => {
      const first = new Subject<{ draft_id: string; status: string }>();
      const deleteDraft = vi
        .fn()
        .mockReturnValueOnce(first.asObservable())
        .mockReturnValueOnce(of({ draft_id: 'd-2', status: 'deleted' }));
      openSpy.mockReturnValue({ afterClosed: () => of(true) } as unknown as ReturnType<MatDialog['open']>);
      const { fixture } = configure(
        vi.fn().mockReturnValue(of([summary('d-1', 'A'), summary('d-2', 'B')])),
        deleteDraft,
      );
      fixture.componentInstance.onOpened();
      fixture.componentInstance.confirmDelete(summary('d-1', 'A'));
      fixture.componentInstance.confirmDelete(summary('d-2', 'B'));
      expect(deleteDraft).toHaveBeenCalledWith('d-1');
      expect(deleteDraft).toHaveBeenCalledWith('d-2');
      expect(fixture.componentInstance.drafts().map((d) => d.draft_id)).toEqual(['d-1']);
      expect(fixture.componentInstance.isDeleting('d-1')).toBe(true);
      expect(fixture.componentInstance.isDeleting('d-2')).toBe(false);
    });
  });
});
