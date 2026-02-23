import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatIconModule } from '@angular/material/icon';
import type { JobStatusResponse, TaskStateEntry } from '../../models';

/** Team display order for swim lanes. */
const TEAM_ORDER = ['git_setup', 'devops', 'backend-code-v2', 'backend', 'frontend'];

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
  ],
  templateUrl: './run-team-tracking.component.html',
  styleUrl: './run-team-tracking.component.scss',
})
export class RunTeamTrackingComponent {
  @Input() jobId: string | null = null;
  @Input() status: JobStatusResponse | null = null;

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
}
