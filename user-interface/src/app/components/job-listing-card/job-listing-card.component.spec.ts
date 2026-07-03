import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideNoopAnimations } from '@angular/platform-browser/animations';
import { JobListingCardComponent } from './job-listing-card.component';
import type { Listing, ListingStatus } from '../../models';

function makeListing(overrides: Partial<Listing> = {}): Listing {
  return {
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
    ...overrides,
  };
}

describe('JobListingCardComponent', () => {
  let fixture: ComponentFixture<JobListingCardComponent>;
  let component: JobListingCardComponent;

  async function setup(listing: Listing, opts: { readonly?: boolean; pending?: boolean } = {}) {
    await TestBed.configureTestingModule({
      imports: [JobListingCardComponent],
      providers: [provideNoopAnimations()],
    }).compileComponents();
    fixture = TestBed.createComponent(JobListingCardComponent);
    component = fixture.componentInstance;
    component.listing = listing;
    component.readonly = opts.readonly ?? false;
    component.pending = opts.pending ?? false;
    fixture.detectChanges();
  }

  function buttonByText(text: string): HTMLButtonElement | undefined {
    const buttons = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>
    );
    return buttons.find((b) => b.textContent?.includes(text));
  }

  it('renders score, recommendation, and status badges', async () => {
    await setup(makeListing());
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.score-value')?.textContent).toContain('87%');
    expect(el.querySelector('.rec-badge')?.textContent).toContain('apply');
    expect(el.querySelector('.status-badge')?.textContent).toContain('New');
    expect(el.textContent).toContain('Staff Engineer');
    expect(el.textContent).toContain('Acme');
    expect(el.textContent).toContain('USD 200k–260k');
  });

  it('emits favorite on the star and toggles back to new when already favorite', async () => {
    await setup(makeListing());
    const emitted: ListingStatus[] = [];
    component.statusChange.subscribe((s) => emitted.push(s));

    component.toggleFavorite();
    expect(emitted).toEqual(['favorite']);

    component.listing = makeListing({ status: 'favorite' });
    component.toggleFavorite();
    expect(emitted).toEqual(['favorite', 'new']);
  });

  it('emits the matching status for each triage button', async () => {
    await setup(makeListing());
    const emitted: ListingStatus[] = [];
    component.statusChange.subscribe((s) => emitted.push(s));

    buttonByText('Poor fit')!.click();
    buttonByText('Not interested')!.click();
    buttonByText('Archive')!.click();
    expect(emitted).toEqual(['poor_fit', 'not_interested', 'archived']);
  });

  it('shows a single Restore action for triaged-away listings', async () => {
    await setup(makeListing({ status: 'archived' }));
    const emitted: ListingStatus[] = [];
    component.statusChange.subscribe((s) => emitted.push(s));

    expect(buttonByText('Archive')).toBeUndefined();
    const restore = buttonByText('Restore');
    expect(restore).toBeDefined();
    restore!.click();
    expect(emitted).toEqual(['new']);
  });

  it('hides all triage actions in readonly mode', async () => {
    await setup(makeListing(), { readonly: true });
    expect(buttonByText('Archive')).toBeUndefined();
    expect(buttonByText('Restore')).toBeUndefined();
    // The review toggle stays available.
    expect(buttonByText('Review')).toBeDefined();
  });

  it('disables actions while pending', async () => {
    await setup(makeListing(), { pending: true });
    expect(buttonByText('Archive')!.disabled).toBe(true);
  });

  it('expands to the review detail with rationale, concerns, and sub-scores', async () => {
    await setup(makeListing());
    expect(fixture.nativeElement.querySelector('.listing-detail')).toBeNull();

    buttonByText('Review')!.click();
    fixture.detectChanges();

    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.listing-detail')).not.toBeNull();
    expect(el.textContent).toContain('Great fit.');
    expect(el.textContent).toContain('Hybrid only');
    expect(el.querySelectorAll('.sub-score-row').length).toBe(6);
    const link = el.querySelector('.detail-facts a') as HTMLAnchorElement;
    expect(link.href).toContain('https://example.com/job');
    expect(link.rel).toContain('noopener');
    expect(buttonByText('Hide')).toBeDefined();
  });

  it('reports salary undisclosed when the posting has no range', async () => {
    const listing = makeListing();
    listing.posting = { ...listing.posting, salary_min: null, salary_max: null };
    await setup(listing);
    expect(component.salaryLabel).toBe('Salary undisclosed');
  });
});
