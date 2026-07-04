import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { axe } from 'vitest-axe';
import { AppShellComponent } from './app-shell.component';

// `color-contrast` is disabled because jsdom can't paint, so axe can't compute
// composited colours (and hangs on HTMLCanvasElement.getContext). Contrast is
// enforced separately by src/styles/scss-contrast-guard.spec.ts + browser axe.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

describe('AppShellComponent a11y', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AppShellComponent, NoopAnimationsModule],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
  });

  it('has no axe violations in the primary navigation shell', async () => {
    const fixture = TestBed.createComponent(AppShellComponent);
    fixture.detectChanges();

    // Guard: the nav actually rendered, so axe isn't auditing an empty DOM.
    expect(fixture.nativeElement.querySelector('nav[aria-label="Main navigation"]')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.nav-group-trigger')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.footer-profile-link')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });
});
