import { Injectable, computed, signal } from '@angular/core';
import { AgentStudioHandoffState, STUDIO_STAGES, StudioStageStatus } from '../models/agent-studio.model';

/** Total number of stages in the journey. */
const STAGE_COUNT = STUDIO_STAGES.length;

/**
 * Holds the Agent Studio handoff state and stepper position for one Studio
 * session. Provided at the Studio shell (not `root`) so navigating afresh to
 * `/agent-studio` starts clean — one instance per session (spec §2.4).
 *
 * Invariants:
 *   - `activeStage()` ∈ [0, STAGE_COUNT − 1].
 *   - `maxReachedStage()` ∈ [`activeStage()`, STAGE_COUNT − 1] and never
 *     decreases except via `reset()`.
 */
@Injectable()
export class AgentStudioStateService {
  // ── Handoff state (spec §2.4) ──────────────────────────────────────────────
  readonly registryAgentId = signal<string | null>(null);
  readonly teamId = signal<string | null>(null);
  readonly processId = signal<string | null>(null);
  readonly personaId = signal<string | null>(null);
  /** Stage-1 build slot — the agent being authored (becomes registryAgentId on save). */
  readonly draftAgentId = signal<string | null>(null);

  // ── Stepper position ───────────────────────────────────────────────────────
  private readonly _activeStage = signal(0);
  private readonly _maxReachedStage = signal(0);

  /** Currently displayed stage index (0-based). */
  readonly activeStage = this._activeStage.asReadonly();
  /** Furthest stage reached this session (drives done/todo styling). */
  readonly maxReachedStage = this._maxReachedStage.asReadonly();
  /** Whether the journey can advance past the current stage. */
  readonly canAdvance = computed(() => this._activeStage() < STAGE_COUNT - 1);

  /** Read-only handoff snapshot for stage components to render. */
  readonly handoff = computed<AgentStudioHandoffState>(() => ({
    registryAgentId: this.registryAgentId(),
    teamId: this.teamId(),
    processId: this.processId(),
    personaId: this.personaId(),
    draftAgentId: this.draftAgentId(),
  }));

  /**
   * Progress status of stage `index` for the stepper header.
   *
   * Preconditions: `index` ∈ [0, STAGE_COUNT − 1].
   * Postconditions: 'active' for the current stage, 'done' for earlier stages,
   *   'todo' otherwise.
   */
  stageStatus(index: number): StudioStageStatus {
    const active = this._activeStage();
    if (index === active) return 'active';
    return index < active ? 'done' : 'todo';
  }

  /**
   * Move the stepper to `index`. The stepper itself is forward-only (its
   * indicators are not backward links); programmatic back-loops call this.
   *
   * Preconditions: `index` is an integer ∈ [0, STAGE_COUNT − 1]; a violation is
   *   a caller bug and is rejected (never silently coerced).
   * Postconditions: `activeStage() === index` and
   *   `maxReachedStage() === max(previous, index)`.
   */
  navigateToStage(index: number): void {
    if (!Number.isInteger(index) || index < 0 || index >= STAGE_COUNT) {
      throw new RangeError(`navigateToStage: index ${index} out of range [0, ${STAGE_COUNT - 1}]`);
    }
    this._activeStage.set(index);
    if (index > this._maxReachedStage()) {
      this._maxReachedStage.set(index);
    }
  }

  /**
   * Advance to the next stage when one exists; otherwise a no-op.
   * Postconditions: when `canAdvance()` held, `activeStage()` increases by 1.
   */
  advance(): void {
    if (this.canAdvance()) {
      this.navigateToStage(this._activeStage() + 1);
    }
  }

  setRegistryAgentId(id: string | null): void {
    this.registryAgentId.set(id);
  }
  setTeamId(id: string | null): void {
    this.teamId.set(id);
  }
  setProcessId(id: string | null): void {
    this.processId.set(id);
  }
  setPersonaId(id: string | null): void {
    this.personaId.set(id);
  }
  setDraftAgentId(id: string | null): void {
    this.draftAgentId.set(id);
  }

  /**
   * Reset the session — clear handoff state and return to Stage 1.
   * Postconditions: every id is null; `activeStage() === 0`; `maxReachedStage() === 0`.
   */
  reset(): void {
    this.registryAgentId.set(null);
    this.teamId.set(null);
    this.processId.set(null);
    this.personaId.set(null);
    this.draftAgentId.set(null);
    this._activeStage.set(0);
    this._maxReachedStage.set(0);
  }
}
