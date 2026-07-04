/**
 * Pull a human-readable message from an HTTP error for an inline error field.
 *
 * Prefers FastAPI's `error.detail` string, then the Error `message`, then the
 * caller's fallback. The array-of-`{msg}` 422 shape is handled centrally by the
 * global error interceptor (which toasts it); this is for the component-level
 * `error`/`scanError` fields that render inline, replacing the copy-pasted
 * `err?.error?.detail ?? err?.message ?? '…'` chain.
 */
export function extractErrorDetail(err: unknown, fallback: string): string {
  const e = err as { error?: { detail?: unknown }; message?: string } | null | undefined;
  const detail = e?.error?.detail;
  if (typeof detail === 'string' && detail) {
    return detail;
  }
  return e?.message ?? fallback;
}
