import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { MatIconModule } from '@angular/material/icon';
import { AvatarColorOption, resolveAvatarColor } from './avatar-colors';

// Grapheme-cluster segmentation keeps combining marks (NFD 'é' = 'e' + U+0301)
// and ZWJ emoji sequences attached to their base — Array.from would split them.
const GRAPHEMES = new Intl.Segmenter(undefined, { granularity: 'grapheme' });

function firstGrapheme(word: string): string {
  // Callers pass non-empty words, so the segment at index 0 always exists.
  return GRAPHEMES.segment(word).containing(0).segment;
}

/**
 * Compute display initials from a person's name.
 *
 * Preconditions: none — any string is accepted.
 * Postconditions: returns '' for an empty/whitespace-only name (callers render
 * a fallback); for one word, the first grapheme cluster uppercased; for two or
 * more words, the first grapheme clusters of the first and last words
 * uppercased. Whole graphemes (not code points or UTF-16 units) are taken, so
 * surrogate pairs, combining marks, and ZWJ emoji stay intact; the result is
 * NFC-normalized so decomposed and precomposed input yield identical output.
 */
export function computeInitials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return '';
  const first = firstGrapheme(words[0]);
  const initials = words.length === 1 ? first : first + firstGrapheme(words[words.length - 1]);
  return initials.toLocaleUpperCase().normalize('NFC');
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
      [style.width.px]="size()"
      [style.height.px]="size()"
      [style.fontSize.px]="size() * 0.4"
      [style.background]="color().fill"
    >
      @if (initials()) {
        {{ initials() }}
      } @else {
        <mat-icon [style.fontSize.px]="size() * 0.6" [style.width.px]="size() * 0.6" [style.height.px]="size() * 0.6"
          >person</mat-icon
        >
      }
    </span>
  `,
  styleUrl: './initials-avatar.component.scss',
  // Signal inputs + computed derivations: `initials`/`color` recompute only when
  // name/colorKey change (not every render), and OnPush skips unrelated ticks.
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class InitialsAvatarComponent {
  /** Name the initials are derived from; blank renders the icon fallback. */
  readonly name = input('');
  /** Palette color key from `preferences` (untrusted; unknown → default). */
  readonly colorKey = input<string | null>(null);
  /** Diameter of the circle in pixels. */
  readonly size = input(40);

  /** Initials derived from `name`, memoized until it changes. */
  readonly initials = computed(() => computeInitials(this.name()));

  /** Palette option resolved from `colorKey`, memoized until it changes. */
  readonly color = computed<AvatarColorOption>(() => resolveAvatarColor(this.colorKey()));
}
