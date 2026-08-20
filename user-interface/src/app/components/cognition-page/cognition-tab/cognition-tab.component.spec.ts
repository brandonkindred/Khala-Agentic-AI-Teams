import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { MatDialog } from '@angular/material/dialog';
import { NEVER, Subject, of, throwError } from 'rxjs';
import { CognitionTabComponent } from './cognition-tab.component';
import { AgentConsoleApiService } from '../../../services/agent-console-api.service';
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
    has_cognition: false,
    has_knowledge_graph: false,
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
    has_cognition: false,
    has_knowledge_graph: false,
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
        { provide: AgentConsoleApiService, useValue: catalog },
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

  // Safety net: if a fake-timer test fails before its own useRealTimers(), make
  // sure real timers are restored so later tests aren't affected.
  afterEach(() => {
    vi.useRealTimers();
  });

  it('loads agents, auto-selects the first, and loads all three sections', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.agents()).toEqual(agents);
    expect(c.selectedAgentId()).toBe('a1');
    expect(api.listProposals).toHaveBeenCalledWith('a1', { status: 'pending', limit: 200 });
    expect(api.listMemoryEvents).toHaveBeenCalledWith('a1', { bySalience: true, topN: 50 });
    expect(api.listRules).toHaveBeenCalledWith('a1', { status: 'active', limit: 500 });
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
    expect(api.listProposals).toHaveBeenLastCalledWith('a1', { limit: 200 });
    f.componentInstance.setProposalFilter('rejected');
    expect(api.listProposals).toHaveBeenLastCalledWith('a1', { status: 'rejected', limit: 200 });
  });

  it('approves: optimistically removes the card and refetches rules', () => {
    const f = build();
    f.detectChanges();
    api.listRules.mockClear();
    f.componentInstance.approve(addProposal);
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
    f.componentInstance.approve(addProposal);
    expect(f.componentInstance.proposals().find((p) => p.id === 'p1')).toBeDefined();
    expect(f.componentInstance.proposalsError()).toBe('conflict');
  });

  it('never approves a stale-evidence proposal', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.approve(staleProposal);
    expect(dialog.open).not.toHaveBeenCalled();
    expect(api.approveProposal).not.toHaveBeenCalled();
  });

  it('re-fetches proposals after approve so server audit fields appear (all filter)', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.setProposalFilter('all');
    // The post-approval reload returns the server's decided proposal.
    api.listProposals = vi
      .fn()
      .mockReturnValue(
        of([{ ...addProposal, status: 'approved', decided_by: 'op', decided_at: 't' }, staleProposal]),
      );
    f.componentInstance.approve(addProposal);
    const card = f.componentInstance.proposals().find((p) => p.id === 'p1');
    expect(card?.status).toBe('approved');
    expect(card?.decided_by).toBe('op');
  });

  it('rejects: optimistically removes the card', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.reject(addProposal);
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
    f.componentInstance.reject(addProposal);
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
    f.componentInstance.reject(addProposal);
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
    expect(api.listRules).toHaveBeenLastCalledWith('a1', { limit: 500 });
    f.componentInstance.setRuleFilter('retired');
    expect(api.listRules).toHaveBeenLastCalledWith('a1', { status: 'retired', limit: 500 });
  });

  it('resolves a target rule from loaded rules, empty/undefined when unknown', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.targetRule('r9')?.id).toBe('r9');
    expect(c.targetRuleText('r9')).toBe('Cap writeback at 8 KB');
    // Never leaks a raw id for an unknown/retired target.
    expect(c.targetRule('unknown')).toBeUndefined();
    expect(c.targetRuleText('unknown')).toBe('');
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
    const err503 = new HttpErrorResponse({ status: 503 });
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
    api.listMemoryEvents = vi.fn().mockReturnValue(throwError(() => new HttpErrorResponse({ status: 503 })));
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.memoryUnavailable()).toBe(true);
    expect(c.storageUnavailable()).toBe(false); // proposals + rules loaded fine
    expect((f.nativeElement as HTMLElement).querySelectorAll('.cognition-section').length).toBe(3);
  });

  it('scrolls directly and highlights when the target rule is already listed', () => {
    vi.useFakeTimers();
    const f = build();
    f.detectChanges(); // rules() = [r9], so 'r9' is present
    f.componentInstance.scrollToRule('r9');
    expect(f.componentInstance.highlightedRuleId()).toBe('r9');
    expect(f.componentInstance.ruleStatusFilter()).toBe('active'); // no filter change needed
    vi.advanceTimersByTime(1500);
    expect(f.componentInstance.highlightedRuleId()).toBeNull();
    f.componentInstance.scrollToRule(null); // no-op branch
    vi.useRealTimers();
  });

  it('scrolls to a revealed target once the all-rules reload includes it', () => {
    vi.useFakeTimers();
    const target = { ...rules[0], id: 'r-old', status: 'retired' as const };
    api.listRules = vi
      .fn()
      .mockImplementation((_id: string, q?: { status?: string }) =>
        q?.status === 'active' ? of([rules[0]]) : of([rules[0], target]),
      );
    const f = build();
    f.detectChanges(); // section shows active [r9]
    f.componentInstance.scrollToRule('r-old'); // not present → filter all + reload, then scroll
    expect(f.componentInstance.ruleStatusFilter()).toBe('all');
    expect(f.componentInstance.rules().some((r) => r.id === 'r-old')).toBe(true);
    vi.advanceTimersByTime(1500); // fire the deferred scroll + clear highlight
    vi.useRealTimers();
  });

  it('surfaces an error when loading more rules fails', () => {
    const fullPage = Array.from({ length: 500 }, (_, i) => ({ ...rules[0], id: 'r' + i, priority: i }));
    // First page full, later pages empty — so the all-rules index terminates.
    api.listRules = vi
      .fn()
      .mockImplementation((_id: string, q?: { offset?: number }) => of((q?.offset ?? 0) > 0 ? [] : fullPage));
    const f = build();
    f.detectChanges();
    api.listRules = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'more-rules-fail' } })));
    f.componentInstance.loadMoreRules();
    expect(f.componentInstance.rulesError()).toBe('more-rules-fail');
    expect(f.componentInstance.loadingRules()).toBe(false);
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

  it('re-syncs proposals when a reload finishes mid-approve (pending filter)', () => {
    const approveSubject = new Subject<typeof rules[0]>();
    api.approveProposal = vi.fn().mockReturnValue(approveSubject);
    const f = build();
    f.detectChanges(); // default filter = pending
    f.componentInstance.approve(addProposal); // optimistic remove, captures reqId
    api.listProposals.mockClear();
    f.componentInstance.loadProposals(); // concurrent reload bumps the request id
    approveSubject.next(rules[0]);
    approveSubject.complete();
    // Request id changed under the pending filter → success path re-fetches.
    expect(api.listProposals).toHaveBeenCalledTimes(2);
  });

  it('re-syncs proposals when a reload finishes mid-reject', () => {
    const rejectSubject = new Subject<RuleProposal>();
    api.rejectProposal = vi.fn().mockReturnValue(rejectSubject);
    const f = build();
    f.detectChanges();
    f.componentInstance.setProposalFilter('all');
    f.componentInstance.reject(addProposal); // captures reqId, optimistic remove
    api.listProposals.mockClear();
    f.componentInstance.loadProposals(); // concurrent reload bumps the request id
    rejectSubject.next({ ...addProposal, status: 'rejected' });
    rejectSubject.complete();
    // The success path re-fetches (manual reload + the mid-flight re-sync) rather
    // than reconciling stale state.
    expect(api.listProposals).toHaveBeenCalledTimes(2);
  });

  it('ignores a second decision while one is already in flight', () => {
    api.approveProposal = vi.fn().mockReturnValue(NEVER); // stays pending
    const f = build();
    f.detectChanges();
    f.componentInstance.approve(addProposal);
    expect(f.componentInstance.actingProposalId()).toBe('p1');
    // A concurrent reject on another proposal must be ignored.
    f.componentInstance.reject({ ...addProposal, id: 'p2' });
    expect(api.rejectProposal).not.toHaveBeenCalled();
  });

  it('clears the storage-unavailable banner once a section loads again', () => {
    const err503 = new HttpErrorResponse({ status: 503 });
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

  it('resets filters and reloads every section when switching agents', () => {
    const f = build();
    f.detectChanges();
    f.componentInstance.setProposalFilter('approved');
    f.componentInstance.setRuleFilter('retired');
    f.componentInstance.selectAgent('a2');
    expect(f.componentInstance.proposalStatusFilter()).toBe('pending');
    expect(f.componentInstance.ruleStatusFilter()).toBe('active');
    expect(api.listProposals).toHaveBeenLastCalledWith('a2', { status: 'pending', limit: 200 });
    expect(api.listMemoryEvents).toHaveBeenLastCalledWith('a2', { bySalience: true, topN: 50 });
    expect(api.listRules).toHaveBeenLastCalledWith('a2', { status: 'active', limit: 500 });
  });

  it('does not act on a proposal whose agent is no longer selected', () => {
    const f = build();
    f.detectChanges();
    // Operator opened the dialog for p1 (agent a1), then switched to a2 and confirmed.
    f.componentInstance.selectAgent('a2');
    f.componentInstance.approve(addProposal); // addProposal.agent_id === 'a1'
    expect(api.approveProposal).not.toHaveBeenCalled();
    // a2's proposal view is left untouched.
    expect(f.componentInstance.proposals()).toEqual([addProposal, staleProposal]);
  });

  it('loads more proposals when a full page is returned, then stops', () => {
    const fullPage = Array.from({ length: 200 }, (_, i) => ({ ...addProposal, id: 'p' + i }));
    api.listProposals = vi.fn().mockReturnValue(of(fullPage));
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.proposalsHasMore()).toBe(true);
    expect(c.proposals().length).toBe(200);
    // The next page is shorter → no more pages.
    api.listProposals = vi.fn().mockReturnValue(of([{ ...addProposal, id: 'p200' }]));
    c.loadMoreProposals();
    expect(api.listProposals).toHaveBeenCalledWith('a1', { status: 'pending', limit: 200, offset: 200 });
    expect(c.proposals().length).toBe(201);
    expect(c.proposalsHasMore()).toBe(false);
  });

  it('surfaces an error when loading more proposals fails', () => {
    const fullPage = Array.from({ length: 200 }, (_, i) => ({ ...addProposal, id: 'p' + i }));
    api.listProposals = vi.fn().mockReturnValue(of(fullPage));
    const f = build();
    f.detectChanges();
    api.listProposals = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'more-fail' } })));
    f.componentInstance.loadMoreProposals();
    expect(f.componentInstance.proposalsError()).toBe('more-fail');
    expect(f.componentInstance.loadingProposals()).toBe(false);
  });

  it('resolves a retired target rule via the all-rules index', () => {
    const retired = { ...rules[0], id: 'r-old', text: 'Old retired rule', status: 'retired' as const };
    // The active-rules section shows r9; the all-rules index also has the retired target.
    api.listRules = vi
      .fn()
      .mockImplementation((_id: string, q?: { status?: string }) =>
        q?.status === 'active' ? of([rules[0]]) : of([rules[0], retired]),
      );
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.rules().map((r) => r.id)).toEqual(['r9']); // section: active only
    expect(c.targetRule('r-old')?.text).toBe('Old retired rule'); // resolved via index
  });

  it('does not show the empty state when a section load errors', () => {
    api.listProposals = vi.fn().mockReturnValue(throwError(() => ({ error: { detail: 'boom' } })));
    const f = build();
    f.detectChanges();
    expect(f.componentInstance.proposalsError()).toBe('boom');
    // The error is shown, but no "Nothing to review" empty state below it.
    expect((f.nativeElement as HTMLElement).querySelectorAll('app-empty-state').length).toBe(0);
  });

  it('clears the previous agent state when switching agents', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.proposals().length).toBe(2);
    // Switching agents wipes the old data before the reload (which then refills).
    let cleared = false;
    api.listProposals = vi.fn().mockImplementation(() => {
      cleared = c.proposals().length === 0; // observed at request time
      return of([addProposal]);
    });
    c.selectAgent('a2');
    expect(cleared).toBe(true);
  });

  it('loads more rules when a full page is returned, then stops', () => {
    const fullPage = Array.from({ length: 500 }, (_, i) => ({ ...rules[0], id: 'r' + i, priority: i }));
    // First page full, later pages empty — so the all-rules index terminates.
    api.listRules = vi
      .fn()
      .mockImplementation((_id: string, q?: { offset?: number }) => of((q?.offset ?? 0) > 0 ? [] : fullPage));
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.rulesHasMore()).toBe(true);
    expect(c.rules().length).toBe(500);
    api.listRules = vi.fn().mockReturnValue(of([{ ...rules[0], id: 'r500' }]));
    c.loadMoreRules();
    expect(api.listRules).toHaveBeenCalledWith('a1', { status: 'active', limit: 500, offset: 500 });
    expect(c.rules().length).toBe(501);
    expect(c.rulesHasMore()).toBe(false);
  });

  it('clears the unavailable flag after a successful load-more', () => {
    const fullPage = Array.from({ length: 200 }, (_, i) => ({ ...addProposal, id: 'p' + i }));
    api.listProposals = vi.fn().mockReturnValue(of(fullPage));
    const f = build();
    f.detectChanges();
    f.componentInstance.proposalsUnavailable.set(true); // simulate a prior 503
    api.listProposals = vi.fn().mockReturnValue(of([{ ...addProposal, id: 'p200' }]));
    f.componentInstance.loadMoreProposals();
    expect(f.componentInstance.proposalsUnavailable()).toBe(false);
  });

  it('reveals a retired target (filter → all) before scrolling, when not in the list', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    api.listRules.mockClear();
    c.scrollToRule('r-not-loaded'); // not in the active-rules list
    expect(c.ruleStatusFilter()).toBe('all');
    expect(api.listRules).toHaveBeenCalledWith('a1', { limit: 500 });
  });

  it('pages the rules section until a scroll target on a later page appears', () => {
    vi.useFakeTimers();
    const scrollSpy = vi.fn();
    const origScroll = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollSpy;
    try {
      const target = { ...rules[0], id: 'r-deep', priority: 5, status: 'retired' as const };
      const page1 = Array.from({ length: 500 }, (_, i) => ({ ...rules[0], id: 'a' + i, priority: 1000 - i }));
      // 'all' view: first page full (no target), second page short and holds it.
      api.listRules = vi
        .fn()
        .mockImplementation((_id: string, q?: { status?: string; offset?: number }) => {
          if (q?.status === 'active') return of([rules[0]]);
          return of((q?.offset ?? 0) >= 500 ? [target] : page1);
        });
      const f = build();
      f.detectChanges();
      const c = f.componentInstance;
      c.scrollToRule('r-deep'); // not in the active section → reveal, then page through 'all'
      expect(c.ruleStatusFilter()).toBe('all');
      expect(api.listRules).toHaveBeenCalledWith('a1', { limit: 500, offset: 500 });
      expect(c.rules().some((r) => r.id === 'r-deep')).toBe(true);
      f.detectChanges(); // render the now-loaded rows so #rule-r-deep exists
      vi.advanceTimersByTime(1500); // fire the deferred scroll
      // The target was consumed and scrolled into view, not left dangling.
      expect(scrollSpy).toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollIntoView = origScroll;
      vi.useRealTimers();
    }
  });

  it('cancels a pending highlight timer on destroy without throwing', () => {
    vi.useFakeTimers();
    try {
      const f = build();
      f.detectChanges();
      f.componentInstance.scrollToRule('r9'); // r9 is loaded → schedules the highlight timer
      expect(() => f.destroy()).not.toThrow(); // teardown clears the pending timer
      vi.advanceTimersByTime(1500); // the cleared timer is now a no-op
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not scroll to a given-up target when a later load includes it', () => {
    vi.useFakeTimers();
    const scrollSpy = vi.fn();
    const origScroll = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollSpy;
    try {
      const f = build();
      f.detectChanges();
      const c = f.componentInstance;
      c.scrollToRule('r-missing'); // absent from every page → widen to 'all', still absent → give up
      // A later, unrelated load brings the id in; the given-up target must not auto-scroll.
      api.listRules = vi.fn().mockReturnValue(of([{ ...rules[0], id: 'r-missing' }]));
      c.setRuleFilter('all');
      f.detectChanges();
      vi.advanceTimersByTime(1500);
      expect(scrollSpy).not.toHaveBeenCalled();
    } finally {
      HTMLElement.prototype.scrollIntoView = origScroll;
      vi.useRealTimers();
    }
  });

  it('renders the disabled-approve a11y contract and memory toggle for a stale proposal', () => {
    api.listProposals = vi.fn().mockReturnValue(of([staleProposal]));
    api.listMemoryEvents = vi.fn().mockReturnValue(of([{ ...events[0], data: { k: 1 } }]));
    const f = build();
    f.detectChanges();
    const host = f.nativeElement as HTMLElement;
    const approve = host.querySelector('button[aria-describedby^="approve-tip-"]');
    expect(approve).not.toBeNull();
    const tipId = approve!.getAttribute('aria-describedby')!;
    expect(host.querySelector('#' + tipId)?.classList.contains('visually-hidden')).toBe(true);
    expect(host.querySelector('.proposal-card__approve-wrap[tabindex="0"]')).not.toBeNull();
    expect(host.querySelector('.memory-event__toggle[aria-expanded="false"]')).not.toBeNull();
  });

  it('builds a proposal summary from the proposed or target rule text', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(c.proposalSummary(addProposal)).toBe('Run make lint-fix');
    expect(c.proposalSummary({ ...staleProposal, proposed_rule: null })).toBe(
      'Cap writeback at 8 KB',
    );
    // No proposed text and no resolvable target → empty string (never a raw id).
    expect(c.proposalSummary({ ...addProposal, proposed_rule: null, target_rule_id: null })).toBe('');
  });

  it('pages the rule index past the first page to resolve a later target', () => {
    const page1 = Array.from({ length: 500 }, (_, i) => ({ ...rules[0], id: 'r' + i, priority: i }));
    const target = { ...rules[0], id: 'r-late', text: 'A rule on page two', status: 'retired' as const };
    // Section list (status:'active') stays small; the index (no status) pages
    // through: page one is full, page two is short and carries the target.
    api.listRules = vi
      .fn()
      .mockImplementation((_id: string, q?: { status?: string; offset?: number }) => {
        if (q?.status === 'active') return of([rules[0]]);
        return of((q?.offset ?? 0) >= 500 ? [target] : page1);
      });
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    expect(api.listRules).toHaveBeenCalledWith('a1', { limit: 500, offset: 0 });
    expect(api.listRules).toHaveBeenCalledWith('a1', { limit: 500, offset: 500 });
    expect(c.targetRule('r-late')?.text).toBe('A rule on page two');
  });

  it('shows a "no agents" empty state and fires no section loads when the catalogue is empty', () => {
    catalog.listAgents = vi.fn().mockReturnValue(of([]));
    const f = build();
    f.detectChanges();
    const host = f.nativeElement as HTMLElement;
    expect(f.componentInstance.selectedAgentId()).toBeNull();
    expect(host.textContent).toContain('No agents available');
    expect(host.querySelectorAll('.cognition-section').length).toBe(0);
    expect(api.listProposals).not.toHaveBeenCalled();
  });

  it('shows a spinner while the agent catalogue is still loading', () => {
    catalog.listAgents = vi.fn().mockReturnValue(NEVER);
    const f = build();
    f.detectChanges();
    const host = f.nativeElement as HTMLElement;
    expect(f.componentInstance.loadingAgents()).toBe(true);
    expect(host.querySelector('app-loading-spinner')).not.toBeNull();
    expect(host.querySelectorAll('.cognition-section').length).toBe(0);
  });

  it('prompts to select an agent when agents exist but none is selected', () => {
    const f = build();
    f.detectChanges();
    const c = f.componentInstance;
    c.selectedAgentId.set(null); // de-select while the catalogue still has agents
    f.detectChanges();
    const host = f.nativeElement as HTMLElement;
    expect(host.textContent).toContain('Select an agent');
    expect(host.querySelectorAll('.cognition-section').length).toBe(0);
  });

  it('wraps every decorative chip glyph in an aria-hidden span (label-only for screen readers)', () => {
    // Cover all three glyph-bearing chips: the proposal "evidence outdated" warn
    // chip, the rule-mode chip, and the rule "needs review" warn chip.
    api.listProposals = vi.fn().mockReturnValue(of([staleProposal]));
    api.listRules = vi
      .fn()
      .mockReturnValue(of([{ ...rules[0], mode: 'enforced' as const, needs_review: true }]));
    const f = build();
    f.detectChanges();
    const host = f.nativeElement as HTMLElement;

    const decoratedChips = host.querySelectorAll('.proposal-card .is-warn, .is-mode, .rule-row .is-warn');
    expect(decoratedChips.length).toBe(3);
    for (const chip of Array.from(decoratedChips)) {
      const icon = chip.querySelector('span[aria-hidden="true"]');
      // The glyph lives in an aria-hidden span...
      expect(icon).not.toBeNull();
      expect(icon!.textContent?.trim()).toMatch(/[⚠⚖💬]/u);
      // ...and is the chip's ONLY emoji: the text exposed to AT is just the label.
      const labelText = chip.textContent?.replace(icon!.textContent ?? '', '') ?? '';
      expect(labelText).not.toMatch(/[⚠⚖💬]/u);
    }
  });
});
