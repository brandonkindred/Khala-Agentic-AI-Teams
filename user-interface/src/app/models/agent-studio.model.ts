/**
 * Agent Studio — shared types and the stage registry for the guided journey.
 *
 * Pure data (no behaviour). See `docs/design/agent-studio-ux-spec.md` §2.1 (the
 * Studio spine / 4-stage stepper) and §2.4 (handoff state). Stages 2–4 are
 * stubbed in the current scaffold; this array is the single source the shell
 * stepper and `AgentStudioStateService` both read.
 */

/** The four top-level stages, in forward order. */
export type StudioStageKey = 'build' | 'test' | 'compose' | 'personas';

export interface StudioStage {
  /** Stable identity used by the state service and stepper. */
  key: StudioStageKey;
  /** Display label shown in the stepper. */
  label: string;
  /** Material icon name. */
  icon: string;
  /** One-line description of what the stage does. */
  blurb: string;
  /**
   * Label for this stage's forward action (the spec's stage-specific guided
   * affordances). Absent on the final stage, which has no forward step.
   */
  forwardLabel?: string;
}

/** The forward-only stage order. Index `i` is stage number `i + 1`. */
export const STUDIO_STAGES: readonly StudioStage[] = [
  { key: 'build', label: 'Build Agent', icon: 'build_circle', blurb: 'Author a new agent — or refine a copy of an existing one.', forwardLabel: 'Test this agent →' },
  { key: 'test', label: 'Test Agent', icon: 'play_circle', blurb: 'Run the agent in its sandbox and compare runs.', forwardLabel: 'Add to team →' },
  { key: 'compose', label: 'Compose Team', icon: 'groups', blurb: 'Assemble a team and design its process.', forwardLabel: 'Test this team →' },
  { key: 'personas', label: 'Test Team w/ Personas', icon: 'science', blurb: 'Drive the team manually or with autonomous personas.' },
] as const;

/**
 * The three fixed operating "states of being" every authored agent is seeded
 * with. The key set is fixed; only a state's prompt is editable. Mirrors the
 * backend `AgentStateKey` literal.
 */
export type AgentStateKey = 'planning' | 'executing' | 'researching';

/** Display labels for the operating states, keyed by `AgentStateKey`. */
export const AGENT_STATE_LABELS: Readonly<Record<AgentStateKey, string>> = {
  planning: 'Planning',
  executing: 'Executing',
  researching: 'Researching',
} as const;

/**
 * One behavioral operating state on an authored agent. `system_prompt` is the
 * snake_case wire field name returned by the backend; the (future)
 * agent-studio API service owns any snake↔camel mapping.
 */
export interface AgentState {
  key: AgentStateKey;
  label: string;
  system_prompt: string;
}

/**
 * The draft agent being authored in Stage 1 (Build). Mirrors the backend
 * `AgentDefinition` wire shape. The build UI is still a placeholder; this is the
 * contract the future build component / API service will bind to. Field names
 * match the JSON wire shape (snake_case) the backend serializes.
 */
export interface AgentDefinition {
  name: string;
  role: string;
  description: string | null;
  tags: string[];
  tools: string[];
  system_prompt: string;
  input_schema: Record<string, unknown> | null;
  output_schema: Record<string, unknown> | null;
  states: AgentState[];
  mode: 'new' | 'refine';
  cloned_from: string | null;
}

/** Progress state of a single stepper indicator. */
export type StudioStageStatus = 'done' | 'active' | 'todo';

/**
 * Cross-stage handoff state (spec §2.4). Each id is set as the user progresses;
 * later stages read it to pre-seed themselves. Null until the producing stage
 * sets it.
 */
export interface AgentStudioHandoffState {
  registryAgentId: string | null;
  teamId: string | null;
  processId: string | null;
  personaId: string | null;
  /** Stage-1 build slot: the agent currently being authored (spec §2.4). */
  draftAgentId: string | null;
}
