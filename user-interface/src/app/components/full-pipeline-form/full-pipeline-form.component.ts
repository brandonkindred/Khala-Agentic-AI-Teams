import { Component, OnDestroy, output } from '@angular/core';
import { Subscription } from 'rxjs';
import { FormBuilder, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatCardModule } from '@angular/material/card';
import type { BlogContentProfile, FullPipelineRequest } from '../../models';

const CONTENT_PROFILE_OPTIONS: { value: BlogContentProfile; label: string; hint: string }[] = [
  {
    value: 'short_listicle',
    label: 'Short listicle / high-level',
    hint: 'Scannable, concise (~750 words target)',
  },
  {
    value: 'standard_article',
    label: 'Standard article',
    hint: 'Balanced depth (~1000 words target) — default',
  },
  {
    value: 'technical_deep_dive',
    label: 'Technical deep dive',
    hint: 'Substantive detail (~2200 words target)',
  },
  {
    value: 'series_instalment',
    label: 'Series instalment',
    hint: 'One part of a multi-post arc (~1400 words target)',
  },
];

/**
 * Form for POST /full-pipeline.
 * Emits the request payload on submit.
 */
@Component({
  selector: 'app-full-pipeline-form',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatCardModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatSlideToggleModule,
    MatButtonModule,
  ],
  templateUrl: './full-pipeline-form.component.html',
  styleUrl: './full-pipeline-form.component.scss',
})
export class FullPipelineFormComponent implements OnDestroy {
  readonly submitRequest = output<FullPipelineRequest>();

  readonly profileOptions = CONTENT_PROFILE_OPTIONS;

  form: FormGroup;

  private readonly profileSub: Subscription;

  /** Hint text for the selected writing format (template-safe). */
  get selectedProfileHint(): string {
    const v = this.form.get('content_profile')?.value as BlogContentProfile | undefined;
    const row = CONTENT_PROFILE_OPTIONS.find((p) => p.value === v);
    return row?.hint ?? '';
  }

  /** Series-only fields are shown only for the series instalment profile. */
  get showSeriesFields(): boolean {
    return this.form.get('content_profile')?.value === 'series_instalment';
  }

  constructor(private readonly fb: FormBuilder) {
    this.form = this.fb.nonNullable.group({
      brief: ['', [Validators.required, Validators.minLength(3)]],
      title_concept: [''],
      audience: [''],
      tone_or_purpose: [''],
      max_results: [20, [Validators.required, Validators.min(1), Validators.max(50)]],
      run_gates: [true],
      max_rewrite_iterations: [3, [Validators.required, Validators.min(1), Validators.max(10)]],
      content_profile: ['standard_article' as BlogContentProfile],
      length_notes: [''],
      use_custom_word_count: [false],
      target_word_count: [1000, [Validators.min(100), Validators.max(10000)]],
      series_title: [''],
      part_number: [1, [Validators.min(1)]],
      planned_parts: [null as number | null],
      instalment_scope: [''],
    });

    this.profileSub = this.form.get('content_profile')!.valueChanges.subscribe((profile) => {
      if (profile !== 'series_instalment') {
        this.form.patchValue(
          {
            series_title: '',
            part_number: 1,
            planned_parts: null,
            instalment_scope: '',
          },
          { emitEvent: false },
        );
      }
    });
  }

  ngOnDestroy(): void {
    this.profileSub.unsubscribe();
  }

  onSubmit(): void {
    if (this.form.invalid) {
      return;
    }
    const v = this.form.getRawValue();
    const payload: FullPipelineRequest = {
      brief: v.brief,
      title_concept: v.title_concept || undefined,
      audience: v.audience || undefined,
      tone_or_purpose: v.tone_or_purpose || undefined,
      max_results: v.max_results,
      run_gates: v.run_gates,
      max_rewrite_iterations: v.max_rewrite_iterations,
      content_profile: v.content_profile as BlogContentProfile,
    };
    if (v.length_notes?.trim()) {
      payload.length_notes = v.length_notes.trim();
    }
    if (v.use_custom_word_count) {
      payload.target_word_count = v.target_word_count;
    }
    if (v.content_profile === 'series_instalment') {
      const st = v.series_title?.trim();
      const isc = v.instalment_scope?.trim();
      const pp = v.planned_parts;
      if (st || isc || pp != null) {
        payload.series_context = {
          ...(st ? { series_title: st } : {}),
          part_number: v.part_number ?? 1,
          ...(pp != null && pp !== '' ? { planned_parts: Number(pp) } : {}),
          ...(isc ? { instalment_scope: isc } : {}),
        };
      }
    }
    this.submitRequest.emit(payload);
  }
}
