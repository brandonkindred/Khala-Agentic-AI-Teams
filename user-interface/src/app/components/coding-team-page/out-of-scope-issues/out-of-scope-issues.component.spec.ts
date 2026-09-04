import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { OutOfScopeIssuesComponent } from './out-of-scope-issues.component';
import type { OutOfScopeProposalItem } from '../../../models/integrations.model';

/**
 * Builds an out-of-scope proposal with sane defaults, overridable per field.
 *
 * Preconditions: `id` is unique within a single test's proposal list, since
 *   `compositeId` (job_id:id) is the selection key and the `@for` track.
 * Postconditions: returns a fully-populated `OutOfScopeProposalItem` — no
 *   partial objects leak into the component's `proposals` input.
 */
function proposal(id: string, over: Partial<OutOfScopeProposalItem> = {}): OutOfScopeProposalItem {
  return {
    id,
    job_id: 'job-1',
    pr_number: 42,
    pr_url: 'https://github.com/o/r/pull/42',
    severity: 'high',
    category: 'logic',
    file_path: 'a.py',
    line: 3,
    description: `bug ${id}`,
    suggestion: 'fix it',
    locations: [],
    issue_number: null,
    issue_url: null,
    ...over,
  };
}

describe('OutOfScopeIssuesComponent', () => {
  let component: OutOfScopeIssuesComponent;
  let fixture: ComponentFixture<OutOfScopeIssuesComponent>;

  /**
   * Compiles the component and renders it with the given proposals.
   *
   * Preconditions: called once per test, before any `setInput`/DOM assertion.
   * Postconditions: `component` and `fixture` refer to a rendered instance
   *   whose `proposals` input holds `proposals`; a first change-detection pass
   *   has run, so `ngOnChanges` has already seen its `firstChange`.
   */
  async function setup(proposals: OutOfScopeProposalItem[]): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [OutOfScopeIssuesComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(OutOfScopeIssuesComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('proposals', proposals);
    fixture.detectChanges();
  }

  /** The rendered host element, typed for DOM queries. */
  function host(): HTMLElement {
    return fixture.nativeElement as HTMLElement;
  }

  /**
   * Finds a rendered `<button>` by a substring of its text.
   *
   * Preconditions: the template has been rendered (`setup` has run).
   * Postconditions: returns the first matching button, or `undefined` when the
   *   current render branch does not include one.
   */
  function buttonWithText(text: string): HTMLButtonElement | undefined {
    return Array.from(host().querySelectorAll('button')).find((b) => b.textContent?.includes(text));
  }

  it('creates', async () => {
    await setup([proposal('p0')]);
    expect(component).toBeTruthy();
  });

  describe('loading state', () => {
    it('announces itself through the shared app-loading-spinner role="status" region', async () => {
      await setup([]);
      fixture.componentRef.setInput('loading', true);
      fixture.detectChanges();

      expect(host().querySelector('app-loading-spinner')).not.toBeNull();
      // The hand-rolled block (and its SCSS hook) is gone.
      expect(host().querySelector('.oos-issues__loading')).toBeNull();

      const statuses = Array.from(host().querySelectorAll('[role="status"]'));
      expect(
        statuses.some((el) => el.textContent?.includes('Loading out-of-scope issues…')),
      ).toBe(true);

      // Every surviving mat-spinner is either the in-button filing spinner (whose
      // button text conveys state) or the shared spinner's aria-hidden inner one.
      const spinners = Array.from(host().querySelectorAll('mat-spinner'));
      expect(spinners.length).toBeGreaterThan(0);
      for (const spinner of spinners) {
        expect(spinner.closest('button') ?? spinner.closest('app-loading-spinner')).not.toBeNull();
      }
    });

    it('suppresses the empty and populated branches while loading', async () => {
      await setup([proposal('p0')]);
      fixture.componentRef.setInput('loading', true);
      fixture.detectChanges();
      expect(host().querySelector('app-empty-state')).toBeNull();
      expect(host().querySelector('.oos-issues__list')).toBeNull();
    });
  });

  describe('render branches', () => {
    it('renders the error banner when an error is set', async () => {
      await setup([proposal('p0')]);
      fixture.componentRef.setInput('error', 'proposals fetch failed');
      fixture.detectChanges();
      expect(host().querySelector('app-inline-banner')).not.toBeNull();
      expect(host().textContent).toContain('proposals fetch failed');
    });

    it('renders no banner when there is no error', async () => {
      await setup([proposal('p0')]);
      expect(host().querySelector('app-inline-banner')).toBeNull();
    });

    it('renders the empty state with a Refresh button when there are no proposals', async () => {
      await setup([]);
      expect(host().querySelector('app-empty-state')).not.toBeNull();
      expect(host().textContent).toContain('No unfiled out-of-scope issues found.');
      const statuses = Array.from(host().querySelectorAll('[role="status"]'));
      expect(statuses.some((el) => el.textContent?.includes('No unfiled out-of-scope issues found.'))).toBe(true);
      const refreshButton = buttonWithText('Refresh');
      expect(refreshButton).toBeTruthy();
      // Projected via <ng-content>, which sits outside app-empty-state's role="status" region.
      expect(refreshButton?.closest('[role="status"]')).toBeNull();
    });

    it('renders one list item per proposal with its severity, category, PR, and text', async () => {
      await setup([
        proposal('p0', { description: 'latent leak', suggestion: 'close the handle' }),
        proposal('p1', { severity: 'critical', category: 'security' }),
      ]);
      expect(host().querySelectorAll('.oos-issue').length).toBe(2);
      const text = host().querySelector('.oos-issues__list')?.textContent ?? '';
      expect(text).toContain('latent leak');
      expect(text).toContain('close the handle');
      expect(text).toContain('critical');
      expect(text).toContain('security');
      expect(text).toContain('PR #42');
    });

    it('omits the suggestion paragraph when a proposal has none', async () => {
      await setup([proposal('p0', { suggestion: '' })]);
      expect(host().querySelector('.oos-issue__suggestion')).toBeNull();
    });
  });

  describe('composite ids and selection', () => {
    it('keys a proposal by job_id:id', async () => {
      await setup([]);
      expect(component.compositeId(proposal('p0', { job_id: 'job-9' }))).toBe('job-9:p0');
    });

    it('toggles a single proposal on and off', async () => {
      const p = proposal('p0');
      await setup([p]);
      expect(component.isSelected(p)).toBe(false);
      component.toggleSelection(p);
      expect(component.isSelected(p)).toBe(true);
      expect(component.selectedCount).toBe(1);
      component.toggleSelection(p);
      expect(component.isSelected(p)).toBe(false);
      expect(component.selectedCount).toBe(0);
    });

    it('reports none, some, and all selected', async () => {
      const [p0, p1] = [proposal('p0'), proposal('p1')];
      await setup([p0, p1]);
      expect(component.allSelected).toBe(false);
      expect(component.someSelected).toBe(false);

      component.toggleSelection(p0);
      expect(component.someSelected).toBe(true);
      expect(component.allSelected).toBe(false);

      component.toggleSelection(p1);
      expect(component.allSelected).toBe(true);
      expect(component.someSelected).toBe(false);
    });

    it('is never "all selected" with an empty proposals list', async () => {
      await setup([]);
      expect(component.allSelected).toBe(false);
      expect(component.someSelected).toBe(false);
    });

    it('selects and deselects every proposal at once', async () => {
      await setup([proposal('p0'), proposal('p1')]);
      component.selectAll();
      expect(component.selectedCount).toBe(2);
      component.deselectAll();
      expect(component.selectedCount).toBe(0);
    });

    it('toggles select-all in both directions', async () => {
      await setup([proposal('p0'), proposal('p1')]);
      component.toggleSelectAll();
      expect(component.allSelected).toBe(true);
      component.toggleSelectAll();
      expect(component.selectedCount).toBe(0);
    });

    it('selects a proposal from its list checkbox', async () => {
      await setup([proposal('p0')]);
      const checkbox = host().querySelector('.oos-issue__checkbox input') as HTMLInputElement;
      checkbox.click();
      fixture.detectChanges();
      expect(component.selectedCount).toBe(1);
    });

    it('selects everything from the Select All checkbox', async () => {
      await setup([proposal('p0'), proposal('p1')]);
      const selectAll = host().querySelector(
        '.oos-issues__select-controls input',
      ) as HTMLInputElement;
      selectAll.click();
      fixture.detectChanges();
      expect(component.allSelected).toBe(true);
    });
  });

  describe('proposalLocation', () => {
    it('summarises a multi-location proposal as a count', async () => {
      await setup([]);
      const combined = proposal('p0', {
        locations: [
          { file_path: 'a.py', line: 1, description: 'bare import', suggestion: 'scope it' },
          { file_path: 'b.py', line: 5, description: 'bare import', suggestion: 'scope it' },
        ],
      });
      expect(component.proposalLocation(combined)).toBe('2 locations');
    });

    it('renders path:line, path, or nothing for a single location', async () => {
      await setup([]);
      expect(component.proposalLocation(proposal('p0'))).toBe('a.py:3');
      expect(component.proposalLocation(proposal('p0', { line: null }))).toBe('a.py');
      expect(component.proposalLocation(proposal('p0', { file_path: '', line: null }))).toBe('');
    });

    it('treats a proposal with no locations array as single-location', async () => {
      await setup([]);
      // The API omits `locations` for proposals that were never combined.
      expect(component.proposalLocation(proposal('p0', { locations: undefined }))).toBe('a.py:3');
    });

    it('omits the location element when there is no location to show', async () => {
      await setup([proposal('p0', { file_path: '', line: null })]);
      expect(host().querySelector('.oos-issue__loc')).toBeNull();
    });
  });

  describe('filing', () => {
    it('emits the selected composite ids', async () => {
      const p0 = proposal('p0');
      await setup([p0, proposal('p1')]);
      component.toggleSelection(p0);
      const emitted: string[][] = [];
      component.fileIssuesRequested.subscribe((ids) => emitted.push(ids));
      component.onFileIssues();
      expect(emitted).toEqual([['job-1:p0']]);
    });

    it('does not emit when nothing is selected', async () => {
      await setup([proposal('p0')]);
      const emitted: string[][] = [];
      component.fileIssuesRequested.subscribe((ids) => emitted.push(ids));
      component.onFileIssues();
      expect(emitted).toEqual([]);
    });

    it('does not emit while a filing request is already in flight', async () => {
      const p0 = proposal('p0');
      await setup([p0]);
      component.toggleSelection(p0);
      fixture.componentRef.setInput('filing', true);
      fixture.detectChanges();
      const emitted: string[][] = [];
      component.fileIssuesRequested.subscribe((ids) => emitted.push(ids));
      component.onFileIssues();
      expect(emitted).toEqual([]);
    });

    it('emits from the Add Github Issues button click', async () => {
      await setup([proposal('p0')]);
      const checkbox = host().querySelector('.oos-issue__checkbox input') as HTMLInputElement;
      checkbox.click();
      fixture.detectChanges();
      const emitted: string[][] = [];
      component.fileIssuesRequested.subscribe((ids) => emitted.push(ids));
      buttonWithText('Add Github Issues')?.click();
      fixture.detectChanges();
      expect(emitted).toEqual([['job-1:p0']]);
    });

    it('shows the in-button filing spinner and disables both actions while filing', async () => {
      const p0 = proposal('p0');
      await setup([p0]);
      // Select first, so `filing` is the only thing disabling the file button —
      // an empty selection would disable it regardless and make the assertion vacuous.
      component.toggleSelection(p0);
      fixture.componentRef.setInput('filing', true);
      fixture.detectChanges();
      expect(host().textContent).toContain('Filing...');
      const inButton = host().querySelector('button mat-spinner');
      expect(inButton).not.toBeNull();
      expect(inButton?.getAttribute('aria-hidden')).toBe('true');
      expect(buttonWithText('Refresh')?.disabled).toBe(true);
      expect(buttonWithText('Filing...')?.disabled).toBe(true);
    });
  });

  describe('refresh', () => {
    it('emits from the empty-state Refresh button', async () => {
      await setup([]);
      const emitted: number[] = [];
      component.refreshRequested.subscribe(() => emitted.push(1));
      buttonWithText('Refresh')?.click();
      expect(emitted.length).toBe(1);
    });

    it('emits from the toolbar Refresh button', async () => {
      await setup([proposal('p0')]);
      const emitted: number[] = [];
      component.refreshRequested.subscribe(() => emitted.push(1));
      buttonWithText('Refresh')?.click();
      expect(emitted.length).toBe(1);
    });
  });

  describe('selection pruning', () => {
    it('drops selections for proposals removed by a later proposals input', async () => {
      const [p0, p1] = [proposal('p0'), proposal('p1')];
      await setup([p0, p1]);
      component.selectAll();
      expect(component.selectedCount).toBe(2);

      // The parent replaces `proposals` after filing removes p0 from the unfiled list.
      fixture.componentRef.setInput('proposals', [p1]);
      fixture.detectChanges();
      expect(component.selectedCount).toBe(1);
      expect(component.isSelected(p1)).toBe(true);
      expect(component.isSelected(p0)).toBe(false);
    });

    it('leaves the selection alone on the first proposals binding', async () => {
      await setup([proposal('p0')]);
      // ngOnChanges fired once with firstChange=true during setup; nothing was
      // selected then, and a later selection must survive until the input changes.
      component.selectAll();
      expect(component.selectedCount).toBe(1);
    });

    it('prunes on demand via pruneSelection()', async () => {
      const [p0, p1] = [proposal('p0'), proposal('p1')];
      await setup([p0, p1]);
      component.selectAll();
      // Assign the property directly rather than through setInput, so ngOnChanges
      // never fires and pruneSelection() is the only thing that prunes.
      component.proposals = [p1];
      component.pruneSelection();
      expect(component.selectedCount).toBe(1);
      expect(component.isSelected(p1)).toBe(true);
    });
  });
});
