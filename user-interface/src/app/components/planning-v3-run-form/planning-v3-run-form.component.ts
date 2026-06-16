import { Component, EventEmitter, Output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatCheckboxModule } from '@angular/material/checkbox';
import type { PlanningV3RunRequest } from '../../models';

@Component({
  selector: 'app-planning-v3-run-form',
  standalone: true,
  imports: [
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatCheckboxModule,
  ],
  templateUrl: './planning-v3-run-form.component.html',
  styleUrl: './planning-v3-run-form.component.scss',
})
export class PlanningV3RunFormComponent {
  @Output() submitRequest = new EventEmitter<PlanningV3RunRequest>();

  repoPath = '';
  clientName = '';
  initialBrief = '';
  specContent = '';
  useProductAnalysis = true;
  usePlanningV2 = false;
  useMarketResearch = false;

  get canSubmit(): boolean {
    return !!this.initialBrief.trim();
  }

  onSubmit(): void {
    if (!this.canSubmit) return;
    const request: PlanningV3RunRequest = {
      repo_path: this.repoPath.trim() || undefined,
      client_name: this.clientName.trim() || undefined,
      initial_brief: this.initialBrief.trim() || undefined,
      spec_content: this.specContent.trim() || undefined,
      use_product_analysis: this.useProductAnalysis,
      use_planning_v2: this.usePlanningV2,
      use_market_research: this.useMarketResearch,
    };
    this.submitRequest.emit(request);
  }
}
