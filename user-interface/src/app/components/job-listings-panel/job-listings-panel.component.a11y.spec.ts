import { TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { JobListingsPanelComponent } from './job-listings-panel.component';
import type { Listing } from '../../models';
import { expectNoAxeViolations } from '../../testing/a11y';

const LISTING: Listing = {
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
};

describe('JobListingsPanelComponent a11y', () => {
  async function createFixture(listings: Listing[]) {
    const apiSpy = {
      listListings: vi
        .fn()
        .mockReturnValue(of({ listings, total: listings.length, counts: { new: listings.length } })),
      updateListing: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [JobListingsPanelComponent],
      providers: [provideNoopAnimations(), { provide: JobMatchingApiService, useValue: apiSpy }],
    }).compileComponents();
    const fixture = TestBed.createComponent(JobListingsPanelComponent);
    fixture.detectChanges();
    return fixture;
  }

  it('has no axe violations with rendered listings', async () => {
    const fixture = await createFixture([LISTING, { ...LISTING, fingerprint: 'fp2' }]);
    // Guards: the results list wraps role="listitem" cards, and the radiogroup
    // toolbar is present — so the list/listitem parent-child pair is exercised.
    const list = fixture.nativeElement.querySelector('[role="list"]');
    expect(list).toBeTruthy();
    expect(list.querySelectorAll('[role="listitem"]').length).toBe(2);
    expect(fixture.nativeElement.querySelector('[role="radiogroup"]')).toBeTruthy();
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);

  it('has no axe violations in the empty state', async () => {
    const fixture = await createFixture([]);
    expect(fixture.nativeElement.textContent).toContain('No listings yet');
    await expectNoAxeViolations(fixture.nativeElement);
  }, 15000);
});
