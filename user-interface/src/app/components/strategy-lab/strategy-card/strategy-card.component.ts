import { ChangeDetectionStrategy, Component, EventEmitter, Input, OnDestroy, Output } from '@angular/core';
import { CommonModule, DecimalPipe, DatePipe } from '@angular/common';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDividerModule } from '@angular/material/divider';
import { MatExpansionModule } from '@angular/material/expansion';
import type { PageEvent } from '@angular/material/paginator';

import { TradeLedgerComponent } from '../trade-ledger/trade-ledger.component';
import { QualityGateListComponent } from '../quality-gate-list/quality-gate-list.component';
import { PaperTradingPanelComponent } from '../paper-trading-panel/paper-trading-panel.component';
import {
  returnColor,
  returnColorLabel,
  verdictColor,
  gateIcon as pureGateIcon,
  gateSeverityClass as pureGateSeverityClass,
  type GateViewModel,
  flattenObjectRows,
  entryRuleRows,
  exitRuleRows,
  sizingRows,
} from '../strategy-lab.formatters';
import type {
  PaperTradingSession,
  QualityGateResult,
  StrategyLabRecord,
} from '../../../models';

/**
 * Presentational strategy-lab result card. Renders one record's summary
 * (header/metrics), its expandable detail accordion (signal intelligence,
 * trade ledger, strategy details, quality gates, strategy code), and its
 * paper-trading section. Owns no cross-record state (expand/collapse
 * tracking, delete-in-flight, pagination, paper-trading-in-flight all live on
 * the host, which passes the derived value for *this* record down as
 * `@Input()`s) and performs no externally-observable side effects beyond a
 * clipboard write and confirmation flash (`copyStrategyCode()`) and an
 * internal memoization cache (`gateViewModels()`) — every user action that
 * matters to the host is reported up via the `@Output()`s below.
 *
 * Preconditions: `record` is set before the first render (required input).
 * Postconditions: renders identically for the same `record`/inputs regardless
 *   of how many times change detection runs (OnPush; every derived value is a
 *   pure function of the inputs).
 */
@Component({
  selector: 'app-strategy-card',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    CommonModule,
    DecimalPipe,
    DatePipe,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatTooltipModule,
    MatDividerModule,
    MatExpansionModule,
    TradeLedgerComponent,
    QualityGateListComponent,
    PaperTradingPanelComponent,
  ],
  templateUrl: './strategy-card.component.html',
  styleUrl: './strategy-card.component.scss',
})
export class StrategyCardComponent implements OnDestroy {
  /** The lab record this card renders. */
  @Input({ required: true }) record!: StrategyLabRecord;
  /** Whether the card title renders as an `<h3>` (dashboard-tab context) or `<h2>` (standalone route). */
  @Input() showTitle = true;
  /** Whether the detail accordion/hypothesis/narrative region is expanded. */
  @Input() expanded = false;
  /** True while this record's delete request is in flight (shows the spinner, per the host's `deletingLabRecordId`). */
  @Input() isDeleting = false;
  /** True when the delete button should be disabled (another delete/clear-all/run is in flight). */
  @Input() deleteDisabled = false;
  /** Current trade-ledger page index for this record (host-owned, keyed by `lab_record_id`). */
  @Input() pageIndex = 0;
  /** This record's paper-trading session, if one exists. */
  @Input() paperSession: PaperTradingSession | null = null;
  /** True while a paper-trading run is in flight for this specific record. */
  @Input() paperTradingInProgress = false;
  /** True when starting/re-running paper trading should be disabled (another record's run, or a strategy run, is in flight). */
  @Input() paperTradingBlocked = false;

  /** User asked to delete this record. */
  @Output() deleteRequested = new EventEmitter<void>();
  /** User toggled the expand/collapse disclosure. */
  @Output() expandToggled = new EventEmitter<void>();
  /** User changed the trade-ledger paginator page. */
  @Output() pageChanged = new EventEmitter<PageEvent>();
  /** User asked to run (or re-run) paper trading for this record. */
  @Output() paperTradeRequested = new EventEmitter<void>();

  readonly returnColor = returnColor;
  readonly returnColorLabel = returnColorLabel;
  readonly verdictColor = verdictColor;
  readonly flattenObjectRows = flattenObjectRows;
  readonly entryRuleRows = entryRuleRows;
  readonly exitRuleRows = exitRuleRows;
  readonly sizingRows = sizingRows;

  /** True for ~1.5s after `copyStrategyCode()` runs, flashing a confirmation icon on the copy button. */
  strategyCodeCopied = false;
  private copyResetTimer: ReturnType<typeof setTimeout> | null = null;

  ngOnDestroy(): void {
    if (this.copyResetTimer) {
      clearTimeout(this.copyResetTimer);
      this.copyResetTimer = null;
    }
  }

  /**
   * Copy the generated strategy code to the clipboard, flashing a confirmation icon.
   * Mirrors `CodingTeamPageComponent.copyJobId()`'s pattern.
   *
   * Preconditions: none — a falsy `code` (e.g. if ever invoked outside the
   *   template's `@if (strategyCode(); as code)` guard) is a safe no-op rather
   *   than a caller contract violation.
   * Postconditions: a falsy `code` leaves all state unchanged. Otherwise, `code`
   *   is written to the clipboard when the Clipboard API is available (a no-op
   *   when it isn't) with any write rejection swallowed so it cannot surface as
   *   an unhandled rejection; `strategyCodeCopied` is set true for ~1.5s
   *   regardless of clipboard availability or write outcome (the reset timer is
   *   tracked so it is cancelled on destroy and never fires twice).
   */
  copyStrategyCode(code: string): void {
    if (!code) return;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(code).catch(() => {
        // Clipboard write can reject (permission denied, insecure context); ignore — the user can
        // still read/select the code manually.
      });
    }
    this.strategyCodeCopied = true;
    if (this.copyResetTimer) clearTimeout(this.copyResetTimer);
    this.copyResetTimer = setTimeout(() => {
      this.strategyCodeCopied = false;
      this.copyResetTimer = null;
    }, 1500);
  }

  /** Delete-button click handler. Postconditions: `deleteRequested` emits exactly once; no local state changes (the host owns delete-in-flight tracking). */
  onDelete(): void {
    this.deleteRequested.emit();
  }

  /** Disclosure-button click handler. Postconditions: `expandToggled` emits exactly once; `this.expanded` is unchanged (the host owns expand/collapse tracking and feeds the new value back in via the input). */
  onToggleExpand(): void {
    this.expandToggled.emit();
  }

  /** Trade-ledger paginator page-change handler. Postconditions: `pageChanged` emits `event` unchanged exactly once; `this.pageIndex` is unchanged (the host owns pagination state and feeds the new page back in via the input). */
  onPageChange(event: PageEvent): void {
    this.pageChanged.emit(event);
  }

  /** Paper-trade / re-run button click handler. Postconditions: `paperTradeRequested` emits exactly once; no local state changes (the host owns paper-trading-in-flight tracking). */
  onRunPaperTrading(): void {
    this.paperTradeRequested.emit();
  }

  /**
   * Accessible name for the card's disclosure button. States the action
   * available (Show/Hide) rather than the current state, per standard
   * toggle-button ARIA labeling convention.
   *
   * Preconditions: `record.strategy.asset_class` is a non-empty string.
   * Postconditions: returns "Show details for {asset_class} strategy" when
   *   collapsed, "Hide details for {asset_class} strategy" when expanded.
   */
  cardToggleLabel(): string {
    const verb = this.expanded ? 'Hide' : 'Show';
    return `${verb} details for ${this.record.strategy.asset_class} strategy`;
  }

  /**
   * Accessible name for the card's disclosure region (`role="region"`).
   *
   * Preconditions: `record.strategy.asset_class` is a non-empty string.
   * Postconditions: returns a non-empty, state-independent label.
   */
  cardRegionLabel(): string {
    return `${this.record.strategy.asset_class} strategy details`;
  }

  /** DOM id for this card's disclosure region, shared by the toggle button's `aria-controls` and the region's `id`. */
  cardBodyId(): string {
    return 'card-body-' + this.record.lab_record_id;
  }

  /**
   * Truncated hypothesis for the card title, shared by both the `showTitle`
   * `h3` and `h2` template branches so they can't drift out of sync.
   *
   * Preconditions: `record.strategy.hypothesis` is a string.
   * Postconditions: returns the hypothesis unchanged when 70 characters or
   *   fewer; otherwise the first 70 characters followed by an ellipsis.
   */
  truncatedHypothesis(): string {
    const hypothesis = this.record.strategy.hypothesis;
    return hypothesis.length > 70 ? `${hypothesis.slice(0, 70)}…` : hypothesis;
  }

  /**
   * Resolved strategy code for the "Strategy Code" panel. The backend has
   * populated this field under two different locations across schema
   * versions (top-level `record.strategy_code` on newer records,
   * `record.strategy.strategy_code` on older ones); resolving the fallback
   * here once keeps it out of the template and in one testable place.
   *
   * Postconditions: returns `record.strategy_code` when truthy; otherwise
   *   `record.strategy.strategy_code` verbatim (which may itself be an empty
   *   string or `undefined` — not coerced here, since the "Strategy Code"
   *   panel's own template gate independently re-checks truthiness before
   *   rendering this value).
   */
  strategyCode(): string | undefined {
    return this.record.strategy_code || this.record.strategy.strategy_code;
  }

  /**
   * Whether the Signal Intelligence panel should render. `signal_intelligence_brief`
   * is a legacy-optional field (older records predate the feature) and the
   * backend can also send an empty object rather than omitting the field, so
   * both must be checked to avoid rendering an empty panel.
   *
   * Postconditions: returns `true` only when `record.signal_intelligence_brief`
   *   is set and has at least one key.
   */
  hasSignalBrief(): boolean {
    return this.record.signal_intelligence_brief != null && Object.keys(this.record.signal_intelligence_brief).length > 0;
  }

  /**
   * A failed gate is "remedied" if a later run of the same logical
   * validator produced a passing result. Two channels:
   *
   * - Standard refinement-loop gates (refinement_round >= 0): remedied
   *   when `gate.refinement_round < record.refinement_rounds`. Backend
   *   semantics: `refinement_rounds` is not a count of rounds run with
   *   valid indices `0..refinement_rounds-1` — it's the 0-indexed round
   *   number of the last round the refinement loop actually reached (a
   *   round only counts once the loop advances past it with an applied
   *   fix). So `gateRound < maxRound` means a later round genuinely ran
   *   after this gate's failure; `gateRound === maxRound` means this
   *   gate's round *was* the last one reached, with no later round to
   *   fix it.
   * - Pre-synthesis gates (refinement_round = -1): refinement itself is
   *   code-only and cannot fix them, but the zero-trade repair path
   *   re-runs the spec validator after committing whitelisted spec
   *   updates and emits gates with gate_name `zero_trade_repair_<original>`.
   *   If any such later validator pass produced a passing result for the
   *   same logical check, the original pre-synthesis warning is remedied.
   */
  isRemedied(gate: QualityGateResult): boolean {
    if (gate.passed) return false;

    const gateRound = gate.refinement_round ?? 0;
    if (gateRound < 0) {
      // Pre-synthesis gate. Remedied only if a later validator pass for
      // the same logical check returned a passing result.
      const gates = this.record.quality_gate_results ?? [];
      const baseName = gate.gate_name;
      const repairName = `zero_trade_repair_${baseName}`;
      return gates.some(
        (g) =>
          g.passed &&
          (g.refinement_round ?? -1) >= 0 &&
          (g.gate_name === baseName || g.gate_name === repairName),
      );
    }

    const maxRound = this.record.refinement_rounds ?? 0;
    if (maxRound === 0) return false;
    // Gate failed in an earlier round — the strategy continued past it
    return gateRound < maxRound;
  }

  /** Icon for a quality-gate result, accounting for `isRemedied` (see `strategy-lab.formatters.ts`'s `gateIcon` for the icon-selection rules). */
  gateIcon(gate: QualityGateResult): string {
    return pureGateIcon(gate, this.isRemedied(gate));
  }

  /** CSS severity class for a quality-gate result, accounting for `isRemedied` (see `strategy-lab.formatters.ts`'s `gateSeverityClass` for the class-selection rules). */
  gateSeverityClass(gate: QualityGateResult): string {
    return pureGateSeverityClass(gate, this.isRemedied(gate));
  }

  // Invalidates on `record` reference identity, not deep equality — relies on
  // the host always replacing the record with a new object on change (never
  // mutating one in place), same as `pagedTradesCache`/`winCountCache` in
  // `TradeLedgerComponent`.
  private gateViewModelsCache: { record: StrategyLabRecord; viewModels: GateViewModel[] } | null = null;

  /**
   * Per-gate template data (icon, severity class, remedied flag), computed
   * once per record on first access and cached — the template iterates this
   * instead of calling `gateIcon`/`gateSeverityClass`/`isRemedied` directly
   * per gate on every change-detection pass.
   */
  gateViewModels(): GateViewModel[] {
    const cached = this.gateViewModelsCache;
    if (cached && cached.record === this.record) return cached.viewModels;
    const viewModels = (this.record.quality_gate_results ?? []).map((gate) => {
      const isRemedied = this.isRemedied(gate);
      return {
        gate,
        icon: pureGateIcon(gate, isRemedied),
        severityClass: pureGateSeverityClass(gate, isRemedied),
        isRemedied,
      };
    });
    this.gateViewModelsCache = { record: this.record, viewModels };
    return viewModels;
  }
}
