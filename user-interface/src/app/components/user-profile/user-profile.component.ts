import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { RouterLink } from '@angular/router';
import { DashboardShellComponent } from '../../shared/dashboard-shell/dashboard-shell.component';
import { UserProfileApiService } from '../../services/user-profile-api.service';
import type { Association, ProfileIntegration } from '../../models/user-profile.model';

/** A display group of associations sharing one artifact type. */
interface AssociationGroup {
  type: string;
  label: string;
  icon: string;
  items: Association[];
}

const ARTIFACT_GROUPS: { type: string; label: string; icon: string }[] = [
  { type: 'brand', label: 'Brands', icon: 'palette' },
  { type: 'blog_post', label: 'Blog Posts', icon: 'article' },
  { type: 'project', label: 'Projects', icon: 'terminal' },
  { type: 'agentic_team', label: 'Agentic Teams', icon: 'groups' },
];

/**
 * User Profile page: review/update the single profile and view the artifacts
 * (brands, blog posts, projects, agentic teams) and integrations linked to it.
 * Reached via the "User Profile" icon in the Settings nav group.
 */
@Component({
  selector: 'app-user-profile',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    RouterLink,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    DashboardShellComponent,
  ],
  templateUrl: './user-profile.component.html',
  styleUrl: './user-profile.component.scss',
})
export class UserProfileComponent implements OnInit {
  private readonly api = inject(UserProfileApiService);
  private readonly fb = inject(FormBuilder);

  loading = false;
  saving = false;
  error: string | null = null;
  success: string | null = null;

  groups: AssociationGroup[] = [];
  integrations: ProfileIntegration[] = [];
  /** Total linked artifacts, computed once per load (not per change-detection tick). */
  totalAssociations = 0;

  readonly form = this.fb.group({
    display_name: [''],
    email: ['', [Validators.email]],
    bio: [''],
  });

  ngOnInit(): void {
    this.load();
  }

  /**
   * Load the profile, its associations, and integration status in one request.
   *
   * Preconditions: none.
   * Postconditions: on success `form` is patched and `groups`/`totalAssociations`/
   * `integrations` reflect the response; on an HTTP error, or a 2xx response whose
   * shape is malformed (missing `profile`/`associations`/`integrations`), `error`
   * is set and the others are left unchanged. `loading` is false either way.
   */
  load(): void {
    this.loading = true;
    this.error = null;
    this.api.getOverview().subscribe({
      next: (overview) => {
        // A 2xx response with a malformed body slips past the error handler, so
        // guard the shape before destructuring rather than throw deep in render.
        if (!overview?.profile || !overview.associations || !overview.integrations) {
          this.error = 'Received an unexpected response from the server.';
          this.loading = false;
          return;
        }
        const { profile, associations, integrations } = overview;
        this.form.patchValue({
          display_name: profile.display_name,
          email: profile.email,
          bio: profile.bio,
        });
        this.groups = this.groupAssociations(associations);
        this.totalAssociations = this.groups.reduce((sum, g) => sum + g.items.length, 0);
        this.integrations = integrations;
        this.loading = false;
      },
      error: () => {
        this.error = 'Failed to load your profile. Please try again.';
        this.loading = false;
      },
    });
  }

  /**
   * Persist the editable profile fields.
   *
   * Preconditions: none enforced — a no-op (marking the form touched) when
   * `form.invalid` (e.g. a malformed email).
   * Postconditions: when the form is valid, exactly one of `success` ('Profile
   * saved.') or `error` is set after the request settles, and `saving` is false.
   */
  save(): void {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    this.saving = true;
    this.error = null;
    this.success = null;
    const value = this.form.getRawValue();
    this.api
      .updateProfile({
        display_name: value.display_name ?? '',
        email: value.email ?? '',
        bio: value.bio ?? '',
      })
      .subscribe({
        next: () => {
          this.saving = false;
          this.success = 'Profile saved.';
        },
        error: () => {
          this.saving = false;
          this.error = 'Failed to save your profile. Please try again.';
        },
      });
  }

  /** Group flat associations into the fixed display order, dropping empties. */
  private groupAssociations(items: Association[]): AssociationGroup[] {
    return ARTIFACT_GROUPS.map((g) => ({
      ...g,
      items: items.filter((a) => a.artifact_type === g.type),
    })).filter((g) => g.items.length > 0);
  }
}
