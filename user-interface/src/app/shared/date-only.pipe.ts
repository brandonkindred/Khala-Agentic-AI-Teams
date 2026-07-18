import { Pipe, PipeTransform } from '@angular/core';

/**
 * Trim a date/datetime string to its leading `YYYY-MM-DD` portion, e.g. for
 * dropping a timestamp's time-of-day. Passes `null`/`undefined` through
 * unchanged, matching `SlicePipe`'s behavior for the templates it replaces.
 */
@Pipe({ name: 'dateOnly', standalone: true })
export class DateOnlyPipe implements PipeTransform {
  transform(value: string | null | undefined): string | null | undefined {
    return value == null ? value : value.slice(0, 10);
  }
}
