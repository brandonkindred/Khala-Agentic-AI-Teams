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
import { PLANNING_V2_PHASES, CODE_TEAM_PHASES, MICROTASK_PHASES, PRODUCT_ANALYSIS_PHASES, type PhaseDefinition } from '../../models';

/** Team display order for swim lanes. */
const TEAM_ORDER = ['git_setup', 'devops', 'backend-code-v2', 'frontend-code-v2', 'backend', 'frontend'];

export interface TaskWithId {
  task_id: string;
  state: TaskStateEntry;
}

/** Flattened tree node for progress tree view. */
export interface FlatTreeNode {
  id: string;
  label: string;
  icon: string;
  status: 'completed' | 'current' | 'pending';
  level: number;
  detail?: string;
  isLast: boolean;
  parentIsLast: boolean[];
}

/** Hierarchical DAG node for visual tree view. */
export interface DAGNode {
  id: string;
  label: string;
  icon: string;
  status: 'completed' | 'current' | 'pending';
  detail?: string;
  children?: DAGNode[];
}

type WorkItemStatus = 'completed' | 'in_progress' | 'failed' | 'pending';
type WorkItemLevel = 'root' | 'initiative' | 'epic' | 'task' | 'subtask';

interface WorkTreeNode {
  id: string;
  label: string;
  level: WorkItemLevel;
  status: WorkItemStatus;
  children: WorkTreeNode[];
}

interface FlatWorkTreeNode {
  id: string;
  label: string;
  level: WorkItemLevel;
  status: WorkItemStatus;
  depth: number;
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
  workTreeRows: FlatWorkTreeNode[] = [];
  loading = true;
  private pollSub: Subscription | null = null;

  /** All phases in order for the visual stepper. */
  readonly ALL_PHASES: PhaseDefinition[] = [
    { id: 'product_analysis', label: 'Product Analysis', icon: 'analytics' },
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
      this.workTreeRows = [];
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
    const isProductAnalysisActive =
      this.status?.phase === 'product_analysis' &&
      (this.status?.status === 'running' || this.status?.status === 'pending');
    const pollInterval =
      this.status?.waiting_for_answers || isProductAnalysisActive ? 5000 : 15000;
    this.pollSub = timer(0, pollInterval)
      .pipe(switchMap(() => this.api.getJobStatus(this.jobId!)))
      .subscribe({
        next: (res) => {
          const wasWaiting = this.status?.waiting_for_answers;
          const isWaiting = res.waiting_for_answers;
          const needFastPoll = (s: JobStatusResponse | null) =>
            s?.waiting_for_answers ||
            (s?.phase === 'product_analysis' &&
              (s?.status === 'running' || s?.status === 'pending'));
          const newInterval = needFastPoll(res) ? 5000 : 15000;
          const oldInterval = needFastPoll(this.status) ? 5000 : 15000;
          this.status = res;
          this.workTreeRows = this.buildWorkTreeRows(res);
          this.statusChange.emit(res);
          this.loading = false;
          if (res.status === 'completed' || res.status === 'failed' || res.status === 'cancelled') {
            this.pollSub?.unsubscribe();
            this.pollSub = null;
          } else if (wasWaiting !== isWaiting || newInterval !== oldInterval) {
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

  /** Get all planning-v3 subprocess phases for display. */
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
  // Product Analysis Subprocess Tracking
  // ---------------------------------------------------------------------------

  /** Get all product analysis subprocess phases for display. */
  getProductAnalysisPhases(): PhaseDefinition[] {
    return PRODUCT_ANALYSIS_PHASES;
  }

  /** Check if a product analysis subprocess phase has been completed. */
  isAnalysisSubprocessCompleted(phaseId: string): boolean {
    if (this.isPhaseCompleted('product_analysis')) {
      return true;
    }
    const completedPhases = this.status?.analysis_completed_phases ?? [];
    return completedPhases.includes(phaseId);
  }

  /** Check if a product analysis subprocess phase is currently active. */
  isAnalysisSubprocessCurrent(phaseId: string): boolean {
    if (this.isPhaseCompleted('product_analysis')) {
      return false;
    }
    return this.status?.analysis_subprocess === phaseId;
  }

  /** Check if a product analysis subprocess phase is pending (not started). */
  isAnalysisSubprocessPending(phaseId: string): boolean {
    return !this.isAnalysisSubprocessCompleted(phaseId) && !this.isAnalysisSubprocessCurrent(phaseId);
  }

  /** Check if we should show the product analysis subprocess section. */
  showProductAnalysisSubprocess(): boolean {
    return this.status?.phase === 'product_analysis' && !!this.status?.analysis_subprocess;
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

  /** Normalize microtask phase ID for comparison (handles Frontend V2's simpler phase names). */
  private normalizeMicrotaskPhase(phase: string | undefined): string | undefined {
    if (!phase) return undefined;
    if (phase === 'review') return 'code_review';
    if (phase === 'problem_solving') return 'code_review';
    return phase;
  }

  /** Check if a microtask phase has been completed for a team. */
  isMicrotaskPhaseCompleted(teamId: string, phaseId: string): boolean {
    const rawPhase = this.status?.team_progress?.[teamId]?.current_microtask_phase;
    const currentPhase = this.normalizeMicrotaskPhase(rawPhase);
    if (!currentPhase) return false;
    if (rawPhase === 'completed') return true;
    const phaseOrder = MICROTASK_PHASES.map(p => p.id);
    const currentIdx = phaseOrder.indexOf(currentPhase);
    const targetIdx = phaseOrder.indexOf(phaseId);
    return currentIdx > targetIdx && targetIdx >= 0;
  }

  /** Check if a microtask phase is currently active for a team. */
  isMicrotaskPhaseCurrent(teamId: string, phaseId: string): boolean {
    const rawPhase = this.status?.team_progress?.[teamId]?.current_microtask_phase;
    const currentPhase = this.normalizeMicrotaskPhase(rawPhase);
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

  // ---------------------------------------------------------------------------
  // Progress Tree View
  // ---------------------------------------------------------------------------

  /** Build the flattened progress tree for the tree view card. */
  buildProgressTree(): FlatTreeNode[] {
    if (!this.status) return [];

    const nodes: FlatTreeNode[] = [];
    const parentIsLast: boolean[] = [];

    // Root node - Job
    nodes.push({
      id: 'job',
      label: this.status.repo_path || 'Job',
      icon: 'folder',
      status: this.getJobRootStatus(),
      level: 0,
      isLast: true,
      parentIsLast: [],
    });

    // Main phases
    const mainPhases = this.ALL_PHASES;
    mainPhases.forEach((phase, phaseIdx) => {
      const isLastPhase = phaseIdx === mainPhases.length - 1;
      const phaseStatus = this.getPhaseStatus(phase.id);

      nodes.push({
        id: `phase-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status: phaseStatus,
        level: 1,
        isLast: isLastPhase,
        parentIsLast: [true],
      });

      // Add sub-phases based on the phase type
      if (phase.id === 'product_analysis') {
        this.addProductAnalysisSubtree(nodes, isLastPhase);
      } else if (phase.id === 'planning') {
        this.addPlanningSubtree(nodes, isLastPhase);
      } else if (phase.id === 'execution') {
        this.addExecutionSubtree(nodes, isLastPhase);
      }
    });

    return nodes;
  }

  private getJobRootStatus(): 'completed' | 'current' | 'pending' {
    if (this.status?.status === 'completed') return 'completed';
    if (this.status?.status === 'running') return 'current';
    return 'pending';
  }

  private getPhaseStatus(phaseId: string): 'completed' | 'current' | 'pending' {
    if (this.isPhaseCompleted(phaseId)) return 'completed';
    if (this.isCurrentPhase(phaseId)) return 'current';
    return 'pending';
  }

  private addProductAnalysisSubtree(nodes: FlatTreeNode[], parentIsLast: boolean): void {
    const phases = PRODUCT_ANALYSIS_PHASES;
    const productAnalysisCompleted = this.isPhaseCompleted('product_analysis');
    phases.forEach((phase, idx) => {
      const isLast = idx === phases.length - 1;
      let status: 'completed' | 'current' | 'pending' = 'pending';
      if (productAnalysisCompleted || this.isAnalysisSubprocessCompleted(phase.id)) {
        status = 'completed';
      } else if (this.isAnalysisSubprocessCurrent(phase.id)) {
        status = 'current';
      }

      nodes.push({
        id: `analysis-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status,
        level: 2,
        isLast,
        parentIsLast: [true, parentIsLast],
      });
    });
  }

  private addPlanningSubtree(nodes: FlatTreeNode[], parentIsLast: boolean): void {
    const phases = PLANNING_V2_PHASES;
    phases.forEach((phase, idx) => {
      const isLast = idx === phases.length - 1;
      let status: 'completed' | 'current' | 'pending' = 'pending';
      if (this.isPlanningSubprocessCompleted(phase.id)) {
        status = 'completed';
      } else if (this.isPlanningSubprocessCurrent(phase.id)) {
        status = 'current';
      }

      nodes.push({
        id: `planning-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status,
        level: 2,
        isLast,
        parentIsLast: [true, parentIsLast],
      });
    });
  }

  private addExecutionSubtree(nodes: FlatTreeNode[], parentIsLast: boolean): void {
    const teams = this.getExecutionTeams();
    teams.forEach((team, teamIdx) => {
      const isLastTeam = teamIdx === teams.length - 1;
      const teamStatus = this.getTeamStatus(team.teamId);
      const progressDetail = team.progress.progress != null ? `${team.progress.progress}%` : undefined;

      nodes.push({
        id: `team-${team.teamId}`,
        label: team.label,
        icon: 'groups',
        status: teamStatus,
        level: 2,
        detail: progressDetail,
        isLast: isLastTeam,
        parentIsLast: [true, parentIsLast],
      });

      // Add current task if exists
      const taskTitle = this.getTaskTitle(team.teamId);
      if (taskTitle) {
        nodes.push({
          id: `team-${team.teamId}-task`,
          label: `Task: ${taskTitle}`,
          icon: 'assignment',
          status: 'current',
          level: 3,
          isLast: false,
          parentIsLast: [true, parentIsLast, isLastTeam],
        });

        // Add current microtask if exists
        const progress = team.progress;
        if (progress.current_microtask) {
          const microtaskDetail = progress.current_microtask_index != null && progress.microtasks_total != null
            ? `(${progress.current_microtask_index}/${progress.microtasks_total})`
            : undefined;

          nodes.push({
            id: `team-${team.teamId}-microtask`,
            label: progress.current_microtask,
            icon: 'code',
            status: 'current',
            level: 4,
            detail: microtaskDetail,
            isLast: true,
            parentIsLast: [true, parentIsLast, isLastTeam, false],
          });

          // Add microtask phases
          if (this.showMicrotaskPhases(team.teamId)) {
            this.addMicrotaskPhasesSubtree(nodes, team.teamId, [true, parentIsLast, isLastTeam, false, true]);
          }
        }
      }

      // Add team phases (Setup, Planning, Execution, Documentation, Deliver)
      this.addTeamPhasesSubtree(nodes, team.teamId, [true, parentIsLast, isLastTeam]);
    });
  }

  private getTeamStatus(teamId: string): 'completed' | 'current' | 'pending' {
    const progress = this.status?.team_progress?.[teamId];
    if (!progress) return 'pending';
    if (progress.current_phase === 'deliver' && progress.progress === 100) return 'completed';
    if (progress.current_phase) return 'current';
    return 'pending';
  }

  private addMicrotaskPhasesSubtree(nodes: FlatTreeNode[], teamId: string, parentIsLastArr: boolean[]): void {
    const phases = MICROTASK_PHASES;
    phases.forEach((phase, idx) => {
      const isLast = idx === phases.length - 1;
      let status: 'completed' | 'current' | 'pending' = 'pending';
      if (this.isMicrotaskPhaseCompleted(teamId, phase.id)) {
        status = 'completed';
      } else if (this.isMicrotaskPhaseCurrent(teamId, phase.id)) {
        status = 'current';
      }

      nodes.push({
        id: `team-${teamId}-microtask-phase-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status,
        level: 5,
        isLast,
        parentIsLast: parentIsLastArr,
      });
    });
  }

  private addTeamPhasesSubtree(nodes: FlatTreeNode[], teamId: string, parentIsLastArr: boolean[]): void {
    const phases = CODE_TEAM_PHASES;
    phases.forEach((phase, idx) => {
      const isLast = idx === phases.length - 1;
      let status: 'completed' | 'current' | 'pending' = 'pending';
      if (this.isCodeTeamPhaseCompleted(teamId, phase.id)) {
        status = 'completed';
      } else if (this.isCodeTeamPhaseCurrent(teamId, phase.id)) {
        status = 'current';
      }

      nodes.push({
        id: `team-${teamId}-phase-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status,
        level: 3,
        isLast,
        parentIsLast: parentIsLastArr,
      });
    });
  }

  /** Get the tree connector class for a node based on its position. */
  getTreeConnectorClass(node: FlatTreeNode): string {
    if (node.level === 0) return '';
    return node.isLast ? 'tree-connector-last' : 'tree-connector-mid';
  }

  // ---------------------------------------------------------------------------
  // DAG Tree View (Visual Graph Layout)
  // ---------------------------------------------------------------------------

  /** Build the hierarchical DAG tree for the visual graph view.
   * Returns an array of main phases (no root node).
   */
  buildDAGTree(): DAGNode[] {
    if (!this.status) return [];

    const phases: DAGNode[] = [];

    // Build each main phase with its sub-phases
    for (const phase of this.ALL_PHASES) {
      const phaseNode: DAGNode = {
        id: `phase-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status: this.getPhaseStatus(phase.id),
        children: [],
      };

      // Add sub-phases based on phase type
      if (phase.id === 'product_analysis') {
        phaseNode.children = this.buildProductAnalysisDAGChildren();
      } else if (phase.id === 'planning') {
        phaseNode.children = this.buildPlanningDAGChildren();
      } else if (phase.id === 'execution') {
        phaseNode.children = this.buildExecutionDAGChildren();
      }
      // 'completed' phase has no children

      phases.push(phaseNode);
    }

    return phases;
  }

  private buildProductAnalysisDAGChildren(): DAGNode[] {
    const productAnalysisCompleted = this.isPhaseCompleted('product_analysis');
    return PRODUCT_ANALYSIS_PHASES.map(phase => {
      let status: 'completed' | 'current' | 'pending' = 'pending';
      if (productAnalysisCompleted || this.isAnalysisSubprocessCompleted(phase.id)) {
        status = 'completed';
      } else if (this.isAnalysisSubprocessCurrent(phase.id)) {
        status = 'current';
      }
      return {
        id: `analysis-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status,
      };
    });
  }

  private buildPlanningDAGChildren(): DAGNode[] {
    return PLANNING_V2_PHASES.map(phase => {
      let status: 'completed' | 'current' | 'pending' = 'pending';
      if (this.isPlanningSubprocessCompleted(phase.id)) {
        status = 'completed';
      } else if (this.isPlanningSubprocessCurrent(phase.id)) {
        status = 'current';
      }
      return {
        id: `planning-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status,
      };
    });
  }

  private buildExecutionDAGChildren(): DAGNode[] {
    const teams = this.getExecutionTeams();
    return teams.map(team => {
      const teamNode: DAGNode = {
        id: `team-${team.teamId}`,
        label: team.label,
        icon: 'groups',
        status: this.getTeamStatus(team.teamId),
        detail: team.progress.progress != null ? `${team.progress.progress}%` : undefined,
        children: [],
      };

      // Add team phases as children
      teamNode.children = this.buildTeamPhasesDAGChildren(team.teamId);

      return teamNode;
    });
  }

  private buildTeamPhasesDAGChildren(teamId: string): DAGNode[] {
    return CODE_TEAM_PHASES.map(phase => {
      let status: 'completed' | 'current' | 'pending' = 'pending';
      if (this.isCodeTeamPhaseCompleted(teamId, phase.id)) {
        status = 'completed';
      } else if (this.isCodeTeamPhaseCurrent(teamId, phase.id)) {
        status = 'current';
      }

      const phaseNode: DAGNode = {
        id: `team-${teamId}-phase-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status,
      };

      // If this is the execution phase and there's a current microtask, add microtask phases
      if (phase.id === 'execution' && this.showMicrotaskPhases(teamId)) {
        phaseNode.children = this.buildMicrotaskPhasesDAGChildren(teamId);
      }

      return phaseNode;
    });
  }

  private buildMicrotaskPhasesDAGChildren(teamId: string): DAGNode[] {
    return MICROTASK_PHASES.map(phase => {
      let status: 'completed' | 'current' | 'pending' = 'pending';
      if (this.isMicrotaskPhaseCompleted(teamId, phase.id)) {
        status = 'completed';
      } else if (this.isMicrotaskPhaseCurrent(teamId, phase.id)) {
        status = 'current';
      }
      return {
        id: `team-${teamId}-microtask-${phase.id}`,
        label: phase.label,
        icon: phase.icon,
        status,
      };
    });
  }

  // ---------------------------------------------------------------------------
  // Work Breakdown Tree (initiative/epic/task/subtask)
  // ---------------------------------------------------------------------------

  private buildWorkTreeRows(status: JobStatusResponse): FlatWorkTreeNode[] {
    const root = this.buildWorkBreakdownTree(status);
    return this.flattenWorkTree(root);
  }

  private buildWorkBreakdownTree(status: JobStatusResponse): WorkTreeNode {
    const root: WorkTreeNode = {
      id: status.job_id || 'project-root',
      label: status.requirements_title || status.repo_path || 'Project Root',
      level: 'root',
      status: this.getRootWorkStatus(status.status),
      children: [],
    };

    const taskIds = status.task_ids ?? [];
    const taskStates = status.task_states ?? {};

    if (!taskIds.length) {
      return root;
    }

    // Use planning hierarchy data if available (new approach)
    if (status.planning_hierarchy) {
      return this.buildTreeFromHierarchy(root, status.planning_hierarchy, taskIds, taskStates);
    }

    // Fallback to legacy text-pattern matching for older jobs without hierarchy data
    return this.buildTreeFromLegacyClassification(root, taskIds, taskStates);
  }

  /**
   * Build work tree using proper planning hierarchy data from the API.
   * Tasks are assigned to their correct initiative/epic/story parents using metadata.
   */
  private buildTreeFromHierarchy(
    root: WorkTreeNode,
    hierarchy: NonNullable<JobStatusResponse['planning_hierarchy']>,
    taskIds: string[],
    taskStates: Record<string, TaskStateEntry>
  ): WorkTreeNode {
    const initiativeNodes = new Map<string, WorkTreeNode>();
    const epicNodes = new Map<string, WorkTreeNode>();
    const storyNodes = new Map<string, WorkTreeNode>();

    // Create initiative nodes
    for (const init of hierarchy.initiatives) {
      const node: WorkTreeNode = {
        id: init.id,
        label: init.title,
        level: 'initiative',
        status: 'pending',
        children: [],
      };
      initiativeNodes.set(init.id, node);
    }

    // Create epic nodes and attach to initiatives
    for (const epic of hierarchy.epics) {
      const node: WorkTreeNode = {
        id: epic.id,
        label: epic.title,
        level: 'epic',
        status: 'pending',
        children: [],
      };
      epicNodes.set(epic.id, node);

      const parentInit = initiativeNodes.get(epic.initiative_id);
      if (parentInit) {
        parentInit.children.push(node);
      }
    }

    // Create story nodes and attach to epics
    for (const story of hierarchy.stories) {
      const node: WorkTreeNode = {
        id: story.id,
        label: story.title,
        level: 'task',
        status: 'pending',
        children: [],
      };
      storyNodes.set(story.id, node);

      const parentEpic = epicNodes.get(story.epic_id);
      if (parentEpic) {
        parentEpic.children.push(node);
      }
    }

    // Assign tasks to their parent stories (or epics if no story)
    const orphanTasks: WorkTreeNode[] = [];
    for (const taskId of taskIds) {
      const state = taskStates[taskId];
      if (!state) continue;

      const taskNode: WorkTreeNode = {
        id: taskId,
        label: state.title || taskId,
        level: 'subtask',
        status: this.mapWorkItemStatus(state.status),
        children: [],
      };

      // Try to attach to story, then epic, then orphan
      if (state.story_id && storyNodes.has(state.story_id)) {
        storyNodes.get(state.story_id)!.children.push(taskNode);
      } else if (state.epic_id && epicNodes.has(state.epic_id)) {
        epicNodes.get(state.epic_id)!.children.push(taskNode);
      } else if (state.initiative_id && initiativeNodes.has(state.initiative_id)) {
        // Attach directly to initiative if no epic/story
        initiativeNodes.get(state.initiative_id)!.children.push(taskNode);
      } else {
        orphanTasks.push(taskNode);
      }
    }

    // Handle orphan tasks with fallback
    if (orphanTasks.length > 0) {
      const fallbackInitiative: WorkTreeNode = {
        id: 'initiative-uncategorized',
        label: 'Uncategorized Initiative',
        level: 'initiative',
        status: 'pending',
        children: [],
      };
      const fallbackEpic: WorkTreeNode = {
        id: 'epic-uncategorized',
        label: 'General Epic',
        level: 'epic',
        status: 'pending',
        children: orphanTasks,
      };
      fallbackInitiative.children.push(fallbackEpic);
      initiativeNodes.set(fallbackInitiative.id, fallbackInitiative);
    }

    // Derive statuses bottom-up
    for (const story of storyNodes.values()) {
      story.status = this.deriveStatusFromChildren(story.status, story.children);
    }
    for (const epic of epicNodes.values()) {
      epic.status = this.deriveStatusFromChildren(epic.status, epic.children);
    }
    for (const initiative of initiativeNodes.values()) {
      initiative.status = this.deriveStatusFromChildren(initiative.status, initiative.children);
    }

    root.children = Array.from(initiativeNodes.values());
    root.status = this.deriveStatusFromChildren(root.status, root.children);
    return root;
  }

  /**
   * Legacy fallback: build work tree using text pattern matching.
   * Used for older jobs that don't have planning_hierarchy data.
   */
  private buildTreeFromLegacyClassification(
    root: WorkTreeNode,
    taskIds: string[],
    taskStates: Record<string, TaskStateEntry>
  ): WorkTreeNode {
    const initiatives: WorkTreeNode[] = [];
    const initiativeById = new Map<string, WorkTreeNode>();
    const epicsByParent = new Map<string, WorkTreeNode[]>();
    const tasksByParent = new Map<string, WorkTreeNode[]>();
    const subtasksByParent = new Map<string, WorkTreeNode[]>();

    const getOrCreateInitiative = (id: string, label: string, status: WorkItemStatus): WorkTreeNode => {
      const existing = initiativeById.get(id);
      if (existing) {
        existing.status = this.mergeStatuses(existing.status, status);
        return existing;
      }
      const node: WorkTreeNode = { id, label, level: 'initiative', status, children: [] };
      initiativeById.set(id, node);
      initiatives.push(node);
      return node;
    };

    let fallbackInitiative: WorkTreeNode | null = null;
    const getFallbackInitiative = (): WorkTreeNode => {
      if (!fallbackInitiative) {
        fallbackInitiative = getOrCreateInitiative('initiative-uncategorized', 'Uncategorized Initiative', 'pending');
      }
      return fallbackInitiative;
    };
    const fallbackEpic: WorkTreeNode = {
      id: 'epic-uncategorized',
      label: 'General Epic',
      level: 'epic',
      status: 'pending',
      children: [],
    };
    const fallbackTask: WorkTreeNode = {
      id: 'task-uncategorized',
      label: 'General Task Group',
      level: 'task',
      status: 'pending',
      children: [],
    };

    for (const taskId of taskIds) {
      const state = taskStates[taskId];
      const label = state?.title || taskId;
      const classification = this.classifyWorkItem(label, taskId);
      const status = this.mapWorkItemStatus(state?.status);

      const node: WorkTreeNode = {
        id: taskId,
        label,
        level: classification,
        status,
        children: [],
      };

      if (classification === 'initiative') {
        getOrCreateInitiative(taskId, label, status);
        continue;
      }

      if (classification === 'epic') {
        const parentKey = this.findParentByLevel(state?.dependencies, taskStates, 'initiative') ?? getFallbackInitiative().id;
        const epics = epicsByParent.get(parentKey) ?? [];
        epics.push(node);
        epicsByParent.set(parentKey, epics);
        continue;
      }

      if (classification === 'task') {
        const parentKey = this.findParentByLevel(state?.dependencies, taskStates, 'epic') ?? fallbackEpic.id;
        const tasks = tasksByParent.get(parentKey) ?? [];
        tasks.push(node);
        tasksByParent.set(parentKey, tasks);
        continue;
      }

      const parentKey = this.findParentByLevel(state?.dependencies, taskStates, 'task') ?? fallbackTask.id;
      const subtasks = subtasksByParent.get(parentKey) ?? [];
      subtasks.push(node);
      subtasksByParent.set(parentKey, subtasks);
    }

    // Ensure fallbacks are connected only when needed.
    if (tasksByParent.has(fallbackEpic.id) || subtasksByParent.has(fallbackTask.id)) {
      const fallbackInitiativeId = getFallbackInitiative().id;
      const fallbackEpics = epicsByParent.get(fallbackInitiativeId) ?? [];
      if (!fallbackEpics.some((item) => item.id === fallbackEpic.id)) {
        fallbackEpics.push(fallbackEpic);
        epicsByParent.set(fallbackInitiativeId, fallbackEpics);
      }
    }
    if (subtasksByParent.has(fallbackTask.id)) {
      const fallbackTasks = tasksByParent.get(fallbackEpic.id) ?? [];
      if (!fallbackTasks.some((item) => item.id === fallbackTask.id)) {
        fallbackTasks.push(fallbackTask);
        tasksByParent.set(fallbackEpic.id, fallbackTasks);
      }
    }

    for (const initiative of initiatives) {
      const initiativeEpics = epicsByParent.get(initiative.id) ?? [];
      for (const epic of initiativeEpics) {
        const epicTasks = tasksByParent.get(epic.id) ?? [];
        for (const task of epicTasks) {
          task.children = subtasksByParent.get(task.id) ?? [];
          task.status = this.deriveStatusFromChildren(task.status, task.children);
        }
        epic.children = epicTasks;
        epic.status = this.deriveStatusFromChildren(epic.status, epic.children);
      }
      initiative.children = initiativeEpics;
      initiative.status = this.deriveStatusFromChildren(initiative.status, initiative.children);
    }

    root.children = initiatives;
    root.status = this.deriveStatusFromChildren(root.status, root.children);
    return root;
  }

  private flattenWorkTree(root: WorkTreeNode): FlatWorkTreeNode[] {
    const rows: FlatWorkTreeNode[] = [];
    const visit = (node: WorkTreeNode, depth: number): void => {
      rows.push({
        id: node.id,
        label: node.label,
        level: node.level,
        status: node.status,
        depth,
      });
      for (const child of node.children) {
        visit(child, depth + 1);
      }
    };
    visit(root, 0);
    return rows;
  }

  private classifyWorkItem(label: string, taskId: string): WorkItemLevel {
    const text = `${label} ${taskId}`.toLowerCase();
    if (/(^|\b)(initiative|init)(\b|:)/.test(text)) return 'initiative';
    if (/(^|\b)epic(\b|:)/.test(text)) return 'epic';
    if (/(^|\b)(subtask|microtask)(\b|:)/.test(text)) return 'subtask';
    return 'task';
  }

  private findParentByLevel(
    dependencies: string[] | undefined,
    taskStates: Record<string, TaskStateEntry>,
    targetLevel: WorkItemLevel
  ): string | null {
    if (!dependencies?.length) return null;
    for (const dep of dependencies) {
      const state = taskStates[dep];
      const label = state?.title || dep;
      if (this.classifyWorkItem(label, dep) === targetLevel) {
        return dep;
      }
    }
    return null;
  }

  private mapWorkItemStatus(rawStatus: string | undefined): WorkItemStatus {
    const status = (rawStatus ?? '').toLowerCase();
    if (['completed', 'done', 'success'].includes(status)) return 'completed';
    if (['in_progress', 'running', 'active'].includes(status)) return 'in_progress';
    if (['failed', 'error', 'cancelled'].includes(status)) return 'failed';
    return 'pending';
  }

  private getRootWorkStatus(jobStatus: string | undefined): WorkItemStatus {
    const status = (jobStatus ?? '').toLowerCase();
    if (status === 'completed') return 'completed';
    if (status === 'failed' || status === 'cancelled') return 'failed';
    if (status === 'running') return 'in_progress';
    return 'pending';
  }

  private deriveStatusFromChildren(base: WorkItemStatus, children: WorkTreeNode[]): WorkItemStatus {
    if (!children.length) return base;
    const childStatuses = children.map((item) => item.status);
    if (childStatuses.some((status) => status === 'failed')) return 'failed';
    if (childStatuses.some((status) => status === 'in_progress')) return 'in_progress';
    if (childStatuses.every((status) => status === 'completed')) return 'completed';
    return base === 'completed' ? 'in_progress' : 'pending';
  }

  private mergeStatuses(a: WorkItemStatus, b: WorkItemStatus): WorkItemStatus {
    const order: WorkItemStatus[] = ['pending', 'completed', 'in_progress', 'failed'];
    return order[Math.max(order.indexOf(a), order.indexOf(b))] ?? a;
  }

  workItemStatusIcon(status: WorkItemStatus): string {
    switch (status) {
      case 'completed': return 'check_circle';
      case 'in_progress': return 'autorenew';
      case 'failed': return 'error';
      default: return 'radio_button_unchecked';
    }
  }
}
