import { TestBed } from '@angular/core/testing';
import { Subject, of, throwError } from 'rxjs';
import { describe, expect, it, vi } from 'vitest';
import { LoadDraftMenuComponent } from './load-draft-menu.component';
import { AgentStudioFacade } from '../../../../services/agent-studio.facade';
import type { AgentStudioDraftSummary } from '../../../../models/agent-studio.model';

const summary = (id: string, name: string): AgentStudioDraftSummary => ({
  draft_id: id,
  name,
  updated_at: '2026-01-01T00:00:00Z',
});

function configure(listDrafts = vi.fn().mockReturnValue(of([]))) {
  const facade = { listDrafts };
  TestBed.configureTestingModule({
    imports: [LoadDraftMenuComponent],
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

  it('select emits draftSelected and makes no HTTP call', () => {
    const { fixture, facade } = configure();
    const spy = vi.fn();
    fixture.componentInstance.draftSelected.subscribe(spy);
    fixture.componentInstance.select('d-1');
    expect(spy).toHaveBeenCalledWith('d-1');
    expect(facade.listDrafts).not.toHaveBeenCalled();
  });

  it('the busy input disables the trigger button', () => {
    const { fixture } = configure();
    fixture.componentInstance.busy = true;
    fixture.detectChanges();
    const button: HTMLButtonElement = fixture.nativeElement.querySelector('.studio__draft-btn');
    expect(button.disabled).toBe(true);
  });
});
