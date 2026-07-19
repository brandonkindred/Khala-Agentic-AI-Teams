import type { QualityGateResult } from '../../models';
import {
  ASSET_CLASS_ICONS,
  gateIcon,
  gateSeverityClass,
  getAssetClassIcon,
  returnColor,
  returnColorLabel,
  verdictColor,
  verdictLabel,
} from './strategy-lab.formatters';

function makeGate(overrides: Partial<QualityGateResult> = {}): QualityGateResult {
  return { gate_name: 'g', passed: false, details: '', severity: 'warning', ...overrides };
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
