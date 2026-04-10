import {
  Component,
  Input,
  OnDestroy,
  OnInit,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatChipsModule } from '@angular/material/chips';
import { Subscription, interval, switchMap, takeWhile } from 'rxjs';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import type {
  AgenticTeam,
  TestPipelineRun,
  PipelineStepResult,
} from '../../models';

@Component({
  selector: 'app-pipeline-test-runner',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatSelectModule,
    MatChipsModule,
  ],
  templateUrl: './pipeline-test-runner.component.html',
  styleUrl: './pipeline-test-runner.component.scss',
})
export class PipelineTestRunnerComponent implements OnInit, OnDestroy {
  @Input() team!: AgenticTeam;

  private readonly api = inject(AgenticTeamApiService);
  private readonly fb = inject(FormBuilder);

  selectedProcessId = signal<string | null>(null);
  initialInput = signal('');
  activeRun = signal<TestPipelineRun | null>(null);
  runs = signal<TestPipelineRun[]>([]);
  expandedStepId = signal<string | null>(null);
  error = signal<string | null>(null);

  waitInputForm = this.fb.nonNullable.group({
    input: ['', [Validators.required, Validators.minLength(1)]],
  });

  private pollSub: Subscription | null = null;

  ngOnInit(): void {
    if (this.team.processes.length > 0) {
      this.selectedProcessId.set(this.team.processes[0].process_id);
    }
    this.loadRuns();
  }

  ngOnDestroy(): void {
    this.stopPolling();
  }

  loadRuns(): void {
    this.api.listPipelineRuns(this.team.team_id).subscribe({
      next: (runs) => this.runs.set(runs),
    });
  }

  startRun(): void {
    const processId = this.selectedProcessId();
    if (!processId) return;

    const input = this.initialInput().trim() || undefined;
    this.api.startPipelineRun(this.team.team_id, processId, input).subscribe({
      next: (run) => {
        this.activeRun.set(run);
        this.runs.update((r) => [run, ...r]);
        this.startPolling(run.run_id);
      },
      error: (err) => this.error.set(err?.error?.detail ?? 'Failed to start run'),
    });
  }

  cancelRun(): void {
    const run = this.activeRun();
    if (!run) return;
    this.api.cancelPipelineRun(this.team.team_id, run.run_id).subscribe({
      next: (updated) => {
        this.activeRun.set(updated);
        this.updateRunInList(updated);
        this.stopPolling();
      },
    });
  }

  submitWaitInput(): void {
    const run = this.activeRun();
    if (!run || this.waitInputForm.invalid) return;
    const input = this.waitInputForm.getRawValue().input.trim();
    this.api.submitPipelineInput(this.team.team_id, run.run_id, input).subscribe({
      next: (updated) => {
        this.activeRun.set(updated);
        this.updateRunInList(updated);
        this.waitInputForm.reset({ input: '' });
      },
    });
  }

  selectRun(run: TestPipelineRun): void {
    this.activeRun.set(run);
    if (this.isTerminal(run.status)) {
      this.stopPolling();
    } else {
      this.startPolling(run.run_id);
    }
  }

  toggleStep(stepId: string): void {
    this.expandedStepId.update((cur) => (cur === stepId ? null : stepId));
  }

  getStepIcon(status: string): string {
    switch (status) {
      case 'completed': return 'check_circle';
      case 'running': return 'sync';
      case 'waiting_for_input': return 'pause_circle';
      case 'failed': return 'error';
      case 'cancelled': return 'cancel';
      default: return 'hourglass_empty';
    }
  }

  getStepIconClass(status: string): string {
    switch (status) {
      case 'completed': return 'step-completed';
      case 'running': return 'step-running';
      case 'waiting_for_input': return 'step-waiting';
      case 'failed': return 'step-failed';
      case 'cancelled': return 'step-cancelled';
      default: return 'step-pending';
    }
  }

  getStatusChipClass(status: string): string {
    switch (status) {
      case 'completed': return 'status-completed';
      case 'running': return 'status-running';
      case 'waiting_for_input': return 'status-waiting';
      case 'failed': return 'status-failed';
      case 'cancelled': return 'status-cancelled';
      default: return '';
    }
  }

  getProcessSteps(): { step_id: string; name: string; agent_name: string }[] {
    const processId = this.activeRun()?.process_id ?? this.selectedProcessId();
    const process = this.team.processes.find((p) => p.process_id === processId);
    if (!process) return [];
    return process.steps.map((s) => ({
      step_id: s.step_id,
      name: s.name,
      agent_name: s.agents[0]?.agent_name ?? '',
    }));
  }

  getStepResult(stepId: string): PipelineStepResult | undefined {
    return this.activeRun()?.step_results?.find((r) => r.step_id === stepId);
  }

  formatDuration(run: TestPipelineRun): string {
    if (!run.started_at) return '';
    const start = new Date(run.started_at).getTime();
    const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now();
    const seconds = Math.floor((end - start) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    return `${minutes}m ${seconds % 60}s`;
  }

  private startPolling(runId: string): void {
    this.stopPolling();
    this.pollSub = interval(2000)
      .pipe(
        switchMap(() => this.api.getPipelineRun(this.team.team_id, runId)),
        takeWhile((run) => !this.isTerminal(run.status), true),
      )
      .subscribe({
        next: (run) => {
          this.activeRun.set(run);
          this.updateRunInList(run);
        },
      });
  }

  private stopPolling(): void {
    this.pollSub?.unsubscribe();
    this.pollSub = null;
  }

  private isTerminal(status: string): boolean {
    return ['completed', 'failed', 'cancelled'].includes(status);
  }

  private updateRunInList(updated: TestPipelineRun): void {
    this.runs.update((list) =>
      list.map((r) => (r.run_id === updated.run_id ? updated : r)),
    );
  }
}
