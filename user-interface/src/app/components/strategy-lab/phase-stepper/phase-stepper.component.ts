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
 * Presentational run-phase stepper for an in-progress Strategy Lab cycle.
 * Renders the four fixed pipeline phases (ideate/code/backtest/analyze) and
 * highlights which are completed, current, or pending relative to `currentPhase`.
 *
 * Preconditions: `currentPhase` is either `undefined`/`null` (no active cycle
 *   yet) or one of the ids in `STRATEGY_LAB_PHASES`; an unrecognized id is
 *   treated as "no phase" (every step renders pending).
 * Postconditions: renders exactly one `.phase-step` per entry in
 *   `STRATEGY_LAB_PHASES`, each with exactly one of the `completed`/`current`/
 *   `pending` classes applied.
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
}
