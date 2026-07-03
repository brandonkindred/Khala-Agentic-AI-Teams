import { Component, DestroyRef, OnInit, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import type { MatChipInputEvent } from '@angular/material/chips';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatSliderModule } from '@angular/material/slider';
import { MatSnackBar } from '@angular/material/snack-bar';
import { COMMA, ENTER } from '@angular/cdk/keycodes';
import { JobMatchingApiService } from '../../services/job-matching-api.service';
import { LoadingSpinnerComponent } from '../../shared/loading-spinner/loading-spinner.component';
import type { JobSeekerProfile, RankingWeights } from '../../models';

/** The profile's string-list fields, grouped into form sections. */
export type ChipFieldKey =
  | 'target_titles'
  | 'seniority_levels'
  | 'keywords'
  | 'locations'
  | 'company_stages'
  | 'company_sizes'
  | 'industries'
  | 'preferred_companies'
  | 'excluded_companies'
  | 'must_have_skills'
  | 'nice_to_have_skills'
  | 'deal_breakers';

interface ChipField {
  key: ChipFieldKey;
  label: string;
  hint?: string;
  /** Rendered with warning styling (hard exclusions). */
  warn?: boolean;
}

interface ChipSection {
  title: string;
  fields: ChipField[];
}

const CHIP_SECTIONS: ChipSection[] = [
  {
    title: 'Targeting',
    fields: [
      { key: 'target_titles', label: 'Target titles' },
      { key: 'seniority_levels', label: 'Seniority levels' },
      { key: 'keywords', label: 'Extra search keywords' },
    ],
  },
  {
    title: 'Location',
    fields: [{ key: 'locations', label: 'Locations' }],
  },
  {
    title: 'Company',
    fields: [
      { key: 'company_stages', label: 'Preferred stages' },
      { key: 'company_sizes', label: 'Preferred sizes' },
      { key: 'industries', label: 'Industries' },
      { key: 'preferred_companies', label: "Companies you'd love" },
      { key: 'excluded_companies', label: 'Excluded companies', hint: 'Always skipped', warn: true },
    ],
  },
  {
    title: 'Skills & constraints',
    fields: [
      { key: 'must_have_skills', label: 'Must-have skills' },
      { key: 'nice_to_have_skills', label: 'Nice-to-have skills' },
      { key: 'deal_breakers', label: 'Deal breakers', hint: 'Any match forces "skip"', warn: true },
    ],
  },
];

const WEIGHT_FIELDS: { key: keyof RankingWeights; label: string }[] = [
  { key: 'title_fit', label: 'Title fit' },
  { key: 'seniority_fit', label: 'Seniority fit' },
  { key: 'location_fit', label: 'Location fit' },
  { key: 'comp_fit', label: 'Comp fit' },
  { key: 'company_fit', label: 'Company fit' },
  { key: 'skills_fit', label: 'Skills fit' },
];

/**
 * Career profile editor. Loads the resolved profile from the Job Matching API
 * and saves it back as the career section of the central user profile
 * (PUT /profile). String-list criteria are edited as chip grids.
 */
@Component({
  selector: 'app-job-profile-form',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    RouterLink,
    MatButtonModule,
    MatCardModule,
    MatChipsModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatSelectModule,
    MatSliderModule,
    LoadingSpinnerComponent,
  ],
  templateUrl: './job-profile-form.component.html',
  styleUrl: './job-profile-form.component.scss',
})
export class JobProfileFormComponent implements OnInit {
  private readonly api = inject(JobMatchingApiService);
  private readonly fb = inject(FormBuilder);
  private readonly snackBar = inject(MatSnackBar);
  private readonly destroyRef = inject(DestroyRef);

  readonly chipSections = CHIP_SECTIONS;
  readonly weightFields = WEIGHT_FIELDS;
  readonly separatorKeyCodes = [ENTER, COMMA] as const;

  loading = false;
  saving = false;
  error: string | null = null;
  savedAt: string | null = null;
  /** True once a chip has been added/removed since the last load/save. */
  chipsDirty = false;

  /** Chip-list values, keyed by profile field. */
  chips: Record<ChipFieldKey, string[]> = {
    target_titles: [],
    seniority_levels: [],
    keywords: [],
    locations: [],
    company_stages: [],
    company_sizes: [],
    industries: [],
    preferred_companies: [],
    excluded_companies: [],
    must_have_skills: [],
    nice_to_have_skills: [],
    deal_breakers: [],
  };

  readonly form = this.fb.nonNullable.group({
    remote_preference: ['any'],
    salary_min: [0, [Validators.min(0)]],
    currency: ['USD'],
    work_authorization: [''],
    title_fit: [0.25, [Validators.min(0)]],
    seniority_fit: [0.1, [Validators.min(0)]],
    location_fit: [0.15, [Validators.min(0)]],
    comp_fit: [0.15, [Validators.min(0)]],
    company_fit: [0.15, [Validators.min(0)]],
    skills_fit: [0.2, [Validators.min(0)]],
  });

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    this.loading = true;
    this.error = null;
    this.api
      .getProfile()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (profile) => {
          this.populate(profile);
          this.loading = false;
        },
        error: (err) => {
          this.error = err?.error?.detail ?? err?.message ?? 'Failed to load the profile.';
          this.loading = false;
        },
      });
  }

  private populate(profile: JobSeekerProfile): void {
    for (const key of Object.keys(this.chips) as ChipFieldKey[]) {
      this.chips[key] = [...(profile[key] ?? [])];
    }
    this.form.patchValue({
      remote_preference: profile.remote_preference ?? 'any',
      salary_min: profile.salary_min ?? 0,
      currency: profile.currency ?? 'USD',
      work_authorization: profile.work_authorization ?? '',
      ...(profile.weights ?? {}),
    });
    this.form.markAsPristine();
    this.chipsDirty = false;
  }

  /** True when there are edits not yet saved to the user profile. */
  get dirty(): boolean {
    return this.form.dirty || this.chipsDirty;
  }

  addChip(key: ChipFieldKey, event: MatChipInputEvent): void {
    const value = event.value.trim();
    if (value && !this.chips[key].includes(value)) {
      this.chips[key] = [...this.chips[key], value];
      this.chipsDirty = true;
    }
    event.chipInput.clear();
  }

  removeChip(key: ChipFieldKey, value: string): void {
    this.chips[key] = this.chips[key].filter((v) => v !== value);
    this.chipsDirty = true;
  }

  /**
   * Share of the final score a dimension gets after the ranker normalizes the
   * six weights (uniform split when all are zero — mirrors the backend).
   */
  weightShare(key: keyof RankingWeights): number {
    const raw = this.form.getRawValue();
    const values = WEIGHT_FIELDS.map((w) => Math.max(0, raw[w.key] ?? 0));
    const total = values.reduce((sum, v) => sum + v, 0);
    if (total <= 0) {
      return Math.round(100 / WEIGHT_FIELDS.length);
    }
    const idx = WEIGHT_FIELDS.findIndex((w) => w.key === key);
    return Math.round((values[idx] / total) * 100);
  }

  /** Assemble the snake_case payload the backend expects. */
  toProfile(): JobSeekerProfile {
    const raw = this.form.getRawValue();
    return {
      target_titles: this.chips.target_titles,
      seniority_levels: this.chips.seniority_levels,
      locations: this.chips.locations,
      remote_preference: raw.remote_preference as JobSeekerProfile['remote_preference'],
      salary_min: raw.salary_min,
      currency: raw.currency,
      company_stages: this.chips.company_stages,
      company_sizes: this.chips.company_sizes,
      industries: this.chips.industries,
      must_have_skills: this.chips.must_have_skills,
      nice_to_have_skills: this.chips.nice_to_have_skills,
      deal_breakers: this.chips.deal_breakers,
      preferred_companies: this.chips.preferred_companies,
      excluded_companies: this.chips.excluded_companies,
      work_authorization: raw.work_authorization,
      keywords: this.chips.keywords,
      weights: {
        title_fit: raw.title_fit,
        seniority_fit: raw.seniority_fit,
        location_fit: raw.location_fit,
        comp_fit: raw.comp_fit,
        company_fit: raw.company_fit,
        skills_fit: raw.skills_fit,
      },
    };
  }

  save(): void {
    if (this.form.invalid || this.saving) {
      return;
    }
    this.saving = true;
    this.api
      .saveProfile(this.toProfile())
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: () => {
          this.saving = false;
          this.savedAt = new Date().toLocaleTimeString();
          this.form.markAsPristine();
          this.chipsDirty = false;
          this.snackBar.open('Career profile saved to your user profile.', 'Dismiss', {
            duration: 3500,
          });
        },
        error: (err) => {
          this.saving = false;
          const detail = err?.error?.detail ?? err?.message ?? 'Failed to save the profile.';
          this.snackBar.open(detail, 'Dismiss', { duration: 6000 });
        },
      });
  }
}
