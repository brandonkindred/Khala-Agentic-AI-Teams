import { Component, EventEmitter, Output, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { ProductAnalysisRunResponse } from '../../models';

/** Project name: no spaces, only letters, numbers, hyphen, underscore */
const PROJECT_NAME_PATTERN = /^[a-zA-Z0-9_-]+$/;
const ALLOWED_EXTENSIONS = ['.txt', '.md'];
const MAX_FILE_SIZE_BYTES = 500_000;

@Component({
  selector: 'app-start-from-spec-form',
  standalone: true,
  imports: [
    FormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
  ],
  templateUrl: './start-from-spec-form.component.html',
  styleUrl: './start-from-spec-form.component.scss',
})
export class StartFromSpecFormComponent {
  @Output() submitSuccess = new EventEmitter<ProductAnalysisRunResponse>();
  @Output() submitError = new EventEmitter<string>();

  projectName = '';
  selectedFile: File | null = null;
  projectNameError = '';
  fileError = '';
  loading = false;

  private readonly api = inject(SoftwareEngineeringApiService);

  get isProjectNameValid(): boolean {
    if (!this.projectName.trim()) return false;
    return PROJECT_NAME_PATTERN.test(this.projectName.trim());
  }

  get isFormValid(): boolean {
    return this.isProjectNameValid && this.selectedFile !== null && !this.loading;
  }

  onProjectNameBlur(): void {
    this.updateProjectNameError();
  }

  onProjectNameInput(): void {
    this.updateProjectNameError();
  }

  private updateProjectNameError(): void {
    const v = this.projectName.trim();
    if (!v) {
      this.projectNameError = '';
      return;
    }
    if (PROJECT_NAME_PATTERN.test(v)) {
      this.projectNameError = '';
    } else {
      this.projectNameError = 'Use only letters, numbers, hyphens, and underscores (no spaces).';
    }
  }

  onFileChange(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    this.fileError = '';
    this.selectedFile = null;
    if (!file) return;
    const ext = '.' + (file.name.split('.').pop() ?? '').toLowerCase();
    if (!ALLOWED_EXTENSIONS.includes(ext)) {
      this.fileError = 'Please select a .txt or .md file.';
      input.value = '';
      return;
    }
    if (file.size > MAX_FILE_SIZE_BYTES) {
      this.fileError = `File is too large (max ${MAX_FILE_SIZE_BYTES / 1024} KB).`;
      input.value = '';
      return;
    }
    this.selectedFile = file;
  }

  submit(): void {
    this.updateProjectNameError();
    if (!this.isProjectNameValid) {
      this.projectNameError = this.projectNameError || 'Enter a valid project name (no spaces).';
      return;
    }
    if (!this.selectedFile) {
      this.fileError = 'Please select a .txt or .md spec file.';
      return;
    }
    this.loading = true;
    this.submitError.emit('');
    const reader = new FileReader();
    reader.onload = () => {
      const specContent = typeof reader.result === 'string' ? reader.result : '';
      if (!specContent.trim()) {
        this.fileError = 'File is empty.';
        this.loading = false;
        return;
      }
      this.api
        .startProductAnalysisFromSpec(this.projectName.trim(), specContent)
        .subscribe({
          next: (res) => {
            this.loading = false;
            this.submitSuccess.emit(res);
          },
          error: (err) => {
            this.loading = false;
            const msg = err?.error?.detail ?? err?.message ?? 'Failed to start project.';
            this.submitError.emit(msg);
          },
        });
    };
    reader.onerror = () => {
      this.loading = false;
      this.fileError = 'Failed to read file.';
    };
    reader.readAsText(this.selectedFile, 'utf-8');
  }
}
