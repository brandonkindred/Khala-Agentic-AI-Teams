import type { QualityGateResult } from '../../models';

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
