import { Component, Input, OnDestroy, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AbstractControl, FormGroup, ReactiveFormsModule, ValidatorFn, Validators } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { Subscription } from 'rxjs';

import { SIZING_KINDS, SizingKind } from '../../../models';

@Component({
  selector: 'app-sizing-editor',
  standalone: true,
  imports: [CommonModule, ReactiveFormsModule, MatFormFieldModule, MatInputModule, MatSelectModule],
  templateUrl: './sizing-editor.component.html',
})
export class SizingEditorComponent implements OnInit, OnDestroy {
  @Input({ required: true }) group!: FormGroup;

  readonly kindOptions = SIZING_KINDS;

  private kindSub?: Subscription;

  ngOnInit(): void {
    const kindCtrl = this.group.get('kind');
    if (!kindCtrl) return;
    this.applyForKind(kindCtrl.value as SizingKind);
    this.kindSub = kindCtrl.valueChanges.subscribe((kind: SizingKind) => {
      this.applyForKind(kind);
    });
  }

  ngOnDestroy(): void {
    this.kindSub?.unsubscribe();
  }

  get currentKind(): SizingKind {
    return (this.group.get('kind')?.value ?? 'fixed_fraction') as SizingKind;
  }

  private setValidators(name: string, validators: ValidatorFn[]): void {
    const ctrl: AbstractControl | null = this.group.get(name);
    if (!ctrl) return;
    ctrl.setValidators(validators.length ? validators : null);
    ctrl.updateValueAndValidity({ emitEvent: false });
  }

  private applyForKind(kind: SizingKind): void {
    this.setValidators('fraction', []);
    this.setValidators('target_annual_vol', []);
    this.setValidators('notional_usd', []);

    switch (kind) {
      case 'fixed_fraction':
        this.setValidators('fraction', [Validators.required, Validators.min(0.0000001), Validators.max(1.0)]);
        break;
      case 'volatility_target':
        this.setValidators('target_annual_vol', [Validators.required, Validators.min(0.0000001)]);
        break;
      case 'fixed_notional':
        this.setValidators('notional_usd', [Validators.required, Validators.min(0.0000001)]);
        break;
    }
  }
}
