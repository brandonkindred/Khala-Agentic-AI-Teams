import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
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
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatSelectModule } from '@angular/material/select';
import { MatTooltipModule } from '@angular/material/tooltip';
import { AgentCatalogApiService } from '../../../services/agent-catalog-api.service';
import { CognitionApiService } from '../../../services/cognition-api.service';
import {
  ConfirmDialogComponent,
  ConfirmDialogData,
} from '../../../shared/confirm-dialog/confirm-dialog.component';
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

/**
 * Cognition tab — operator surface for an agent's learned cognition.
 *
 * Sections, in page order: **Rule proposals** (the HITL approve/reject gate),
 * **Memory** timeline, and **Rules** list. The agent picker reuses the Agent
 * Console catalogue (`/api/agents`); everything is scoped to the chosen agent.
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
    MatProgressSpinnerModule,
    MatSelectModule,
    MatTooltipModule,
  ],
  templateUrl: './cognition-tab.component.html',
  styleUrl: './cognition-tab.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CognitionTabComponent implements OnInit {
  private readonly catalog = inject(AgentCatalogApiService);
  private readonly api = inject(CognitionApiService);
  private readonly dialog = inject(MatDialog);

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
  /** Proposal id currently being approved/rejected (disables its buttons). */
  readonly actingProposalId = signal<string | null>(null);

  // Memory --------------------------------------------------------------------
  readonly events = signal<MemoryEvent[]>([]);
  readonly memoryBySalience = signal<boolean>(true);
  readonly memoryTopN = signal<number>(50);
  readonly loadingEvents = signal<boolean>(false);
  readonly memoryError = signal<string | null>(null);

  // Rules ---------------------------------------------------------------------
  readonly rules = signal<Rule[]>([]);
  readonly ruleStatusFilter = signal<RuleFilter>('active');
  readonly loadingRules = signal<boolean>(false);
  readonly rulesError = signal<string | null>(null);

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

  selectAgent(agentId: string): void {
    this.selectedAgentId.set(agentId);
    this.refreshAll();
  }

  refreshAll(): void {
    if (!this.selectedAgentId()) return;
    this.loadProposals();
    this.loadMemory();
    this.loadRules();
  }

  // Proposals -----------------------------------------------------------------

  loadProposals(): void {
    const agentId = this.selectedAgentId();
    if (!agentId) return;
    this.loadingProposals.set(true);
    this.proposalsError.set(null);
    const filter = this.proposalStatusFilter();
    this.api
      .listProposals(agentId, filter === 'all' ? {} : { status: filter })
      .subscribe({
        next: (rows) => {
          this.proposals.set(rows);
          this.loadingProposals.set(false);
        },
        error: (err) => {
          this.proposals.set([]);
          this.proposalsError.set(this.extractError(err, 'Failed to load proposals.'));
          this.loadingProposals.set(false);
        },
      });
  }

  setProposalFilter(filter: ProposalFilter): void {
    this.proposalStatusFilter.set(filter);
    this.loadProposals();
  }

  /** Open a confirm dialog, then approve on confirm. No-op if evidence is outdated. */
  approve(p: RuleProposal): void {
    if (p.stale_evidence) return;
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

  /** Public for tests. Optimistic remove + rollback; refetch rules on success. */
  performApprove(p: RuleProposal): void {
    const agentId = this.selectedAgentId();
    if (!agentId || p.stale_evidence) return;
    this.actingProposalId.set(p.id);
    this.proposalsError.set(null);
    const prev = this.proposals();
    this.proposals.set(prev.filter((x) => x.id !== p.id));
    this.api.approveProposal(agentId, p.id).subscribe({
      next: () => {
        this.actingProposalId.set(null);
        this.loadRules();
      },
      error: (err) => {
        this.proposals.set(prev);
        this.proposalsError.set(this.extractError(err, 'Failed to approve proposal.'));
        this.actingProposalId.set(null);
      },
    });
  }

  reject(p: RuleProposal): void {
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

  /** Public for tests. Optimistic remove + rollback on error. */
  performReject(p: RuleProposal): void {
    const agentId = this.selectedAgentId();
    if (!agentId) return;
    this.actingProposalId.set(p.id);
    this.proposalsError.set(null);
    const prev = this.proposals();
    this.proposals.set(prev.filter((x) => x.id !== p.id));
    this.api.rejectProposal(agentId, p.id).subscribe({
      next: () => {
        this.actingProposalId.set(null);
      },
      error: (err) => {
        this.proposals.set(prev);
        this.proposalsError.set(this.extractError(err, 'Failed to reject proposal.'));
        this.actingProposalId.set(null);
      },
    });
  }

  // Proposal display helpers --------------------------------------------------

  evidenceCount(p: RuleProposal): number {
    return p.evidence?.length ?? 0;
  }

  proposedRuleText(p: RuleProposal): string {
    return p.proposed_rule?.text ?? '';
  }

  proposedRuleMode(p: RuleProposal): RuleMode {
    return p.proposed_rule?.mode ?? 'advisory';
  }

  /** Resolve a target rule's text from the loaded rules; fall back to its id. */
  targetRuleText(id: string | null | undefined): string {
    if (!id) return '';
    const rule = this.rules().find((r) => r.id === id);
    return rule ? rule.text : id;
  }

  // Memory --------------------------------------------------------------------

  loadMemory(): void {
    const agentId = this.selectedAgentId();
    if (!agentId) return;
    this.loadingEvents.set(true);
    this.memoryError.set(null);
    this.api
      .listMemoryEvents(agentId, { bySalience: this.memoryBySalience(), topN: this.memoryTopN() })
      .subscribe({
        next: (rows) => {
          this.events.set(rows);
          this.loadingEvents.set(false);
        },
        error: (err) => {
          this.events.set([]);
          this.memoryError.set(this.extractError(err, 'Failed to load memory.'));
          this.loadingEvents.set(false);
        },
      });
  }

  setMemoryOrder(bySalience: boolean): void {
    this.memoryBySalience.set(bySalience);
    this.loadMemory();
  }

  setMemoryTopN(topN: number): void {
    this.memoryTopN.set(topN);
    this.loadMemory();
  }

  // Rules ---------------------------------------------------------------------

  loadRules(): void {
    const agentId = this.selectedAgentId();
    if (!agentId) return;
    this.loadingRules.set(true);
    this.rulesError.set(null);
    const filter = this.ruleStatusFilter();
    this.api.listRules(agentId, filter === 'all' ? {} : { status: filter }).subscribe({
      next: (rows) => {
        this.rules.set(rows);
        this.loadingRules.set(false);
      },
      error: (err) => {
        this.rules.set([]);
        this.rulesError.set(this.extractError(err, 'Failed to load rules.'));
        this.loadingRules.set(false);
      },
    });
  }

  setRuleFilter(filter: RuleFilter): void {
    this.ruleStatusFilter.set(filter);
    this.loadRules();
  }

  // ---------------------------------------------------------------------------

  private extractError(err: unknown, fallback: string): string {
    const e = err as { error?: { detail?: string }; message?: string } | undefined;
    return e?.error?.detail ?? e?.message ?? fallback;
  }
}
