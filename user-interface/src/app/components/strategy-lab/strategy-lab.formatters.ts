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

/** Title-Case a snake_case (or already-plain) field key for display, e.g. 'target_annual_vol' → 'Target Annual Vol'. */
export function humanizeKey(key: string): string {
  return key
    .split('_')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
}

/**
 * Flattens a rule/sizing/signal-brief object into `{ label, value }` rows for
 * display, replacing a raw `| json` dump. Generic and defensive rather than
 * per-discriminated-union: real payloads (see this component's own test
 * fixtures) don't reliably match the declared EntryRule/ExitRule/SizingRule
 * TS unions, and signal_intelligence_brief is declared as free-form
 * `Record<string, unknown> | null`.
 *
 * Preconditions: none — accepts any value, including null/undefined/arrays/
 *   primitives.
 * Postconditions: returns `[]` for null/undefined; a single fallback
 *   `{ label: 'Value', value: String(obj) }` row for a non-object (primitive
 *   or array) input; otherwise one row per own-enumerable key whose value is
 *   not null/undefined, with the key humanized and a nested-object value
 *   JSON.stringify'd (never recurses; never throws for realistic, JSON-derived
 *   input — a hand-built object containing a circular reference, which cannot
 *   arise from an actual JSON API response, would still throw via
 *   `JSON.stringify`).
 */
export function flattenObjectRows(obj: unknown): { label: string; value: string }[] {
  if (obj == null) return [];
  if (typeof obj !== 'object' || Array.isArray(obj)) {
    return [{ label: 'Value', value: String(obj) }];
  }
  return Object.entries(obj as Record<string, unknown>)
    .filter(([, value]) => value != null)
    .map(([key, value]) => ({
      label: humanizeKey(key),
      value: typeof value === 'object' ? JSON.stringify(value) : String(value),
    }));
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
  // Defensive: unexpected shape for a predicate side — never throws, never drops data.
  // Surfaced via console.warn (matching reviewDuration's clock-skew warning in
  // review-metrics.ts) rather than silently swallowed, since it signals a real
  // conformance gap in upstream data rather than an expected case.
  console.warn('Unexpected predicate side shape:', side);
  return typeof side === 'object' ? JSON.stringify(side) : String(side);
}

function formatComparisonOp(op: ComparisonOp | null | undefined): string {
  if (!op) return '—';
  return COMPARISON_OP_OPTIONS.find((option) => option.value === op)?.label ?? String(op);
}

/** Formats `value` with `formatter` when it's a real number, else the same em-dash fallback used for missing predicate data. */
function formatNumericField(value: number | null | undefined, formatter: (value: number) => string): string {
  return typeof value === 'number' ? formatter(value) : '—';
}

function predicateRows(predicate: Predicate | null | undefined): { label: string; value: string }[] {
  if (!predicate || typeof predicate !== 'object') return flattenObjectRows(predicate);
  const lhsLabel = isIndicatorRef(predicate.lhs) ? 'Indicator' : 'Price Field';
  return [
    { label: lhsLabel, value: formatPredicateSide(predicate.lhs) },
    { label: 'Operator', value: formatComparisonOp(predicate.op) },
    { label: 'Threshold', value: formatPredicateSide(predicate.rhs) },
  ];
}

/**
 * Label/value rows for one entry rule, breaking its predicate down into
 * semantic Indicator/Price-Field, Operator, and Threshold rows rather than
 * `flattenObjectRows`'s generic per-key treatment (which leaves a nested
 * `when` predicate as one opaque JSON-stringified row).
 *
 * Preconditions: none enforced — `rule` is expected to be an `EntryRule`,
 *   but this function tolerates any shape defensively, since real callers
 *   include non-conforming legacy/test data whose `entry_rules` entries
 *   predate the current `kind`-discriminated shape.
 * Postconditions: never throws. When `rule.kind === 'entry'`, returns a
 *   `Side` row, the predicate rows, and a trailing `Note` row only when
 *   `rule.note` is non-empty. Otherwise delegates to `flattenObjectRows(rule)`.
 */
export function entryRuleRows(rule: EntryRule): { label: string; value: string }[] {
  if (!rule || rule.kind !== 'entry') return flattenObjectRows(rule);
  const rows: { label: string; value: string }[] = [
    { label: 'Side', value: rule.side === 'short' ? 'Short' : 'Long' },
    ...predicateRows(rule.when),
  ];
  if (rule.note) rows.push({ label: 'Note', value: rule.note });
  return rows;
}

/**
 * Label/value rows for one exit rule, handling all three `kind` variants
 * with real semantic rows instead of `flattenObjectRows`'s generic treatment.
 *
 * Preconditions: none enforced — see `entryRuleRows`'s note.
 * Postconditions: never throws. Each variant's rows start with a `Type` row
 *   (multiple exit-rule kinds can appear in the same list, so each row-set
 *   must self-identify). An unrecognized `kind` delegates to `flattenObjectRows(rule)`.
 */
export function exitRuleRows(rule: ExitRule): { label: string; value: string }[] {
  if (!rule) return flattenObjectRows(rule);
  let rows: { label: string; value: string }[];
  switch (rule.kind) {
    case 'stop_loss':
      rows = [
        { label: 'Type', value: 'Stop Loss' },
        { label: 'Stop Distance', value: formatNumericField(rule.pct, (pct) => formatPct(pct * 100)) },
        { label: 'Basis', value: STOP_LOSS_BASIS_LABELS[rule.basis ?? 'entry_price'] },
      ];
      break;
    case 'take_profit':
      rows = [
        { label: 'Type', value: 'Take Profit' },
        { label: 'Target', value: formatNumericField(rule.pct, (pct) => formatPct(pct * 100)) },
      ];
      break;
    case 'signal_exit':
      rows = [{ label: 'Type', value: 'Signal Exit' }, ...predicateRows(rule.when)];
      break;
    default:
      return flattenObjectRows(rule);
  }
  if (rule.note) rows.push({ label: 'Note', value: rule.note });
  return rows;
}

/**
 * Label/value rows for a strategy's sizing rule, handling all three `kind`
 * variants with real semantic rows instead of `flattenObjectRows`'s generic
 * treatment.
 *
 * Preconditions: none enforced — see `entryRuleRows`'s note.
 * Postconditions: never throws. Each variant's rows start with a `Method`
 *   row. An unrecognized `kind` delegates to `flattenObjectRows(sizing)`.
 */
export function sizingRows(sizing: SizingRule): { label: string; value: string }[] {
  if (!sizing) return flattenObjectRows(sizing);
  let rows: { label: string; value: string }[];
  switch (sizing.kind) {
    case 'fixed_fraction':
      rows = [
        { label: 'Method', value: 'Fixed Fraction' },
        { label: 'Position Size', value: formatNumericField(sizing.fraction, (fraction) => formatPct(fraction * 100)) },
      ];
      break;
    case 'volatility_target':
      rows = [
        { label: 'Method', value: 'Volatility Target' },
        {
          label: 'Target Annual Volatility',
          value: formatNumericField(sizing.target_annual_vol, (vol) => formatPct(vol * 100)),
        },
      ];
      break;
    case 'fixed_notional':
      rows = [
        { label: 'Method', value: 'Fixed Notional' },
        { label: 'Notional (USD)', value: formatNumericField(sizing.notional_usd, formatUsd) },
      ];
      break;
    default:
      return flattenObjectRows(sizing);
  }
  if (sizing.note) rows.push({ label: 'Note', value: sizing.note });
  return rows;
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
