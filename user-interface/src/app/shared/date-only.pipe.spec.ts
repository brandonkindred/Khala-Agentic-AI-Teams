import { DateOnlyPipe } from './date-only.pipe';

describe('DateOnlyPipe', () => {
  const pipe = new DateOnlyPipe();

  it('trims an ISO datetime string to its date prefix', () => {
    expect(pipe.transform('2026-07-18T14:32:00Z')).toBe('2026-07-18');
  });

  it('passes a plain 10-char date string through unchanged', () => {
    expect(pipe.transform('2026-07-18')).toBe('2026-07-18');
  });

  it('passes null and undefined through unchanged', () => {
    expect(pipe.transform(null)).toBeNull();
    expect(pipe.transform(undefined)).toBeUndefined();
  });

  it('returns a short or empty string unchanged (slice is a no-op past the end)', () => {
    expect(pipe.transform('')).toBe('');
    expect(pipe.transform('2026')).toBe('2026');
  });
});
