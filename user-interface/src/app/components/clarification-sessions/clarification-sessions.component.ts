import { Component, output } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatCardModule } from '@angular/material/card';
import type { ClarificationCreateRequest } from '../../models';

@Component({
  selector: 'app-clarification-sessions',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
  ],
  templateUrl: './clarification-sessions.component.html',
  styleUrl: './clarification-sessions.component.scss',
})
export class ClarificationSessionsComponent {
  readonly submitRequest = output<ClarificationCreateRequest>();

  form: FormGroup;

  constructor(private readonly fb: FormBuilder) {
    this.form = this.fb.nonNullable.group({
      spec_text: ['', [Validators.required, Validators.minLength(10)]],
    });
  }

  onSubmit(): void {
    if (this.form.valid) {
      this.submitRequest.emit(this.form.getRawValue());
    }
  }
}
