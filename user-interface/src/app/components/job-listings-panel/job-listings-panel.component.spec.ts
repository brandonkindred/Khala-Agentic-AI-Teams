import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MatSnackBar } from '@angular/material/snack-bar';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { EMPTY, Subject, of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobListingsPanelComponent } from './job-listings-panel.component';
import type { Listing, ListingsResponse } from '../../models';

function makeListing(overrides: Partial<Listing> = {}): Listing {
  return {
    fingerprint: 'fp1',
    posting: {
      title: 'Staff Engineer',
      company: 'Acme',
      location: 'NYC',
      remote_mode: 'remote',
      currency: 'USD',
      url: '',
      source: '',
      description: '',
      fingerprint: 'fp1',
    },
    score: 0.9,
    sub_scores: {
      title_fit: 0.9,
      seniority_fit: 0.8,
      location_fit: 1,
      comp_fit: 0.7,
      company_fit: 0.6,
      skills_fit: 0.85,
    },
    recommendation: 'apply',
    rationale: 'Fit.',
    concerns: [],
    run_id: 'r1',
    times_seen: 1,
    status: 'new',
    ...overrides,
  };
}

function makeResponse(listings: Listing[], counts: Record<string, number>): ListingsResponse {
  return { listings, total: listings.length, counts };
}

describe('JobListingsPanelComponent', () => {
  let fixture: ComponentFixture<JobListingsPanelComponent>;
  let component: JobListingsPanelComponent;
  let apiSpy: {
    listListings: ReturnType<typeof vi.fn>;
    updateListing: ReturnType<typeof vi.fn>;
  };
  let snackSpy: { open: ReturnType<typeof vi.fn> };

  function mockSnackRef(onAction = EMPTY as ReturnType<typeof of>) {
    return { onAction: () => onAction };
  }

  async function setup(
    response: ListingsResponse = makeResponse([makeListing()], { new: 1 })
  ): Promise<void> {
    apiSpy = {
      listListings: vi.fn().mockReturnValue(of(response)),
      updateListing: vi.fn(),
    };
    snackSpy = { open: vi.fn().mockReturnValue(mockSnackRef()) };
    await TestBed.configureTestingModule({
      imports: [JobListingsPanelComponent],
      providers: [
        provideNoopAnimations(),
        { provide: JobMatchingApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackSpy },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(JobListingsPanelComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  }

  it('loads active listings on init and announces the count', async () => {
    await setup();
    expect(apiSpy.listListings).toHaveBeenCalledWith('active');
    expect(component.listings.length).toBe(1);
    expect(component.resultAnnouncement).toBe('1 listing shown');
    expect(fixture.nativeElement.textContent).toContain('Staff Engineer');
  });

  it('shows both empty-state CTAs on the active filter and emits from each', async () => {
    await setup(makeResponse([], {}));
    expect(fixture.nativeElement.textContent).toContain('No listings yet');
    const scan = vi.fn();
    const setup2 = vi.fn();
    component.startScanRequested.subscribe(scan);
    component.setupProfileRequested.subscribe(setup2);
    const button = (text: string) =>
      Array.from(
        fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>
      ).find((b) => b.textContent?.includes(text));

    const scanCta = button('Start a scan');
    const profileCta = button('Set up your profile first');
    expect(scanCta).toBeDefined();
    expect(profileCta).toBeDefined();
    scanCta!.click();
    profileCta!.click();
    expect(scan).toHaveBeenCalled();
    expect(setup2).toHaveBeenCalled();
  });

  it('surfaces a load error with retry', async () => {
    apiSpy = {
      listListings: vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'boom' } }))),
      updateListing: vi.fn(),
    };
    snackSpy = { open: vi.fn().mockReturnValue(mockSnackRef()) };
    await TestBed.configureTestingModule({
      imports: [JobListingsPanelComponent],
      providers: [
        provideNoopAnimations(),
        { provide: JobMatchingApiService, useValue: apiSpy },
        { provide: MatSnackBar, useValue: snackSpy },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(JobListingsPanelComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();

    expect(component.error).toBe('boom');
    expect(fixture.nativeElement.textContent).toContain('Retry');
  });

  it('discards a stale response that arrives after the filter changed', async () => {
    await setup();
    // Pill-driven loads are debounced; advance the timer to let each fire.
    vi.useFakeTimers();
    const slowFavorites = new Subject<ListingsResponse>();
    const fastAll = new Subject<ListingsResponse>();
    apiSpy.listListings.mockImplementation((filter: string) =>
      filter === 'favorite' ? slowFavorites.asObservable() : fastAll.asObservable()
    );

    component.setFilter('favorite');
    vi.advanceTimersByTime(200); // slow favorites request in flight
    component.setFilter('all');
    vi.advanceTimersByTime(200); // user moved on — fast all request in flight

    fastAll.next(makeResponse([makeListing(), makeListing({ fingerprint: 'fp2' })], { new: 2 }));
    // The older favorites response lands last — it must be ignored.
    slowFavorites.next(makeResponse([makeListing({ status: 'favorite' })], { favorite: 1 }));

    expect(component.filter).toBe('all');
    expect(component.listings.length).toBe(2);
    expect(component.counts).toEqual({ new: 2 });
    vi.useRealTimers();
  });

  it('discards a stale response even when the user returns to the same filter (A→B→A)', async () => {
    await setup();
    vi.useFakeTimers();
    // Three in-flight requests: favorite (abandoned), active (stale), active
    // (fresh). A filter-value guard would accept the stale active response
    // because its filter matches again; the sequence token must reject it.
    const responses: Subject<ListingsResponse>[] = [];
    apiSpy.listListings.mockImplementation(() => {
      const s = new Subject<ListingsResponse>();
      responses.push(s);
      return s.asObservable();
    });

    component.setFilter('favorite');
    vi.advanceTimersByTime(200); // favorite request fires, then abandoned
    component.setFilter('active');
    vi.advanceTimersByTime(200); // stale active request fires
    component.load(); // fresh active request (e.g. post-scan refresh), immediate

    responses[2].next(
      makeResponse([makeListing(), makeListing({ fingerprint: 'fp2' })], { new: 2 })
    );
    // Older same-filter response lands last — it must NOT win.
    responses[1].next(makeResponse([makeListing()], { new: 1 }));

    expect(component.listings.length).toBe(2);
    expect(component.counts).toEqual({ new: 2 });
    vi.useRealTimers();
  });

  it('debounces the pill-driven load and reloads with the selected filter', async () => {
    await setup();
    vi.useFakeTimers();
    component.setFilter('favorite');
    // Selection is immediate; the network load is not fired until the debounce elapses.
    expect(component.filter).toBe('favorite');
    const before = apiSpy.listListings.mock.calls.length;
    vi.advanceTimersByTime(199);
    expect(apiSpy.listListings.mock.calls.length).toBe(before);
    vi.advanceTimersByTime(1);
    expect(apiSpy.listListings).toHaveBeenLastCalledWith('favorite');

    // Selecting the same filter again does not refetch.
    const calls = apiSpy.listListings.mock.calls.length;
    component.setFilter('favorite');
    vi.advanceTimersByTime(200);
    expect(apiSpy.listListings.mock.calls.length).toBe(calls);
    vi.useRealTimers();
  });

  it('coalesces rapid arrow-key roving into a single load', async () => {
    await setup();
    vi.useFakeTimers();
    const pills = fixture.nativeElement.querySelectorAll('.filter-pill');
    // Only the selected pill participates in the tab order.
    expect(pills[0].getAttribute('tabindex')).toBe('0');
    expect(pills[1].getAttribute('tabindex')).toBe('-1');
    const before = apiSpy.listListings.mock.calls.length;

    component.onPillKeydown(
      new KeyboardEvent('keydown', { key: 'ArrowRight' }),
      'active'
    );
    expect(component.filter).toBe('favorite');

    component.onPillKeydown(new KeyboardEvent('keydown', { key: 'End' }), 'favorite');
    expect(component.filter).toBe('all');

    component.onPillKeydown(new KeyboardEvent('keydown', { key: 'Home' }), 'all');
    expect(component.filter).toBe('active');

    // Rapid roving fired no load yet; after the debounce, exactly one fires.
    expect(apiSpy.listListings.mock.calls.length).toBe(before);
    vi.advanceTimersByTime(200);
    expect(apiSpy.listListings.mock.calls.length).toBe(before + 1);

    // Wrap-around from the first pill going left.
    component.onPillKeydown(new KeyboardEvent('keydown', { key: 'ArrowLeft' }), 'active');
    expect(component.filter).toBe('all');
    vi.advanceTimersByTime(200); // flush the trailing debounce before restoring timers
    vi.useRealTimers();
  });

  it('derives pill counts, excluding archived, not-interested and poor-fit from Active', async () => {
    await setup(
      makeResponse([makeListing()], {
        new: 3,
        favorite: 2,
        archived: 1,
        not_interested: 1,
        poor_fit: 2,
      })
    );
    expect(component.countFor('all')).toBe(9);
    // Active is the inbox: everything except archived/not-interested/poor-fit.
    expect(component.countFor('active')).toBe(5);
    expect(component.countFor('favorite')).toBe(2);
    expect(component.countFor('poor_fit')).toBe(2);
  });

  it('patches the listing pessimistically, replaces the row, and offers Undo', async () => {
    await setup();
    const updated = makeListing({ status: 'favorite' });
    apiSpy.updateListing.mockReturnValue(of(updated));

    component.onStatusChange(component.listings[0], 'favorite');

    expect(apiSpy.updateListing).toHaveBeenCalledWith('fp1', { status: 'favorite' });
    expect(component.pendingFingerprint).toBeNull();
    expect(component.listings[0].status).toBe('favorite');
    expect(snackSpy.open).toHaveBeenCalledWith(
      expect.stringContaining('Added to favorites'),
      'Undo',
      expect.anything()
    );
  });

  it('undoes a status change by PATCHing the previous status back', async () => {
    await setup();
    const undoClicks = new Subject<void>();
    snackSpy.open.mockReturnValue({ onAction: () => undoClicks.asObservable() });
    apiSpy.updateListing
      .mockReturnValueOnce(of(makeListing({ status: 'favorite' })))
      .mockReturnValueOnce(of(makeListing({ status: 'new' })));

    component.onStatusChange(component.listings[0], 'favorite');
    undoClicks.next();

    expect(apiSpy.updateListing).toHaveBeenNthCalledWith(2, 'fp1', { status: 'new' });
    expect(component.listings[0].status).toBe('new');
    expect(snackSpy.open).toHaveBeenCalledWith('Change undone.', 'Dismiss', expect.anything());
  });

  it('reloads on undo when the row had left the current filter', async () => {
    await setup();
    const undoClicks = new Subject<void>();
    snackSpy.open.mockReturnValue({ onAction: () => undoClicks.asObservable() });
    apiSpy.updateListing
      .mockReturnValueOnce(of(makeListing({ status: 'archived' })))
      .mockReturnValueOnce(of(makeListing({ status: 'new' })));

    component.onStatusChange(component.listings[0], 'archived');
    expect(component.listings.length).toBe(0); // left the active filter

    const loadsBefore = apiSpy.listListings.mock.calls.length;
    undoClicks.next();
    expect(apiSpy.updateListing).toHaveBeenNthCalledWith(2, 'fp1', { status: 'new' });
    expect(apiSpy.listListings.mock.calls.length).toBeGreaterThan(loadsBefore);
  });

  it('removes the row when its new status leaves the active filter', async () => {
    await setup();
    apiSpy.updateListing.mockReturnValue(of(makeListing({ status: 'archived' })));
    component.onStatusChange(component.listings[0], 'archived');
    expect(component.listings.length).toBe(0);
  });

  it('drops a poor-fit listing from the Active inbox', async () => {
    // Poor fit is a triaged-away status — it must leave Active like archived /
    // not-interested (previously it lingered with only a Restore action).
    await setup();
    apiSpy.updateListing.mockReturnValue(of(makeListing({ status: 'poor_fit' })));
    component.onStatusChange(component.listings[0], 'poor_fit');
    expect(component.listings.length).toBe(0);
  });

  it('keeps the live-region count in step after an in-place triage', async () => {
    // Two rows on the Favorites filter; un-favoriting one removes it, and the
    // polite region must reflect the new count (not the stale load-time count).
    await setup(
      makeResponse(
        [makeListing({ status: 'favorite' }), makeListing({ fingerprint: 'fp2', status: 'favorite' })],
        { favorite: 2 }
      )
    );
    component.filter = 'favorite'; // rows already loaded; pin the filter without a debounced reload
    expect(component.resultAnnouncement).toBe('2 listings shown');
    apiSpy.updateListing.mockReturnValue(of(makeListing({ status: 'new' })));
    component.onStatusChange(component.listings[0], 'new');
    expect(component.resultAnnouncement).toBe('1 listing shown');
  });

  it('keeps the row and shows an error snackbar when the PATCH fails', async () => {
    await setup();
    apiSpy.updateListing.mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
    component.onStatusChange(component.listings[0], 'archived');
    expect(component.pendingFingerprint).toBeNull();
    expect(component.listings.length).toBe(1);
    expect(component.listings[0].status).toBe('new');
    expect(snackSpy.open).toHaveBeenCalledWith('nope', 'Dismiss', expect.anything());
  });

  it('announces removing a favorite distinctly', async () => {
    await setup(makeResponse([makeListing({ status: 'favorite' })], { favorite: 1 }));
    apiSpy.updateListing.mockReturnValue(of(makeListing({ status: 'new' })));
    component.onStatusChange(component.listings[0], 'new');
    expect(snackSpy.open).toHaveBeenCalledWith(
      expect.stringContaining('Removed from favorites'),
      'Undo',
      expect.anything()
    );
  });
});
