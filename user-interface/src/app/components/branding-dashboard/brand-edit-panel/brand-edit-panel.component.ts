import { Component, EventEmitter, Input, OnChanges, Output, SimpleChanges, inject } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatExpansionModule } from '@angular/material/expansion';
import type { Brand, BrandingMissionSnapshot } from '../../../models';

@Component({
  selector: 'app-brand-edit-panel',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    MatButtonModule,
    MatFormFieldModule,
    MatIconModule,
    MatInputModule,
    MatSlideToggleModule,
    MatExpansionModule,
  ],
  templateUrl: './brand-edit-panel.component.html',
  styleUrl: './brand-edit-panel.component.scss',
})
export class BrandEditPanelComponent implements OnChanges {
  @Input() brand: Brand | null = null;
  @Input() mission: BrandingMissionSnapshot | null = null;
  @Input() open = false;
  @Input() skipSave = false;
  @Output() closePanel = new EventEmitter<void>();
  @Output() missionUpdate = new EventEmitter<Partial<BrandingMissionSnapshot>>();
  @Output() skipSaveChange = new EventEmitter<boolean>();

  private readonly fb = inject(FormBuilder);

  form = this.fb.nonNullable.group({
    company_name: ['', [Validators.required, Validators.minLength(2)]],
    company_description: ['', [Validators.required, Validators.minLength(10)]],
    target_audience: ['', [Validators.required, Validators.minLength(3)]],
    desired_voice: [''],
    values_csv: [''],
    differentiators_csv: [''],
  });

  ngOnChanges(changes: SimpleChanges): void {
    const justOpened = changes['open'] && this.open && !changes['open'].previousValue;
    const brandSwitch = changes['brand']
      && changes['brand'].currentValue?.id !== changes['brand'].previousValue?.id;
    const missionCleared = changes['mission'] && !changes['mission'].currentValue;
    if (justOpened || brandSwitch || missionCleared || (!this.open && (changes['mission'] || changes['brand']))) {
      this.patchFormFromMission();
    }
  }

  onApply(): void {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    const raw = this.form.getRawValue();
    const patch: Partial<BrandingMissionSnapshot> = {
      company_name: raw.company_name,
      company_description: raw.company_description,
      target_audience: raw.target_audience,
      desired_voice: raw.desired_voice || undefined,
      values: raw.values_csv ? raw.values_csv.split(',').map((v: string) => v.trim()).filter(Boolean) : [],
      differentiators: raw.differentiators_csv
        ? raw.differentiators_csv.split(',').map((v: string) => v.trim()).filter(Boolean)
        : [],
    };
    this.missionUpdate.emit(patch);
  }

  onSkipSaveToggle(checked: boolean): void {
    this.skipSaveChange.emit(checked);
  }

  onClose(): void {
    this.closePanel.emit();
  }

  private patchFormFromMission(): void {
    const m = this.brand?.mission ?? this.mission;
    if (!m) {
      this.form.reset({ company_name: '', company_description: '', target_audience: '', desired_voice: '', values_csv: '', differentiators_csv: '' });
      return;
    }
    this.form.patchValue({
      company_name: m.company_name || '',
      company_description: m.company_description || '',
      target_audience: m.target_audience || '',
      desired_voice: m.desired_voice || '',
      values_csv: (m.values ?? []).join(', '),
      differentiators_csv: (m.differentiators ?? []).join(', '),
    });
  }
}
