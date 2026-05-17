import { AbstractControl, ValidationErrors, ValidatorFn } from '@angular/forms';

/**
 * Reject non-integer numeric values. Used for indicator params declared as
 * `kind: 'int'` in INDICATOR_SPECS — the backend Pydantic validator rejects
 * floats, so we surface that as a client-side error instead of a 422.
 */
export const integerValidator: ValidatorFn = (ctrl: AbstractControl): ValidationErrors | null => {
  const v = ctrl.value;
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  if (!Number.isFinite(n) || !Number.isInteger(n)) return { notInteger: true };
  return null;
};
