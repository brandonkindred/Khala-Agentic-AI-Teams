import { Component, Input, Output, EventEmitter, inject, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule, ReactiveFormsModule, FormBuilder, FormGroup, Validators } from '@angular/forms';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatSelectModule } from '@angular/material/select';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatCardModule } from '@angular/material/card';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatSlideToggleModule } from '@angular/material/slide-toggle';
import { MatExpansionModule } from '@angular/material/expansion';
import { MatListModule } from '@angular/material/list';
import { MatTooltipModule } from '@angular/material/tooltip';

import { InvestmentApiService } from '../../services/investment-api.service';
import {
  StrategySpec,
  CreateStrategyRequest,
  ValidateStrategyResponse,
  EntryRule,
  ExitRule,
  SizingRule,
  FixedFractionSizing,
} from '../../models';

@Component({
  selector: 'app-investment-strategy',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    ReactiveFormsModule,
    MatFormFieldModule,
    MatInputModule,
    MatSelectModule,
    MatButtonModule,
    MatIconModule,
    MatCardModule,
    MatChipsModule,
    MatDividerModule,
    MatProgressBarModule,
    MatSlideToggleModule,
    MatExpansionModule,
    MatListModule,
    MatTooltipModule,
  ],
  templateUrl: './investment-strategy.component.html',
  styleUrl: './investment-strategy.component.scss',
})
export class InvestmentStrategyComponent implements OnInit {
  @Input() existingStrategy: StrategySpec | null = null;
  @Output() strategyCreated = new EventEmitter<StrategySpec>();
  @Output() validationCompleted = new EventEmitter<ValidateStrategyResponse>();

  private readonly api = inject(InvestmentApiService);
  private readonly fb = inject(FormBuilder);

  readonly assetClasses = ['equities', 'bonds', 'crypto', 'options', 'forex', 'commodities', 'multi_asset'];

  loading = false;
  validating = false;
  error: string | null = null;

  currentStrategy: StrategySpec | null = null;
  validationResult: ValidateStrategyResponse | null = null;

  // Issue #551 — the backend's StrategySpec now uses structured DSL types
  // (EntryRule[], ExitRule[], SizingRule). Building a full visual rule editor
  // is deferred; for now this component edits the three rule fields as JSON.
  // The textareas are validated on submit and any parse / schema error is
  // surfaced inline.
  readonly defaultEntryRulesJson = '[]';
  readonly defaultExitRulesJson = '[]';
  readonly defaultSizingJson = '{\n  "kind": "fixed_fraction",\n  "fraction": 0.02\n}';

  rulesError: string | null = null;

  form: FormGroup = this.fb.group({
    asset_class: ['equities', Validators.required],
    hypothesis: ['', Validators.required],
    signal_definition: ['', Validators.required],
    entry_rules_json: [this.defaultEntryRulesJson],
    exit_rules_json: [this.defaultExitRulesJson],
    sizing_json: [this.defaultSizingJson],
    speculative: [false],
  });

  ngOnInit(): void {
    if (this.existingStrategy) {
      this.currentStrategy = this.existingStrategy;
      this.populateForm(this.existingStrategy);
    }
  }

  populateForm(strategy: StrategySpec): void {
    this.form.patchValue({
      asset_class: strategy.asset_class,
      hypothesis: strategy.hypothesis,
      signal_definition: strategy.signal_definition,
      entry_rules_json: JSON.stringify(strategy.entry_rules ?? [], null, 2),
      exit_rules_json: JSON.stringify(strategy.exit_rules ?? [], null, 2),
      sizing_json: JSON.stringify(
        strategy.sizing ?? ({ kind: 'fixed_fraction', fraction: 0.02 } as FixedFractionSizing),
        null,
        2,
      ),
      speculative: strategy.speculative,
    });
  }

  async createStrategy(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    this.loading = true;
    this.error = null;
    this.rulesError = null;

    const formValue = this.form.value;

    let entryRules: EntryRule[];
    let exitRules: ExitRule[];
    let sizing: SizingRule;
    try {
      entryRules = this.parseRulesJson<EntryRule[]>(formValue.entry_rules_json, 'entry_rules', []);
      exitRules = this.parseRulesJson<ExitRule[]>(formValue.exit_rules_json, 'exit_rules', []);
      sizing = this.parseRulesJson<SizingRule>(formValue.sizing_json, 'sizing', {
        kind: 'fixed_fraction',
        fraction: 0.02,
      } as FixedFractionSizing);
    } catch (parseErr) {
      this.loading = false;
      this.rulesError = (parseErr as Error).message;
      return;
    }

    const request: CreateStrategyRequest = {
      authored_by: 'ui_user',
      asset_class: formValue.asset_class,
      hypothesis: formValue.hypothesis,
      signal_definition: formValue.signal_definition,
      entry_rules: entryRules,
      exit_rules: exitRules,
      sizing,
      speculative: formValue.speculative,
    };

    this.api.createStrategy(request).subscribe({
      next: (response) => {
        this.loading = false;
        this.currentStrategy = response.strategy;
        this.strategyCreated.emit(response.strategy);
      },
      error: (err) => {
        this.loading = false;
        this.error = err.error?.detail || err.message || 'Failed to create strategy';
      },
    });
  }

  validateStrategy(): void {
    if (!this.currentStrategy) return;

    this.validating = true;
    this.validationResult = null;

    this.api.validateStrategy(this.currentStrategy.strategy_id).subscribe({
      next: (result) => {
        this.validating = false;
        this.validationResult = result;
        this.validationCompleted.emit(result);
      },
      error: (err) => {
        this.validating = false;
        this.error = err.error?.detail || err.message || 'Failed to validate strategy';
      },
    });
  }

  getCheckIcon(status: string): string {
    switch (status) {
      case 'pass':
        return 'check_circle';
      case 'warn':
        return 'warning';
      case 'fail':
        return 'cancel';
      default:
        return 'help';
    }
  }

  getCheckClass(status: string): string {
    return `check-${status}`;
  }

  private parseRulesJson<T>(raw: string, field: string, fallback: T): T {
    const text = (raw ?? '').trim();
    if (!text) return fallback;
    try {
      return JSON.parse(text) as T;
    } catch (err) {
      throw new Error(`${field}: invalid JSON — ${(err as Error).message}`);
    }
  }
}
