import { LatestOnly } from './latest-only';

describe('LatestOnly', () => {
  it('treats only the most recently issued token as current', () => {
    const guard = new LatestOnly();
    const a = guard.next();
    const b = guard.next();
    expect(guard.isCurrent(a)).toBe(false); // superseded
    expect(guard.isCurrent(b)).toBe(true);
  });

  it('discards a stale token even after returning to an equal-looking value', () => {
    const guard = new LatestOnly();
    const first = guard.next(); // 1
    guard.next(); // 2
    guard.next(); // 3
    // A later token is never equal to an earlier one — no A→B→A false match.
    expect(guard.isCurrent(first)).toBe(false);
  });

  it('starts with no current token', () => {
    const guard = new LatestOnly();
    expect(guard.isCurrent(0)).toBe(true); // seq starts at 0; nothing issued yet
    expect(guard.isCurrent(1)).toBe(false);
  });
});
