import { Component, DestroyRef, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router, RouterModule } from '@angular/router';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { IntegrationDefinition, getIntegrationById } from '../../models/integrations.model';

@Component({
  selector: 'app-integration-config-page',
  standalone: true,
  imports: [
    CommonModule,
    RouterModule,
    ReactiveFormsModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatFormFieldModule,
    MatInputModule,
  ],
  templateUrl: './integration-config-page.component.html',
  styleUrl: './integration-config-page.component.scss',
})
export class IntegrationConfigPageComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly fb = inject(FormBuilder);
  private readonly destroyRef = inject(DestroyRef);

  integration?: IntegrationDefinition;
  form: FormGroup = this.fb.group({});
  submitted = false;

  ngOnInit(): void {
    this.route.paramMap
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe((params) => {
        this.loadIntegration(params.get('integrationId'));
      });
  }

  onSave(): void {
    if (!this.integration || this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    this.submitted = true;
  }

  private loadIntegration(integrationId: string | null): void {
    if (!integrationId) {
      this.router.navigate(['/integrations']);
      return;
    }

    this.integration = getIntegrationById(integrationId);
    if (!this.integration) {
      this.router.navigate(['/integrations']);
      return;
    }

    this.submitted = false;

    const controls: Record<string, unknown> = {};
    for (const field of this.integration.fields) {
      controls[field.key] = ['', field.required ? Validators.required : []];
    }
    this.form = this.fb.group(controls);
  }
}
