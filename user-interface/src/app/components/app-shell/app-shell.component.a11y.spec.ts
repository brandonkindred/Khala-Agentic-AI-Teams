import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { AppShellComponent } from './app-shell.component';
import { expectNoAxeViolations } from '../../testing/a11y';

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

    await expectNoAxeViolations(fixture.nativeElement);
  });
});
