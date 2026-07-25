import { describe, expect, it } from 'vitest';
import { buildLogMessage, describeRunStatus, CONNECTION_LOST_MESSAGE } from './strategy-lab-log-message';
import type { StrategyLabProgressEvent } from '../models';

const progressEvent = (overrides: Partial<StrategyLabProgressEvent> = {}): StrategyLabProgressEvent => ({
  type: 'progress',
  cycle_index: 0,
  phase: 'ideating',
  ...overrides,
});

describe('buildLogMessage', () => {
  describe('ideating', () => {
    it('reports the started message', () => {
      expect(buildLogMessage('ideating', 'started', progressEvent())).toBe(
        'Ideating new trading strategy & generating code...',
      );
    });

    it('reports the asset class on completion', () => {
      expect(
        buildLogMessage('ideating', 'completed', progressEvent({ strategy: { asset_class: 'stocks' } as never })),
      ).toBe('Strategy ideated — stocks asset class');
    });

    it('falls back to unknown asset class when missing', () => {
      expect(buildLogMessage('ideating', 'completed', progressEvent())).toBe('Strategy ideated — unknown asset class');
    });

    it('falls back to the generic in-progress message for an unmapped sub-phase', () => {
      expect(buildLogMessage('ideating', 'unmapped', progressEvent())).toBe('Ideating...');
    });
  });

  describe('coding', () => {
    it('reports the started message', () => {
      expect(buildLogMessage('coding', 'started', progressEvent())).toBe(
        'Validating strategy spec and code safety...',
      );
    });

    it('reports checks passed/total on completion', () => {
      expect(
        buildLogMessage('coding', 'completed', progressEvent({ checks_total: 5, checks_passed: 5 })),
      ).toBe('Code validated (5 checks, 5 passed)');
    });

    it('reports the critical-issue count on failure', () => {
      expect(
        buildLogMessage('coding', 'failed', progressEvent({ checks_total: 5, checks_passed: 3 })),
      ).toBe('Validation failed (2 critical issue(s))');
    });

    it('reports the refinement round and failing phase while refining', () => {
      expect(
        buildLogMessage(
          'coding',
          'refining',
          progressEvent({ refinement_round: 1, failure_phase: 'backtesting' }),
        ),
      ).toBe('Refining code (round 2/10) — fixing backtesting...');
    });

    it('reports the changes made once refined', () => {
      expect(
        buildLogMessage('coding', 'refined', progressEvent({ changes_made: 'tightened stop loss' })),
      ).toBe('Code refined — tightened stop loss');
    });

    it('falls back to the generic in-progress message for an unmapped sub-phase', () => {
      expect(buildLogMessage('coding', 'unmapped', progressEvent())).toBe('Coding...');
    });
  });

  describe('backtesting', () => {
    it('reports fetching data', () => {
      expect(buildLogMessage('backtesting', 'fetching_data', progressEvent())).toBe(
        'Fetching historical market data...',
      );
    });

    it('reports symbols/bars once data is loaded', () => {
      expect(
        buildLogMessage('backtesting', 'data_loaded', progressEvent({ symbols_count: 3, bars_count: 1234 })),
      ).toBe('Market data loaded (3 symbols, 1,234 bars)');
    });

    it('reports running the sandbox', () => {
      expect(buildLogMessage('backtesting', 'running_code', progressEvent())).toBe(
        'Executing strategy backtest in sandbox...',
      );
    });

    it('reports trades and execution time on completion', () => {
      expect(
        buildLogMessage('backtesting', 'completed', progressEvent({ trades_count: 12, execution_time: 4.567 })),
      ).toBe('Backtest complete — 12 trades in 4.6s');
    });

    it('falls back to the generic in-progress message for an unmapped sub-phase', () => {
      expect(buildLogMessage('backtesting', 'unmapped', progressEvent())).toBe('Backtesting...');
    });
  });

  describe('analyzing', () => {
    it('reports drafting the narrative', () => {
      expect(buildLogMessage('analyzing', 'draft', progressEvent())).toBe('Generating analysis narrative...');
    });

    it('reports self-review', () => {
      expect(buildLogMessage('analyzing', 'review', progressEvent())).toBe(
        'Self-reviewing analysis against metrics...',
      );
    });

    it('reports WINNING/LOSING on completion', () => {
      expect(buildLogMessage('analyzing', 'completed', progressEvent({ is_winning: true }))).toBe(
        'Analysis complete — WINNING',
      );
      expect(buildLogMessage('analyzing', 'completed', progressEvent({ is_winning: false }))).toBe(
        'Analysis complete — LOSING',
      );
    });

    it('falls back to the generic in-progress message for an unmapped sub-phase', () => {
      expect(buildLogMessage('analyzing', 'unmapped', progressEvent())).toBe('Analyzing...');
    });
  });

  it('falls back to a generic phase/sub-phase message for an unrecognized phase', () => {
    expect(buildLogMessage('phase_transition', 'foo', progressEvent())).toBe('phase_transition — foo');
  });

  it('falls back to "processing" when sub-phase is undefined for an unrecognized phase', () => {
    expect(buildLogMessage('phase_transition', undefined, progressEvent())).toBe('phase_transition — processing');
  });
});

describe('describeRunStatus', () => {
  it('returns the connection-lost message for a null status', () => {
    expect(describeRunStatus(null)).toBe(CONNECTION_LOST_MESSAGE);
  });

  it('reports a failed run', () => {
    expect(describeRunStatus({ status: 'failed' })).toBe('Strategy Lab run failed.');
  });

  it('reports a cancelled run', () => {
    expect(describeRunStatus({ status: 'cancelled' })).toBe('Strategy Lab run cancelled.');
  });

  it('reports an interrupted run', () => {
    expect(describeRunStatus({ status: 'interrupted' })).toBe('Strategy Lab run interrupted.');
  });

  it('reports errors from an explicit completed_with_errors status', () => {
    expect(describeRunStatus({ status: 'completed_with_errors' })).toBe('Strategy Lab run finished with errors.');
  });

  it('reports errors from a non-zero errored_cycles count regardless of status', () => {
    expect(describeRunStatus({ status: 'completed', errored_cycles: 1 })).toBe(
      'Strategy Lab run finished with errors.',
    );
  });

  it('reports skipped strategies when there are no errors', () => {
    expect(describeRunStatus({ status: 'completed', skipped_cycles: 2 })).toBe(
      'Strategy Lab run finished with some strategies skipped.',
    );
  });

  it('reports a clean completion when there are no errors or skips', () => {
    expect(describeRunStatus({ status: 'completed' })).toBe('Strategy Lab run complete.');
  });
});
