/**
 * Backend-value → operator-facing copy for the Cognition panel.
 *
 * Single source of truth for the copy glossary in `DESIGN.md §8`. The UI must
 * never render raw enums, ids, or internal terms; every chip/label routes
 * through here. All chip labels are lowercase by convention.
 *
 * These are pure functions (no I/O). Preconditions: each takes a valid member
 * of its backend enum; an unknown value falls back to the raw input rather than
 * throwing, so a backend that adds a value degrades gracefully.
 */

import type {
  EventKind,
  ProposalAction,
  RuleMode,
  RuleSource,
} from './cognition.model';

/** The single shared term for stale proposal evidence / rules needing review. */
export const EVIDENCE_OUTDATED = 'evidence outdated';

const EVENT_KIND_LABELS: Record<EventKind, string> = {
  observation: 'observation',
  action: 'action',
  tool_call: 'tool call',
  outcome: 'outcome',
  error: 'error',
  feedback: 'feedback',
};

const PROPOSAL_ACTION_LABELS: Record<ProposalAction, string> = {
  add: 'new rule',
  amend: 'amend rule',
  retire: 'retire rule',
};

const RULE_SOURCE_LABELS: Record<RuleSource, string> = {
  seed: 'built-in',
  derived: 'learned',
  operator: 'added by you',
};

const RULE_MODE_TOOLTIPS: Record<RuleMode, string> = {
  enforced: 'Enforced: blocks the agent',
  advisory: 'Advisory: guidance only',
};

/** `tool_call` → `tool call`; never the raw snake_case enum. */
export function eventKindLabel(kind: EventKind): string {
  return EVENT_KIND_LABELS[kind] ?? kind;
}

/** Memory `salience` surfaced as `relevance 0.82`. */
export function relevanceLabel(salience: number): string {
  return `relevance ${salience.toFixed(2)}`;
}

/** Memory order control: `by_salience` → `Most relevant` / `Most recent`. */
export function memoryOrderLabel(bySalience: boolean): string {
  return bySalience ? 'Most relevant' : 'Most recent';
}

/** `add|amend|retire` → `new rule` / `amend rule` / `retire rule`. */
export function proposalActionLabel(action: ProposalAction): string {
  return PROPOSAL_ACTION_LABELS[action] ?? action;
}

/** `seed|derived|operator` → `built-in` / `learned` / `added by you`. */
export function ruleSourceLabel(source: RuleSource): string {
  return RULE_SOURCE_LABELS[source] ?? source;
}

/** Spelled out, not `prio`: `priority 90`. */
export function rulePriorityLabel(priority: number): string {
  return `priority ${priority}`;
}

/** Tooltip text distinguishing enforced vs advisory rules. */
export function ruleModeTooltip(mode: RuleMode): string {
  return RULE_MODE_TOOLTIPS[mode] ?? mode;
}
