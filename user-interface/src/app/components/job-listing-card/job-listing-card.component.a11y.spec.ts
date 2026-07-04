import { TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { JobListingCardComponent } from './job-listing-card.component';
import type { Listing } from '../../models';
import { expectNoAxeViolations } from '../../testing/a11y';

const LISTING: Listing = {
  fingerprint: 'fp1',
  posting: {
    title: 'Staff Engineer',
    company: 'Acme',
    location: 'NYC',
    remote_mode: 'remote',
    salary_min: 200000,
    salary_max: 260000,
    currency: 'USD',
    url: 'https://example.com/job',
    source: 'web_search',
    description: 'Build things.',
    fingerprint: 'fp1',
  },
  score: 0.87,
  sub_scores: {
    title_fit: 0.9,
    seniority_fit: 0.8,
    location_fit: 1,
    comp_fit: 0.7,
    company_fit: 0.6,
    skills_fit: 0.85,
  },
  recommendation: 'apply',
  rationale: 'Great fit.',
  concerns: ['Hybrid only'],
  run_id: 'r1',
  times_seen: 2,
  status: 'new',
};

// `aria-required-parent` is disabled (on top of the shared color-contrast
// exception) because the card host is role="listitem": its required
// role="list" parent is supplied by the listings panel (verified in that
// panel's own a11y spec), not by this isolated fragment.
const cardExtraRules = { 'aria-required-parent': { enabled: false } };

describe('JobListingCardComponent a11y', () => {
  async function createFixture() {
    await TestBed.configureTestingModule({
      imports: [JobListingCardComponent],
      providers: [provideNoopAnimations()],
    }).compileComponents();
    const fixture = TestBed.createComponent(JobListingCardComponent);
    fixture.componentInstance.listing = LISTING;
    fixture.detectChanges();
    return fixture;
  }

  it('has no axe violations collapsed', async () => {
    const fixture = await createFixture();
    // Guard: don't pass axe vacuously against an empty DOM.
    expect(fixture.nativeElement.querySelector('.listing-card')).toBeTruthy();
    await expectNoAxeViolations(fixture.nativeElement, cardExtraRules);
  }, 15000);

  it('has no axe violations with the review detail expanded', async () => {
    const fixture = await createFixture();
    fixture.componentInstance.expanded = true;
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.listing-detail')).toBeTruthy();
    expect(
      fixture.nativeElement.querySelectorAll('.sub-score-bar[role="meter"]').length
    ).toBe(6);
    await expectNoAxeViolations(fixture.nativeElement, cardExtraRules);
  }, 15000);
});
