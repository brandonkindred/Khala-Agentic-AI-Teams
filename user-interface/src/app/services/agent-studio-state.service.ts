import { Injectable, computed, signal } from '@angular/core';
import {
  AgentStudioHandoffState,
  BUILD_SUB_STAGES,
  STUDIO_STAGES,
  StudioStageStatus,
} from '../models/agent-studio.model';
import type { ProcessStatus } from '../models/agentic-team.model';

/** Total number of stages in the journey. */
const STAGE_COUNT = STUDIO_STAGES.length;

/** Total number of sub-stages in Stage 1's Start → Define → Configure sub-stepper. */
const BUILD_SUB_STAGE_COUNT = BUILD_SUB_STAGES.length;
/** Index of the Define sub-stage — the sub-stepper's only backward target. */
const DEFINE_SUB_STAGE_INDEX = BUILD_SUB_STAGES.findIndex((s) => s.key === 'define');
/** Index of the Configure sub-stage — the only sub-stage `backToDefine()` may be called from. */
const CONFIGURE_SUB_STAGE_INDEX = BUILD_SUB_STAGES.findIndex((s) => s.key === 'configure');

/**
 * Holds the Agent Studio handoff state and stepper position for one Studio
 * session. Provided at the Studio shell (not `root`) so navigating afresh to
 * `/agent-studio` starts clean — one instance per session (spec §2.4).
 *
 * Invariants:
 *   - `activeStage()` ∈ [0, STAGE_COUNT − 1].
 *   - `maxReachedStage()` ∈ [`activeStage()`, STAGE_COUNT − 1] and never
 *     decreases except via `reset()`.
 *   - `activeBuildSubStage()` ∈ [0, BUILD_SUB_STAGE_COUNT − 1].
 *   - `maxReachedBuildSubStage()` ∈ [`activeBuildSubStage()`,
 *     BUILD_SUB_STAGE_COUNT − 1] and never decreases except via `reset()`.
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
  /** Stage-3 gate: whether the composed team's roster fully covers its process needs. */
  readonly rosterFullyStaffed = signal(false);
  /** Stage-3 gate: status of the process selected as the Stage-4 handoff target. */
  readonly composeProcessStatus = signal<ProcessStatus | null>(null);

  // ── Server draft binding (spec §3.5) ───────────────────────────────────────
  /**
   * Server draft id this session is bound to; `null` until the first
   * successful save. Re-saving with this set issues a PUT (update-in-place)
   * instead of a POST (create) — see `AgentStudioApiService`.
   */
  readonly currentDraftId = signal<string | null>(null);
  /** Server draft name from the last successful save — pre-fills the
   *  Save-draft popover on re-save so it doesn't silently rename on confirm. */
  readonly currentDraftName = signal<string | null>(null);

  /**
   * `${teamId}::${manifestId}` keys the Stage-2 handoff agent has already been
   * auto-added for (spec §2.4 handoff). Kept in shared session state — not on
   * the Compose component — so the "at most one auto-add per (team, agent)"
   * guard survives the Compose component being destroyed and recreated across a
   * Stage-4 → "iterate roster" back-loop. Instance-local tracking would reset on
   * that recreation and re-add a handoff agent the user had manually removed,
   * since `registryAgentId` intentionally stays set for the back-loop.
   */
  private readonly handoffConsumed = new Set<string>();

  // ── Stepper position ───────────────────────────────────────────────────────
  private readonly _activeStage = signal(0);
  private readonly _maxReachedStage = signal(0);

  /** Currently displayed stage index (0-based). */
  readonly activeStage = this._activeStage.asReadonly();
  /**
   * Furthest stage reached this session. Tracked now for the real per-stage
   * gates and draft-resume logic in later increments; not yet consumed by the
   * scaffold UI.
   */
  readonly maxReachedStage = this._maxReachedStage.asReadonly();
  /** Whether the journey can advance past the current stage. */
  readonly canAdvance = computed(() => this._activeStage() < STAGE_COUNT - 1);

  // ── Stage-1 build sub-stepper (spec §3, Stage 1: 1.1 Start → 1.2 Define →
  // 1.3 Configure) ────────────────────────────────────────────────────────
  private readonly _activeBuildSubStage = signal(0);
  private readonly _maxReachedBuildSubStage = signal(0);

  /** Currently displayed Stage-1 sub-stage index (0-based). */
  readonly activeBuildSubStage = this._activeBuildSubStage.asReadonly();
  /** Furthest Stage-1 sub-stage reached this session (mirrors `maxReachedStage`). */
  readonly maxReachedBuildSubStage = this._maxReachedBuildSubStage.asReadonly();
  /** Whether the sub-stepper can advance past the current sub-stage. */
  readonly canAdvanceBuildSubStage = computed(
    () => this._activeBuildSubStage() < BUILD_SUB_STAGE_COUNT - 1,
  );

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
   * Preconditions: `index` is an integer ∈ [0, STAGE_COUNT − 1]; a violation is
   *   a caller bug and is rejected (never returns a misleading 'todo').
   * Postconditions: 'active' for the current stage, 'done' for earlier stages,
   *   'todo' otherwise.
   */
  stageStatus(index: number): StudioStageStatus {
    this.assertIndexInRange(index, STAGE_COUNT, 'stageStatus');
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
    this.assertIndexInRange(index, STAGE_COUNT, 'navigateToStage');
    this._activeStage.set(index);
    if (index > this._maxReachedStage()) {
      this._maxReachedStage.set(index);
    }
  }

  /**
   * Shared precondition guard for stage/sub-stage index parameters.
   *
   * Preconditions: none.
   * Postconditions: returns normally iff `index` is an integer in
   *   [0, count − 1]; otherwise throws `RangeError`.
   */
  private assertIndexInRange(index: number, count: number, method: string): void {
    if (!Number.isInteger(index) || index < 0 || index >= count) {
      throw new RangeError(`${method}: index ${index} out of range [0, ${count - 1}]`);
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

  /**
   * Progress status of Stage-1 sub-stage `index` for the sub-stepper header.
   * Same contract as `stageStatus`, scoped to the build sub-stepper.
   *
   * Preconditions: `index` is an integer ∈ [0, BUILD_SUB_STAGE_COUNT − 1]; a
   *   violation is a caller bug and is rejected.
   * Postconditions: 'active' for the current sub-stage, 'done' for earlier
   *   sub-stages, 'todo' otherwise.
   */
  buildSubStageStatus(index: number): StudioStageStatus {
    this.assertIndexInRange(index, BUILD_SUB_STAGE_COUNT, 'buildSubStageStatus');
    const active = this._activeBuildSubStage();
    if (index === active) return 'active';
    return index < active ? 'done' : 'todo';
  }

  /**
   * Advance the Stage-1 sub-stepper to the next sub-stage when one exists;
   * otherwise a no-op (mirrors `advance()`, forward-only — spec §3, Stage 1).
   * Postconditions: when `canAdvanceBuildSubStage()` held,
   *   `activeBuildSubStage()` increases by 1 and `maxReachedBuildSubStage()`
   *   is raised to match if it was lower.
   */
  advanceBuildSubStage(): void {
    if (this.canAdvanceBuildSubStage()) {
      const next = this._activeBuildSubStage() + 1;
      this._activeBuildSubStage.set(next);
      if (next > this._maxReachedBuildSubStage()) {
        this._maxReachedBuildSubStage.set(next);
      }
    }
  }

  /**
   * The sub-stepper's one explicit backward move — Configure ◂ Define (spec
   * §3, Stage 1: "the only backward move is the explicit `◂ back to Define`
   * action on the Configure step").
   *
   * Preconditions: `activeBuildSubStage() === CONFIGURE_SUB_STAGE_INDEX` — a
   *   violation is a caller bug (the calling action only ever renders on the
   *   Configure sub-stage) and is rejected rather than silently coerced.
   * Postconditions: `activeBuildSubStage() === DEFINE_SUB_STAGE_INDEX`;
   *   `maxReachedBuildSubStage()` is unchanged (same as a backward
   *   `navigateToStage` call on the main stepper).
   */
  backToDefine(): void {
    if (this._activeBuildSubStage() !== CONFIGURE_SUB_STAGE_INDEX) {
      throw new RangeError(
        `backToDefine: only callable from the Configure sub-stage (index ${CONFIGURE_SUB_STAGE_INDEX}), was at index ${this._activeBuildSubStage()}`,
      );
    }
    this._activeBuildSubStage.set(DEFINE_SUB_STAGE_INDEX);
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
  setRosterFullyStaffed(staffed: boolean): void {
    this.rosterFullyStaffed.set(staffed);
  }
  setComposeProcessStatus(status: ProcessStatus | null): void {
    this.composeProcessStatus.set(status);
  }

  /**
   * Record the server draft this session is now bound to. A single combined
   * setter (rather than two) because `draft_id`/`name` always change together
   * after a create/update response — this prevents a caller from updating one
   * without the other and leaving them inconsistent mid-frame.
   *
   * Preconditions: none.
   * Postconditions: `currentDraftId() === id` and `currentDraftName() === name`.
   */
  setCurrentDraft(id: string | null, name: string | null): void {
    this.currentDraftId.set(id);
    this.currentDraftName.set(name);
  }

  /**
   * Whether the Stage-2 handoff agent has already been auto-added for `key`
   * (`${teamId}::${manifestId}`) this session.
   *
   * Preconditions: none.
   * Postconditions: returns `true` iff `markHandoffConsumed(key)` was called
   *   since the last `reset()`.
   */
  hasConsumedHandoff(key: string): boolean {
    return this.handoffConsumed.has(key);
  }

  /**
   * Record that the Stage-2 handoff agent's auto-add was attempted for `key`
   * (`${teamId}::${manifestId}`), so it is not retried this session — including
   * after the Compose component is recreated by a Stage-4 back-loop.
   *
   * Preconditions: none.
   * Postconditions: `hasConsumedHandoff(key)` returns `true`.
   */
  markHandoffConsumed(key: string): void {
    this.handoffConsumed.add(key);
  }

  /**
   * Reset the session — clear handoff state and return to Stage 1.
   * Postconditions: every id is null; `activeStage() === 0`; `maxReachedStage() === 0`;
   *   `activeBuildSubStage() === 0`; `maxReachedBuildSubStage() === 0`.
   */
  reset(): void {
    this.registryAgentId.set(null);
    this.teamId.set(null);
    this.processId.set(null);
    this.personaId.set(null);
    this.draftAgentId.set(null);
    this.rosterFullyStaffed.set(false);
    this.composeProcessStatus.set(null);
    this.currentDraftId.set(null);
    this.currentDraftName.set(null);
    this.handoffConsumed.clear();
    this._activeStage.set(0);
    this._maxReachedStage.set(0);
    this._activeBuildSubStage.set(0);
    this._maxReachedBuildSubStage.set(0);
  }
}
