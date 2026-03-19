import { Component, Input, OnInit } from '@angular/core';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatCardModule } from '@angular/material/card';
import { SoftwareEngineeringApiService } from '../../services/software-engineering-api.service';
import type { ClarificationSessionResponse } from '../../models';

@Component({
  selector: 'app-clarification-chat',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
  ],
  templateUrl: './clarification-chat.component.html',
  styleUrl: './clarification-chat.component.scss',
})
export class ClarificationChatComponent implements OnInit {
  @Input() sessionId: string | null = null;

  session: ClarificationSessionResponse | null = null;
  form: FormGroup;

  constructor(
    private readonly fb: FormBuilder,
    private readonly api: SoftwareEngineeringApiService
  ) {
    this.form = this.fb.nonNullable.group({
      message: ['', [Validators.required, Validators.minLength(1)]],
    });
  }

  ngOnInit(): void {
    if (this.sessionId) {
      this.api.getClarificationSession(this.sessionId).subscribe({
        next: (res) => (this.session = res),
      });
    }
  }

  onSubmit(): void {
    if (this.form.valid && this.sessionId) {
      const msg = this.form.getRawValue().message;
      this.form.reset();
      this.api.sendClarificationMessage(this.sessionId, { message: msg }).subscribe({
        next: (res) => {
          this.session = {
            ...this.session!,
            open_questions: res.open_questions,
            assumptions: res.assumptions,
            refined_spec: res.refined_spec,
            status: res.done_clarifying ? 'completed' : this.session!.status,
            turns: [
              ...(this.session?.turns ?? []),
              { role: 'user', message: msg },
              { role: 'assistant', message: res.assistant_message },
            ],
          };
        },
      });
    }
  }
}
