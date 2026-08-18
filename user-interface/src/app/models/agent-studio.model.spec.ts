import { describe, it, expect } from 'vitest';
import {
  AGENT_STATE_LABELS,
  STAGE_INDEX,
  STUDIO_STAGES,
  type AgentDefinition,
  type AgentState,
  type AgentStateKey,
} from './agent-studio.model';

describe('agent-studio.model', () => {
  it('labels every operating-state key', () => {
    // The three keys are fixed; each maps to a display label.
    const expected: Record<AgentStateKey, string> = {
      planning: 'Planning',
      executing: 'Executing',
      researching: 'Researching',
    };
    expect(AGENT_STATE_LABELS).toEqual(expected);
  });

  it('exposes the four stages in forward order', () => {
    expect(STUDIO_STAGES.map((s) => s.key)).toEqual(['build', 'test', 'compose', 'personas']);
    // Only the final stage has no forward action.
    expect(STUDIO_STAGES[STUDIO_STAGES.length - 1].forwardLabel).toBeUndefined();
  });

  it('derives STAGE_INDEX matching STUDIO_STAGES order', () => {
    expect(STAGE_INDEX).toEqual({ build: 0, test: 1, compose: 2, personas: 3 });
  });

  it('models a definition carrying the three seeded states', () => {
    // Type-level contract check: a definition with states compiles and round-trips.
    const states: AgentState[] = (Object.keys(AGENT_STATE_LABELS) as AgentStateKey[]).map(
      (key) => ({ key, label: AGENT_STATE_LABELS[key], system_prompt: `${key} prompt` }),
    );
    const definition: AgentDefinition = {
      name: 'Planner',
      role: 'Plans things',
      description: null,
      tags: [],
      tools: [],
      system_prompt: '',
      input_schema: null,
      output_schema: null,
      states,
      mode: 'new',
      cloned_from: null,
    };
    expect(definition.states.map((s) => s.key)).toEqual(['planning', 'executing', 'researching']);
  });
});
