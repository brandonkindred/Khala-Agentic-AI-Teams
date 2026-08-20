import {
  HttpContext,
  HttpContextToken,
  HttpInterceptorFn,
  HttpErrorResponse,
  HttpStatusCode,
} from '@angular/common/http';
import { inject } from '@angular/core';
import { MatSnackBar } from '@angular/material/snack-bar';
import { catchError, throwError } from 'rxjs';

export { extractErrorDetail } from '../shared/extract-error-detail';

/**
 * Set on a request's `HttpContext` to suppress the global error toast for that
 * request (the caller handles failure itself — e.g. background/decorative
 * fetches that must not surface an error banner over an unrelated page).
 */
export const SKIP_ERROR_NOTIFY = new HttpContextToken<boolean>(() => false);

/**
 * Build an `HttpContext` that opts a request out of the global error toast, for
 * callers that render their own inline error (e.g. the config dashboards).
 *
 * Preconditions: none.
 * Postconditions: returns a fresh context with `SKIP_ERROR_NOTIFY` set true;
 * never mutates a shared context.
 */
export function skipErrorNotify(): HttpContext {
  return new HttpContext().set(SKIP_ERROR_NOTIFY, true);
}

/**
 * HTTP interceptor that catches API errors and displays user-friendly messages via MatSnackBar.
 * Re-throws the error so callers can still handle it. Requests carrying
 * `SKIP_ERROR_NOTIFY` in their context are re-thrown without a toast.
 */
export const errorHandlerInterceptor: HttpInterceptorFn = (req, next) => {
  const snackBar = inject(MatSnackBar);

  return next(req).pipe(
    catchError((err: unknown) => {
      if (!req.context.get(SKIP_ERROR_NOTIFY)) {
        const message = formatErrorMessage(err);
        snackBar.open(message, 'Close', {
          duration: 6000,
          horizontalPosition: 'end',
          verticalPosition: 'top',
          // Errors must interrupt: polite announcements are routinely missed.
          politeness: 'assertive',
          // Severity styling from the design system (red border accent).
          panelClass: 'kh-snack-error',
        });
      }
      return throwError(() => err);
    })
  );
};

function formatErrorMessage(err: unknown): string {
  if (!(err instanceof HttpErrorResponse)) {
    return 'An unexpected error occurred.';
  }

  const status = err.status;
  const statusText = err.statusText ?? 'Unknown error';

  switch (status) {
    case HttpStatusCode.NotFound:
      return `Not found: ${err.url ?? statusText}`;
    // FastAPI reports request-validation failures as 422 with an array-of-{msg}
    // detail; both validation shapes format identically.
    case HttpStatusCode.BadRequest:
    case HttpStatusCode.UnprocessableEntity:
      return formatValidationError(err) ?? `Bad request: ${statusText}`;
    case HttpStatusCode.Unauthorized:
      return 'Unauthorized. Please check your credentials.';
    case HttpStatusCode.Forbidden:
      return 'Access forbidden.';
    case HttpStatusCode.InternalServerError:
      return `Server error: ${formatServerError(err)}`;
    case HttpStatusCode.ServiceUnavailable: {
      // A 503 detail explains what is unavailable and how to fix it (e.g.
      // "Career profile storage requires Postgres…") — don't flatten it.
      const detail = err.error?.detail;
      return typeof detail === 'string' && detail
        ? detail
        : 'Service temporarily unavailable. Please try again later.';
    }
    case 0:
      return 'Network error. Please check your connection and that the API is running.';
    default:
      return `Error ${status}: ${formatServerError(err)}`;
  }
}

function formatValidationError(err: HttpErrorResponse): string | null {
  const detail = err.error?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d: { msg?: string }) => d.msg)
      .filter(Boolean);
    return msgs.length > 0 ? msgs.join('; ') : null;
  }
  return null;
}

function formatServerError(err: HttpErrorResponse): string {
  const detail = err.error?.detail;
  if (typeof detail === 'string') return detail;
  return err.error?.message ?? err.message ?? err.statusText ?? 'Unknown error';
}
