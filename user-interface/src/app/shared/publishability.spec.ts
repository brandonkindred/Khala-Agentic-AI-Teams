import type { StrategyLabRecord } from '../models';
import { publishabilitySkipLabel } from './publishability';

function makeRecord(overrides: Partial<StrategyLabRecord> = {}): StrategyLabRecord {
  return {
    lab_record_id: 'lab-1',
    is_winning: true,
    is_publishable: false,
    strategy_rationale: '',
    analysis_narrative: '',
    created_at: '',
    strategy: {} as never,
    backtest: {} as never,
    ...overrides,
  };
}

describe('publishabilitySkipLabel', () => {
  it('prefers publishability_skip_reason over paper_trading_skipped_reason', () => {
    expect(
      publishabilitySkipLabel(
        makeRecord({
          publishability_skip_reason: 'realism_failed',
          paper_trading_skipped_reason: 'realism_failed,alignment_unresolved',
        }),
      ),
    ).toBe('realism_failed');
  });

  it('falls back to paper_trading_skipped_reason when no publishability_skip_reason is set', () => {
    expect(
      publishabilitySkipLabel(makeRecord({ paper_trading_skipped_reason: 'alignment_unresolved' })),
    ).toBe('alignment_unresolved');
  });

  it('treats the not_winning/disabled reasons as non-answers, returning null', () => {
    expect(publishabilitySkipLabel(makeRecord({ paper_trading_skipped_reason: 'not_winning' }))).toBeNull();
    expect(publishabilitySkipLabel(makeRecord({ paper_trading_skipped_reason: 'disabled' }))).toBeNull();
  });

  it('returns null when neither reason is present', () => {
    expect(publishabilitySkipLabel(makeRecord())).toBeNull();
  });
});
