import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import type { PhaseDefinition } from '../../models/software-engineering.model';
import type { CodingTeamAgentStatus, CodingTeamJobStatus } from '../../models/coding-team.model';

/** Phase-stepper steps for a coding-team job: build the task graph, code it, done. */
const CODING_TEAM_PHASES: PhaseDefinition[] = [
  { id: 'task_graph', label: 'Planning', icon: 'account_tree' },
  { id: 'coding', label: 'Coding', icon: 'code' },
  { id: 'completed', label: 'Completed', icon: 'check_circle' },
];

/**
 * Presentational monitor for a coding-team run. Renders the team's current objective, an overall
 * progress bar + phase stepper, the live sub-agent activity, and a per-agent roster (who is
 * working now and each agent's status). Purely `@Input()`-driven — the parent page polls
 * `/status` and re-feeds `status`, so the monitor re-renders for free. Every block is guarded so
 * a minimal/early status (no agents/progress/activity) renders cleanly.
 */
@Component({
  selector: 'app-coding-team-monitor',
  standalone: true,
  imports: [CommonModule, MatIconModule, MatProgressBarModule],
  templateUrl: './coding-team-monitor.component.html',
  styleUrl: './coding-team-monitor.component.scss',
})
export class CodingTeamMonitorComponent {
  /** Latest polled job status; null until the first poll lands. */
  @Input() status: CodingTeamJobStatus | null = null;

  readonly ALL_PHASES = CODING_TEAM_PHASES;

  // --- Objective / current focus ---------------------------------------------------------

  /** Titles of tasks actively being worked (in progress or under review), for the objective line. */
  workingTaskTitles(): string[] {
    const tasks = this.status?.task_graph_snapshot ?? [];
    return tasks
      .filter((t) => t.status === 'in_progress' || t.status === 'in_review')
      .map((t) => t.title)
      .filter((title): title is string => !!title);
  }

  /** Human summary of what the team is currently doing, for the objective header. */
  objectiveText(): string {
    const s = this.status;
    if (s?.status_text) return s.status_text;
    if (s?.phase === 'task_graph') return 'Building the task graph';
    if (s?.phase === 'coding') return 'Implementing the task graph';
    if (s?.phase === 'completed') return 'Run complete';
    return 'Coding team run';
  }

  // --- Overall progress + phase stepper --------------------------------------------------

  /** Overall progress as a clamped 0-100 number, or null when the record carries none. */
  overallProgress(): number | null {
    const p = this.status?.progress;
    if (p === undefined || p === null) return null;
    return Math.min(Math.max(p, 0), 100);
  }

  /** Indeterminate while the job runs before any numeric progress lands; else determinate. */
  progressMode(): 'determinate' | 'indeterminate' {
    if (this.overallProgress() === null && this.status?.status === 'running') return 'indeterminate';
    return 'determinate';
  }

  /** 'warn' once the job has failed so the bar reads red; 'primary' otherwise. */
  progressColor(): 'primary' | 'warn' {
    return this.status?.status === 'failed' ? 'warn' : 'primary';
  }

  /** The phase that drives the stepper; folds terminal states and HITL pauses onto a real step. */
  private effectivePhase(): string {
    const s = this.status;
    if (!s) return '';
    if (
      s.status === 'completed' ||
      s.status === 'completed_with_failures' ||
      s.phase === 'completed'
    ) {
      return 'completed';
    }
    if (s.phase === 'task_graph' || s.phase === 'coding') return s.phase;
    // A paused job keeps the step it paused in; infer it from whether a task graph exists yet.
    if (s.phase === 'paused') {
      return (s.task_graph_snapshot?.length ?? 0) > 0 ? 'coding' : 'task_graph';
    }
    return s.phase ?? '';
  }

  isPhaseCompleted(phaseId: string): boolean {
    const current = this.effectivePhase();
    if (current === 'completed') return true;
    const order = this.ALL_PHASES.map((p) => p.id);
    const currentIdx = order.indexOf(current);
    const targetIdx = order.indexOf(phaseId);
    return targetIdx >= 0 && currentIdx > targetIdx;
  }

  isCurrentPhase(phaseId: string): boolean {
    return this.effectivePhase() === phaseId;
  }

  isPhasePending(phaseId: string): boolean {
    return !this.isPhaseCompleted(phaseId) && !this.isCurrentPhase(phaseId);
  }

  // --- Job-level current activity sub-bar ------------------------------------------------

  /** 0-1 fraction of the live sub-agent activity, clamped; null when absent. */
  activityFraction(): number | null {
    const fraction = this.status?.current_activity?.fraction;
    if (fraction === undefined || fraction === null) return null;
    return Math.min(Math.max(fraction, 0), 1);
  }

  /** Human label for the current sub-agent activity (mirrors the SE tracking component). */
  activityAgentLabel(): string {
    const agent = this.status?.current_activity?.agent;
    if (agent === 'tech_lead_review') return 'Tech Lead review';
    if (agent === 'code_review') return 'Code review';
    return agent || 'Agent activity';
  }

  // --- Agent roster cards ----------------------------------------------------------------

  /** True for any agent that is not idle, so the card can be visually emphasized. */
  isAgentActive(agent: CodingTeamAgentStatus): boolean {
    return agent.status !== 'idle';
  }

  /** Material icon for the agent's role. */
  agentRoleIcon(agent: CodingTeamAgentStatus): string {
    return agent.role === 'tech_lead' ? 'supervisor_account' : 'code';
  }

  /** Human-readable status label for the agent badge. */
  agentStatusLabel(agent: CodingTeamAgentStatus): string {
    switch (agent.status) {
      case 'working':
        return 'Working';
      case 'in_review':
        return 'In review';
      case 'reviewing':
        return 'Reviewing';
      case 'planning':
        return 'Planning';
      case 'idle':
        return 'Idle';
      default:
        return agent.status;
    }
  }

  /** Per-agent live sub-step fraction as a clamped 0-1 number, or null. */
  agentFraction(agent: CodingTeamAgentStatus): number | null {
    const fraction = agent.activity_fraction;
    if (fraction === undefined || fraction === null) return null;
    return Math.min(Math.max(fraction, 0), 1);
  }
}
