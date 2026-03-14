import { Component, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { CommonModule } from '@angular/common';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatChipsModule } from '@angular/material/chips';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { MatDividerModule } from '@angular/material/divider';
import { StudioGridApiService } from '../../services/studio-grid-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import type { StartRunResponse, RunStatus, Decision } from '../../models';

const PHASES = ['INTAKE', 'DISCOVERY', 'IA', 'WIREFRAMES', 'SYSTEM', 'HIFI', 'ASSETS', 'HANDOFF', 'DONE'];

@Component({
  selector: 'app-studio-grid-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatChipsModule,
    MatCardModule,
    MatIconModule,
    MatDividerModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
  ],
  templateUrl: './studio-grid-dashboard.component.html',
  styleUrl: './studio-grid-dashboard.component.scss',
})
export class StudioGridDashboardComponent {
  private readonly api = inject(StudioGridApiService);

  readonly phases = PHASES;

  // Form state
  projectName = '';
  intakeJson = '{\n  "brief": ""\n}';
  intakeError: string | null = null;

  // Async state
  loading = false;
  error: string | null = null;

  // Run state
  run: StartRunResponse | RunStatus | null = null;
  decisions: Decision[] = [];

  // Resolve state
  resolvingDecisionId: string | null = null;
  resolveError: string | null = null;

  healthCheck = (): ReturnType<StudioGridApiService['health']> => this.api.health();

  get currentPhaseIndex(): number {
    if (!this.run) return -1;
    return PHASES.indexOf(this.run.phase);
  }

  onStartRun(): void {
    this.intakeError = null;
    let intake: Record<string, unknown>;
    try {
      intake = JSON.parse(this.intakeJson);
    } catch {
      this.intakeError = 'Intake must be valid JSON';
      return;
    }

    this.loading = true;
    this.error = null;
    this.run = null;
    this.decisions = [];

    this.api.startRun({ project_name: this.projectName, intake }).subscribe({
      next: (res) => {
        this.run = res;
        this.loading = false;
        this.loadDecisions(res.run_id);
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to start run';
        this.loading = false;
      },
    });
  }

  onRefreshStatus(): void {
    if (!this.run) return;
    this.api.getRunStatus(this.run.run_id).subscribe({
      next: (status) => {
        this.run = status;
        this.loadDecisions(status.run_id);
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to refresh status';
      },
    });
  }

  private loadDecisions(runId: string): void {
    this.api.listDecisions(runId).subscribe({
      next: (res) => { this.decisions = res.decisions; },
      error: () => { /* decisions are optional, ignore errors */ },
    });
  }

  onResolveDecision(decisionId: string, optionKey: string): void {
    this.resolvingDecisionId = decisionId;
    this.resolveError = null;
    this.api.resolveDecision(decisionId, optionKey).subscribe({
      next: (updated) => {
        this.decisions = this.decisions.map((d) => d.decision_id === decisionId ? updated : d);
        this.resolvingDecisionId = null;
      },
      error: (err) => {
        this.resolveError = err?.error?.detail ?? err?.message ?? 'Failed to resolve decision';
        this.resolvingDecisionId = null;
      },
    });
  }
}
