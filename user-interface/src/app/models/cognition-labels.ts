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

/**
 * Decorative glyph paired with each event-kind label so the kind is conveyed by
 * shape + text, not colour alone (WCAG 1.4.1), matching the icon+text treatment
 * of the action / mode / warn chips.
 */
const EVENT_KIND_ICONS: Record<EventKind, string> = {
  observation: '👁',
  action: '⚡',
  tool_call: '🔧',
  outcome: '✅',
  error: '⛔',
  feedback: '💬',
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
  enforced: 'enforced: blocks the agent',
  advisory: 'advisory: guidance only',
};

/** `tool_call` → `tool call`; never the raw snake_case enum. */
export function eventKindLabel(kind: EventKind): string {
  return EVENT_KIND_LABELS[kind] ?? kind;
}

/** Decorative glyph for an event kind (paired with `eventKindLabel`); `•` for unknown kinds. */
export function eventKindIcon(kind: EventKind): string {
  return EVENT_KIND_ICONS[kind] ?? '•';
}

/** Memory `salience` surfaced as `relevance 0.82`; non-finite → `relevance N/A`. */
export function relevanceLabel(salience: number): string {
  if (!Number.isFinite(salience)) return 'relevance N/A';
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

/** Spelled out, not `prio`, and rounded to an integer: `priority 90`; non-finite → `priority N/A`. */
export function rulePriorityLabel(priority: number): string {
  if (!Number.isFinite(priority)) return 'priority N/A';
  return `priority ${Math.round(priority)}`;
}

/** Tooltip text distinguishing enforced vs advisory rules. */
export function ruleModeTooltip(mode: RuleMode): string {
  return RULE_MODE_TOOLTIPS[mode] ?? mode;
}
