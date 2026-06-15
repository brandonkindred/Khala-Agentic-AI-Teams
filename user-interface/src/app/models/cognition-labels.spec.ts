import {
  EVIDENCE_OUTDATED,
  eventKindLabel,
  eventKindIcon,
  memoryOrderLabel,
  proposalActionLabel,
  relevanceLabel,
  rulePriorityLabel,
  ruleModeTooltip,
  ruleSourceLabel,
} from './cognition-labels';
import type {
  EventKind,
  ProposalAction,
  RuleMode,
  RuleSource,
} from './cognition.model';

describe('cognition-labels', () => {
  it('maps every event kind to a lowercase, space-separated label', () => {
    const expected: Record<EventKind, string> = {
      observation: 'observation',
      action: 'action',
      tool_call: 'tool call',
      outcome: 'outcome',
      error: 'error',
      feedback: 'feedback',
    };
    for (const [kind, label] of Object.entries(expected)) {
      expect(eventKindLabel(kind as EventKind)).toBe(label);
    }
  });

  it('maps every proposal action to a plain-language label', () => {
    const expected: Record<ProposalAction, string> = {
      add: 'new rule',
      amend: 'amend rule',
      retire: 'retire rule',
    };
    for (const [action, label] of Object.entries(expected)) {
      expect(proposalActionLabel(action as ProposalAction)).toBe(label);
    }
  });

  it('maps every rule source to a friendly label', () => {
    const expected: Record<RuleSource, string> = {
      seed: 'built-in',
      derived: 'learned',
      operator: 'added by you',
    };
    for (const [source, label] of Object.entries(expected)) {
      expect(ruleSourceLabel(source as RuleSource)).toBe(label);
    }
  });

  it('describes both rule modes for tooltips', () => {
    const expected: Record<RuleMode, string> = {
      enforced: 'enforced: blocks the agent',
      advisory: 'advisory: guidance only',
    };
    for (const [mode, label] of Object.entries(expected)) {
      expect(ruleModeTooltip(mode as RuleMode)).toBe(label);
    }
  });

  it('formats relevance and priority', () => {
    expect(relevanceLabel(0.82)).toBe('relevance 0.82');
    expect(relevanceLabel(0.4)).toBe('relevance 0.40');
    expect(rulePriorityLabel(90)).toBe('priority 90');
    expect(rulePriorityLabel(0)).toBe('priority 0');
  });

  it('guards relevance against non-finite values', () => {
    expect(relevanceLabel(Number.NaN)).toBe('relevance N/A');
    expect(relevanceLabel(Number.POSITIVE_INFINITY)).toBe('relevance N/A');
  });

  it('rounds priority to an integer', () => {
    expect(rulePriorityLabel(90.6)).toBe('priority 91');
    expect(rulePriorityLabel(10.2)).toBe('priority 10');
  });

  it('guards priority against non-finite values', () => {
    expect(rulePriorityLabel(Number.NaN)).toBe('priority N/A');
    expect(rulePriorityLabel(Number.POSITIVE_INFINITY)).toBe('priority N/A');
  });

  it('gives every event kind a decorative icon, with a fallback for unknown kinds', () => {
    const kinds: EventKind[] = ['observation', 'action', 'tool_call', 'outcome', 'error', 'feedback'];
    for (const k of kinds) {
      expect(eventKindIcon(k)).toBeTruthy();
    }
    // Distinct glyph per kind so colour isn't the sole differentiator.
    expect(new Set(kinds.map(eventKindIcon)).size).toBe(kinds.length);
    expect(eventKindIcon('mystery' as EventKind)).toBe('•');
  });

  it('labels memory order from the by_salience flag', () => {
    expect(memoryOrderLabel(true)).toBe('Most relevant');
    expect(memoryOrderLabel(false)).toBe('Most recent');
  });

  it('exposes the single shared outdated-evidence term', () => {
    expect(EVIDENCE_OUTDATED).toBe('evidence outdated');
  });

  it('falls back to the raw value for unknown enum members', () => {
    expect(eventKindLabel('mystery' as EventKind)).toBe('mystery');
    expect(proposalActionLabel('mystery' as ProposalAction)).toBe('mystery');
    expect(ruleSourceLabel('mystery' as RuleSource)).toBe('mystery');
    expect(ruleModeTooltip('mystery' as RuleMode)).toBe('mystery');
  });
});
