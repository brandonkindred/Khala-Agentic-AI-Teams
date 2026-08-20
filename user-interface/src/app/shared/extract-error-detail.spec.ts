import { extractErrorDetail } from './extract-error-detail';

describe('extractErrorDetail', () => {
  it('prefers a FastAPI detail string', () => {
    expect(extractErrorDetail({ error: { detail: 'nope' } }, 'fallback')).toBe('nope');
  });

  it('falls back to the Error message when detail is absent or non-string', () => {
    expect(extractErrorDetail({ message: 'boom' }, 'fallback')).toBe('boom');
  });

  it('uses the fallback when nothing usable is present', () => {
    expect(extractErrorDetail({}, 'fallback')).toBe('fallback');
    expect(extractErrorDetail(null, 'fallback')).toBe('fallback');
    expect(extractErrorDetail(undefined, 'fallback')).toBe('fallback');
  });

  it('returns fallback for an empty detail string', () => {
    expect(extractErrorDetail({ error: { detail: '' } }, 'fallback')).toBe('fallback');
  });

  describe('without joinValidationArray (default)', () => {
    it('skips array detail and falls back to message', () => {
      expect(
        extractErrorDetail({ error: { detail: [{ msg: 'x' }] }, message: 'boom' }, 'f'),
      ).toBe('boom');
    });

    it('skips array detail and falls back to fallback when no message', () => {
      expect(extractErrorDetail({ error: { detail: [{ msg: 'x' }] } }, 'fallback')).toBe(
        'fallback',
      );
    });
  });

  describe('with joinValidationArray: true', () => {
    const opts = { joinValidationArray: true } as const;

    it('joins the msg fields of a validation-error detail array', () => {
      const err = { error: { detail: [{ msg: 'field x' }, { msg: 'field y' }, {}] } };
      expect(extractErrorDetail(err, 'fallback', opts)).toBe('field x; field y');
    });

    it('falls back to message when the detail array has no messages', () => {
      expect(
        extractErrorDetail({ error: { detail: [{}, {}] }, message: 'net' }, 'fallback', opts),
      ).toBe('net');
    });

    it('falls back to fallback when the detail array has no messages and no message', () => {
      expect(extractErrorDetail({ error: { detail: [{}, {}] } }, 'fallback', opts)).toBe(
        'fallback',
      );
    });

    it('still prefers a string detail over the array path', () => {
      expect(extractErrorDetail({ error: { detail: 'direct' } }, 'fallback', opts)).toBe(
        'direct',
      );
    });
  });
});
