import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { axe } from 'vitest-axe';
import { InlineBannerComponent, InlineBannerVariant } from './inline-banner.component';

// `color-contrast` is disabled because jsdom can't paint; contrast is enforced
// by the --kh-* token system + the SCSS contrast guard spec.
const axeOptions = { rules: { 'color-contrast': { enabled: false } } };

describe('InlineBannerComponent', () => {
  let fixture: ComponentFixture<InlineBannerComponent>;
  let component: InlineBannerComponent;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [InlineBannerComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(InlineBannerComponent);
    component = fixture.componentInstance;
  });

  it('creates and defaults to the error variant', () => {
    fixture.detectChanges();
    expect(component).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.kh-banner--error')).toBeTruthy();
  });

  it('renders the message inside the live region', () => {
    fixture.componentRef.setInput('message', 'Something failed');
    fixture.detectChanges();
    const region = fixture.nativeElement.querySelector('.kh-banner__msg') as HTMLElement;
    expect(region.textContent).toContain('Something failed');
  });

  // One assertion set per variant: correct modifier class, default icon, and the
  // assertive/polite live-region role the variant maps to.
  const cases: { variant: InlineBannerVariant; icon: string; role: 'alert' | 'status' }[] = [
    { variant: 'error', icon: 'error_outline', role: 'alert' },
    { variant: 'warning', icon: 'warning_amber', role: 'alert' },
    { variant: 'success', icon: 'check_circle', role: 'status' },
    { variant: 'info', icon: 'info', role: 'status' },
  ];

  for (const { variant, icon, role } of cases) {
    it(`renders the ${variant} variant with icon "${icon}" and role="${role}"`, () => {
      fixture.componentRef.setInput('variant', variant);
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector(`.kh-banner--${variant}`)).toBeTruthy();
      expect(fixture.nativeElement.querySelector('.kh-banner__icon')?.textContent?.trim()).toBe(icon);
      expect(fixture.nativeElement.querySelector(`.kh-banner__msg[role="${role}"]`)).toBeTruthy();
    });
  }

  it('honours an explicit icon override', () => {
    fixture.componentRef.setInput('icon', 'cloud_off');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.kh-banner__icon')?.textContent?.trim()).toBe('cloud_off');
  });

  // The `live` override decouples announcement urgency from the visual severity.
  it('forces a polite live region on a warning banner when live="polite"', () => {
    fixture.componentRef.setInput('variant', 'warning');
    fixture.componentRef.setInput('live', 'polite');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.kh-banner--warning')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('.kh-banner__msg[role="status"]')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('[role="alert"]')).toBeNull();
  });

  it('renders no live region when live="off"', () => {
    fixture.componentRef.setInput('variant', 'info');
    fixture.componentRef.setInput('live', 'off');
    fixture.detectChanges();
    const msg = fixture.nativeElement.querySelector('.kh-banner__msg') as HTMLElement;
    expect(msg.hasAttribute('role')).toBe(false);
  });

  it('collapses the action slot when nothing is projected', () => {
    fixture.detectChanges();
    const actions = fixture.nativeElement.querySelector('.kh-banner__actions') as HTMLElement;
    expect(actions.children.length).toBe(0);
  });

  it('has no axe violations for an error banner with a projected action', async () => {
    const host = TestBed.createComponent(BannerHostComponent);
    host.detectChanges();
    const alert = host.nativeElement.querySelector('[role="alert"]') as HTMLElement;
    // The projected control must be a sibling of the live region, never inside it.
    expect(alert.querySelector('button')).toBeNull();
    expect(host.nativeElement.querySelector('.kh-banner__actions button')).toBeTruthy();
    const results = await axe(host.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  }, 15000);
});

@Component({
  standalone: true,
  imports: [InlineBannerComponent],
  template: `
    <app-inline-banner variant="error" message="Failed to load.">
      <button type="button">Retry</button>
    </app-inline-banner>
  `,
})
class BannerHostComponent {}
