import { Component, Input, output } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatCardModule } from '@angular/material/card';
import type { RePlanWithClarificationsRequest } from '../../models';

@Component({
  selector: 'app-re-plan-with-clarifications',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
  ],
  templateUrl: './re-plan-with-clarifications.component.html',
  styleUrl: './re-plan-with-clarifications.component.scss',
})
export class RePlanWithClarificationsComponent {
  @Input() jobId: string | null = null;
  @Input() disabled = false;

  readonly submitRequest = output<RePlanWithClarificationsRequest>();

  form: FormGroup;

  constructor(private readonly fb: FormBuilder) {
    this.form = this.fb.nonNullable.group({
      clarification_session_id: ['', Validators.required],
    });
  }

  onSubmit(): void {
    if (this.form.valid && this.jobId) {
      this.submitRequest.emit(this.form.getRawValue());
    }
  }
}
