import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { PendingIssueProposalsComponent } from './pending-issue-proposals.component';
import type { PendingIssueProposal } from '../../../models/coding-team.model';

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

describe('PendingIssueProposalsComponent', () => {
  let component: PendingIssueProposalsComponent;
  let fixture: ComponentFixture<PendingIssueProposalsComponent>;

  async function setup(proposals: PendingIssueProposal[]): Promise<void> {
    await TestBed.configureTestingModule({
      imports: [PendingIssueProposalsComponent, NoopAnimationsModule],
    }).compileComponents();

    fixture = TestBed.createComponent(PendingIssueProposalsComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('proposals', proposals);
    fixture.detectChanges();
  }

  it('creates', async () => {
    await setup([proposal('p0')]);
    expect(component).toBeTruthy();
  });

  it('exposes only proposals not yet filed as open', async () => {
    await setup([proposal('p0'), proposal('p1', { issue_url: 'https://x/1' })]);
    expect(component.openProposals.length).toBe(1);
    expect(component.openProposals[0].id).toBe('p0');
  });

  it('formats a proposal location (path:line, path, or empty)', async () => {
    await setup([]);
    expect(component.proposalLocation(proposal('p0'))).toBe('a.py:3');
    expect(component.proposalLocation(proposal('p0', { line: null }))).toBe('a.py');
    expect(component.proposalLocation(proposal('p0', { file_path: '', line: null }))).toBe('');
  });

  it('formats a combined proposal location as an occurrence count', async () => {
    await setup([]);
    const combined = proposal('p0', {
      locations: [
        { file_path: 'a.py', line: 1, description: 'bare import `os`', suggestion: 'scope it' },
        { file_path: 'b.py', line: 5, description: 'bare import `sys`', suggestion: 'scope it' },
      ],
    });
    expect(component.isCombinedProposal(combined)).toBe(true);
    expect(component.proposalLocation(combined)).toBe('2 locations');
    expect(component.isCombinedProposal(proposal('p0'))).toBe(false);
  });

  it('formats a single location as path:line, path, or empty', async () => {
    await setup([]);
    expect(
      component.locationText({ file_path: 'a.py', line: 3, description: '', suggestion: '' }),
    ).toBe('a.py:3');
    expect(
      component.locationText({ file_path: 'a.py', line: null, description: '', suggestion: '' }),
    ).toBe('a.py');
    expect(
      component.locationText({ file_path: '', line: null, description: '', suggestion: '' }),
    ).toBe('');
  });

  it('toggles proposal selection and tracks the count', async () => {
    await setup([proposal('p0')]);
    expect(component.isProposalSelected('p0')).toBe(false);
    component.toggleProposal('p0');
    expect(component.isProposalSelected('p0')).toBe(true);
    expect(component.selectedCount).toBe(1);
    component.toggleProposal('p0');
    expect(component.isProposalSelected('p0')).toBe(false);
    expect(component.selectedCount).toBe(0);
  });

  it('emits selected proposal ids on requestCreateIssues', async () => {
    await setup([proposal('p0'), proposal('p1')]);
    component.toggleProposal('p0');
    const emitted: string[][] = [];
    component.createIssuesRequested.subscribe((ids) => emitted.push(ids));
    component.requestCreateIssues();
    expect(emitted).toEqual([['p0']]);
  });

  it('does not emit when nothing is selected', async () => {
    await setup([proposal('p0')]);
    const emitted: string[][] = [];
    component.createIssuesRequested.subscribe((ids) => emitted.push(ids));
    component.requestCreateIssues();
    expect(emitted).toEqual([]);
  });

  it('does not emit while an issue-creation request is already in flight', async () => {
    await setup([proposal('p0')]);
    component.toggleProposal('p0');
    fixture.componentRef.setInput('creatingIssues', true);
    fixture.detectChanges();
    const emitted: string[][] = [];
    component.createIssuesRequested.subscribe((ids) => emitted.push(ids));
    component.requestCreateIssues();
    expect(emitted).toEqual([]);
  });

  it('prunes the selection when the proposals input updates to reflect newly-filed issues', async () => {
    await setup([proposal('p0'), proposal('p1')]);
    component.toggleProposal('p0');
    component.toggleProposal('p1');
    expect(component.selectedCount).toBe(2);
    // The parent replaces `proposals` after filing issues — p0 was just created,
    // p1 was already filed by another tab (skipped server-side, but its returned
    // copy already carries an issue_url). Both must drop out of the selection.
    fixture.componentRef.setInput('proposals', [
      proposal('p0', { issue_number: 5, issue_url: 'https://x/issues/5' }),
      proposal('p1', { issue_number: 9, issue_url: 'https://x/issues/9' }),
    ]);
    fixture.detectChanges();
    expect(component.selectedCount).toBe(0);
    expect(component.isProposalSelected('p0')).toBe(false);
    expect(component.isProposalSelected('p1')).toBe(false);
  });

  it('keeps a still-open proposal selected across a proposals input update', async () => {
    await setup([proposal('p0'), proposal('p1')]);
    component.toggleProposal('p0');
    component.toggleProposal('p1');
    fixture.componentRef.setInput('proposals', [
      proposal('p0', { issue_number: 5, issue_url: 'https://x/issues/5' }),
      proposal('p1'), // still open — not filed
    ]);
    fixture.detectChanges();
    expect(component.selectedCount).toBe(1);
    expect(component.isProposalSelected('p1')).toBe(true);
  });

  it('renders proposals and emits on the Create GitHub issue(s) button click', async () => {
    await setup([proposal('p0', { description: 'latent leak' })]);
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.cr-proposals')?.textContent).toContain('latent leak');
    const emitted: string[][] = [];
    component.createIssuesRequested.subscribe((ids) => emitted.push(ids));
    const checkbox = host.querySelector('.cr-proposal__select input') as HTMLInputElement;
    checkbox.click();
    fixture.detectChanges();
    const button = Array.from(host.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Create GitHub issue'),
    ) as HTMLButtonElement;
    button.click();
    fixture.detectChanges();
    expect(emitted).toEqual([['p0']]);
  });

  it('renders a filed proposal as filed, without a checkbox', async () => {
    await setup([proposal('p0', { issue_number: 1, issue_url: 'https://x/1' })]);
    const host = fixture.nativeElement as HTMLElement;
    expect(host.querySelector('.cr-proposal__filed')).toBeTruthy();
    expect(host.querySelector('.cr-proposal__select input')).toBeFalsy();
  });

  it('renders a combined proposal\'s per-location breakdown', async () => {
    await setup([
      proposal('p0', {
        locations: [
          { file_path: 'a.py', line: 1, description: 'bare import `os`', suggestion: 'scope it' },
          { file_path: 'b.py', line: 5, description: 'bare import `sys`', suggestion: 'scope it' },
        ],
      }),
    ]);
    const host = fixture.nativeElement as HTMLElement;
    const text = host.querySelector('.cr-proposal__locations')?.textContent ?? '';
    expect(text).toContain('a.py:1');
    expect(text).toContain('bare import `os`');
    expect(text).toContain('b.py:5');
    expect(text).toContain('bare import `sys`');
  });

  it('renders the create-issue error banner when set', async () => {
    await setup([proposal('p0')]);
    fixture.componentRef.setInput('createIssueError', 'no scope');
    fixture.detectChanges();
    const host = fixture.nativeElement as HTMLElement;
    expect(host.textContent).toContain('no scope');
  });

  it('hides the actions button when there are no open proposals', async () => {
    await setup([proposal('p0', { issue_number: 1, issue_url: 'https://x/1' })]);
    const host = fixture.nativeElement as HTMLElement;
    const button = Array.from(host.querySelectorAll('button')).find((b) =>
      b.textContent?.includes('Create GitHub issue'),
    );
    expect(button).toBeFalsy();
  });
});
