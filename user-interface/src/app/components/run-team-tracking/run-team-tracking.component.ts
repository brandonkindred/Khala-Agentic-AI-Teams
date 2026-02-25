import { Component, Input, OnChanges, OnDestroy, OnInit, output, SimpleChanges } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import { MatExpansionModule } from '@angular/material/expansion';
import { Subscription, switchMap, timer } from 'rxjs';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { JobStatusResponse, TaskStateEntry, TeamProgressEntry } from '../../models';
import { PLANNING_V2_PHASES, CODE_TEAM_PHASES, MICROTASK_PHASES, type PhaseDefinition } from '../../models';

/** Team display order for swim lanes. */
const TEAM_ORDER = ['git_setup', 'devops', 'backend-code-v2', 'frontend-code-v2', 'backend', 'frontend'];

export interface TaskWithId {
  task_id: string;
  state: TaskStateEntry;
}

@Component({
  selector: 'app-run-team-tracking',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatProgressBarModule,
    MatChipsModule,
    MatIconModule,
    MatExpansionModule,
  ],
  templateUrl: './run-team-tracking.component.html',
  styleUrl: './run-team-tracking.component.scss',
})
export class RunTeamTrackingComponent implements OnInit, OnChanges, OnDestroy {
  @Input() jobId: string | null = null;

  /** Emits status updates for parent components that need them. */
  readonly statusChange = output<JobStatusResponse>();

  status: JobStatusResponse | null = null;
  loading = true;
  private pollSub: Subscription | null = null;

  /** All phases in order for the visual stepper. */
  readonly ALL_PHASES: PhaseDefinition[] = [
    { id: 'planning', label: 'Planning', icon: 'architecture' },
    { id: 'execution', label: 'Execution', icon: 'play_arrow' },
    { id: 'completed', label: 'Completed', icon: 'check_circle' },
  ];

  constructor(private readonly api: SoftwareEngineeringApiService) {}

  ngOnInit(): void {
    if (this.jobId) {
      this.startPolling();
    } else {
      this.loading = false;
    }
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['jobId'] && !changes['jobId'].firstChange) {
      this.status = null;
      this.loading = true;
      if (this.jobId) {
        this.startPolling();
      } else {
        this.pollSub?.unsubscribe();
        this.pollSub = null;
        this.loading = false;
      }
    }
  }

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
  }

  private startPolling(): void {
    this.pollSub?.unsubscribe();
    const pollInterval = this.status?.waiting_for_answers ? 5000 : 15000;
    this.pollSub = timer(0, pollInterval)
      .pipe(switchMap(() => this.api.getJobStatus(this.jobId!)))
      .subscribe({
        next: (res) => {
          const wasWaiting = this.status?.waiting_for_answers;
          const isWaiting = res.waiting_for_answers;
          this.status = res;
          this.statusChange.emit(res);
          this.loading = false;
          if (res.status === 'completed' || res.status === 'failed') {
            this.pollSub?.unsubscribe();
            this.pollSub = null;
          } else if (wasWaiting !== isWaiting) {
            this.startPolling();
          }
        },
        error: () => {
          this.loading = false;
          this.pollSub?.unsubscribe();
          this.pollSub = null;
        },
      });
  }

  /** Check if a phase has been completed (current phase is past it). */
  isPhaseCompleted(phaseId: string): boolean {
    const currentPhase = this.status?.phase ?? '';
    if (currentPhase === 'completed') return true;
    const order = this.ALL_PHASES.map(p => p.id);
    const currentIdx = order.indexOf(currentPhase);
    const targetIdx = order.indexOf(phaseId);
    return currentIdx > targetIdx && targetIdx >= 0;
  }

  /** Check if this is the current active phase. */
  isCurrentPhase(phaseId: string): boolean {
    return this.status?.phase === phaseId;
  }

  /** Check if a phase is still pending (not started yet). */
  isPhasePending(phaseId: string): boolean {
    return !this.isPhaseCompleted(phaseId) && !this.isCurrentPhase(phaseId);
  }

  /** Get the status badge text. */
  getStatusBadge(): string {
    if (this.status?.waiting_for_answers) {
      return 'Waiting for answers';
    }
    return this.status?.status ?? 'pending';
  }

  /** Get CSS class for status badge. */
  getStatusBadgeClass(): string {
    if (this.status?.waiting_for_answers) return 'status-waiting';
    switch (this.status?.status) {
      case 'completed': return 'status-completed';
      case 'failed': return 'status-failed';
      case 'running': return 'status-running';
      default: return 'status-pending';
    }
  }

  /** Team ID -> list of tasks in execution order for that team. */
  getTeamsWithTasks(): { teamId: string; label: string; tasks: TaskWithId[] }[] {
    const status = this.status;
    if (!status?.task_states || !status.task_ids?.length) {
      return [];
    }
    const byTeam = new Map<string, TaskWithId[]>();
    for (const taskId of status.task_ids) {
      const state = status.task_states[taskId];
      if (!state) continue;
      const list = byTeam.get(state.assignee) ?? [];
      list.push({ task_id: taskId, state });
      byTeam.set(state.assignee, list);
    }
    const result: { teamId: string; label: string; tasks: TaskWithId[] }[] = [];
    const seen = new Set(byTeam.keys());
    for (const teamId of TEAM_ORDER) {
      if (byTeam.has(teamId)) {
        result.push({
          teamId,
          label: this.teamLabel(teamId),
          tasks: byTeam.get(teamId)!,
        });
        seen.delete(teamId);
      }
    }
    for (const teamId of seen) {
      result.push({
        teamId,
        label: this.teamLabel(teamId),
        tasks: byTeam.get(teamId)!,
      });
    }
    return result;
  }

  teamLabel(teamId: string): string {
    const labels: Record<string, string> = {
      'git_setup': 'Git setup',
      'devops': 'DevOps',
      'backend-code-v2': 'Backend (v2)',
      'frontend-code-v2': 'Frontend (v2)',
      'backend': 'Backend',
      'frontend': 'Frontend',
    };
    return labels[teamId] ?? teamId;
  }

  taskStatusIcon(status: string): string {
    switch (status) {
      case 'done': return 'check_circle';
      case 'failed': return 'error';
      case 'in_progress': return 'pending';
      default: return 'radio_button_unchecked';
    }
  }

  taskStatusClass(status: string): string {
    switch (status) {
      case 'done': return 'task-done';
      case 'failed': return 'task-failed';
      case 'in_progress': return 'task-active';
      default: return 'task-pending';
    }
  }

  phaseLabel(phase: string): string {
    if (!phase) return '';
    return phase.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
  }

  isCurrentTask(teamId: string, taskId: string): boolean {
    const current = this.status?.team_progress?.[teamId]?.current_task_id;
    return current === taskId;
  }

  getTeamProgressKeys(): string[] {
    const status = this.status;
    if (!status?.team_progress) return [];
    const keys = Object.keys(status.team_progress);
    const ordered: string[] = [];
    for (const id of TEAM_ORDER) {
      if (keys.includes(id)) ordered.push(id);
    }
    for (const id of keys) {
      if (!ordered.includes(id)) ordered.push(id);
    }
    return ordered;
  }

  // ---------------------------------------------------------------------------
  // Subprocess Tracking (Planning and Execution phases)
  // ---------------------------------------------------------------------------

  /** Get all planning-v2 subprocess phases for display. */
  getPlanningSubprocessPhases(): PhaseDefinition[] {
    return PLANNING_V2_PHASES;
  }

  /** Get all code team phases for display (used by backend-code-v2, frontend-code-v2). */
  getCodeTeamPhases(): PhaseDefinition[] {
    return CODE_TEAM_PHASES;
  }

  /** Check if a planning subprocess phase has been completed. */
  isPlanningSubprocessCompleted(phaseId: string): boolean {
    const completedPhases = this.status?.planning_completed_phases ?? [];
    return completedPhases.includes(phaseId);
  }

  /** Check if a planning subprocess phase is currently active. */
  isPlanningSubprocessCurrent(phaseId: string): boolean {
    return this.status?.planning_subprocess === phaseId;
  }

  /** Check if a planning subprocess phase is pending (not started). */
  isPlanningSubprocessPending(phaseId: string): boolean {
    return !this.isPlanningSubprocessCompleted(phaseId) && !this.isPlanningSubprocessCurrent(phaseId);
  }

  /** Check if a code team subprocess phase has been completed. */
  isCodeTeamPhaseCompleted(teamId: string, phaseId: string): boolean {
    const teamProgress = this.status?.team_progress?.[teamId];
    if (!teamProgress?.current_phase) return false;
    const phaseOrder = CODE_TEAM_PHASES.map(p => p.id);
    const currentIdx = phaseOrder.indexOf(teamProgress.current_phase);
    const targetIdx = phaseOrder.indexOf(phaseId);
    return currentIdx > targetIdx && targetIdx >= 0;
  }

  /** Check if a code team subprocess phase is currently active. */
  isCodeTeamPhaseCurrent(teamId: string, phaseId: string): boolean {
    const teamProgress = this.status?.team_progress?.[teamId];
    return teamProgress?.current_phase === phaseId;
  }

  /** Check if a code team subprocess phase is pending. */
  isCodeTeamPhasePending(teamId: string, phaseId: string): boolean {
    return !this.isCodeTeamPhaseCompleted(teamId, phaseId) && !this.isCodeTeamPhaseCurrent(teamId, phaseId);
  }

  /** Get execution teams that have progress info (for subprocess display). */
  getExecutionTeams(): { teamId: string; label: string; progress: TeamProgressEntry }[] {
    const teamProgress = this.status?.team_progress;
    if (!teamProgress) return [];
    const codeTeamIds = ['backend-code-v2', 'frontend-code-v2'];
    const result: { teamId: string; label: string; progress: TeamProgressEntry }[] = [];
    for (const teamId of codeTeamIds) {
      const progress = teamProgress[teamId];
      if (progress) {
        result.push({
          teamId,
          label: this.teamLabel(teamId),
          progress,
        });
      }
    }
    return result;
  }

  /** Check if we should show the planning subprocess section. */
  showPlanningSubprocess(): boolean {
    return this.status?.phase === 'planning' && !!this.status?.planning_subprocess;
  }

  /** Check if we should show the execution subprocess section. */
  showExecutionSubprocess(): boolean {
    return this.status?.phase === 'execution' && this.getExecutionTeams().length > 0;
  }

  // ---------------------------------------------------------------------------
  // Task Title and Microtask Phase Tracking
  // ---------------------------------------------------------------------------

  /** Get the title of the job-level current task from task_states. */
  getCurrentTaskTitle(): string {
    const taskId = this.status?.current_task;
    if (!taskId) return '';
    return this.status?.task_states?.[taskId]?.title ?? taskId;
  }

  /** Get the title of the current task for a team from task_states. */
  getTaskTitle(teamId: string): string | null {
    const taskId = this.status?.team_progress?.[teamId]?.current_task_id;
    if (!taskId) return null;
    const taskState = this.status?.task_states?.[taskId];
    return taskState?.title ?? taskId;
  }

  /** Get microtask phases for display. */
  getMicrotaskPhases(): PhaseDefinition[] {
    return MICROTASK_PHASES;
  }

  /** Check if a microtask phase has been completed for a team. */
  isMicrotaskPhaseCompleted(teamId: string, phaseId: string): boolean {
    const currentPhase = this.status?.team_progress?.[teamId]?.current_microtask_phase;
    if (!currentPhase) return false;
    if (currentPhase === 'completed') return true;
    const phaseOrder = MICROTASK_PHASES.map(p => p.id);
    const currentIdx = phaseOrder.indexOf(currentPhase);
    const targetIdx = phaseOrder.indexOf(phaseId);
    return currentIdx > targetIdx && targetIdx >= 0;
  }

  /** Check if a microtask phase is currently active for a team. */
  isMicrotaskPhaseCurrent(teamId: string, phaseId: string): boolean {
    const currentPhase = this.status?.team_progress?.[teamId]?.current_microtask_phase;
    return currentPhase === phaseId;
  }

  /** Check if a microtask phase is pending for a team. */
  isMicrotaskPhasePending(teamId: string, phaseId: string): boolean {
    return !this.isMicrotaskPhaseCompleted(teamId, phaseId) && !this.isMicrotaskPhaseCurrent(teamId, phaseId);
  }

  /** Check if we should show microtask phase stepper for a team. */
  showMicrotaskPhases(teamId: string): boolean {
    const teamProgress = this.status?.team_progress?.[teamId];
    return teamProgress?.current_phase === 'execution' && !!teamProgress?.current_microtask;
  }
}
