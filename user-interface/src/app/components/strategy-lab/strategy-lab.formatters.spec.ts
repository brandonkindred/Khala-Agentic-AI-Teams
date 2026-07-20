import type { QualityGateResult, StrategyLabRecord, EntryRule, ExitRule, SizingRule } from '../../models';
import {
  ASSET_CLASS_ICONS,
  entryRuleRows,
  exitRuleRows,
  flattenToRows,
  gateIcon,
  gateSeverityClass,
  getAssetClassIcon,
  humanizeKey,
  publishabilitySkipLabel,
  returnColor,
  returnColorLabel,
  signalBriefRows,
  sizingRows,
  verdictColor,
  verdictLabel,
} from './strategy-lab.formatters';

function makeGate(overrides: Partial<QualityGateResult> = {}): QualityGateResult {
  return { gate_name: 'g', passed: false, details: '', severity: 'warning', ...overrides };
}

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

describe('returnColor', () => {
  it('is winning above 8%', () => {
    expect(returnColor(8.01)).toBe('winning');
  });

  it('is neutral at the 8% boundary and down to 0%', () => {
    expect(returnColor(8)).toBe('neutral');
    expect(returnColor(0)).toBe('neutral');
  });

  it('is losing below 0%', () => {
    expect(returnColor(-0.01)).toBe('losing');
  });
});

describe('returnColorLabel', () => {
  it('mirrors returnColor\'s boundaries with a text alternative', () => {
    expect(returnColorLabel(8.01)).toBe('Above target');
    expect(returnColorLabel(8)).toBe('Neutral');
    expect(returnColorLabel(0)).toBe('Neutral');
    expect(returnColorLabel(-0.01)).toBe('Negative');
  });
});

describe('getAssetClassIcon', () => {
  it('looks up a known asset class case-insensitively', () => {
    expect(getAssetClassIcon('crypto')).toBe(ASSET_CLASS_ICONS['crypto']);
    expect(getAssetClassIcon('CRYPTO')).toBe(ASSET_CLASS_ICONS['crypto']);
  });

  it('falls back to trending_up for an unknown asset class', () => {
    expect(getAssetClassIcon('bonds')).toBe('trending_up');
  });
});

describe('verdictLabel / verdictColor', () => {
  it('maps ready_for_live', () => {
    expect(verdictLabel('ready_for_live')).toBe('READY FOR LIVE');
    expect(verdictColor('ready_for_live')).toBe('winning');
  });

  it('maps not_performant', () => {
    expect(verdictLabel('not_performant')).toBe('NOT PERFORMANT');
    expect(verdictColor('not_performant')).toBe('losing');
  });

  it('falls back to inconclusive for null, undefined, or unknown values', () => {
    expect(verdictLabel(null)).toBe('INCONCLUSIVE');
    expect(verdictLabel(undefined)).toBe('INCONCLUSIVE');
    expect(verdictLabel('something_else')).toBe('INCONCLUSIVE');
    expect(verdictColor(null)).toBe('neutral');
    expect(verdictColor(undefined)).toBe('neutral');
  });
});

describe('gateIcon / gateSeverityClass', () => {
  it('shows a check icon for a passed gate regardless of isRemedied', () => {
    const gate = makeGate({ passed: true, severity: 'critical' });
    expect(gateIcon(gate, false)).toBe('check_circle');
    expect(gateIcon(gate, true)).toBe('check_circle');
    expect(gateSeverityClass(gate, false)).toBe('gate-critical');
  });

  it('shows a remedied icon/class for a failed-but-remedied gate', () => {
    const gate = makeGate({ passed: false, severity: 'critical' });
    expect(gateIcon(gate, true)).toBe('build_circle');
    expect(gateSeverityClass(gate, true)).toBe('gate-remedied');
  });

  it('distinguishes critical vs non-critical failures when not remedied', () => {
    expect(gateIcon(makeGate({ severity: 'critical' }), false)).toBe('cancel');
    expect(gateIcon(makeGate({ severity: 'warning' }), false)).toBe('warning');
    expect(gateIcon(makeGate({ severity: 'info' }), false)).toBe('warning');
  });

  it('derives the severity class from the gate when not remedied', () => {
    expect(gateSeverityClass(makeGate({ severity: 'info' }), false)).toBe('gate-info');
    expect(gateSeverityClass(makeGate({ severity: 'warning' }), false)).toBe('gate-warning');
  });
});

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

describe('entryRuleRows', () => {
  it('flattens a long entry rule with an indicator lhs and numeric threshold', () => {
    const rule: EntryRule = {
      kind: 'entry',
      side: 'long',
      when: { lhs: { name: 'rsi', params: { period: 14 } }, op: '<', rhs: 30 },
    };
    expect(entryRuleRows(rule)).toEqual([
      { label: 'Side', value: 'Long' },
      { label: 'Indicator', value: 'RSI(Period: 14)' },
      { label: 'Operator', value: '<' },
      { label: 'Threshold', value: '30' },
    ]);
  });

  it('labels a bar-field lhs as "Price Field" and humanizes cross_above/cross_below operators', () => {
    const rule: EntryRule = {
      kind: 'entry',
      side: 'short',
      when: { lhs: 'bar.close', op: 'cross_above', rhs: { name: 'sma', params: { period: 50 } } },
    };
    expect(entryRuleRows(rule)).toEqual([
      { label: 'Side', value: 'Short' },
      { label: 'Price Field', value: 'Close' },
      { label: 'Operator', value: 'crosses above' },
      { label: 'Threshold', value: 'SMA(Period: 50)' },
    ]);
  });

  it('appends a Note row only when note is set', () => {
    const rule: EntryRule = {
      kind: 'entry',
      side: 'long',
      when: { lhs: 'bar.volume', op: '>', rhs: 1000 },
      note: 'Confirms breakout volume',
    };
    const rows = entryRuleRows(rule);
    expect(rows[rows.length - 1]).toEqual({ label: 'Note', value: 'Confirms breakout volume' });
  });

  it('falls back to flattenToRows for the exact non-conforming ad hoc shape used in real test fixtures', () => {
    const legacyRule = { indicator: 'volume_zscore', operator: '>', value: 2 } as unknown as EntryRule;
    expect(entryRuleRows(legacyRule)).toEqual([
      { label: 'Indicator', value: 'volume_zscore' },
      { label: 'Operator', value: '>' },
      { label: 'Value', value: '2' },
    ]);
  });

  it('returns an empty array for null/undefined', () => {
    expect(entryRuleRows(null as unknown as EntryRule)).toEqual([]);
    expect(entryRuleRows(undefined as unknown as EntryRule)).toEqual([]);
  });

  it('stringifies a predicate side that is an object but not a recognized IndicatorRef shape', () => {
    const rule: EntryRule = {
      kind: 'entry',
      side: 'long',
      when: { lhs: { foo: 'bar' } as unknown as EntryRule['when']['lhs'], op: '>', rhs: 1 },
    };
    expect(entryRuleRows(rule)).toEqual([
      { label: 'Side', value: 'Long' },
      { label: 'Price Field', value: '{"foo":"bar"}' },
      { label: 'Operator', value: '>' },
      { label: 'Threshold', value: '1' },
    ]);
  });
});

describe('exitRuleRows', () => {
  it('flattens a stop_loss rule with an explicit basis', () => {
    const rule: ExitRule = { kind: 'stop_loss', pct: 0.05, basis: 'trailing_high', note: 'Wide stop for volatility' };
    expect(exitRuleRows(rule)).toEqual([
      { label: 'Type', value: 'Stop Loss' },
      { label: 'Stop Distance', value: '5.0%' },
      { label: 'Basis', value: 'Trailing High' },
      { label: 'Note', value: 'Wide stop for volatility' },
    ]);
  });

  it('defaults basis to Entry Price when unset', () => {
    const rule: ExitRule = { kind: 'stop_loss', pct: 0.02 };
    expect(exitRuleRows(rule)).toEqual([
      { label: 'Type', value: 'Stop Loss' },
      { label: 'Stop Distance', value: '2.0%' },
      { label: 'Basis', value: 'Entry Price' },
    ]);
  });

  it('flattens a take_profit rule', () => {
    const rule: ExitRule = { kind: 'take_profit', pct: 0.1 };
    expect(exitRuleRows(rule)).toEqual([
      { label: 'Type', value: 'Take Profit' },
      { label: 'Target', value: '10.0%' },
    ]);
  });

  it('flattens a signal_exit rule by reusing predicate rows', () => {
    const rule: ExitRule = {
      kind: 'signal_exit',
      when: { lhs: { name: 'rsi', params: { period: 14 } }, op: 'cross_below', rhs: 70 },
    };
    expect(exitRuleRows(rule)).toEqual([
      { label: 'Type', value: 'Signal Exit' },
      { label: 'Indicator', value: 'RSI(Period: 14)' },
      { label: 'Operator', value: 'crosses below' },
      { label: 'Threshold', value: '70' },
    ]);
  });

  it('falls back to flattenToRows for the exact non-conforming ad hoc shape used in real test fixtures', () => {
    const legacyRule = { indicator: 'days_held', operator: '>', value: 10 } as unknown as ExitRule;
    expect(exitRuleRows(legacyRule)).toEqual([
      { label: 'Indicator', value: 'days_held' },
      { label: 'Operator', value: '>' },
      { label: 'Value', value: '10' },
    ]);
  });

  it('falls back to flattenToRows for an unrecognized kind string', () => {
    const rule = { kind: 'time_stop', bars: 5 } as unknown as ExitRule;
    expect(exitRuleRows(rule)).toEqual([
      { label: 'Kind', value: 'time_stop' },
      { label: 'Bars', value: '5' },
    ]);
  });

  it('returns an empty array for null/undefined', () => {
    expect(exitRuleRows(null as unknown as ExitRule)).toEqual([]);
  });
});

describe('sizingRows', () => {
  it('flattens a fixed_fraction sizing rule using the real a11y-spec shape', () => {
    const sizing: SizingRule = { kind: 'fixed_fraction', fraction: 0.02 };
    expect(sizingRows(sizing)).toEqual([
      { label: 'Method', value: 'Fixed Fraction' },
      { label: 'Position Size', value: '2.0%' },
    ]);
  });

  it('flattens a volatility_target sizing rule', () => {
    const sizing: SizingRule = { kind: 'volatility_target', target_annual_vol: 0.15, note: 'Scale to vol' };
    expect(sizingRows(sizing)).toEqual([
      { label: 'Method', value: 'Volatility Target' },
      { label: 'Target Annual Volatility', value: '15.0%' },
      { label: 'Note', value: 'Scale to vol' },
    ]);
  });

  it('flattens a fixed_notional sizing rule', () => {
    const sizing: SizingRule = { kind: 'fixed_notional', notional_usd: 15000 };
    expect(sizingRows(sizing)).toEqual([
      { label: 'Method', value: 'Fixed Notional' },
      { label: 'Notional (USD)', value: '$15,000' },
    ]);
  });

  it('falls back to flattenToRows for the exact non-conforming ad hoc shape used in real test fixtures', () => {
    const legacySizing = { method: 'fixed_fraction', value: 0.1 } as unknown as SizingRule;
    expect(sizingRows(legacySizing)).toEqual([
      { label: 'Method', value: 'fixed_fraction' },
      { label: 'Value', value: '0.1' },
    ]);
  });

  it('returns an empty array for an empty object, matching the plain spec fixture', () => {
    expect(sizingRows({} as unknown as SizingRule)).toEqual([]);
  });

  it('returns an empty array for null/undefined', () => {
    expect(sizingRows(null as unknown as SizingRule)).toEqual([]);
  });
});

describe('flattenToRows', () => {
  it('returns an empty array for null, undefined, an empty object, and an empty array', () => {
    expect(flattenToRows(null)).toEqual([]);
    expect(flattenToRows(undefined)).toEqual([]);
    expect(flattenToRows({})).toEqual([]);
    expect(flattenToRows([])).toEqual([]);
  });

  it('flattens a plain object into one row per key, humanizing keys and stringifying values', () => {
    expect(flattenToRows({ target_annual_vol: 0.1, note: 'x' })).toEqual([
      { label: 'Target Annual Vol', value: '0.1' },
      { label: 'Note', value: 'x' },
    ]);
  });

  it('JSON-stringifies nested object/array values rather than dropping them', () => {
    expect(flattenToRows({ meta: { a: 1 } })).toEqual([{ label: 'Meta', value: '{"a":1}' }]);
    expect(flattenToRows({ tags: ['a', 'b'] })).toEqual([{ label: 'Tags', value: '["a","b"]' }]);
  });

  it('wraps a top-level primitive in a single Value row', () => {
    expect(flattenToRows('hello')).toEqual([{ label: 'Value', value: 'hello' }]);
    expect(flattenToRows(42)).toEqual([{ label: 'Value', value: '42' }]);
  });

  it('wraps a non-empty top-level array in a single stringified Value row', () => {
    expect(flattenToRows([1, 2, 3])).toEqual([{ label: 'Value', value: '[1,2,3]' }]);
  });
});

describe('signalBriefRows', () => {
  it('renders a friendly message for the documented { skipped, skipped_reason } shape', () => {
    expect(signalBriefRows({ skipped: true, skipped_reason: 'insufficient batch history' })).toEqual([
      { label: 'Signal Intelligence', value: 'Skipped — insufficient batch history' },
    ]);
  });

  it('falls back to a generic message when skipped but no reason is given', () => {
    expect(signalBriefRows({ skipped: true })).toEqual([
      { label: 'Signal Intelligence', value: 'Skipped — no reason given' },
    ]);
  });

  it('does not special-case a falsy skipped flag, flattening generically instead', () => {
    expect(signalBriefRows({ skipped: false, summary: 'Momentum confirmed by volume.' })).toEqual([
      { label: 'Skipped', value: 'false' },
      { label: 'Summary', value: 'Momentum confirmed by volume.' },
    ]);
  });

  it('generically flattens an arbitrary expert-brief object', () => {
    expect(signalBriefRows({ summary: 'Bullish', confidence: 0.8 })).toEqual([
      { label: 'Summary', value: 'Bullish' },
      { label: 'Confidence', value: '0.8' },
    ]);
  });

  it('returns an empty array for null', () => {
    expect(signalBriefRows(null)).toEqual([]);
  });
});

describe('humanizeKey', () => {
  it('Title-Cases every underscore-separated word', () => {
    expect(humanizeKey('target_annual_vol')).toBe('Target Annual Vol');
  });

  it('returns an empty string for an empty key', () => {
    expect(humanizeKey('')).toBe('');
  });

  it('capitalizes a single word', () => {
    expect(humanizeKey('note')).toBe('Note');
  });
});
