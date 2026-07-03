import { ChangeDetectionStrategy, Component, Input } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { AvatarColorOption, resolveAvatarColor } from './avatar-colors';

/**
 * Compute display initials from a person's name.
 *
 * Preconditions: none — any string is accepted.
 * Postconditions: returns '' for an empty/whitespace-only name (callers render
 * a fallback); for one word, the first code point uppercased; for two or more
 * words, the first code points of the first and last words uppercased.
 * Code points (not UTF-16 units) are taken so surrogate pairs aren't split,
 * and `toLocaleUpperCase` keeps accented letters intact (é → É).
 */
export function computeInitials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return '';
  const first = Array.from(words[0])[0];
  if (words.length === 1) return first.toLocaleUpperCase();
  const last = Array.from(words[words.length - 1])[0];
  return (first + last).toLocaleUpperCase();
}

/**
 * Circular initials avatar filled with a named palette color.
 *
 * Renders the initials derived from `name`, or a `person` icon when the name
 * is blank. The circle is `aria-hidden`: hosts must place the accessible name
 * (e.g. the display-name text or field) adjacent to it.
 *
 * Invariants: the rendered fill always comes from a `--kh-*` theme token
 * (unknown `colorKey` values resolve to the default palette color), and the
 * circle is `size` × `size` pixels.
 */
@Component({
  selector: 'app-initials-avatar',
  standalone: true,
  imports: [MatIconModule],
  template: `
    <span
      class="ia-circle"
      aria-hidden="true"
      [style.width.px]="size"
      [style.height.px]="size"
      [style.fontSize.px]="size * 0.4"
      [style.background]="'var(' + color.cssVar + ')'"
    >
      @if (initials) {
        {{ initials }}
      } @else {
        <mat-icon [style.fontSize.px]="size * 0.6" [style.width.px]="size * 0.6" [style.height.px]="size * 0.6"
          >person</mat-icon
        >
      }
    </span>
  `,
  styleUrl: './initials-avatar.component.scss',
  // Inputs are plain values, so OnPush limits the template getters to ticks
  // where name/colorKey/size actually changed instead of every parent tick.
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InitialsAvatarComponent {
  /** Name the initials are derived from; blank renders the icon fallback. */
  @Input() name = '';
  /** Palette color key from `preferences` (untrusted; unknown → default). */
  @Input() colorKey: string | null = null;
  /** Diameter of the circle in pixels. */
  @Input() size = 40;

  get initials(): string {
    return computeInitials(this.name);
  }

  get color(): AvatarColorOption {
    return resolveAvatarColor(this.colorKey);
  }
}
