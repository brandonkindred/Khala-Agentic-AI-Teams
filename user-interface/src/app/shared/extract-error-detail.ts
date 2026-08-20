export interface ExtractErrorDetailOptions {
  /**
   * When `true`, a FastAPI 422 validation-error array (`detail: [{msg: …}]`)
   * is joined into a semicolon-separated string. When `false` (default), array
   * details are skipped so the global error interceptor can toast them instead.
   */
  joinValidationArray?: boolean;
}

/**
 * Pull a human-readable message from an HTTP error for an inline error field.
 *
 * Prefers FastAPI's `error.detail` string, then (optionally) a joined
 * validation-error array, then the Error `message`, then the caller's fallback.
 *
 * By default, 422 array details are left to the global interceptor's toast.
 * Components that suppress the global toast (via `SKIP_ERROR_NOTIFY`) should
 * pass `{ joinValidationArray: true }` to handle all error shapes inline.
 */
export function extractErrorDetail(
  err: unknown,
  fallback: string,
  options?: ExtractErrorDetailOptions,
): string {
  const e = err as { error?: { detail?: unknown }; message?: unknown } | null | undefined;
  const detail = e?.error?.detail;
  if (typeof detail === 'string' && detail) {
    return detail;
  }
  if (options?.joinValidationArray && Array.isArray(detail)) {
    const msgs = detail.map((d: { msg?: string }) => d?.msg).filter(Boolean);
    if (msgs.length > 0) return msgs.join('; ');
  }
  if (typeof e?.message === 'string' && e.message) return e.message;
  return fallback;
}
