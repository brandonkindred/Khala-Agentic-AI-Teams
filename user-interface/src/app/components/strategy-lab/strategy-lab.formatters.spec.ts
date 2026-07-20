import type { QualityGateResult, StrategyLabRecord } from '../../models';
import {
  ASSET_CLASS_ICONS,
  flattenObjectRows,
  gateIcon,
  gateSeverityClass,
  getAssetClassIcon,
  humanizeKey,
  publishabilitySkipLabel,
  returnColor,
  returnColorLabel,
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

describe('humanizeKey', () => {
  it('title-cases a snake_case key', () => {
    expect(humanizeKey('target_annual_vol')).toBe('Target Annual Vol');
  });

  it('title-cases a single-word key', () => {
    expect(humanizeKey('kind')).toBe('Kind');
  });
});

describe('flattenObjectRows', () => {
  it('returns an empty array for null/undefined', () => {
    expect(flattenObjectRows(null)).toEqual([]);
    expect(flattenObjectRows(undefined)).toEqual([]);
  });

  it('falls back to a single "Value" row for a primitive, never throwing', () => {
    expect(flattenObjectRows(42)).toEqual([{ label: 'Value', value: '42' }]);
    expect(flattenObjectRows('plain string')).toEqual([{ label: 'Value', value: 'plain string' }]);
  });

  it('falls back to a single "Value" row for an array, never throwing', () => {
    expect(flattenObjectRows([1, 2, 3])).toEqual([{ label: 'Value', value: '1,2,3' }]);
  });

  it('flattens a flat object into humanized { label, value } rows, in key order', () => {
    expect(flattenObjectRows({ indicator: 'rsi', operator: '>', value: 30 })).toEqual([
      { label: 'Indicator', value: 'rsi' },
      { label: 'Operator', value: '>' },
      { label: 'Value', value: '30' },
    ]);
  });

  it('drops keys whose value is null or undefined, rather than rendering them', () => {
    expect(flattenObjectRows({ kind: 'stop_loss', pct: 5, note: null, basis: undefined })).toEqual([
      { label: 'Kind', value: 'stop_loss' },
      { label: 'Pct', value: '5' },
    ]);
  });

  it('JSON.stringifies a nested-object value rather than recursing or throwing', () => {
    expect(
      flattenObjectRows({ side: 'long', when: { lhs: 'rsi', op: '>', rhs: 60 } }),
    ).toEqual([
      { label: 'Side', value: 'long' },
      { label: 'When', value: '{"lhs":"rsi","op":">","rhs":60}' },
    ]);
  });

  it('handles an empty object by returning no rows', () => {
    expect(flattenObjectRows({})).toEqual([]);
  });
});
