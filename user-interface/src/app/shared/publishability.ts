import type { StrategyLabRecord } from '../models';

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
