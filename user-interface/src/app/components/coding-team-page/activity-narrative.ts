import type { CodingTeamAgentStatus, CodingTeamJobStatus } from '../../models/coding-team.model';
import { isCodingTeamTerminalStatus } from '../../models/job-status.model';

/** Newest-line cap for the in-memory Jobs activity narrative. */
export const ACTIVITY_NARRATIVE_MAX_LINES = 200;

export interface ActivityNarrativeLine {
  readonly at: string;
  readonly text: string;
}

export interface ActivityNarrativeState {
  readonly lines: readonly ActivityNarrativeLine[];
  /** Join of display candidate lines from the last applied status snapshot (cheap no-op check only). */
  readonly fingerprint: string;
  /** Structural display candidate lines from the last applied status snapshot, used for diffing. */
  readonly candidates: readonly string[];
}

/**
 * Preconditions: none.
 * Postconditions: returns a state with empty `lines`, `fingerprint`, and `candidates`.
 */
export function emptyActivityNarrative(): ActivityNarrativeState {
  return { lines: [], fingerprint: '', candidates: [] };
}

/**
 * Preconditions: `receiveTimeIso` is a non-empty ISO-ish timestamp string.
 * Postconditions: prefers `last_activity_at` (real orchestrator progress; not refreshed by
 * wait-loop heartbeats), then `updated_at`, else `receiveTimeIso`.
 */
export function activityTimestamp(status: CodingTeamJobStatus, receiveTimeIso: string): string {
  return status.last_activity_at || status.updated_at || receiveTimeIso;
}

function formatCurrentActivity(status: CodingTeamJobStatus): string {
  const a = status.current_activity;
  if (!a) return '';
  const who = a.agent?.trim() || 'agent';
  const detail = (a.detail || a.step || a.task_title || '').trim();
  if (!detail) return `Activity: ${who}`;
  return `Activity: ${who} — ${detail}`;
}

function formatAgentLine(agent: CodingTeamAgentStatus): string {
  const step = (agent.activity_detail || agent.current_step || '').trim();
  const task = (agent.current_task_title || '').trim();
  if (!step && !task) return '';
  const body = task && step ? `${task} — ${step}` : task || step;
  return `Agent ${agent.display_name}: ${body}`;
}

/** Stable ordered display lines for a status snapshot (agents sorted by agent_id). */
function candidateLines(status: CodingTeamJobStatus): string[] {
  const out: string[] = [];
  if (status.phase) out.push(`Phase → ${status.phase}`);
  if (status.status_text) out.push(`Status: ${status.status_text}`);
  if (isCodingTeamTerminalStatus(status.status)) out.push(`Status: ${status.status}`);
  const activity = formatCurrentActivity(status);
  if (activity) out.push(activity);
  const agents = [...(status.agents ?? [])].sort((a, b) => a.agent_id.localeCompare(b.agent_id));
  for (const agent of agents) {
    const line = formatAgentLine(agent);
    if (line) out.push(line);
  }
  return out;
}

/**
 * Preconditions: `status` is a job status snapshot (fields may be absent).
 * Postconditions: returns `candidateLines(status).join('\n')`; identical inputs yield identical fingerprints.
 */
export function statusActivityFingerprint(status: CodingTeamJobStatus): string {
  return candidateLines(status).join('\n');
}

/**
 * Preconditions: `at` is the timestamp to stamp on any new lines.
 * Postconditions: returns one `{ at, text }` per candidate display line not present in
 * `prevCandidates`. `prevCandidates` is compared structurally (not via a joined/split string), so a
 * candidate line that itself contains a newline is never mistaken for multiple entries. When
 * `prevCandidates` is empty, every current candidate line is returned. Never mutates inputs.
 */
export function diffActivityLines(
  prevCandidates: readonly string[],
  status: CodingTeamJobStatus,
  at: string,
): ActivityNarrativeLine[] {
  const prev = new Set(prevCandidates);
  return candidateLines(status)
    .filter((text) => !prev.has(text))
    .map((text) => ({ at, text }));
}

/**
 * Preconditions: `receiveTimeIso` is non-empty.
 * Postconditions: when the status fingerprint matches `state.fingerprint`, returns `state`
 * unchanged (same reference). Otherwise appends `diffActivityLines(...)` (diffed against
 * `state.candidates`, not a joined/split fingerprint) and caps to `ACTIVITY_NARRATIVE_MAX_LINES`
 * newest lines; new `fingerprint`/`candidates` reflect this snapshot's `candidateLines(status)`.
 */
export function appendActivityNarrative(
  state: ActivityNarrativeState,
  status: CodingTeamJobStatus,
  receiveTimeIso: string,
): ActivityNarrativeState {
  const candidates = candidateLines(status);
  const fingerprint = candidates.join('\n');
  if (fingerprint === state.fingerprint) {
    return state;
  }
  const at = activityTimestamp(status, receiveTimeIso);
  const added = diffActivityLines(state.candidates, status, at);
  if (added.length === 0) {
    return { lines: state.lines, fingerprint, candidates };
  }
  const lines = [...state.lines, ...added];
  const capped =
    lines.length > ACTIVITY_NARRATIVE_MAX_LINES
      ? lines.slice(lines.length - ACTIVITY_NARRATIVE_MAX_LINES)
      : lines;
  return { lines: capped, fingerprint, candidates };
}

/**
 * Preconditions: none.
 * Postconditions: `null` when neither source is present; `'Agent thinking'` when
 * `hasThinking`; otherwise `'Agent activity'`.
 */
export function thoughtStreamPanelTitle(
  hasThinking: boolean,
  hasNarrative: boolean,
): 'Agent thinking' | 'Agent activity' | null {
  if (!hasThinking && !hasNarrative) return null;
  return hasThinking ? 'Agent thinking' : 'Agent activity';
}
