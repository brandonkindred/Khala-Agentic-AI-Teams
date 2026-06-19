import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import type { CodingTeamAgentStatus, CodingTeamJobStatus } from '../../models/coding-team.model';
import { ALREADY_COMPLETE, COMPLETED_WITH_FAILURES } from '../../models/job-status.model';

/** One step of the phase stepper. Local, to keep the monitor decoupled from other features. */
interface PhaseDefinition {
  id: string;
  label: string;
  icon: string;
}

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
  imports: [MatIconModule, MatProgressBarModule],
  templateUrl: './coding-team-monitor.component.html',
  styleUrl: './coding-team-monitor.component.scss',
  // Purely @Input()-driven; the parent re-feeds a fresh `status` object on each poll, so OnPush
  // re-renders on that reference change and skips redundant change-detection cycles in between.
  changeDetection: ChangeDetectionStrategy.OnPush,
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
    switch (s?.phase) {
      case 'task_graph':
        return 'Building the task graph';
      case 'coding':
        return 'Implementing the task graph';
      case 'publishing':
        return 'Opening the pull request';
      case 'reviewing':
        return 'Reviewing the pull request';
      case 'paused':
        return 'Paused — waiting for input';
      case 'completed':
        return 'Run complete';
      default:
        return 'Coding team run';
    }
  }

  // --- Overall progress + phase stepper --------------------------------------------------

  /** Overall progress as a clamped 0-100 number, or null when the record carries none. */
  overallProgress(): number | null {
    const p = this.status?.progress;
    if (p === undefined || p === null) return null;
    return Math.min(Math.max(p, 0), 100);
  }

  /** Indeterminate while a started job has no numeric progress yet; else determinate. */
  progressMode(): 'determinate' | 'indeterminate' {
    const s = this.status?.status;
    if (this.overallProgress() === null && (s === 'running' || s === 'pending')) {
      return 'indeterminate';
    }
    return 'determinate';
  }

  /** 'warn' when the run failed, was cancelled, or completed with task failures; 'primary' otherwise. */
  progressColor(): 'primary' | 'warn' {
    return this.isFailed() || this.status?.status === 'completed_with_failures' ? 'warn' : 'primary';
  }

  /**
   * True once the run has finished successfully — a clean completion, a partial success (per-task
   * failures), or an already-complete no-op (the work was already done). All are terminal successes
   * that must render the stepper as finished rather than leave it spinning forever.
   */
  private isDone(): boolean {
    const s = this.status?.status;
    return s === 'completed' || s === COMPLETED_WITH_FAILURES || s === ALREADY_COMPLETE;
  }

  /** True once the run has ended unsuccessfully (hard failure or cancellation). */
  private isFailed(): boolean {
    const s = this.status?.status;
    return s === 'failed' || s === 'cancelled';
  }

  /**
   * The stepper step the run is currently at (or stopped at), as a phase id in ALL_PHASES.
   *
   * Folds the backend's many phases/states onto the three steps so the stepper is never blank and
   * never lies: `publishing`/`reviewing` are post-coding finishing work (Coding); a failed run
   * stamps phase='completed' even when it died in planning, so it is located from the task graph
   * rather than trusted as done; an unknown phase on a live run defaults to Planning.
   */
  private currentStepId(): string {
    const s = this.status;
    if (!s) return '';
    const hasGraph = (s.task_graph_snapshot?.length ?? 0) > 0;
    if (this.isFailed()) return hasGraph ? 'coding' : 'task_graph';
    switch (s.phase) {
      case 'task_graph':
        return 'task_graph';
      case 'coding':
      case 'publishing': // coding done; pushing the branch / opening the PR
      case 'reviewing': // reviewing a pull request
        return 'coding';
      case 'completed':
        return 'completed';
      case 'paused':
        return hasGraph ? 'coding' : 'task_graph';
      default:
        // Unknown/empty phase on a live run: show at least Planning so the stepper isn't blank.
        return 'task_graph';
    }
  }

  private currentStepIndex(): number {
    return this.ALL_PHASES.findIndex((p) => p.id === this.currentStepId());
  }

  /** True for a step the run has already passed (and every step once the run finished). */
  isPhaseCompleted(phaseId: string): boolean {
    if (this.isDone()) return true;
    const idx = this.ALL_PHASES.findIndex((p) => p.id === phaseId);
    return idx >= 0 && idx < this.currentStepIndex();
  }

  /** The step a failed/cancelled run stopped at — rendered red, never as a green/in-progress step. */
  isFailedPhase(phaseId: string): boolean {
    return this.isFailed() && this.currentStepId() === phaseId;
  }

  /**
   * True for the step currently in progress. A finished run marks every step completed (green) and
   * a failed run's reached step renders failed — neither is also "current", so a step never carries
   * two conflicting state classes.
   */
  isCurrentPhase(phaseId: string): boolean {
    if (this.isDone() || this.isFailed()) return false;
    return this.currentStepId() === phaseId;
  }

  /** True for a step that is neither completed, current, nor failed (not started yet). */
  isPhasePending(phaseId: string): boolean {
    return (
      !this.isPhaseCompleted(phaseId) &&
      !this.isCurrentPhase(phaseId) &&
      !this.isFailedPhase(phaseId)
    );
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

  /**
   * Safe, known CSS-class suffix for the agent's status. Maps only the recognized statuses to a
   * styled class and folds anything else to 'unknown', so an unexpected backend value can never
   * inject an invalid CSS class name (e.g. one with spaces) into the template.
   */
  agentStatusClass(agent: CodingTeamAgentStatus): string {
    switch (agent.status) {
      case 'working':
      case 'in_review':
      case 'reviewing':
      case 'planning':
      case 'idle':
        return agent.status;
      default:
        return 'unknown';
    }
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
