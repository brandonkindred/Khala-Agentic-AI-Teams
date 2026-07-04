import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import {
  AVATAR_COLOR_OPTIONS,
  DEFAULT_AVATAR_COLOR,
  resolveAvatarColor,
} from './avatar-colors';
import { InitialsAvatarComponent, computeInitials } from './initials-avatar.component';

describe('computeInitials', () => {
  it('returns an empty string for empty and whitespace-only names', () => {
    expect(computeInitials('')).toBe('');
    expect(computeInitials('   ')).toBe('');
  });

  it('uppercases the first letter of a single word', () => {
    expect(computeInitials('brandon')).toBe('B');
  });

  it('takes first and last word initials for multi-word names', () => {
    expect(computeInitials('Brandon Kindred')).toBe('BK');
    // Middle names are skipped — first + last only.
    expect(computeInitials('Ada Grace Lovelace')).toBe('AL');
  });

  it('uppercases accented letters correctly', () => {
    expect(computeInitials('édouard ångström')).toBe('ÉÅ');
  });

  it('treats decomposed (NFD) and precomposed (NFC) input identically', () => {
    // Built via normalize('NFD') so the input is unambiguously 'e' + U+0301
    // combining acute etc. Grapheme segmentation + NFC normalization must
    // keep the accents ('\u00c9', '\u00c5'), not strip them to bare 'E'/'A'.
    const precomposed = '\u00e9douard \u00e5ngstr\u00f6m';
    const decomposed = precomposed.normalize('NFD');
    expect(decomposed).not.toBe(precomposed); // really decomposed
    expect(computeInitials(decomposed)).toBe('\u00c9\u00c5');
    expect(computeInitials(decomposed)).toBe(computeInitials(precomposed));
  });

  it('does not split surrogate pairs', () => {
    // '𝔘' is outside the BMP; charAt(0) would return half a code point.
    expect(computeInitials('𝔘nicode')).toBe('𝔘');
  });

  it('keeps ZWJ emoji sequences intact as a single grapheme', () => {
    // Family emoji = 5 code points joined by ZWJs; code-point slicing would
    // strand the first person and drop the joiners.
    expect(computeInitials('👨‍👩‍👧 family')).toBe('👨‍👩‍👧F');
  });

  it('tolerates leading/trailing/repeated whitespace', () => {
    expect(computeInitials('  jane   doe  ')).toBe('JD');
  });
});

describe('AVATAR_COLOR_OPTIONS', () => {
  it('derives every fill from its cssVar so the two cannot drift', () => {
    for (const option of AVATAR_COLOR_OPTIONS) {
      expect(option.fill).toBe(`var(${option.cssVar})`);
    }
  });

  it('keeps amber as the intended default color', () => {
    // The default is derived from the first palette entry, so this literal
    // pin is what catches an accidental palette reordering.
    expect(DEFAULT_AVATAR_COLOR).toBe('amber');
  });
});

describe('resolveAvatarColor', () => {
  it('round-trips every palette key', () => {
    for (const option of AVATAR_COLOR_OPTIONS) {
      expect(resolveAvatarColor(option.key)).toBe(option);
    }
  });

  it('falls back to the default for unknown or non-string keys', () => {
    // Values come from the free-form preferences JSONB — anything goes.
    for (const garbage of ['magenta', undefined, null, 42, {}, []]) {
      expect(resolveAvatarColor(garbage).key).toBe(DEFAULT_AVATAR_COLOR);
    }
  });
});

describe('InitialsAvatarComponent', () => {
  let fixture: ComponentFixture<InitialsAvatarComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [InitialsAvatarComponent, NoopAnimationsModule],
    }).compileComponents();
    fixture = TestBed.createComponent(InitialsAvatarComponent);
  });

  afterEach(() => {
    fixture?.destroy();
  });

  function circle(): HTMLElement {
    return (fixture.nativeElement as HTMLElement).querySelector('.ia-circle') as HTMLElement;
  }

  it('renders the initials with the requested size and a theme-token fill', () => {
    fixture.componentRef.setInput('name', 'Brandon Kindred');
    fixture.componentRef.setInput('colorKey', 'green');
    fixture.componentRef.setInput('size', 64);
    fixture.detectChanges();
    const el = circle();
    expect(el.textContent?.trim()).toBe('BK');
    expect(el.style.width).toBe('64px');
    expect(el.style.height).toBe('64px');
    expect(el.style.background).toContain('var(--kh-success)');
  });

  it('is hidden from the accessibility tree (hosts supply the name)', () => {
    fixture.detectChanges();
    expect(circle().getAttribute('aria-hidden')).toBe('true');
  });

  it('renders a person icon fallback when the name is blank', () => {
    fixture.componentRef.setInput('name', '   ');
    fixture.detectChanges();
    const icon = circle().querySelector('mat-icon');
    expect(icon).toBeTruthy();
    expect(icon?.textContent?.trim()).toBe('person');
  });

  it('falls back to the default color for an unknown key', () => {
    fixture.componentRef.setInput('name', 'X');
    fixture.componentRef.setInput('colorKey', 'not-a-color');
    fixture.detectChanges();
    expect(circle().style.background).toContain('var(--kh-accent)');
  });
});
