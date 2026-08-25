import { clamp } from './clamp.util';

describe('clamp', () => {
  it('returns the value unchanged when already within bounds', () => {
    expect(clamp(5, 1, 10)).toBe(5);
  });

  it('clamps a value above the max down to the max', () => {
    expect(clamp(999, 1, 25)).toBe(25);
  });

  it('clamps a value below the min up to the min', () => {
    expect(clamp(-5, 1, 25)).toBe(1);
  });

  it('floors a non-integer value within bounds', () => {
    expect(clamp(5.9, 1, 10)).toBe(5);
  });

  it('falls back to min for a non-finite value (e.g. NaN from a cleared number input)', () => {
    expect(clamp(NaN, 1, 25)).toBe(1);
    expect(clamp(Infinity, 1, 25)).toBe(1);
  });
});
