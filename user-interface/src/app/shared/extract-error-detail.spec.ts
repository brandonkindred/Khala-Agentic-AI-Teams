import { extractErrorDetail } from './extract-error-detail';

describe('extractErrorDetail', () => {
  it('prefers a FastAPI detail string', () => {
    expect(extractErrorDetail({ error: { detail: 'nope' } }, 'fallback')).toBe('nope');
  });

  it('falls back to the Error message when detail is absent or non-string', () => {
    expect(extractErrorDetail({ message: 'boom' }, 'fallback')).toBe('boom');
    // An array detail (FastAPI 422) is left to the global interceptor; here we
    // skip past it to the message / fallback.
    expect(extractErrorDetail({ error: { detail: [{ msg: 'x' }] }, message: 'boom' }, 'f')).toBe(
      'boom'
    );
  });

  it('uses the fallback when nothing usable is present', () => {
    expect(extractErrorDetail({}, 'fallback')).toBe('fallback');
    expect(extractErrorDetail(null, 'fallback')).toBe('fallback');
    expect(extractErrorDetail(undefined, 'fallback')).toBe('fallback');
  });
});
