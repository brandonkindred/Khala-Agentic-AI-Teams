import type { QualityGateResult, StrategyLabRecord } from '../../models';

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
 *   JSON.stringify'd (never recurses, never throws).
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
