import { Component, EventEmitter, Input, OnDestroy, OnInit, Output } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AbstractControl, FormGroup, ReactiveFormsModule, ValidatorFn, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatIconModule } from '@angular/material/icon';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { Subscription } from 'rxjs';

import { EXIT_RULE_KINDS, ExitRuleKind, STOP_LOSS_BASIS_OPTIONS } from '../../../models';
import { PredicateEditorComponent } from './predicate-editor.component';
import { integerValidator } from './strategy-validators';

@Component({
  selector: 'app-exit-rule-editor',
  standalone: true,
  imports: [
    CommonModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    PredicateEditorComponent,
  ],
  templateUrl: './exit-rule-editor.component.html',
})
export class ExitRuleEditorComponent implements OnInit, OnDestroy {
  @Input({ required: true }) group!: FormGroup;
  @Input() index = 0;
  @Output() remove = new EventEmitter<void>();

  readonly kindOptions = EXIT_RULE_KINDS;
  readonly basisOptions = STOP_LOSS_BASIS_OPTIONS;

  private kindSub?: Subscription;

  ngOnInit(): void {
    const kindCtrl = this.group.get('kind');
    if (!kindCtrl) return;
    this.applyForKind(kindCtrl.value as ExitRuleKind);
    this.kindSub = kindCtrl.valueChanges.subscribe((kind: ExitRuleKind) => {
      this.applyForKind(kind);
    });
  }

  ngOnDestroy(): void {
    this.kindSub?.unsubscribe();
  }

  get whenGroup(): FormGroup {
    return this.group.get('when') as FormGroup;
  }

  get currentKind(): ExitRuleKind {
    return (this.group.get('kind')?.value ?? 'time_stop') as ExitRuleKind;
  }

  private setValidators(name: string, validators: ValidatorFn[]): void {
    const ctrl: AbstractControl | null = this.group.get(name);
    if (!ctrl) return;
    ctrl.setValidators(validators.length ? validators : null);
    ctrl.updateValueAndValidity({ emitEvent: false });
  }

  private applyForKind(kind: ExitRuleKind): void {
    // Clear everything first, then apply per kind. Numeric fields keep their
    // value across kind switches (cheap; harmless).
    this.setValidators('n_bars', []);
    this.setValidators('pct', []);
    this.setValidators('basis', []);

    switch (kind) {
      case 'time_stop':
        // n_bars is declared as `int` on the backend; reject 1.5 client-side.
        this.setValidators('n_bars', [Validators.required, Validators.min(1), integerValidator]);
        break;
      case 'stop_loss':
        this.setValidators('pct', [Validators.required, Validators.min(0.0000001), Validators.max(1.0)]);
        this.setValidators('basis', [Validators.required]);
        break;
      case 'take_profit':
        this.setValidators('pct', [Validators.required, Validators.min(0.0000001)]);
        break;
      case 'signal_exit':
        // Nested predicate child components own their own validation.
        break;
    }

    // The nested `when` predicate is only serialized for signal_exit; its
    // default IndicatorRef params have required fields that would otherwise
    // keep the form invalid.
    const whenGroup = this.group.get('when');
    if (whenGroup) {
      const opts = { emitEvent: false };
      if (kind === 'signal_exit') whenGroup.enable(opts);
      else whenGroup.disable(opts);
    }
  }
}
