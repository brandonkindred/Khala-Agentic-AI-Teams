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

  afterEach(() => {
    // Menus render into an overlay attached to the document body; clear it so
    // one test's open menu can't leak into the next test's queries.
    document.querySelectorAll('.cdk-overlay-container').forEach((el) => el.remove());
  });

  function buttonByText(text: string, root: ParentNode = fixture.nativeElement): HTMLButtonElement | undefined {
    const buttons = Array.from(root.querySelectorAll('button') as NodeListOf<HTMLButtonElement>);
    return buttons.find((b) => b.textContent?.includes(text));
  }

  function openMoreActions(): void {
    const trigger = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>
    ).find((b) => b.getAttribute('aria-label')?.startsWith('More actions'));
    expect(trigger).toBeDefined();
    trigger!.click();
    fixture.detectChanges();
  }

  it('renders score, recommendation, and status badges', async () => {
    await setup(makeListing());
    const el: HTMLElement = fixture.nativeElement;
    expect(el.querySelector('.score-value')?.textContent).toContain('87%');
    expect(el.querySelector('.score-value')?.textContent).toContain('Match score');
    // Recommendation renders the human label with a screen-reader qualifier.
    expect(el.querySelector('.rec-badge')?.textContent).toContain('Apply');
    expect(el.querySelector('.rec-badge')?.textContent).toContain('Recommendation:');
    expect(el.querySelector('.status-badge')?.textContent).toContain('New');
    expect(el.querySelector('.status-badge')?.textContent).toContain('Status:');
    expect(el.textContent).toContain('Staff Engineer');
    expect(el.textContent).toContain('Acme');
    expect(el.textContent).toContain('USD 200k–260k');
    expect(el.textContent).toContain('Seen 2 times');
  });

  it('exposes the job title as a heading and the card as a list item', async () => {
    await setup(makeListing());
    const el: HTMLElement = fixture.nativeElement;
    const heading = el.querySelector('h3.listing-title');
    expect(heading?.textContent).toContain('Staff Engineer');
    expect(fixture.nativeElement.getAttribute('role')).toBe('listitem');
  });

  it('names each per-card control with the listing title', async () => {
    await setup(makeListing());
    const label = (sel: string) =>
      fixture.nativeElement.querySelector(sel)?.getAttribute('aria-label') ?? '';
    expect(buttonByText('Not interested')!.getAttribute('aria-label')).toContain('Staff Engineer');
    expect(buttonByText('Review')!.getAttribute('aria-label')).toContain('Staff Engineer');
    // The star's accessible name keeps its visible intent AND names the listing.
    const star = fixture.nativeElement.querySelector('button[aria-pressed]');
    expect(star.getAttribute('aria-label')).toContain('Add to favorites');
    expect(star.getAttribute('aria-label')).toContain('Staff Engineer');
    expect(label).toBeDefined();
  });

  it('maps the recommendation enum to a human label', async () => {
    await setup(makeListing({ recommendation: 'maybe' }));
    expect(component.recommendationLabel).toBe('Worth a look');
    component.listing = makeListing({ recommendation: 'skip' });
    expect(component.recommendationLabel).toBe('Skip');
    component.listing = makeListing({ recommendation: 'apply' });
    expect(component.recommendationLabel).toBe('Apply');
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

  it('keeps Not interested inline and emits its status', async () => {
    await setup(makeListing());
    const emitted: ListingStatus[] = [];
    component.statusChange.subscribe((s) => emitted.push(s));
    buttonByText('Not interested')!.click();
    expect(emitted).toEqual(['not_interested']);
  });

  it('emits poor_fit and archived from the more-actions menu', async () => {
    await setup(makeListing());
    const emitted: ListingStatus[] = [];
    component.statusChange.subscribe((s) => emitted.push(s));

    // Archive and Poor fit are not inline anymore.
    expect(buttonByText('Archive')).toBeUndefined();
    expect(buttonByText('Poor fit')).toBeUndefined();

    openMoreActions();
    buttonByText('Mark as poor fit', document)!.click();
    fixture.detectChanges();
    openMoreActions();
    buttonByText('Archive', document)!.click();
    expect(emitted).toEqual(['poor_fit', 'archived']);
  });

  it('shows a single Restore action for triaged-away listings', async () => {
    await setup(makeListing({ status: 'archived' }));
    const emitted: ListingStatus[] = [];
    component.statusChange.subscribe((s) => emitted.push(s));

    expect(buttonByText('Not interested')).toBeUndefined();
    const restore = buttonByText('Restore');
    expect(restore).toBeDefined();
    restore!.click();
    expect(emitted).toEqual(['new']);
  });

  it('hides all triage actions in readonly mode', async () => {
    await setup(makeListing(), { readonly: true });
    expect(buttonByText('Not interested')).toBeUndefined();
    expect(buttonByText('Restore')).toBeUndefined();
    // The review toggle stays available.
    expect(buttonByText('Review')).toBeDefined();
  });

  it('disables actions while pending', async () => {
    await setup(makeListing(), { pending: true });
    expect(buttonByText('Not interested')!.disabled).toBe(true);
  });

  it('expands to the review detail with rationale, concerns, and meters', async () => {
    await setup(makeListing());
    expect(fixture.nativeElement.querySelector('.listing-detail')).toBeNull();

    const toggle = buttonByText('Review')!;
    expect(toggle.getAttribute('aria-controls')).toBe('listing-detail-fp1');
    toggle.click();
    fixture.detectChanges();

    const el: HTMLElement = fixture.nativeElement;
    const detail = el.querySelector('.listing-detail');
    expect(detail).not.toBeNull();
    expect(detail!.id).toBe('listing-detail-fp1');
    expect(el.textContent).toContain('Great fit.');
    expect(el.textContent).toContain('Hybrid only');
    const meters = el.querySelectorAll('.sub-score-bar[role="meter"]');
    expect(meters.length).toBe(6);
    expect(meters[0].getAttribute('aria-valuenow')).toBe('0.9');
    expect(meters[0].getAttribute('aria-valuemax')).toBe('1');
    // The meter announces a readable percentage (role="meter" alone reads a bare
    // decimal inconsistently across screen readers), and the visible value is
    // on the same 0–100 scale as the headline — no longer aria-hidden.
    expect(meters[0].getAttribute('aria-valuetext')).toBe('90%');
    const values = el.querySelectorAll('.sub-score-value');
    expect(values[0].textContent).toContain('90%');
    expect(values[0].getAttribute('aria-hidden')).toBeNull();
    // "Compensation" is spelled out rather than the cryptic "Comp".
    expect(el.textContent).toContain('Compensation');
    const link = el.querySelector('.detail-facts a') as HTMLAnchorElement;
    expect(link.href).toContain('https://example.com/job');
    expect(link.rel).toContain('noopener');
    expect(link.textContent).toContain('(opens in new tab)');
    expect(buttonByText('Hide')).toBeDefined();
  });

  it('reports salary undisclosed when the posting has no range', async () => {
    const listing = makeListing();
    listing.posting = { ...listing.posting, salary_min: null, salary_max: null };
    await setup(listing);
    expect(component.salaryLabel).toBe('Salary undisclosed');
  });
});
