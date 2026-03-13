import { Component, inject, output, signal } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatCardModule } from '@angular/material/card';
import { MatIconModule } from '@angular/material/icon';
import { CommonModule } from '@angular/common';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { RunTeamResponse } from '../../models';

@Component({
  selector: 'app-run-team-form',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule, MatCardModule,
            MatFormFieldModule, MatInputModule, MatButtonModule, MatIconModule],
  templateUrl: './run-team-form.component.html',
  styleUrl: './run-team-form.component.scss',
})
export class RunTeamFormComponent {
  readonly submitRequest = output<RunTeamResponse>();

  form: FormGroup;
  selectedFile: File | null = null;
  readonly selectedFileName = signal('');
  readonly isSubmitting = signal(false);
  readonly uploadError = signal('');

  private readonly api = inject(SoftwareEngineeringApiService);

  constructor(private readonly fb: FormBuilder) {
    this.form = this.fb.nonNullable.group({
      project_name: ['', [Validators.required, Validators.minLength(1), Validators.maxLength(200)]],
    });
  }

  onFileSelected(event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0] ?? null;
    this.selectedFile = file;
    this.selectedFileName.set(file?.name ?? '');
    this.uploadError.set('');
  }

  get canSubmit(): boolean {
    return this.form.valid && this.selectedFile !== null && !this.isSubmitting();
  }

  onSubmit(): void {
    if (!this.canSubmit) return;
    this.isSubmitting.set(true);
    this.uploadError.set('');
    this.api.runTeamFromUpload(this.form.getRawValue().project_name, this.selectedFile!).subscribe({
      next: (res) => { this.isSubmitting.set(false); this.submitRequest.emit(res); },
      error: (err) => {
        this.isSubmitting.set(false);
        const d = err?.error?.detail ?? err?.message ?? 'Upload failed';
        this.uploadError.set(typeof d === 'string' ? d : JSON.stringify(d));
      },
    });
  }
}
