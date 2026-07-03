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

  it('shows the empty state with a Start a scan CTA on the active filter', async () => {
    await setup(makeResponse([], {}));
    expect(fixture.nativeElement.textContent).toContain('No listings yet');
    const emitted = vi.fn();
    component.startScanRequested.subscribe(emitted);
    const cta = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>
    ).find((b) => b.textContent?.includes('Start a scan'));
    expect(cta).toBeDefined();
    cta!.click();
    expect(emitted).toHaveBeenCalled();
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
    const slowFavorites = new Subject<ListingsResponse>();
    const fastAll = new Subject<ListingsResponse>();
    apiSpy.listListings.mockImplementation((filter: string) =>
      filter === 'favorite' ? slowFavorites.asObservable() : fastAll.asObservable()
    );

    component.setFilter('favorite'); // slow request in flight
    component.setFilter('all'); // user moves on

    fastAll.next(makeResponse([makeListing(), makeListing({ fingerprint: 'fp2' })], { new: 2 }));
    // The older favorites response lands last — it must be ignored.
    slowFavorites.next(makeResponse([makeListing({ status: 'favorite' })], { favorite: 1 }));

    expect(component.filter).toBe('all');
    expect(component.listings.length).toBe(2);
    expect(component.counts).toEqual({ new: 2 });
  });

  it('discards a stale response even when the user returns to the same filter (A→B→A)', async () => {
    await setup();
    // Three in-flight requests: favorite (abandoned), active (stale), active
    // (fresh). A filter-value guard would accept the stale active response
    // because its filter matches again; the sequence token must reject it.
    const responses: Subject<ListingsResponse>[] = [];
    apiSpy.listListings.mockImplementation(() => {
      const s = new Subject<ListingsResponse>();
      responses.push(s);
      return s.asObservable();
    });

    component.setFilter('favorite'); // request in flight, then abandoned
    component.setFilter('active'); // stale active request in flight
    component.load(); // fresh active request (e.g. post-scan refresh)

    responses[2].next(
      makeResponse([makeListing(), makeListing({ fingerprint: 'fp2' })], { new: 2 })
    );
    // Older same-filter response lands last — it must NOT win.
    responses[1].next(makeResponse([makeListing()], { new: 1 }));

    expect(component.listings.length).toBe(2);
    expect(component.counts).toEqual({ new: 2 });
  });

  it('reloads with the selected filter', async () => {
    await setup();
    component.setFilter('favorite');
    expect(component.filter).toBe('favorite');
    expect(apiSpy.listListings).toHaveBeenLastCalledWith('favorite');
    // Selecting the same filter again does not refetch.
    const calls = apiSpy.listListings.mock.calls.length;
    component.setFilter('favorite');
    expect(apiSpy.listListings.mock.calls.length).toBe(calls);
  });

  it('moves selection and focus with arrow keys on the pills', async () => {
    await setup();
    const pills = fixture.nativeElement.querySelectorAll('.filter-pill');
    // Only the selected pill participates in the tab order.
    expect(pills[0].getAttribute('tabindex')).toBe('0');
    expect(pills[1].getAttribute('tabindex')).toBe('-1');

    component.onPillKeydown(
      new KeyboardEvent('keydown', { key: 'ArrowRight' }),
      'active'
    );
    expect(component.filter).toBe('favorite');

    component.onPillKeydown(new KeyboardEvent('keydown', { key: 'End' }), 'favorite');
    expect(component.filter).toBe('all');

    component.onPillKeydown(new KeyboardEvent('keydown', { key: 'Home' }), 'all');
    expect(component.filter).toBe('active');

    // Wrap-around from the first pill going left.
    component.onPillKeydown(new KeyboardEvent('keydown', { key: 'ArrowLeft' }), 'active');
    expect(component.filter).toBe('all');
  });

  it('derives pill counts from the counts map', async () => {
    await setup(
      makeResponse([makeListing()], {
        new: 3,
        favorite: 2,
        archived: 1,
        not_interested: 1,
      })
    );
    expect(component.countFor('all')).toBe(7);
    expect(component.countFor('active')).toBe(5);
    expect(component.countFor('favorite')).toBe(2);
    expect(component.countFor('poor_fit')).toBe(0);
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
