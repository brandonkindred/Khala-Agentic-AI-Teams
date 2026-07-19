import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PendingIssueProposalsComponent } from './pending-issue-proposals.component';
import type { PendingIssueProposal } from '../../../models/coding-team.model';
import { expectNoAxeViolations } from '../../../testing/a11y';

function proposal(id: string, over: Record<string, unknown> = {}): PendingIssueProposal {
  return {
    id,
    severity: 'high',
    category: 'logic',
    file_path: 'a.py',
    line: 3,
    description: `bug ${id}`,
    suggestion: 'fix',
    issue_number: null,
    issue_url: null,
    ...over,
  };
}

describe('PendingIssueProposalsComponent a11y', () => {
  async function createFixture(
    proposals: PendingIssueProposal[],
  ): Promise<ComponentFixture<PendingIssueProposalsComponent>> {
    await TestBed.configureTestingModule({
      imports: [PendingIssueProposalsComponent, NoopAnimationsModule],
    }).compileComponents();

    const fixture = TestBed.createComponent(PendingIssueProposalsComponent);
    fixture.componentRef.setInput('proposals', proposals);
    fixture.detectChanges();
    return fixture;
  }

  it('has no axe violations with open, selectable proposals', async () => {
    const fixture = await createFixture([
      proposal('p0'),
      proposal('p1', { severity: 'critical', description: 'unbounded recursion' }),
    ]);
    const host: HTMLElement = fixture.nativeElement;
    // Guard: don't pass axe vacuously against an empty DOM.
    expect(host.querySelector('.cr-proposals')).toBeTruthy();
    expect(host.querySelectorAll('.cr-proposal').length).toBe(2);
    expect(host.querySelector('.cr-proposal__select input')).toBeTruthy();
    await expectNoAxeViolations(host);
  }, 15000);

  it('has no axe violations with filed, matched-existing, and combined-location proposals', async () => {
    const fixture = await createFixture([
      proposal('p0', { description: 'freshly filed bug', issue_number: 7, issue_url: 'https://x/issues/7' }),
      proposal('p1', {
        description: 'already tracked bug',
        issue_number: 42,
        issue_url: 'https://x/issues/42',
        matched_existing: true,
      }),
      proposal('p2', {
        locations: [
          { file_path: 'a.py', line: 1, description: 'bare import `os`', suggestion: 'scope it' },
          { file_path: 'b.py', line: 5, description: 'bare import `sys`', suggestion: 'scope it' },
        ],
      }),
    ]);
    const host: HTMLElement = fixture.nativeElement;
    expect(host.querySelector('.cr-proposal__filed')).toBeTruthy();
    expect(host.querySelector('.cr-proposal__matched')).toBeTruthy();
    expect(host.querySelector('.cr-proposal__locations')).toBeTruthy();
    await expectNoAxeViolations(host);
  }, 15000);
});
