import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatChipsModule } from '@angular/material/chips';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatDividerModule } from '@angular/material/divider';
import { MatSliderModule } from '@angular/material/slider';
import { MatTooltipModule } from '@angular/material/tooltip';
import { StudioGridApiService } from '../../services/studio-grid-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import type { FindAgentsResponse, AgentInfo } from '../../models';

@Component({
  selector: 'app-agent-finder',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatChipsModule,
    MatButtonModule,
    MatIconModule,
    MatDividerModule,
    MatSliderModule,
    MatTooltipModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
  ],
  templateUrl: './agent-finder.component.html',
  styleUrl: './agent-finder.component.scss',
})
export class AgentFinderComponent {
  private readonly api = inject(StudioGridApiService);

  // Form state
  problem = '';
  newSkill = '';
  skills: string[] = [];
  limit = 5;

  // Async state
  loading = false;
  error: string | null = null;

  // Results
  result: FindAgentsResponse | null = null;

  addSkill(): void {
    const skill = this.newSkill.trim();
    if (skill && !this.skills.includes(skill)) {
      this.skills = [...this.skills, skill];
    }
    this.newSkill = '';
  }

  removeSkill(index: number): void {
    this.skills = this.skills.filter((_, i) => i !== index);
  }

  onSkillKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      this.addSkill();
    }
  }

  onFind(): void {
    if (!this.problem.trim()) return;
    this.loading = true;
    this.error = null;
    this.result = null;

    this.api.findAgents({ problem: this.problem, skills: this.skills, limit: this.limit }).subscribe({
      next: (res) => {
        this.result = res;
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to find agents';
        this.loading = false;
      },
    });
  }

  onClear(): void {
    this.problem = '';
    this.skills = [];
    this.newSkill = '';
    this.limit = 5;
    this.result = null;
    this.error = null;
  }

  shortId(agentId: string): string {
    return agentId.length > 24 ? agentId.slice(0, 24) + '…' : agentId;
  }

  totalCapabilities(agent: AgentInfo): number {
    return (
      (agent.skills?.length ?? 0) +
      (agent.tools?.length ?? 0) +
      (agent.keywords?.length ?? 0) +
      (agent.actions?.length ?? 0)
    );
  }
}
