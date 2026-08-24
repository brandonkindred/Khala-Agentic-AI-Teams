import { Component, Input, Output, EventEmitter, inject, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule, ReactiveFormsModule, FormArray, FormBuilder, FormControl, FormGroup, Validators } from '@angular/forms';
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
  ExitRuleKind,
  SizingKind,
  SizingRule,
  Predicate,
  PredicateSide,
  IndicatorRef,
  IndicatorName,
  BarFieldRef,
  ComparisonOp,
  StrategyTimeframe,
  INDICATOR_SPECS,
  STRATEGY_TIMEFRAME_OPTIONS,
  EXIT_RULE_KINDS,
  SIZING_KINDS,
} from '../../models';

import { InlineBannerComponent } from '../../shared/inline-banner/inline-banner.component';
import { EntryRuleEditorComponent } from './editors/entry-rule-editor.component';
import { ExitRuleEditorComponent } from './editors/exit-rule-editor.component';
import { SizingEditorComponent } from './editors/sizing-editor.component';
import { integerValidator } from './editors/strategy-validators';
import { extractErrorDetail } from '../../shared/extract-error-detail';

const ALLOWED_EXIT_KINDS: ReadonlySet<string> = new Set(EXIT_RULE_KINDS);
const ALLOWED_SIZING_KINDS: ReadonlySet<string> = new Set(SIZING_KINDS);
const BAR_FIELDS: ReadonlySet<string> = new Set(['bar.close', 'bar.high', 'bar.low', 'bar.volume']);

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
    EntryRuleEditorComponent,
    ExitRuleEditorComponent,
    SizingEditorComponent,
    InlineBannerComponent,
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
  readonly timeframeOptions = STRATEGY_TIMEFRAME_OPTIONS;

  loading = false;
  validating = false;
  error: string | null = null;

  currentStrategy: StrategySpec | null = null;
  validationResult: ValidateStrategyResponse | null = null;

  prefillErrors: string[] = [];

  form: FormGroup = this.fb.group({
    asset_class: ['equities', Validators.required],
    hypothesis: ['', Validators.required],
    signal_definition: ['', Validators.required],
    timeframe: ['1d' as StrategyTimeframe, Validators.required],
    entry_rules: this.fb.array<FormGroup>([]),
    exit_rules: this.fb.array<FormGroup>([]),
    sizing: this.buildSizingGroup(),
    speculative: [false],
  });

  ngOnInit(): void {
    if (this.existingStrategy) {
      this.currentStrategy = this.existingStrategy;
      this.populateForm(this.existingStrategy);
    }
  }

  // ---- Template helpers ---------------------------------------------------

  get entryRulesArray(): FormArray<FormGroup> {
    return this.form.get('entry_rules') as FormArray<FormGroup>;
  }

  get exitRulesArray(): FormArray<FormGroup> {
    return this.form.get('exit_rules') as FormArray<FormGroup>;
  }

  get sizingGroup(): FormGroup {
    return this.form.get('sizing') as FormGroup;
  }

  get hasPrefillErrors(): boolean {
    return this.prefillErrors.length > 0;
  }

  addEntryRule(): void {
    this.entryRulesArray.push(this.buildEntryRuleGroup());
  }

  removeEntryRule(i: number): void {
    this.entryRulesArray.removeAt(i);
  }

  addExitRule(): void {
    this.exitRulesArray.push(this.buildExitRuleGroup());
  }

  removeExitRule(i: number): void {
    this.exitRulesArray.removeAt(i);
  }

  // ---- Form-group builders ------------------------------------------------

  buildIndicatorGroup(initial?: IndicatorRef): FormGroup {
    const name = (initial?.name && INDICATOR_SPECS[initial.name as IndicatorName] ? initial.name : 'sma') as IndicatorName;
    if (initial?.name && !INDICATOR_SPECS[initial.name as IndicatorName]) {
      this.prefillErrors.push(`indicator:${initial.name}`);
    }
    const paramsGroup = this.fb.group({});
    const spec = INDICATOR_SPECS[name];
    for (const p of spec.params) {
      const seeded = initial?.params?.[p.key];
      const value =
        seeded !== undefined
          ? seeded
          : (p.default ?? (p.kind === 'enum' ? (p.options?.[0] ?? null) : null));
      const validators = [];
      if (p.required) validators.push(Validators.required);
      if (p.kind !== 'enum') {
        if (p.min !== undefined) validators.push(Validators.min(p.min));
        if (p.max !== undefined) validators.push(Validators.max(p.max));
      }
      if (p.kind === 'int') validators.push(integerValidator);
      paramsGroup.addControl(p.key, new FormControl(value, validators));
    }
    const sourceCtrl = new FormControl(initial?.source ?? 'close');
    if (!spec.allowSource) sourceCtrl.disable();
    return this.fb.group({
      name: [name, Validators.required],
      params: paramsGroup,
      source: sourceCtrl,
    });
  }

  buildPredicateSideGroup(initial?: PredicateSide | number): FormGroup {
    let sideKind: 'indicator' | 'bar_field' | 'number' = 'indicator';
    let indicatorSeed: IndicatorRef | undefined;
    let barFieldSeed: BarFieldRef = 'bar.close';
    let numberSeed: number | null = null;

    if (typeof initial === 'number') {
      sideKind = 'number';
      numberSeed = initial;
    } else if (typeof initial === 'string') {
      if (BAR_FIELDS.has(initial)) {
        sideKind = 'bar_field';
        barFieldSeed = initial as BarFieldRef;
      } else {
        this.prefillErrors.push(`bar_field:${initial}`);
      }
    } else if (initial && typeof initial === 'object' && 'name' in initial) {
      sideKind = 'indicator';
      indicatorSeed = initial as IndicatorRef;
    }

    const group = this.fb.group({
      side_kind: [sideKind, Validators.required],
      indicator: this.buildIndicatorGroup(indicatorSeed),
      bar_field: [barFieldSeed],
      number_val: [numberSeed],
    });

    this.applySideKindEnablement(group, sideKind);
    group.get('side_kind')!.valueChanges.subscribe((kind: string | null) => {
      this.applySideKindEnablement(group, kind ?? 'indicator');
    });

    return group;
  }

  private applySideKindEnablement(group: FormGroup, kind: string): void {
    const indicator = group.get('indicator');
    const barField = group.get('bar_field');
    const number = group.get('number_val');
    const opts = { emitEvent: false };
    if (kind === 'indicator') {
      indicator?.enable(opts);
      barField?.disable(opts);
      number?.disable(opts);
      number?.clearValidators();
    } else if (kind === 'bar_field') {
      indicator?.disable(opts);
      barField?.enable(opts);
      number?.disable(opts);
      number?.clearValidators();
    } else if (kind === 'number') {
      indicator?.disable(opts);
      barField?.disable(opts);
      number?.enable(opts);
      // Blank constants must block submit; without this, Number(null) → 0
      // silently lands as a 0-valued predicate threshold.
      number?.setValidators([Validators.required]);
    }
    number?.updateValueAndValidity(opts);
  }

  buildPredicateGroup(initial?: Predicate): FormGroup {
    return this.fb.group({
      lhs: this.buildPredicateSideGroup(initial?.lhs),
      op: [(initial?.op ?? '<') as ComparisonOp, Validators.required],
      rhs: this.buildPredicateSideGroup(initial?.rhs),
    });
  }

  buildEntryRuleGroup(initial?: EntryRule): FormGroup {
    return this.fb.group({
      kind: ['entry'],
      side: [initial?.side ?? 'long', Validators.required],
      when: this.buildPredicateGroup(initial?.when),
      note: [initial?.note ?? ''],
    });
  }

  buildExitRuleGroup(initial?: ExitRule): FormGroup {
    const kind = (initial?.kind && ALLOWED_EXIT_KINDS.has(initial.kind) ? initial.kind : 'stop_loss') as ExitRuleKind;
    const pct =
      initial && (initial.kind === 'stop_loss' || initial.kind === 'take_profit')
        ? initial.pct
        : 0.05;
    const basis = initial && initial.kind === 'stop_loss' ? (initial.basis ?? 'entry_price') : 'entry_price';
    const whenSeed = initial && initial.kind === 'signal_exit' ? initial.when : undefined;

    const group = this.fb.group({
      kind: [kind, Validators.required],
      pct: [pct],
      basis: [basis],
      when: this.buildPredicateGroup(whenSeed),
      note: [initial?.note ?? ''],
    });

    // The `when` predicate is only relevant to signal_exit. For other kinds
    // it carries default required IndicatorRef params that would otherwise
    // keep the form invalid (and the submit button disabled).
    if (kind !== 'signal_exit') {
      group.get('when')!.disable({ emitEvent: false });
    }

    return group;
  }

  buildSizingGroup(initial?: SizingRule): FormGroup {
    let kind: SizingKind = 'fixed_fraction';
    if (initial && ALLOWED_SIZING_KINDS.has(initial.kind)) {
      kind = initial.kind;
    } else if (initial) {
      this.prefillErrors.push(`sizing:${initial.kind}`);
    }
    const fraction = initial && initial.kind === 'fixed_fraction' ? initial.fraction : 0.02;
    const targetVol = initial && initial.kind === 'volatility_target' ? initial.target_annual_vol : 0.15;
    const notional = initial && initial.kind === 'fixed_notional' ? initial.notional_usd : 10000;

    return this.fb.group({
      kind: [kind, Validators.required],
      fraction: [fraction],
      target_annual_vol: [targetVol],
      notional_usd: [notional],
      note: [initial?.note ?? ''],
    });
  }

  // ---- Prefill ------------------------------------------------------------

  populateForm(strategy: StrategySpec): void {
    this.prefillErrors = [];

    this.form.patchValue({
      asset_class: strategy.asset_class,
      hypothesis: strategy.hypothesis,
      signal_definition: strategy.signal_definition,
      timeframe: strategy.timeframe ?? '1d',
      speculative: strategy.speculative ?? false,
    });

    this.entryRulesArray.clear();
    for (const e of strategy.entry_rules ?? []) {
      this.entryRulesArray.push(this.buildEntryRuleGroup(e));
    }

    this.exitRulesArray.clear();
    for (const x of strategy.exit_rules ?? []) {
      if (!ALLOWED_EXIT_KINDS.has(x.kind)) {
        this.prefillErrors.push(`exit_rule:${(x as { kind: string }).kind}`);
        continue;
      }
      this.exitRulesArray.push(this.buildExitRuleGroup(x));
    }

    this.form.setControl('sizing', this.buildSizingGroup(strategy.sizing));

    if (strategy.requires_redesign) {
      this.prefillErrors.push('backend:requires_redesign');
    }
    for (const item of strategy.unparsed_rules ?? []) {
      this.prefillErrors.push(`unparsed:${item}`);
    }
  }

  // ---- Submit -------------------------------------------------------------

  async createStrategy(): Promise<void> {
    if (this.hasPrefillErrors) return;
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    this.loading = true;
    this.error = null;

    const raw = this.form.getRawValue();

    const request: CreateStrategyRequest = {
      authored_by: 'ui_user',
      asset_class: raw.asset_class,
      hypothesis: raw.hypothesis,
      signal_definition: raw.signal_definition,
      timeframe: raw.timeframe,
      entry_rules: (raw.entry_rules ?? []).map((r: Record<string, unknown>) => this.serializeEntryRule(r)),
      exit_rules: (raw.exit_rules ?? []).map((r: Record<string, unknown>) => this.serializeExitRule(r)),
      sizing: this.serializeSizing(raw.sizing),
      speculative: raw.speculative,
    };

    this.api.createStrategy(request).subscribe({
      next: (response) => {
        this.loading = false;
        this.currentStrategy = response.strategy;
        this.strategyCreated.emit(response.strategy);
      },
      error: (err) => {
        this.loading = false;
        this.error = extractErrorDetail(err, 'Failed to create strategy');
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
        this.error = extractErrorDetail(err, 'Failed to validate strategy');
      },
    });
  }

  // ---- Serializers --------------------------------------------------------

  private serializeEntryRule(raw: Record<string, unknown>): EntryRule {
    return {
      kind: 'entry',
      side: raw['side'] as 'long' | 'short',
      when: this.serializePredicate(raw['when'] as Record<string, unknown>),
      ...(raw['note'] ? { note: raw['note'] as string } : {}),
    };
  }

  private serializeExitRule(raw: Record<string, unknown>): ExitRule {
    const kind = raw['kind'] as ExitRuleKind;
    const note = raw['note'] ? { note: raw['note'] as string } : {};
    switch (kind) {
      case 'stop_loss':
        return {
          kind,
          pct: Number(raw['pct']),
          basis: raw['basis'] as 'entry_price' | 'trailing_high' | 'trailing_low',
          ...note,
        };
      case 'take_profit':
        return { kind, pct: Number(raw['pct']), ...note };
      case 'signal_exit':
        return { kind, when: this.serializePredicate(raw['when'] as Record<string, unknown>), ...note };
    }
  }

  private serializeSizing(raw: Record<string, unknown>): SizingRule {
    const kind = raw['kind'] as SizingKind;
    const note = raw['note'] ? { note: raw['note'] as string } : {};
    switch (kind) {
      case 'fixed_fraction':
        return { kind, fraction: Number(raw['fraction']), ...note };
      case 'volatility_target':
        return { kind, target_annual_vol: Number(raw['target_annual_vol']), ...note };
      case 'fixed_notional':
        return { kind, notional_usd: Number(raw['notional_usd']), ...note };
    }
  }

  private serializePredicate(raw: Record<string, unknown>): Predicate {
    return {
      lhs: this.serializeSide(raw['lhs'] as Record<string, unknown>, false) as PredicateSide,
      op: raw['op'] as ComparisonOp,
      rhs: this.serializeSide(raw['rhs'] as Record<string, unknown>, true),
    };
  }

  private serializeSide(raw: Record<string, unknown>, allowNumber: boolean): PredicateSide | number {
    const kind = raw['side_kind'] as 'indicator' | 'bar_field' | 'number';
    if (kind === 'number' && allowNumber) {
      return Number(raw['number_val']);
    }
    if (kind === 'bar_field') {
      return raw['bar_field'] as BarFieldRef;
    }
    const indicator = raw['indicator'] as Record<string, unknown>;
    const params = { ...(indicator['params'] as Record<string, unknown>) };
    const name = indicator['name'] as IndicatorName;
    const spec = INDICATOR_SPECS[name];
    for (const p of spec?.params ?? []) {
      const v = params[p.key];
      // Drop blank optional params so the backend applies its own default;
      // otherwise {period: null} fails the Pydantic int|float|str validator.
      if (v === null || v === undefined || v === '') {
        delete params[p.key];
        continue;
      }
      if (p.kind !== 'enum') {
        params[p.key] = Number(v);
      }
    }
    return {
      name,
      params: params as Record<string, number | string>,
      source: indicator['source'] as IndicatorRef['source'],
    };
  }

  // ---- Misc ---------------------------------------------------------------

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
}
