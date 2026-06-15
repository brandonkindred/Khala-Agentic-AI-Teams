/**
 * Models for the Agent Console Cognition panel — mirror of
 * `backend/agents/agent_cognition/models.py`.
 *
 * Field names match the backend JSON exactly so responses deserialize 1:1.
 * Enum-valued fields render through `cognition-labels.ts`, never as raw values.
 */

export type EventKind =
  | 'observation'
  | 'action'
  | 'tool_call'
  | 'outcome'
  | 'error'
  | 'feedback';

export type Scale = 'day' | 'week' | 'month' | 'year';
export type RuleMode = 'advisory' | 'enforced';
export type RuleStatus = 'active' | 'retired';
export type RuleSource = 'seed' | 'derived' | 'operator';
export type ProposalAction = 'add' | 'amend' | 'retire';
export type ProposalStatus = 'pending' | 'approved' | 'rejected' | 'superseded';

/** A single episodic memory event. */
export interface MemoryEvent {
  id: string;
  agent_id: string;
  kind: EventKind;
  content: string;
  data: Record<string, unknown>;
  salience: number;
  occurred_at: string;
  source_run_id: string;
  source_seq: number;
}

/** A calendar rollup summary at a given scale. */
export interface PeriodSummary {
  id: string;
  agent_id: string;
  scale: Scale;
  period_start: string;
  period_end: string;
  summary: string;
  highlights: unknown[];
  source_count: number;
  covers_through?: string | null;
  version: number;
  stale: boolean;
  events_pruned: boolean;
  created_at: string;
}

/** An active or retired rule the agent operates under. */
export interface Rule {
  id: string;
  agent_id: string;
  text: string;
  mode: RuleMode;
  status: RuleStatus;
  predicate?: Record<string, unknown> | null;
  rationale?: string | null;
  source: RuleSource;
  evidence: unknown[];
  needs_review: boolean;
  priority: number;
  created_at: string;
  updated_at: string;
}

/** The rule body carried by an add/amend proposal (`proposed_rule`). */
export interface ProposedRule {
  text: string;
  mode: RuleMode;
  predicate?: Record<string, unknown> | null;
  rationale?: string | null;
  priority?: number;
}

/**
 * A proposed rule change awaiting human review.
 *
 * Invariants (action coherence): `add` ⇒ `proposed_rule` set; `retire` ⇒
 * `target_rule_id` set; `amend` ⇒ both set.
 */
export interface RuleProposal {
  id: string;
  agent_id: string;
  action: ProposalAction;
  target_rule_id?: string | null;
  proposed_rule?: ProposedRule | null;
  evidence: unknown[];
  stale_evidence: boolean;
  status: ProposalStatus;
  decided_by?: string | null;
  decided_at?: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// Query option bags (mapped to query params by `CognitionApiService`)
// ---------------------------------------------------------------------------

/** Query options for listing an agent's memory events. */
export interface MemoryEventsQuery {
  topN?: number;
  bySalience?: boolean;
  since?: string;
}

/** Query options for listing an agent's period summaries. */
export interface SummariesQuery {
  limit?: number;
  offset?: number;
  excludeStale?: boolean;
}

/** Query options for listing an agent's rule proposals. */
export interface ProposalsQuery {
  status?: ProposalStatus;
  limit?: number;
  offset?: number;
}

/** Query options for listing an agent's rules. */
export interface RulesQuery {
  status?: RuleStatus;
  limit?: number;
  offset?: number;
}
