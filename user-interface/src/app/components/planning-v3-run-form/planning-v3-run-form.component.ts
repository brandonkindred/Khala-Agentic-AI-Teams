import { Component, EventEmitter, Input, Output } from '@angular/core';
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
/**
 * Form for launching a Planning V3 workflow.
 *
 * Collects an optional output folder, client name, initial brief, spec content,
 * and the product-analysis / planning-v2 / market-research toggles, then emits a
 * `PlanningV3RunRequest` via `submitRequest`. Submission is gated on a non-empty
 * initial brief or spec content (matching the backend's "at least one of
 * initial_brief / spec_content" rule); `repo_path` is omitted when blank so the
 * backend resolves a server-side workspace.
 */
export class PlanningV3RunFormComponent {
  @Output() submitRequest = new EventEmitter<PlanningV3RunRequest>();

  /**
   * When true, the form is mid-submission: the button is disabled and
   * `onSubmit` is a no-op. The parent drives this from its request lifecycle so
   * a double-click / repeated Enter cannot emit duplicate requests.
   */
  @Input() submitting = false;

  repoPath = '';
  clientName = '';
  initialBrief = '';
  specContent = '';
  useProductAnalysis = true;
  usePlanningV2 = false;
  useMarketResearch = false;

  /**
   * Whether the form may be submitted.
   * @returns true when `initialBrief` or `specContent` is non-empty after
   * trimming (the backend requires at least one of them).
   */
  get canSubmit(): boolean {
    return !!(this.initialBrief.trim() || this.specContent.trim());
  }

  /**
   * Emit the assembled `PlanningV3RunRequest` on `submitRequest`.
   *
   * Precondition: no-op when `canSubmit` is false or a submission is already in
   * flight (`submitting`). Blank text fields are sent as `undefined` (so the
   * backend applies its defaults / resolves the workspace).
   */
  onSubmit(): void {
    if (!this.canSubmit || this.submitting) return;
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
