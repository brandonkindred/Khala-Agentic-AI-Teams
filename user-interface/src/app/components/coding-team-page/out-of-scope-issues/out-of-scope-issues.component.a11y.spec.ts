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

  // NOTE: MatTooltip's overlay open-on-focus behavior relies on FocusMonitor's
  // keyboard-origin detection, which is not reliably exercisable under jsdom
  // (no real focus/paint pipeline). These tests assert the static, DOM-level
  // preconditions for that behavior — an aria-label carrying the tooltip's
  // content, and (for the PR chip) a focusable host — not that the tooltip
  // overlay actually opens. Tooltip-opens-on-Tab is verified manually in
  // Chrome (see PR description).

  it('exposes severity and category as aria-labels without adding new tab stops', async () => {
    const fixture = await createFixture([
      proposal('p0', { severity: 'critical', category: 'security' }),
    ]);
    const host: HTMLElement = fixture.nativeElement;
    const severity = host.querySelector<HTMLElement>('.oos-chip--sev-critical');
    const category = host.querySelector<HTMLElement>('.oos-chip--category');
    expect(severity?.getAttribute('aria-label')).toBe('Severity: critical');
    expect(category?.getAttribute('aria-label')).toBe('Category: security');
    // Deliberately not focusable: aria-label alone replaces the computed accessible
    // name regardless of role, so it already satisfies WCAG 1.3.1 here. Adding
    // tabindex would cost two extra tab stops per proposal in a list that can be long.
    expect(severity?.hasAttribute('tabindex')).toBe(false);
    expect(category?.hasAttribute('tabindex')).toBe(false);
    await expectNoAxeViolations(host);
  }, 15000);

  it('exposes the PR chip as a focusable, labeled element and adds exactly one new tab stop', async () => {
    const fixture = await createFixture([proposal('p0', { pr_number: 123 })]);
    const host: HTMLElement = fixture.nativeElement;
    const prChip = host.querySelector<HTMLElement>('.oos-issue__pr');
    expect(prChip?.tabIndex).toBe(0);
    expect(prChip?.getAttribute('role')).toBe('img');
    // role="img" makes descendant text presentational to AT, so the visible
    // "PR #123" text has to be folded into the label too, not just the
    // tooltip's provenance sentence.
    expect(prChip?.getAttribute('aria-label')).toBe('PR #123 — Found during review of this PR');
    // Confirms the severity/category "no tabindex" decision holds structurally: the
    // PR chip is the only focusable element this row adds.
    const meta = host.querySelector('.oos-issue__meta');
    expect(meta?.querySelectorAll('[tabindex]').length).toBe(1);
    await expectNoAxeViolations(host);
  }, 15000);
});
