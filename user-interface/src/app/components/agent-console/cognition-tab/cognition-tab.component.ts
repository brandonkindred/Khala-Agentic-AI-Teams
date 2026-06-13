import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  WritableSignal,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatDialog, MatDialogModule } from '@angular/material/dialog';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { AgentCatalogApiService } from '../../../services/agent-catalog-api.service';
import { CognitionApiService } from '../../../services/cognition-api.service';
import {
  ConfirmDialogComponent,
  ConfirmDialogData,
} from '../../../shared/confirm-dialog/confirm-dialog.component';
import { LoadingSpinnerComponent } from '../../../shared/loading-spinner/loading-spinner.component';
import { EmptyStateComponent } from '../../../shared/empty-state/empty-state.component';
import { ErrorMessageComponent } from '../../../shared/error-message/error-message.component';
import {
  EVIDENCE_OUTDATED,
  eventKindLabel,
  memoryOrderLabel,
  proposalActionLabel,
  relevanceLabel,
  rulePriorityLabel,
  ruleModeTooltip,
  ruleSourceLabel,
} from '../../../models/cognition-labels';
import type { AgentSummary } from '../../../models/agent-catalog.model';
import type {
  MemoryEvent,
  ProposalStatus,
  Rule,
  RuleMode,
  RuleProposal,
  RuleStatus,
} from '../../../models/cognition.model';

type ProposalFilter = ProposalStatus | 'all';
type RuleFilter = RuleStatus | 'all';

/** Panel-wide message shown when cognition storage is unavailable (503). */
const STORAGE_UNAVAILABLE_MESSAGE = 'Cognition data is temporarily unavailable. Try again shortly.';

/**
 * Page sizes requested per section. Set to the backend maxima so a single
 * fetch covers all but the largest agents; a load-more control is a follow-up
 * for agents that exceed these caps.
 */
const PROPOSAL_PAGE_LIMIT = 200;
const RULE_PAGE_LIMIT = 500;

/**
 * Cognition tab — operator surface for an agent's learned cognition.
 *
 * Sections, in page order: **Rule proposals** (the HITL approve/reject gate),
 * **Memory** timeline, and **Rules** list. The agent picker reuses the Agent
 * Console catalogue (`/api/agents`); everything is scoped to the chosen agent.
 *
 * Invariants: at most one approve/reject is in flight (`actingProposalId`);
 * each section ignores responses from superseded loads (per-section request
 * ids); the panel-wide "unavailable" banner shows only when every section is 503.
 */
@Component({
  selector: 'app-cognition-tab',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatButtonModule,
    MatChipsModule,
    MatDialogModule,
    MatFormFieldModule,
    MatIconModule,
    MatSelectModule,
    MatTooltipModule,
    LoadingSpinnerComponent,
    EmptyStateComponent,
    ErrorMessageComponent,
  ],
  templateUrl: './cognition-tab.component.html',
  styleUrl: './cognition-tab.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CognitionTabComponent implements OnInit {
  private readonly catalog = inject(AgentCatalogApiService);
  private readonly api = inject(CognitionApiService);
  private readonly dialog = inject(MatDialog);

  readonly STORAGE_UNAVAILABLE_MESSAGE = STORAGE_UNAVAILABLE_MESSAGE;

  // Agent picker --------------------------------------------------------------
  readonly agents = signal<AgentSummary[]>([]);
  readonly selectedAgentId = signal<string | null>(null);
  readonly loadingAgents = signal<boolean>(false);
  readonly agentsError = signal<string | null>(null);

  // Proposals -----------------------------------------------------------------
  readonly proposals = signal<RuleProposal[]>([]);
  readonly proposalStatusFilter = signal<ProposalFilter>('pending');
  readonly loadingProposals = signal<boolean>(false);
  readonly proposalsError = signal<string | null>(null);
  readonly proposalsUnavailable = signal<boolean>(false);
  /** Proposal id currently being approved/rejected (one decision at a time). */
  readonly actingProposalId = signal<string | null>(null);

  // Memory --------------------------------------------------------------------
  readonly events = signal<MemoryEvent[]>([]);
  readonly memoryBySalience = signal<boolean>(true);
  readonly memoryTopN = signal<number>(50);
  readonly loadingEvents = signal<boolean>(false);
  readonly memoryError = signal<string | null>(null);
  readonly memoryUnavailable = signal<boolean>(false);
  /** Memory event ids whose `data` payload is expanded. */
  readonly expandedEventIds = signal<Set<string>>(new Set());

  // Rules ---------------------------------------------------------------------
  readonly rules = signal<Rule[]>([]);
  readonly ruleStatusFilter = signal<RuleFilter>('active');
  readonly loadingRules = signal<boolean>(false);
  readonly rulesError = signal<string | null>(null);
  readonly rulesUnavailable = signal<boolean>(false);
  /** Rule id briefly highlighted after a retire-proposal link click. */
  readonly highlightedRuleId = signal<string | null>(null);

  /** Storage is "down" (panel-wide banner) only when every section failed with 503. */
  readonly storageUnavailable = computed(
    () => this.proposalsUnavailable() && this.memoryUnavailable() && this.rulesUnavailable(),
  );

  // Per-section request ids: a load ignores its response if a newer load began.
  private proposalsReqId = 0;
  private memoryReqId = 0;
  private rulesReqId = 0;

  // Filter option lists (lowercase labels per the copy spec) ------------------
  readonly proposalFilters: readonly { value: ProposalFilter; label: string }[] = [
    { value: 'pending', label: 'pending' },
    { value: 'approved', label: 'approved' },
    { value: 'rejected', label: 'rejected' },
    { value: 'superseded', label: 'superseded' },
    { value: 'all', label: 'all' },
  ];
  readonly ruleFilters: readonly { value: RuleFilter; label: string }[] = [
    { value: 'active', label: 'active' },
    { value: 'retired', label: 'retired' },
    { value: 'all', label: 'all' },
  ];
  readonly memoryOrders: readonly { value: boolean; label: string }[] = [
    { value: true, label: memoryOrderLabel(true) },
    { value: false, label: memoryOrderLabel(false) },
  ];
  readonly topNOptions: readonly number[] = [25, 50, 100];

  // Copy helpers exposed to the template --------------------------------------
  readonly EVIDENCE_OUTDATED = EVIDENCE_OUTDATED;
  readonly proposalActionLabel = proposalActionLabel;
  readonly eventKindLabel = eventKindLabel;
  readonly relevanceLabel = relevanceLabel;
  readonly ruleSourceLabel = ruleSourceLabel;
  readonly rulePriorityLabel = rulePriorityLabel;
  readonly ruleModeTooltip = ruleModeTooltip;

  ngOnInit(): void {
    this.loadAgents();
  }

  // Agent picker --------------------------------------------------------------

  /** Load the agent catalogue and auto-select the first agent. */
  loadAgents(): void {
    this.loadingAgents.set(true);
    this.agentsError.set(null);
    this.catalog.listAgents().subscribe({
      next: (rows) => {
        this.agents.set(rows);
        this.loadingAgents.set(false);
        if (rows.length && this.selectedAgentId() === null) {
          this.selectAgent(rows[0].id);
        }
      },
      error: (err) => {
        this.agentsError.set(this.extractError(err, 'Failed to load agents.'));
        this.loadingAgents.set(false);
      },
    });
  }

  /** Scope the panel to `agentId`, reset filters to defaults, and reload all sections. */
  selectAgent(agentId: string): void {
    this.selectedAgentId.set(agentId);
    // Reset filters so each agent starts from the default view.
    this.proposalStatusFilter.set('pending');
    this.ruleStatusFilter.set('active');
    this.refreshAll();
  }

  /** Reload all three sections for the current agent. No-op when none selected. */
  refreshAll(): void {
    if (!this.selectedAgentId()) return;
    this.loadProposals();
    this.loadMemory();
    this.loadRules();
  }

  // Proposals -----------------------------------------------------------------

  /** Load proposals for the current agent + status filter. */
  loadProposals(): void {
    const agentId = this.selectedAgentId();
    if (!agentId) return;
    this.loadingProposals.set(true);
    this.proposalsError.set(null);
    const reqId = ++this.proposalsReqId;
    const filter = this.proposalStatusFilter();
    const query = filter === 'all' ? {} : { status: filter };
    this.api
      .listProposals(agentId, { ...query, limit: PROPOSAL_PAGE_LIMIT })
      .subscribe({
        next: (rows) => {
          if (reqId !== this.proposalsReqId) return;
          this.proposalsUnavailable.set(false);
          this.proposals.set(rows);
          this.loadingProposals.set(false);
        },
        error: (err) => {
          if (reqId !== this.proposalsReqId) return;
          this.proposals.set([]);
          this.applyError(err, this.proposalsError, this.proposalsUnavailable, 'Failed to load proposals.');
          this.loadingProposals.set(false);
        },
      });
  }

  /** Change the proposal status filter and reload. */
  setProposalFilter(filter: ProposalFilter): void {
    this.proposalStatusFilter.set(filter);
    this.loadProposals();
  }

  /** Confirm, then approve. No-op if evidence is outdated or a decision is in flight. */
  approve(p: RuleProposal): void {
    if (p.stale_evidence || this.actingProposalId()) return;
    const data: ConfirmDialogData = {
      title: 'Approve proposal',
      message: `Approve this ${proposalActionLabel(p.action)}? This updates the agent's rules.`,
      confirmLabel: 'Approve',
    };
    this.dialog
      .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(ConfirmDialogComponent, { data })
      .afterClosed()
      .subscribe((ok) => {
        if (ok) this.performApprove(p);
      });
  }

  /**
   * Optimistic remove + rollback; on success refetch rules so the activated
   * rule appears. The approve endpoint returns only a `Rule` (no
   * `decided_by`/`decided_at`), so when the decided card stays visible
   * (`all`/`approved`) or a load superseded us, re-fetch proposals for the
   * server's audit fields rather than fabricating them; under other filters the
   * optimistic removal already reflects reality.
   *
   * @param p The pending proposal to approve.
   */
  private performApprove(p: RuleProposal): void {
    // Bind the decision to the proposal's own agent, not the (possibly changed)
    // selected agent, so switching agents mid-confirm can't misroute it.
    const agentId = p.agent_id;
    if (!agentId || p.stale_evidence || this.actingProposalId()) return;
    this.actingProposalId.set(p.id);
    this.proposalsError.set(null);
    const onCurrentAgent = agentId === this.selectedAgentId();
    const prev = this.proposals();
    const reqId = this.proposalsReqId;
    if (onCurrentAgent) this.proposals.set(prev.filter((x) => x.id !== p.id));
    this.api.approveProposal(agentId, p.id).subscribe({
      next: () => {
        this.actingProposalId.set(null);
        if (agentId !== this.selectedAgentId()) return; // agent changed — leave its view alone
        this.loadRules();
        const filter = this.proposalStatusFilter();
        if (filter === 'all' || filter === 'approved' || reqId !== this.proposalsReqId) {
          this.loadProposals(); // re-sync for server audit fields / superseding load
        }
      },
      error: (err) => {
        this.actingProposalId.set(null);
        if (agentId !== this.selectedAgentId()) return; // don't surface a stale agent's error
        if (reqId === this.proposalsReqId && onCurrentAgent) this.proposals.set(prev);
        this.applyError(err, this.proposalsError, this.proposalsUnavailable, 'Failed to approve proposal.');
      },
    });
  }

  /** Confirm, then reject. No-op if a decision is in flight. */
  reject(p: RuleProposal): void {
    if (this.actingProposalId()) return;
    const data: ConfirmDialogData = {
      title: 'Reject proposal',
      message: `Reject this ${proposalActionLabel(p.action)}? The agent's rules are unchanged.`,
      confirmLabel: 'Reject',
      variant: 'danger',
    };
    this.dialog
      .open<ConfirmDialogComponent, ConfirmDialogData, boolean>(ConfirmDialogComponent, { data })
      .afterClosed()
      .subscribe((ok) => {
        if (ok) this.performReject(p);
      });
  }

  /**
   * Optimistic remove + rollback; on success keep the now-rejected card (from
   * the server response, which carries `decided_by`/`decided_at`) visible when
   * the current filter still includes it. Re-fetches if a load superseded us.
   *
   * @param p The pending proposal to reject.
   */
  private performReject(p: RuleProposal): void {
    const agentId = p.agent_id;
    if (!agentId || this.actingProposalId()) return;
    this.actingProposalId.set(p.id);
    this.proposalsError.set(null);
    const onCurrentAgent = agentId === this.selectedAgentId();
    const prev = this.proposals();
    const reqId = this.proposalsReqId;
    if (onCurrentAgent) this.proposals.set(prev.filter((x) => x.id !== p.id));
    this.api.rejectProposal(agentId, p.id).subscribe({
      next: (updated) => {
        this.actingProposalId.set(null);
        if (agentId !== this.selectedAgentId()) return;
        if (reqId !== this.proposalsReqId) {
          this.loadProposals();
          return;
        }
        this.reconcileDecided(prev, updated);
      },
      error: (err) => {
        this.actingProposalId.set(null);
        if (agentId !== this.selectedAgentId()) return;
        if (reqId === this.proposalsReqId && onCurrentAgent) this.proposals.set(prev);
        this.applyError(err, this.proposalsError, this.proposalsUnavailable, 'Failed to reject proposal.');
      },
    });
  }

  /**
   * After a decision a proposal moves out of `pending`. If the active filter
   * still includes its new status (`all` or the matching status) re-show the
   * updated card in place; otherwise it stays removed.
   */
  private reconcileDecided(prevList: RuleProposal[], updated: RuleProposal): void {
    const filter = this.proposalStatusFilter();
    if (filter === 'all' || filter === updated.status) {
      this.proposals.set(prevList.map((x) => (x.id === updated.id ? updated : x)));
    }
  }

  // Proposal display helpers --------------------------------------------------

  /**
   * @param p A rule proposal.
   * @returns The count of evidence refs backing it (`0` when absent).
   */
  evidenceCount(p: RuleProposal): number {
    return p.evidence?.length ?? 0;
  }

  /**
   * @param p A rule proposal.
   * @returns The proposed rule's text, or `''` when absent.
   */
  proposedRuleText(p: RuleProposal): string {
    return p.proposed_rule?.text ?? '';
  }

  /**
   * @param p A rule proposal.
   * @returns The proposed rule's mode, defaulting to `advisory`.
   */
  proposedRuleMode(p: RuleProposal): RuleMode {
    return p.proposed_rule?.mode ?? 'advisory';
  }

  /**
   * @param p A rule proposal.
   * @returns A short human summary (proposed-rule text, else target-rule text) for aria-labels.
   */
  proposalSummary(p: RuleProposal): string {
    return this.proposedRuleText(p) || this.targetRuleText(p.target_rule_id);
  }

  /**
   * @param id A target rule id (may be null/undefined).
   * @returns The matching loaded `Rule`, or `undefined` if not present.
   */
  targetRule(id: string | null | undefined): Rule | undefined {
    if (!id) return undefined;
    return this.rules().find((r) => r.id === id);
  }

  /**
   * Never returns the raw id, which would leak an internal identifier (e.g. for
   * a retired target absent from the active-rules view).
   *
   * @param id A target rule id (may be null/undefined).
   * @returns The target rule's text when loaded; `''` otherwise.
   */
  targetRuleText(id: string | null | undefined): string {
    return this.targetRule(id)?.text ?? '';
  }

  /** Scroll the Rules section to a target rule and briefly highlight it. */
  scrollToRule(id: string | null | undefined): void {
    if (!id) return;
    this.highlightedRuleId.set(id);
    // Direct DOM read for scroll-into-view only; the highlight itself is a
    // signal-bound class. Guarded for jsdom (no scrollIntoView in tests).
    document.getElementById('rule-' + id)?.scrollIntoView?.({ behavior: 'smooth', block: 'center' });
    setTimeout(() => {
      if (this.highlightedRuleId() === id) this.highlightedRuleId.set(null);
    }, 1500);
  }

  // Memory --------------------------------------------------------------------

  /** Load memory events for the current agent + order/count controls. */
  loadMemory(): void {
    const agentId = this.selectedAgentId();
    if (!agentId) return;
    this.loadingEvents.set(true);
    this.memoryError.set(null);
    const reqId = ++this.memoryReqId;
    this.api
      .listMemoryEvents(agentId, { bySalience: this.memoryBySalience(), topN: this.memoryTopN() })
      .subscribe({
        next: (rows) => {
          if (reqId !== this.memoryReqId) return;
          this.memoryUnavailable.set(false);
          this.events.set(rows);
          this.loadingEvents.set(false);
        },
        error: (err) => {
          if (reqId !== this.memoryReqId) return;
          this.events.set([]);
          this.applyError(err, this.memoryError, this.memoryUnavailable, 'Failed to load memory.');
          this.loadingEvents.set(false);
        },
      });
  }

  /** Toggle relevance vs recency ordering and reload memory. */
  setMemoryOrder(bySalience: boolean): void {
    this.memoryBySalience.set(bySalience);
    this.loadMemory();
  }

  /** Change the `top_n` count and reload memory. */
  setMemoryTopN(topN: number): void {
    this.memoryTopN.set(topN);
    this.loadMemory();
  }

  /** Expand/collapse a memory event's `data` payload. */
  toggleEventData(id: string): void {
    const next = new Set(this.expandedEventIds());
    if (next.has(id)) next.delete(id);
    else next.add(id);
    this.expandedEventIds.set(next);
  }

  /** Whether a memory event's `data` panel is expanded. */
  isEventExpanded(id: string): boolean {
    return this.expandedEventIds().has(id);
  }

  /**
   * @param e A memory event.
   * @returns Whether it carries a non-empty `data` payload.
   */
  hasData(e: MemoryEvent): boolean {
    return !!e.data && Object.keys(e.data).length > 0;
  }

  /**
   * @param e A memory event.
   * @returns Its `data` payload pretty-printed as JSON.
   */
  formatData(e: MemoryEvent): string {
    return JSON.stringify(e.data, null, 2);
  }

  // Rules ---------------------------------------------------------------------

  /** Load rules for the current agent + status filter, highest priority first. */
  loadRules(): void {
    const agentId = this.selectedAgentId();
    if (!agentId) return;
    this.loadingRules.set(true);
    this.rulesError.set(null);
    const reqId = ++this.rulesReqId;
    const filter = this.ruleStatusFilter();
    const query = filter === 'all' ? {} : { status: filter };
    this.api.listRules(agentId, { ...query, limit: RULE_PAGE_LIMIT }).subscribe({
      next: (rows) => {
        if (reqId !== this.rulesReqId) return;
        this.rulesUnavailable.set(false);
        // Defensive: ensure highest priority first regardless of API ordering.
        this.rules.set([...rows].sort((a, b) => b.priority - a.priority));
        this.loadingRules.set(false);
      },
      error: (err) => {
        if (reqId !== this.rulesReqId) return;
        this.rules.set([]);
        this.applyError(err, this.rulesError, this.rulesUnavailable, 'Failed to load rules.');
        this.loadingRules.set(false);
      },
    });
  }

  /** Change the rule status filter and reload. */
  setRuleFilter(filter: RuleFilter): void {
    this.ruleStatusFilter.set(filter);
    this.loadRules();
  }

  // ---------------------------------------------------------------------------

  /**
   * Route a load error: `503` marks the section unavailable (and contributes to
   * the panel-wide banner once every section is down); any other error sets the
   * section's message.
   */
  private applyError(
    err: unknown,
    errSig: WritableSignal<string | null>,
    unavailSig: WritableSignal<boolean>,
    fallback: string,
  ): void {
    if ((err as { status?: number })?.status === 503) {
      unavailSig.set(true);
      errSig.set(STORAGE_UNAVAILABLE_MESSAGE);
      return;
    }
    unavailSig.set(false);
    errSig.set(this.extractError(err, fallback));
  }

  private extractError(err: unknown, fallback: string): string {
    const e = err as { error?: { detail?: string }; message?: string } | undefined;
    return e?.error?.detail ?? e?.message ?? fallback;
  }
}
