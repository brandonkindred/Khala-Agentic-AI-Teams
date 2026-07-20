import type {
  QualityGateResult,
  StrategyLabRecord,
  EntryRule,
  ExitRule,
  SizingRule,
  Predicate,
  PredicateSide,
  IndicatorRef,
  IndicatorParamValue,
  BarFieldRef,
  IndicatorSource,
  IndicatorName,
  StopLossBasis,
  ComparisonOp,
  SignalIntelligenceBriefPayload,
} from '../../models';
import { COMPARISON_OP_OPTIONS } from '../../models';
import { formatPct, formatUsd } from '../../shared/number-format';

/** Material icon per asset class, keyed by the lowercased category value. */
export const ASSET_CLASS_ICONS: Record<string, string> = {
  stocks: 'show_chart',
  crypto: 'currency_bitcoin',
  forex: 'currency_exchange',
  commodities: 'oil_barrel',
  futures: 'schedule',
  options: 'tune',
};

/** CSS class for an annualized-return value: winning (>8%), neutral (0-8%), or losing (<0%). */
export function returnColor(annualized: number): string {
  if (annualized > 8) return 'winning';
  if (annualized >= 0) return 'neutral';
  return 'losing';
}

/** Text alternative for `returnColor`, so the return-value color cue isn't the sole signal. */
export function returnColorLabel(annualized: number): string {
  if (annualized > 8) return 'Above target';
  if (annualized >= 0) return 'Neutral';
  return 'Negative';
}

/** Material icon for an asset class, falling back to a generic trend icon for unknown values. */
export function getAssetClassIcon(assetClass: string): string {
  return ASSET_CLASS_ICONS[assetClass?.toLowerCase()] ?? 'trending_up';
}

/** Display label for a paper-trading verdict. */
export function verdictLabel(verdict: string | undefined | null): string {
  if (verdict === 'ready_for_live') return 'READY FOR LIVE';
  if (verdict === 'not_performant') return 'NOT PERFORMANT';
  return 'INCONCLUSIVE';
}

/** CSS class for a paper-trading verdict. */
export function verdictColor(verdict: string | undefined | null): string {
  if (verdict === 'ready_for_live') return 'winning';
  if (verdict === 'not_performant') return 'losing';
  return 'neutral';
}

/** Precomputed per-gate template data — see `StrategyCardComponent.gateViewModels`. */
export interface GateViewModel {
  gate: QualityGateResult;
  icon: string;
  severityClass: string;
  isRemedied: boolean;
}

/**
 * Icon for a quality-gate result. Takes the already-computed `isRemedied`
 * flag rather than the gate's owning record, since remediation status
 * depends on sibling gates in later refinement rounds — logic that stays on
 * the component (see `isRemedied`) and is computed once per gate by callers.
 */
export function gateIcon(gate: QualityGateResult, isRemedied: boolean): string {
  if (gate.passed) return 'check_circle';
  if (isRemedied) return 'build_circle';
  return gate.severity === 'critical' ? 'cancel' : 'warning';
}

/** CSS class for a quality-gate result; see `gateIcon` for the `isRemedied` parameter. */
export function gateSeverityClass(gate: QualityGateResult, isRemedied: boolean): string {
  if (isRemedied) return 'gate-remedied';
  return 'gate-' + gate.severity;
}

/**
 * Human-readable publishability skip reason for a winning-but-blocked record.
 *
 * Preconditions: `record` is a loaded lab row.
 * Postconditions: returns the persisted skip reason when present, else null.
 */
export function publishabilitySkipLabel(record: StrategyLabRecord): string | null {
  const reason =
    record.publishability_skip_reason ||
    (record.paper_trading_skipped_reason &&
    record.paper_trading_skipped_reason !== 'not_winning' &&
    record.paper_trading_skipped_reason !== 'disabled'
      ? record.paper_trading_skipped_reason
      : null);
  return reason || null;
}

// ---------------------------------------------------------------------------
// Rule / sizing / signal-brief row flattening — turns the structured spec-DSL
// types (and, defensively, any non-conforming legacy/test shape) into plain
// label/value rows for the Strategy Details panel's definition lists.
// ---------------------------------------------------------------------------

/** One label/value pair for a definition-list-style detail row. */
export interface LabelValueRow {
  label: string;
  value: string;
}

const INDICATOR_DISPLAY_NAMES: Record<IndicatorName, string> = {
  sma: 'SMA',
  ema: 'EMA',
  rsi: 'RSI',
  macd: 'MACD',
  bollinger: 'Bollinger Bands',
  atr: 'ATR',
  adx: 'ADX',
  stochastic: 'Stochastic',
  vwap: 'VWAP',
  donchian: 'Donchian Channel',
  keltner: 'Keltner Channel',
  obv: 'OBV',
  mfi: 'MFI',
  roc: 'ROC',
  cci: 'CCI',
  williams_r: 'Williams %R',
};

const BAR_FIELD_LABELS: Record<BarFieldRef, string> = {
  'bar.close': 'Close',
  'bar.high': 'High',
  'bar.low': 'Low',
  'bar.volume': 'Volume',
};

const INDICATOR_SOURCE_LABELS: Record<IndicatorSource, string> = {
  close: 'Close',
  high: 'High',
  low: 'Low',
  open: 'Open',
  volume: 'Volume',
  hl2: 'HL2',
  ohlc4: 'OHLC4',
};

const STOP_LOSS_BASIS_LABELS: Record<StopLossBasis, string> = {
  entry_price: 'Entry Price',
  trailing_high: 'Trailing High',
  trailing_low: 'Trailing Low',
};

/**
 * Title-Cases a snake_case (or already-spaced) key for display, e.g.
 * `target_annual_vol` -> `Target Annual Vol`. Distinct from the `humanize`
 * helper duplicated in `code-review-dashboard/review-metrics.ts` and
 * `agent-studio-persona.component.ts` (each capitalizes only the first
 * character of the WHOLE string) — this capitalizes every word, which reads
 * better for the arbitrary, often multi-word keys `flattenToRows` encounters.
 *
 * Preconditions: none.
 * Postconditions: never throws. Returns `''` for an empty/falsy `key`.
 */
export function humanizeKey(key: string): string {
  if (!key) return '';
  return key
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value) ?? String(value);
  } catch {
    /* v8 ignore next 2 -- unreachable for real HTTP/JSON-sourced payloads (no cycles possible); kept as defense-in-depth so this helper truly never throws */
    return String(value);
  }
}

function isIndicatorRef(side: PredicateSide | number | null | undefined): side is IndicatorRef {
  return typeof side === 'object' && side !== null && 'name' in side;
}

function formatIndicatorParams(params: Record<string, IndicatorParamValue> | null | undefined): string {
  if (!params || typeof params !== 'object') return '';
  return Object.entries(params)
    .map(([key, value]) => `${humanizeKey(key)}: ${value}`)
    .join(', ');
}

function formatPredicateSide(side: PredicateSide | number | null | undefined): string {
  if (side === null || side === undefined) return '—';
  if (typeof side === 'number') return String(side);
  if (typeof side === 'string') return BAR_FIELD_LABELS[side as BarFieldRef] ?? side;
  if (isIndicatorRef(side)) {
    const name = INDICATOR_DISPLAY_NAMES[side.name] ?? String(side.name);
    const paramsStr = formatIndicatorParams(side.params);
    const sourceStr = side.source ? ` on ${INDICATOR_SOURCE_LABELS[side.source] ?? side.source}` : '';
    return paramsStr ? `${name}(${paramsStr})${sourceStr}` : `${name}${sourceStr}`;
  }
  return safeStringify(side); // defensive: unexpected shape, never throws, never drops data
}

function formatComparisonOp(op: ComparisonOp | null | undefined): string {
  if (!op) return '—';
  return COMPARISON_OP_OPTIONS.find((option) => option.value === op)?.label ?? String(op);
}

function predicateRows(predicate: Predicate | null | undefined): LabelValueRow[] {
  if (!predicate || typeof predicate !== 'object') return flattenToRows(predicate);
  const lhsLabel = isIndicatorRef(predicate.lhs) ? 'Indicator' : 'Price Field';
  return [
    { label: lhsLabel, value: formatPredicateSide(predicate.lhs) },
    { label: 'Operator', value: formatComparisonOp(predicate.op) },
    { label: 'Threshold', value: formatPredicateSide(predicate.rhs) },
  ];
}

/**
 * Label/value rows for one entry rule.
 *
 * Preconditions: none enforced — `rule` is expected to be an `EntryRule`,
 *   but this function tolerates any shape defensively (see `flattenToRows`),
 *   since real callers include non-conforming legacy/test data whose
 *   `entry_rules` entries predate the current `kind`-discriminated shape.
 * Postconditions: never throws. When `rule.kind === 'entry'`, returns a
 *   `Side` row, the predicate rows (`Indicator`/`Price Field`, `Operator`,
 *   `Threshold`), and a trailing `Note` row only when `rule.note` is
 *   non-empty. Otherwise delegates to `flattenToRows(rule)`.
 */
export function entryRuleRows(rule: EntryRule): LabelValueRow[] {
  if (!rule || rule.kind !== 'entry') return flattenToRows(rule);
  const rows: LabelValueRow[] = [
    { label: 'Side', value: rule.side === 'short' ? 'Short' : 'Long' },
    ...predicateRows(rule.when),
  ];
  if (rule.note) rows.push({ label: 'Note', value: rule.note });
  return rows;
}

/**
 * Label/value rows for one exit rule, handling all three `kind` variants.
 *
 * Preconditions: none enforced — see `entryRuleRows`'s note.
 * Postconditions: never throws. Each variant's rows start with a `Type` row
 *   (multiple exit-rule kinds can appear in the same list, so each row-set
 *   must self-identify). An unrecognized `kind` delegates to `flattenToRows(rule)`.
 */
export function exitRuleRows(rule: ExitRule): LabelValueRow[] {
  if (!rule) return flattenToRows(rule);
  let rows: LabelValueRow[];
  switch (rule.kind) {
    case 'stop_loss':
      rows = [
        { label: 'Type', value: 'Stop Loss' },
        { label: 'Stop Distance', value: formatPct(rule.pct * 100) },
        { label: 'Basis', value: STOP_LOSS_BASIS_LABELS[rule.basis ?? 'entry_price'] },
      ];
      break;
    case 'take_profit':
      rows = [
        { label: 'Type', value: 'Take Profit' },
        { label: 'Target', value: formatPct(rule.pct * 100) },
      ];
      break;
    case 'signal_exit':
      rows = [{ label: 'Type', value: 'Signal Exit' }, ...predicateRows(rule.when)];
      break;
    default:
      return flattenToRows(rule);
  }
  if (rule.note) rows.push({ label: 'Note', value: rule.note });
  return rows;
}

/**
 * Label/value rows for a strategy's sizing rule, handling all three `kind` variants.
 *
 * Preconditions: none enforced — see `entryRuleRows`'s note.
 * Postconditions: never throws. Each variant's rows start with a `Method`
 *   row. An unrecognized `kind` delegates to `flattenToRows(sizing)`.
 */
export function sizingRows(sizing: SizingRule): LabelValueRow[] {
  if (!sizing) return flattenToRows(sizing);
  let rows: LabelValueRow[];
  switch (sizing.kind) {
    case 'fixed_fraction':
      rows = [
        { label: 'Method', value: 'Fixed Fraction' },
        { label: 'Position Size', value: formatPct(sizing.fraction * 100) },
      ];
      break;
    case 'volatility_target':
      rows = [
        { label: 'Method', value: 'Volatility Target' },
        { label: 'Target Annual Volatility', value: formatPct(sizing.target_annual_vol * 100) },
      ];
      break;
    case 'fixed_notional':
      rows = [
        { label: 'Method', value: 'Fixed Notional' },
        { label: 'Notional (USD)', value: formatUsd(sizing.notional_usd) },
      ];
      break;
    default:
      return flattenToRows(sizing);
  }
  if (sizing.note) rows.push({ label: 'Note', value: sizing.note });
  return rows;
}

function formatFallbackValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return safeStringify(value);
  return String(value);
}

/**
 * Generic label/value flattening for genuinely free-form or unrecognized
 * shapes — the defensive fallback used directly for `signal_intelligence_brief`
 * (via `signalBriefRows`) and internally by `entryRuleRows`/`exitRuleRows`/
 * `sizingRows` whenever a `kind` discriminant doesn't match a known variant.
 *
 * Preconditions: none — `value` may be anything.
 * Postconditions: never throws. Returns `[]` for `null`/`undefined`, an empty
 *   object, or an empty array. Returns a single `{ label: 'Value', value }`
 *   row for a top-level primitive or non-empty array (nested values are
 *   stringified, not recursively flattened — nesting never explodes into an
 *   unbounded number of rows). Returns one row per own-enumerable key for a
 *   plain object (key Title-Cased via `humanizeKey`, value stringified via
 *   `formatFallbackValue` — nested objects/arrays are JSON-stringified, not
 *   dropped, so no data is lost).
 */
export function flattenToRows(value: unknown): LabelValueRow[] {
  if (value === null || value === undefined) return [];
  if (typeof value !== 'object') return [{ label: 'Value', value: String(value) }];
  if (Array.isArray(value)) {
    return value.length === 0 ? [] : [{ label: 'Value', value: safeStringify(value) }];
  }
  return Object.entries(value as Record<string, unknown>).map(([key, val]) => ({
    label: humanizeKey(key),
    value: formatFallbackValue(val),
  }));
}

/**
 * Label/value rows for the signal-intelligence panel. Special-cases the
 * documented `{ skipped, skipped_reason }` shape (see
 * `SignalIntelligenceBriefPayload`'s doc comment — a real wire shape, not a
 * hypothetical) with one friendly message row instead of two
 * boolean-literal-looking rows; anything else falls back to `flattenToRows`.
 *
 * Preconditions: none.
 * Postconditions: never throws. Returns exactly one row when `brief.skipped`
 *   is truthy; otherwise returns `flattenToRows(brief)`.
 */
export function signalBriefRows(brief: SignalIntelligenceBriefPayload): LabelValueRow[] {
  if (brief && typeof brief === 'object' && (brief as { skipped?: unknown }).skipped) {
    const reason = (brief as { skipped_reason?: unknown }).skipped_reason;
    return [{ label: 'Signal Intelligence', value: `Skipped — ${reason ? String(reason) : 'no reason given'}` }];
  }
  return flattenToRows(brief);
}
