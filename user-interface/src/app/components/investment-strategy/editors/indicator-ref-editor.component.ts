import { Component, Input, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { Subscription } from 'rxjs';

import {
  INDICATOR_NAME_OPTIONS,
  INDICATOR_SOURCE_OPTIONS,
  INDICATOR_SPECS,
  IndicatorName,
  IndicatorParamSpec,
} from '../../../models';

@Component({
  selector: 'app-indicator-ref-editor',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule, MatFormFieldModule, MatInputModule, MatSelectModule],
  templateUrl: './indicator-ref-editor.component.html',
})
export class IndicatorRefEditorComponent implements OnInit, OnDestroy {
  @Input({ required: true }) group!: FormGroup;

  readonly indicatorNames = INDICATOR_NAME_OPTIONS;
  readonly sourceOptions = INDICATOR_SOURCE_OPTIONS;

  private nameSub?: Subscription;

  ngOnInit(): void {
    const nameCtrl = this.group.get('name');
    if (!nameCtrl) return;
    // Do NOT call applyForName here — the parent's buildIndicatorGroup()
    // has already seeded the params controls from the prefill payload, and
    // applyForName would wipe those seeds in favour of spec defaults.
    // Only react to user-driven name changes from here on.
    this.nameSub = nameCtrl.valueChanges.subscribe((name: IndicatorName) => {
      this.applyForName(name);
    });
  }

  ngOnDestroy(): void {
    this.nameSub?.unsubscribe();
  }

  get paramsGroup(): FormGroup {
    return this.group.get('params') as FormGroup;
  }

  paramsFor(name: IndicatorName): IndicatorParamSpec[] {
    return INDICATOR_SPECS[name]?.params ?? [];
  }

  currentName(): IndicatorName {
    return (this.group.get('name')?.value ?? 'sma') as IndicatorName;
  }

  allowsSource(): boolean {
    return INDICATOR_SPECS[this.currentName()]?.allowSource ?? false;
  }

  private applyForName(name: IndicatorName): void {
    const spec = INDICATOR_SPECS[name];
    const paramsGroup = this.paramsGroup;

    Object.keys(paramsGroup.controls).forEach((key) => paramsGroup.removeControl(key));

    if (!spec) return;

    for (const p of spec.params) {
      const initial =
        p.default ?? (p.kind === 'enum' ? (p.options?.[0] ?? null) : null);
      const validators = [];
      if (p.required) validators.push(Validators.required);
      if (p.kind !== 'enum') {
        if (p.min !== undefined) validators.push(Validators.min(p.min));
        if (p.max !== undefined) validators.push(Validators.max(p.max));
      }
      paramsGroup.addControl(p.key, new FormControl(initial, validators));
    }

    const sourceCtrl = this.group.get('source');
    if (sourceCtrl) {
      if (!spec.allowSource) {
        sourceCtrl.setValue('close', { emitEvent: false });
        sourceCtrl.disable({ emitEvent: false });
      } else {
        sourceCtrl.enable({ emitEvent: false });
      }
    }
  }
}
