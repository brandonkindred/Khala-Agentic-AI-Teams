import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatExpansionModule } from '@angular/material/expansion';

import type { GateViewModel } from '../strategy-lab.formatters';

/**
 * Presentational quality-gate results panel for a single Strategy Lab
 * result. Renders the precomputed per-gate view models produced by
 * `StrategyCardComponent.gateViewModels()` — remediation status (`isRemedied`)
 * is genuine cross-gate business logic and stays on that component; this
 * component only renders what it's handed.
 *
 * Preconditions: `gateViewModels` is set before the first render (required
 *   input); the panel itself only renders (`@if`) when non-empty.
 * Postconditions: renders identically for the same `gateViewModels`/
 *   `refinementRounds` regardless of how many times change detection runs
 *   (OnPush; purely a function of the inputs).
 */
@Component({
  selector: 'app-quality-gate-list',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule, MatIconModule, MatExpansionModule],
  templateUrl: './quality-gate-list.component.html',
  styleUrl: './quality-gate-list.component.scss',
})
export class QualityGateListComponent {
  /** Precomputed per-gate template data (icon, severity class, remedied flag) — see `StrategyCardComponent.gateViewModels`. */
  @Input({ required: true }) gateViewModels!: GateViewModel[];
  /** Total refinement rounds run for the owning record, shown in the panel description when set. */
  @Input() refinementRounds?: number;
}
