import { ComponentFixture, TestBed } from '@angular/core/testing';
import { MatSnackBar } from '@angular/material/snack-bar';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
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

  async function setup(
    response: ListingsResponse = makeResponse([makeListing()], { new: 1 })
  ): Promise<void> {
    apiSpy = {
      listListings: vi.fn().mockReturnValue(of(response)),
      updateListing: vi.fn(),
    };
    snackSpy = { open: vi.fn() };
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

  it('loads active listings on init', async () => {
    await setup();
    expect(apiSpy.listListings).toHaveBeenCalledWith('active');
    expect(component.listings.length).toBe(1);
    expect(fixture.nativeElement.textContent).toContain('Staff Engineer');
  });

  it('shows the empty state when there are no listings', async () => {
    await setup(makeResponse([], {}));
    expect(fixture.nativeElement.textContent).toContain('No listings yet');
  });

  it('surfaces a load error with retry', async () => {
    apiSpy = {
      listListings: vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'boom' } }))),
      updateListing: vi.fn(),
    };
    snackSpy = { open: vi.fn() };
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

  it('patches the listing pessimistically and replaces the row', async () => {
    await setup();
    const updated = makeListing({ status: 'favorite' });
    apiSpy.updateListing.mockReturnValue(of(updated));

    component.onStatusChange(component.listings[0], 'favorite');

    expect(apiSpy.updateListing).toHaveBeenCalledWith('fp1', { status: 'favorite' });
    expect(component.pendingFingerprint).toBeNull();
    expect(component.listings[0].status).toBe('favorite');
    expect(snackSpy.open).toHaveBeenCalledWith(
      expect.stringContaining('Added to favorites'),
      'Dismiss',
      expect.anything()
    );
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
      'Dismiss',
      expect.anything()
    );
  });
});
