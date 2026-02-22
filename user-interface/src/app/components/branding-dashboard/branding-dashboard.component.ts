import { Component, OnDestroy, inject } from '@angular/core';
import { FormBuilder, FormsModule, ReactiveFormsModule, Validators } from '@angular/forms';
import { interval, Subscription, switchMap } from 'rxjs';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { BrandingApiService } from '../../services/branding-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import { ErrorMessageComponent } from '../../shared/error-message/error-message.component';
import { HealthIndicatorComponent } from '../health-indicator/health-indicator.component';
import type { BrandingQuestion, BrandingSessionResponse, RunBrandingTeamRequest } from '../../models';

@Component({
  selector: 'app-branding-dashboard',
  standalone: true,
  imports: [
    FormsModule,
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    LoadingSpinnerComponent,
    ErrorMessageComponent,
    HealthIndicatorComponent,
  ],
  templateUrl: './branding-dashboard.component.html',
  styleUrl: './branding-dashboard.component.scss',
})
export class BrandingDashboardComponent implements OnDestroy {
  private readonly api = inject(BrandingApiService);
  private readonly fb = inject(FormBuilder);

  loading = false;
  error: string | null = null;
  session: BrandingSessionResponse | null = null;
  answers: Record<string, string> = {};
  private pollSub: Subscription | null = null;

  form = this.fb.nonNullable.group({
    company_name: ['', [Validators.required, Validators.minLength(2)]],
    company_description: ['', [Validators.required, Validators.minLength(10)]],
    target_audience: ['', [Validators.required, Validators.minLength(3)]],
    desired_voice: ['clear, confident, human', [Validators.required]],
    values_csv: [''],
    differentiators_csv: [''],
  });

  healthCheck = (): ReturnType<BrandingApiService['health']> => this.api.health();

  ngOnDestroy(): void {
    this.pollSub?.unsubscribe();
  }

  startSession(): void {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    const raw = this.form.getRawValue();
    const request: RunBrandingTeamRequest = {
      company_name: raw.company_name,
      company_description: raw.company_description,
      target_audience: raw.target_audience,
      desired_voice: raw.desired_voice,
      values: raw.values_csv.split(',').map((v) => v.trim()).filter((v) => !!v),
      differentiators: raw.differentiators_csv.split(',').map((v) => v.trim()).filter((v) => !!v),
    };

    this.loading = true;
    this.error = null;
    this.api.createSession(request).subscribe({
      next: (res) => {
        this.session = res;
        this.loading = false;
        this.startPolling();
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to start branding session';
        this.loading = false;
      },
    });
  }

  submitAnswer(question: BrandingQuestion): void {
    if (!this.session) return;
    const answer = (this.answers[question.id] ?? '').trim();
    if (!answer) return;

    this.loading = true;
    this.error = null;
    this.api.answerQuestion(this.session.session_id, question.id, answer).subscribe({
      next: (res) => {
        this.session = res;
        this.answers[question.id] = '';
        this.loading = false;
      },
      error: (err) => {
        this.error = err?.error?.detail ?? err?.message ?? 'Failed to submit answer';
        this.loading = false;
      },
    });
  }

  private startPolling(): void {
    this.pollSub?.unsubscribe();
    if (!this.session?.session_id) {
      return;
    }
    this.pollSub = interval(3000)
      .pipe(switchMap(() => this.api.getSession(this.session!.session_id)))
      .subscribe({
        next: (res) => {
          this.session = res;
          if (res.open_questions.length === 0) {
            this.pollSub?.unsubscribe();
            this.pollSub = null;
          }
        },
        error: () => {
          this.pollSub?.unsubscribe();
          this.pollSub = null;
        },
      });
  }
}
