import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { OutOfScopeIssuesComponent } from './out-of-scope-issues.component';
import type { OutOfScopeProposalItem } from '../../../models/integrations.model';
import { expectNoAxeViolations } from '../../../testing/a11y';

/**
 * Builds an out-of-scope proposal with sane defaults, overridable per field.
 *
 * Preconditions: `id` is unique within a single test's proposal list.
 * Postconditions: returns a fully-populated `OutOfScopeProposalItem`.
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

describe('OutOfScopeIssuesComponent a11y', () => {
  /**
   * Renders the component with the given inputs.
   *
   * Preconditions: `inputs` keys name real `@Input`s on the component.
   * Postconditions: returns a fixture on which one change-detection pass has run.
   */
  async function createFixture(
    proposals: OutOfScopeProposalItem[],
    inputs: Record<string, unknown> = {},
  ): Promise<ComponentFixture<OutOfScopeIssuesComponent>> {
    await TestBed.configureTestingModule({
      imports: [OutOfScopeIssuesComponent, NoopAnimationsModule],
    }).compileComponents();

    const fixture = TestBed.createComponent(OutOfScopeIssuesComponent);
    fixture.componentRef.setInput('proposals', proposals);
    for (const [name, value] of Object.entries(inputs)) {
      fixture.componentRef.setInput(name, value);
    }
    fixture.detectChanges();
    return fixture;
  }

  // Regression guard only. axe-core has no rule for WCAG 4.1.3 Status Messages, so
  // this cannot prove the loading state is announced — the role="status" assertion in
  // out-of-scope-issues.component.spec.ts is the actual proof. This catches the
  // adjacent breakages axe *does* see (e.g. an unlabeled role="progressbar").
  it('has no axe violations while loading', async () => {
    const fixture = await createFixture([], { loading: true });
    const host: HTMLElement = fixture.nativeElement;
    // Guard: don't pass axe vacuously against a DOM with no spinner in it.
    expect(host.querySelector('app-loading-spinner')).not.toBeNull();
    await expectNoAxeViolations(host);
  }, 15000);

  it('has no axe violations with selectable proposals', async () => {
    const fixture = await createFixture([
      proposal('p0'),
      proposal('p1', { severity: 'critical', description: 'unbounded recursion' }),
    ]);
    const host: HTMLElement = fixture.nativeElement;
    expect(host.querySelectorAll('.oos-issue').length).toBe(2);
    expect(host.querySelector('.oos-issue__checkbox input')).not.toBeNull();
    await expectNoAxeViolations(host);
  }, 15000);

  it('has no axe violations in the empty state with an error banner', async () => {
    const fixture = await createFixture([], { error: 'proposals fetch failed' });
    const host: HTMLElement = fixture.nativeElement;
    expect(host.querySelector('.oos-issues__empty')).not.toBeNull();
    expect(host.querySelector('app-inline-banner')).not.toBeNull();
    await expectNoAxeViolations(host);
  }, 15000);
});
