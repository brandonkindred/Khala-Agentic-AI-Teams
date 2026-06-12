import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { NEVER, Subject, of, throwError } from 'rxjs';
import { CognitionTabComponent } from './cognition-tab.component';
import { AgentCatalogApiService } from '../../../services/agent-catalog-api.service';
import { CognitionApiService } from '../../../services/cognition-api.service';
import type { AgentSummary } from '../../../models/agent-catalog.model';
import type { MemoryEvent, Rule, RuleProposal } from '../../../models/cognition.model';

const agents: AgentSummary[] = [
  {
    id: 'a1',
    team: 'software_engineering',
    name: 'backend-dev-agent',
    summary: '',
    tags: [],
    has_input_schema: false,
    has_output_schema: false,
    has_invoke: false,
    has_sandbox: false,
  },
  {
    id: 'a2',
    team: 't',
    name: 'other',
    summary: '',
    tags: [],
    has_input_schema: false,
    has_output_schema: false,
    has_invoke: false,
    has_sandbox: false,
  },
];

const addProposal: RuleProposal = {
  id: 'p1',
  agent_id: 'a1',
  action: 'add',
  proposed_rule: { text: 'Run make lint-fix', mode: 'enforced' },
  evidence: [1, 2, 3],
  stale_evidence: false,
  status: 'pending',
  created_at: '2026-06-12T09:14:00Z',
};

const staleProposal: RuleProposal = {
  id: 'p2',
  agent_id: 'a1',
  action: 'amend',
  target_rule_id: 'r9',
  proposed_rule: { text: 'Cap writeback at 16 KB', mode: 'enforced' },
  evidence: [],
  stale_evidence: true,
  status: 'pending',
  created_at: '2026-06-12T08:02:00Z',
};

const rules: Rule[] = [
  {
    id: 'r9',
    agent_id: 'a1',
    text: 'Cap writeback at 8 KB',
    mode: 'enforced',
    status: 'active',
    predicate: {},
    rationale: 'learned from outcomes',
    source: 'derived',
    evidence: [],
    needs_review: false,
    priority: 90,
    created_at: '',
    updated_at: '',
  },
];

const events: MemoryEvent[] = [
  {
    id: 'e1',
    agent_id: 'a1',
    kind: 'tool_call',
    content: 'Ran build',
    data: {},
    salience: 0.82,
    occurred_at: '2026-06-12T14:03:11Z',
    source_run_id: 'run',
    source_seq: 0,
  },
];

describe('CognitionTabComponent', () => {
  let catalog: { listAgents: ReturnType<typeof vi.fn> };
  let api: {
    listProposals: ReturnType<typeof vi.fn>;
    approveProposal: ReturnType<typeof vi.fn>;
    rejectProposal: ReturnType<typeof vi.fn>;
    listMemoryEvents: ReturnType<typeof vi.fn>;
    listRules: ReturnType<typeof vi.fn>;
  };
  let dialog: { open: ReturnType<typeof vi.fn> };

  function build() {
    TestBed.configureTestingModule({
      imports: [CognitionTabComponent, NoopAnimationsModule],
      providers: [
        { provide: AgentCatalogApiService, useValue: catalog },
        { provide: CognitionApiService, useValue: api },
      ],
    });
    // MatDialogModule (imported by the component) provides its own MatDialog at
    // the component injector, so override the token globally to use the mock.
    TestBed.overrideProvider(MatDialog, { useValue: dialog });
    return TestBed.createComponent(CognitionTabComponent);
  }

  beforeEach(() => {
    catalog = { listAgents: vi.fn().mockReturnValue(of(agents)) };
    api = {
      listProposals: vi.fn().mockReturnValue(of([addProposal, staleProposal])),
      approveProposal: vi.fn().mockReturnValue(of(rules[0])),
      rejectProposal: vi.fn().mockReturnValue(of({ ...addProposal, status: 'rejected' })),
      listMemoryEvents: vi.fn().mockReturnValue(of(events)),
      listRules: vi.fn().mockReturnValue(of(rules)),
    };
    dialog = { open: vi.fn().mockReturnValue({ afterClosed: () => of(true) }) };
  });

  it('loads agents, auto-selects the first, and loads all three sections', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.agents()).toEqual(agents);
    expect(c.selectedAgentId()).toBe('a1');
    expect(api.listProposals).toHaveBeenCalledWith('a1', { status: 'pending' });
    expect(api.listMemoryEvents).toHaveBeenCalledWith('a1', { bySalience: true, topN: 50 });
    expect(api.listRules).toHaveBeenCalledWith('a1', { status: 'active' });
    expect(c.proposals().length).toBe(2);
  });

  it('surfaces an agent-load error and does not select', () => {
    catalog.listAgents = vi.fn().mockReturnValue(throwError(() => ({ message: 'down' })));
    const f = build();
    f.detectChanges();
    expect(f.componentInstance.agentsError()).toBe('down');
    expect(f.componentInstance.selectedAgentId()).toBeNull();
  });

  it('refetches proposals when the status filter changes (all → no status param)', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.setProposalFilter('all');
    expect(api.listProposals).toHaveBeenLastCalledWith('a1', {});
    f.componentInstance.setProposalFilter('rejected');
    expect(api.listProposals).toHaveBeenLastCalledWith('a1', { status: 'rejected' });
  });

  it('approves: optimistically removes the card and refetches rules', () => {
    const f = build();
    f.detectChanges();
    api.listRules.mockClear();
    f.componentInstance.performApprove(addProposal);
    expect(api.approveProposal).toHaveBeenCalledWith('a1', 'p1');
    expect(f.componentInstance.proposals().find((p) => p.id === 'p1')).toBeUndefined();
    expect(api.listRules).toHaveBeenCalledTimes(1); // refetch after activation
  });

  it('rolls back an approve when the API fails', () => {
    api.approveProposal = vi
      .fn()
      .mockReturnValue(throwError(() => ({ error: { detail: 'conflict' } })));
    const f = build();
    f.detectChanges();
    f.componentInstance.performApprove(addProposal);
    expect(f.componentInstance.proposals().find((p) => p.id === 'p1')).toBeDefined();
    expect(f.componentInstance.proposalsError()).toBe('conflict');
  });

  it('never approves a stale-evidence proposal', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.performApprove(staleProposal);
    expect(api.approveProposal).not.toHaveBeenCalled();
    f.componentInstance.approve(staleProposal);
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('keeps an approved card visible (as approved) under the all filter', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.setProposalFilter('all');
    f.componentInstance.performApprove(addProposal);
    const card = f.componentInstance.proposals().find((p) => p.id === 'p1');
    expect(card?.status).toBe('approved');
  });

  it('rejects: optimistically removes the card', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.performReject(addProposal);
    expect(api.rejectProposal).toHaveBeenCalledWith('a1', 'p1');
    expect(f.componentInstance.proposals().find((p) => p.id === 'p1')).toBeUndefined();
  });

  it('keeps a rejected card visible (from the response) under the all filter', () => {
    api.rejectProposal = vi
      .fn()
      .mockReturnValue(of({ ...addProposal, status: 'rejected', decided_by: 'op', decided_at: 't' }));
    const f = build();
    f.detectChanges();
    f.componentInstance.setProposalFilter('all');
    f.componentInstance.performReject(addProposal);
    const card = f.componentInstance.proposals().find((p) => p.id === 'p1');
    expect(card?.status).toBe('rejected');
    expect(card?.decided_by).toBe('op');
  });

  it('rolls back a reject when the API fails', () => {
    api.rejectProposal = vi
      .fn()
      .mockReturnValue(throwError(() => ({ error: { detail: 'nope' } })));
    const f = build();
    f.detectChanges();
    f.componentInstance.performReject(addProposal);
    expect(f.componentInstance.proposals().find((p) => p.id === 'p1')).toBeDefined();
    expect(f.componentInstance.proposalsError()).toBe('nope');
  });

  it('runs approve through a confirm dialog only when confirmed', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.approve(addProposal);
    expect(dialog.open).toHaveBeenCalled();
    expect(api.approveProposal).toHaveBeenCalledWith('a1', 'p1');
  });

  it('does not approve when the confirm dialog is dismissed', () => {
    dialog.open = vi.fn().mockReturnValue({ afterClosed: () => of(false) });
    const f = build();
    f.detectChanges();
    f.componentInstance.approve(addProposal);
    expect(api.approveProposal).not.toHaveBeenCalled();
  });

  it('runs reject through a confirm dialog', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.reject(addProposal);
    expect(dialog.open).toHaveBeenCalled();
    expect(api.rejectProposal).toHaveBeenCalledWith('a1', 'p1');
  });

  it('refetches memory when order or count changes', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.setMemoryOrder(false);
    expect(api.listMemoryEvents).toHaveBeenLastCalledWith('a1', { bySalience: false, topN: 50 });
    f.componentInstance.setMemoryTopN(100);
    expect(api.listMemoryEvents).toHaveBeenLastCalledWith('a1', { bySalience: false, topN: 100 });
  });

  it('refetches rules when the status filter changes (all → no status param)', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.setRuleFilter('all');
    expect(api.listRules).toHaveBeenLastCalledWith('a1', {});
    f.componentInstance.setRuleFilter('retired');
    expect(api.listRules).toHaveBeenLastCalledWith('a1', { status: 'retired' });
  });

  it('resolves target rule text from loaded rules, else falls back to the id', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.targetRuleText('r9')).toBe('Cap writeback at 8 KB');
    expect(c.targetRuleText('unknown')).toBe('unknown');
    expect(c.targetRuleText(null)).toBe('');
  });

  it('exposes proposal display helpers', () => {
    const f = build();
    const c = f.componentInstance;
    expect(c.evidenceCount(addProposal)).toBe(3);
    expect(c.proposedRuleText(addProposal)).toBe('Run make lint-fix');
    expect(c.proposedRuleMode(addProposal)).toBe('enforced');
    expect(c.proposedRuleMode({ ...addProposal, proposed_rule: null })).toBe('advisory');
  });

  it('surfaces per-section load errors', () => {
    api.listProposals = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'p' } })));
    api.listMemoryEvents = vi.fn().mockReturnValue(throwError(() => ({ message: 'm' })));
    api.listRules = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'r' } })));
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.proposalsError()).toBe('p');
    expect(c.memoryError()).toBe('m');
    expect(c.rulesError()).toBe('r');
    expect(c.proposals()).toEqual([]);
    expect(c.events()).toEqual([]);
    expect(c.rules()).toEqual([]);
  });

  it('renders proposals first, then memory, then rules', () => {
    const f = build();
    f.detectChanges();
    const sections = (f.nativeElement as HTMLElement).querySelectorAll('.cognition-section h3');
    expect(Array.from(sections).map((s) => s.textContent?.trim())).toEqual([
      'Rule proposals',
      'Memory',
      'Rules',
    ]);
  });

  it('sorts rules by priority, highest first', () => {
    api.listRules = vi.fn().mockReturnValue(
      of([
        { ...rules[0], id: 'low', priority: 10 },
        { ...rules[0], id: 'high', priority: 90 },
        { ...rules[0], id: 'mid', priority: 50 },
      ]),
    );
    const f = build();
    f.detectChanges();
    expect(f.componentInstance.rules().map((r) => r.id)).toEqual(['high', 'mid', 'low']);
  });

  it('surfaces a 503 as a single panel-wide banner only when every section is down', () => {
    const err503 = { status: 503, error: { detail: 'down' } };
    api.listProposals = vi.fn().mockReturnValue(throwError(() => err503));
    api.listMemoryEvents = vi.fn().mockReturnValue(throwError(() => err503));
    api.listRules = vi.fn().mockReturnValue(throwError(() => err503));
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.storageUnavailable()).toBe(true);
    expect((f.nativeElement as HTMLElement).querySelectorAll('.cognition-section').length).toBe(0);
  });

  it('does not show the panel-wide banner when only one section is 503', () => {
    api.listMemoryEvents = vi.fn().mockReturnValue(throwError(() => ({ status: 503 })));
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.memoryUnavailable()).toBe(true);
    expect(c.storageUnavailable()).toBe(false); // proposals + rules loaded fine
    expect((f.nativeElement as HTMLElement).querySelectorAll('.cognition-section').length).toBe(3);
  });

  it('highlights a rule via signal when a retire link is clicked', () => {
    vi.useFakeTimers();
    const f = build();
    f.componentInstance.scrollToRule('r9');
    expect(f.componentInstance.highlightedRuleId()).toBe('r9');
    vi.advanceTimersByTime(1500);
    expect(f.componentInstance.highlightedRuleId()).toBeNull();
    f.componentInstance.scrollToRule(null); // no-op branch
    vi.useRealTimers();
  });

  it('toggles a memory event data panel', () => {
    const f = build();
    const c = f.componentInstance;
    expect(c.hasData({ ...events[0], data: { k: 1 } })).toBe(true);
    expect(c.hasData(events[0])).toBe(false);
    c.toggleEventData('e1');
    expect(c.isEventExpanded('e1')).toBe(true);
    expect(c.formatData({ ...events[0], data: { k: 1 } })).toContain('"k": 1');
    c.toggleEventData('e1');
    expect(c.isEventExpanded('e1')).toBe(false);
  });

  it('skips approve reconciliation when proposals reload mid-flight', () => {
    const approveSubject = new Subject<typeof rules[0]>();
    api.approveProposal = vi.fn().mockReturnValue(approveSubject);
    const f = build();
    f.detectChanges();
    f.componentInstance.setProposalFilter('all'); // list = [add, stale]
    f.componentInstance.performApprove(addProposal); // captures reqId, optimistic remove
    f.componentInstance.loadProposals(); // concurrent reload bumps the request id
    approveSubject.next(rules[0]);
    approveSubject.complete();
    // Reconcile is skipped: the reloaded list wins, p1 is not forced to approved.
    expect(f.componentInstance.proposals().find((p) => p.id === 'p1')?.status).toBe('pending');
  });

  it('ignores a second decision while one is already in flight', () => {
    api.approveProposal = vi.fn().mockReturnValue(NEVER); // stays pending
    const f = build();
    f.detectChanges();
    f.componentInstance.performApprove(addProposal);
    expect(f.componentInstance.actingProposalId()).toBe('p1');
    // A concurrent reject on another proposal must be ignored.
    f.componentInstance.performReject({ ...addProposal, id: 'p2' });
    expect(api.rejectProposal).not.toHaveBeenCalled();
  });

  it('clears the storage-unavailable banner once a section loads again', () => {
    const err503 = { status: 503 };
    api.listProposals = vi.fn().mockReturnValue(throwError(() => err503));
    api.listMemoryEvents = vi.fn().mockReturnValue(throwError(() => err503));
    api.listRules = vi.fn().mockReturnValue(throwError(() => err503));
    const f = build();
    f.detectChanges();
    expect(f.componentInstance.storageUnavailable()).toBe(true);
    // A single section recovering (e.g. after a filter change) clears the banner.
    api.listProposals = vi.fn().mockReturnValue(of([]));
    f.componentInstance.loadProposals();
    expect(f.componentInstance.storageUnavailable()).toBe(false);
  });

  it('resets filters to defaults when switching agents', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.setProposalFilter('approved');
    f.componentInstance.setRuleFilter('retired');
    f.componentInstance.selectAgent('a2');
    expect(f.componentInstance.proposalStatusFilter()).toBe('pending');
    expect(f.componentInstance.ruleStatusFilter()).toBe('active');
  });

  it('builds a proposal summary from the proposed or target rule text', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.proposalSummary(addProposal)).toBe('Run make lint-fix');
    expect(c.proposalSummary({ ...staleProposal, proposed_rule: null })).toBe(
      'Cap writeback at 8 KB',
    );
  });
});
