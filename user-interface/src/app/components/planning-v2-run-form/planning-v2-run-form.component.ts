import { Component, EventEmitter, Output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import type { PlanningV2RunRequest } from '../../models';

@Component({
  selector: 'app-planning-v2-run-form',
  standalone: true,
  imports: [
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
  ],
  templateUrl: './planning-v2-run-form.component.html',
  styleUrl: './planning-v2-run-form.component.scss',
})
export class PlanningV2RunFormComponent {
  @Output() submitRequest = new EventEmitter<PlanningV2RunRequest>();

  specContent = '';
  repoPath = '';
  inspirationContent = '';

  get canSubmit(): boolean {
    return !!(this.specContent.trim() && this.repoPath.trim());
  }

  onSubmit(): void {
    if (!this.canSubmit) return;
    const request: PlanningV2RunRequest = {
      spec_content: this.specContent.trim(),
      repo_path: this.repoPath.trim(),
      inspiration_content: this.inspirationContent.trim() || undefined,
    };
    this.submitRequest.emit(request);
  }
}
