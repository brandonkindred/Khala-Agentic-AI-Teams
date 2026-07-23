import { formatPct, formatRatio, formatUsd } from './number-format';

describe('formatPct', () => {
  it('formats with the default 1 decimal and a trailing %', () => {
    expect(formatPct(12.345)).toBe('12.3%');
  });

  it('formats zero and negative values', () => {
    expect(formatPct(0)).toBe('0.0%');
    expect(formatPct(-5.06)).toBe('-5.1%');
  });

  it('honors an explicit decimals count', () => {
    expect(formatPct(12.345, 2)).toBe('12.35%');
    expect(formatPct(12, 0)).toBe('12%');
  });

  it('clamps a negative decimals count to zero', () => {
    expect(formatPct(12.34, -1)).toBe('12%');
  });
});

describe('formatRatio', () => {
  it('formats with the default 2 decimals and no suffix', () => {
    expect(formatRatio(1.5)).toBe('1.50');
  });

  it('formats zero and negative values', () => {
    expect(formatRatio(0)).toBe('0.00');
    expect(formatRatio(-2.006)).toBe('-2.01');
  });

  it('honors an explicit decimals count', () => {
    expect(formatRatio(1.5, 1)).toBe('1.5');
  });

  it('clamps a negative decimals count to zero', () => {
    expect(formatRatio(1.5, -1)).toBe('2');
  });
});

describe('formatUsd', () => {
  it('formats with a leading $ and thousands separators, 0 decimals by default', () => {
    expect(formatUsd(150000)).toBe('$150,000');
  });

  it('honors an explicit decimals count', () => {
    expect(formatUsd(150000.5, 2)).toBe('$150,000.50');
  });

  it('formats zero and negative values, with the sign before the $', () => {
    expect(formatUsd(0)).toBe('$0');
    expect(formatUsd(-150000)).toBe('-$150,000');
    expect(formatUsd(-1234.5, 2)).toBe('-$1,234.50');
  });
});
