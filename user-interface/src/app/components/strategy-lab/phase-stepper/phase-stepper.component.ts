import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';

interface PhaseDefinition {
  id: string;
  label: string;
  icon: string;
}

const STRATEGY_LAB_PHASES: PhaseDefinition[] = [
  { id: 'ideating',     label: 'Ideate',    icon: 'psychology' },
  { id: 'coding',       label: 'Code',      icon: 'code' },
  { id: 'backtesting',  label: 'Backtest',  icon: 'play_circle' },
  { id: 'analyzing',    label: 'Analyze',   icon: 'summarize' },
];

/** Ordered phase IDs for determining completed/pending state. */
const PHASE_ORDER = STRATEGY_LAB_PHASES.map(p => p.id);

/**
 * Human-readable label for a phase id, for display outside the stepper
 * itself (e.g. a "Current phase: X" readout) without duplicating
 * `STRATEGY_LAB_PHASES` at the call site.
 *
 * Preconditions: none — accepts any string, null, or undefined.
 * Postconditions: returns the matching phase's label, or null if `phaseId`
 *   doesn't match any entry in `STRATEGY_LAB_PHASES`.
 */
export function phaseLabel(phaseId: string | null | undefined): string | null {
  return STRATEGY_LAB_PHASES.find(p => p.id === phaseId)?.label ?? null;
}

/**
 * Presentational run-phase stepper for an in-progress Strategy Lab cycle.
 * Renders the four fixed pipeline phases (ideate/code/backtest/analyze) and
 * highlights which are completed, current, or pending relative to `currentPhase`.
 *
 * Preconditions: `currentPhase` is either `undefined`/`null` (no active cycle
 *   yet) or one of the ids in `STRATEGY_LAB_PHASES`; an unrecognized id is
 *   treated as "no phase" (every step renders pending).
 * Postconditions: renders exactly one `.phase-step` per entry in
 *   `STRATEGY_LAB_PHASES`, each with exactly one of the `completed`/`current`/
 *   `pending` classes applied; the container has `role="list"`, each step has
 *   `role="listitem"`, the current step (if any) carries `aria-current="step"`,
 *   and each step's label carries a visually-hidden state suffix so its
 *   completed/current/not-started state reaches assistive tech, not just sighted
 *   users via CSS.
 */
@Component({
  selector: 'app-phase-stepper',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MatIconModule],
  templateUrl: './phase-stepper.component.html',
  styleUrl: './phase-stepper.component.scss',
})
export class PhaseStepperComponent {
  /** The active cycle's current phase id, or null/undefined when no cycle is in progress. */
  @Input() currentPhase: string | null | undefined;

  readonly STRATEGY_LAB_PHASES = STRATEGY_LAB_PHASES;

  isPhaseCompleted(phaseId: string): boolean {
    const current = this.currentPhase;
    if (!current) return false;
    const currentIdx = PHASE_ORDER.indexOf(current);
    const phaseIdx = PHASE_ORDER.indexOf(phaseId);
    if (currentIdx < 0 || phaseIdx < 0) return false;
    return phaseIdx < currentIdx;
  }

  isCurrentPhase(phaseId: string): boolean {
    return this.currentPhase === phaseId;
  }

  isPhasePending(phaseId: string): boolean {
    return !this.isPhaseCompleted(phaseId) && !this.isCurrentPhase(phaseId);
  }

  /**
   * Screen-reader state text for a phase-stepper step, mirroring the visual
   * completed/current/pending cue that sighted users get from CSS classes and
   * the icon swap.
   *
   * Preconditions: `phaseId` is one of the STRATEGY_LAB_PHASES ids; called only
   *   while a run is active (the stepper renders under `runStatus.current_cycle`).
   * Postconditions: returns exactly one of `'completed'`, `'current step'`, or
   *   `'not started'`, derived solely from `isPhaseCompleted`/`isCurrentPhase`
   *   (no independent ordering logic), so it never disagrees with the visual state.
   */
  phaseStateLabel(phaseId: string): string {
    if (this.isPhaseCompleted(phaseId)) return 'completed';
    if (this.isCurrentPhase(phaseId)) return 'current step';
    return 'not started';
  }
}
