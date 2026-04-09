import {
  Component,
  Input,
  Output,
  EventEmitter,
  OnChanges,
  SimpleChanges,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatChipsModule } from '@angular/material/chips';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { AgenticTeamApiService } from '../../services/agentic-team-api.service';
import type {
  ProcessStep,
  ProcessStepAgent,
  StepType,
  RecommendedAgent,
  ProcessDefinition,
} from '../../models';

@Component({
  selector: 'app-flow-step-editor',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    MatChipsModule,
    MatTooltipModule,
    MatDividerModule,
    MatProgressBarModule,
  ],
  templateUrl: './flow-step-editor.component.html',
  styleUrl: './flow-step-editor.component.scss',
})
export class FlowStepEditorComponent implements OnChanges {
  @Input() step!: ProcessStep;
  @Input() process!: ProcessDefinition;
  @Input() allSteps: ProcessStep[] = [];

  @Output() stepUpdated = new EventEmitter<ProcessStep>();
  @Output() stepDeleted = new EventEmitter<string>();
  @Output() closed = new EventEmitter<void>();

  private readonly agenticApi = inject(AgenticTeamApiService);
  private readonly fb = inject(FormBuilder);

  stepTypes: { value: StepType; label: string }[] = [
    { value: 'action', label: 'Action' },
    { value: 'decision', label: 'Decision' },
    { value: 'parallel_split', label: 'Parallel Split' },
    { value: 'parallel_join', label: 'Parallel Join' },
    { value: 'wait', label: 'Wait' },
    { value: 'subprocess', label: 'Subprocess' },
  ];

  form = this.fb.nonNullable.group({
    name: ['', [Validators.required, Validators.minLength(1)]],
    description: [''],
    step_type: ['action' as StepType],
    condition: [''],
  });

  // Recommendations
  recommendations = signal<RecommendedAgent[]>([]);
  recommendationsLoading = signal(false);

  // Assigned agents (local working copy)
  assignedAgents = signal<ProcessStepAgent[]>([]);

  // Next steps selection
  selectedNextSteps = signal<string[]>([]);

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['step'] && this.step) {
      this.form.patchValue({
        name: this.step.name,
        description: this.step.description,
        step_type: this.step.step_type,
        condition: this.step.condition ?? '',
      });
      this.assignedAgents.set([...this.step.agents]);
      this.selectedNextSteps.set([...this.step.next_steps]);
      this.loadRecommendations();
    }
  }

  private loadRecommendations(): void {
    if (!this.process?.process_id || !this.step?.step_id) return;
    this.recommendationsLoading.set(true);
    this.agenticApi
      .recommendAgentsForStep(this.process.process_id, this.step.step_id)
      .subscribe({
        next: (res) => {
          this.recommendations.set(res.recommended_agents);
          this.recommendationsLoading.set(false);
        },
        error: () => {
          this.recommendations.set([]);
          this.recommendationsLoading.set(false);
        },
      });
  }

  assignAgent(agentName: string, role: string): void {
    const current = this.assignedAgents();
    if (current.some((a) => a.agent_name === agentName)) return;
    this.assignedAgents.set([...current, { agent_name: agentName, role }]);
    this.emitUpdate();
  }

  removeAgent(agentName: string): void {
    this.assignedAgents.update((agents) => agents.filter((a) => a.agent_name !== agentName));
    this.emitUpdate();
  }

  toggleNextStep(stepId: string): void {
    this.selectedNextSteps.update((steps) =>
      steps.includes(stepId) ? steps.filter((s) => s !== stepId) : [...steps, stepId],
    );
    this.emitUpdate();
  }

  onSave(): void {
    this.emitUpdate();
  }

  onDelete(): void {
    this.stepDeleted.emit(this.step.step_id);
  }

  onClose(): void {
    this.closed.emit();
  }

  private emitUpdate(): void {
    const formValue = this.form.getRawValue();
    const updated: ProcessStep = {
      ...this.step,
      name: formValue.name,
      description: formValue.description,
      step_type: formValue.step_type,
      condition: formValue.condition || null,
      agents: this.assignedAgents(),
      next_steps: this.selectedNextSteps(),
    };
    this.stepUpdated.emit(updated);
  }

  isAgentAssigned(agentName: string): boolean {
    return this.assignedAgents().some((a) => a.agent_name === agentName);
  }

  otherSteps(): ProcessStep[] {
    return this.allSteps.filter((s) => s.step_id !== this.step.step_id);
  }

  isNextStep(stepId: string): boolean {
    return this.selectedNextSteps().includes(stepId);
  }
}
