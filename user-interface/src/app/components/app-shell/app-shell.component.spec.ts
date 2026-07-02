import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { AppShellComponent } from './app-shell.component';

describe('AppShellComponent', () => {
  let component: AppShellComponent;
  let fixture: ComponentFixture<AppShellComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AppShellComponent, NoopAnimationsModule],
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();

    fixture = TestBed.createComponent(AppShellComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  afterEach(() => TestBed.resetTestingModule());

  it('should create', () => {
    expect(component).toBeTruthy();
  });

  it('isActive should return true when router url starts with path', () => {
    (component as any).router = { url: '/dashboard' };
    expect(component.isActive('/dashboard')).toBe(true);
    expect(component.isActive('/')).toBe(true);
  });

  it('isActive should return false when router url does not start with path', () => {
    (component as any).router = { url: '/dashboard' };
    expect(component.isActive('/software-engineering')).toBe(false);
  });

  it('openFlyout activates a group and scheduleClose clears it after the delay', () => {
    vi.useFakeTimers();
    const [firstGroup] = component.navGroups;
    const origin = document.createElement('button');

    component.openFlyout(firstGroup, origin);
    expect(component.activeGroup()).toBe(firstGroup);
    expect(component.activeOrigin()).toBe(origin);

    component.scheduleClose();
    vi.advanceTimersByTime(149);
    expect(component.activeGroup()).toBe(firstGroup);

    vi.advanceTimersByTime(1);
    expect(component.activeGroup()).toBeNull();

    vi.useRealTimers();
  });

  it('renders a footer profile link that navigates to the profile page', () => {
    const link = (fixture.nativeElement as HTMLElement).querySelector(
      '.footer-profile-link',
    ) as HTMLAnchorElement;
    expect(link).toBeTruthy();
    expect(link.getAttribute('href')).toBe('/user-profile');
    expect(link.getAttribute('aria-label')).toBe('User profile');
    // Not the current page by default, so no aria-current.
    expect(link.getAttribute('aria-current')).toBeNull();
  });

  it('marks the footer profile link as current when on /user-profile', () => {
    (component as any).router = { url: '/user-profile' };
    fixture.detectChanges();
    const link = (fixture.nativeElement as HTMLElement).querySelector(
      '.footer-profile-link',
    ) as HTMLAnchorElement;
    expect(link.getAttribute('aria-current')).toBe('page');
  });

  it('includes the footer profile link last in the arrow-key focus order', () => {
    const focusables = component.navFocusableElements.toArray().map((el) => el.nativeElement);
    expect(focusables.length).toBeGreaterThan(0);
    const last = focusables[focusables.length - 1];
    expect(last.classList.contains('footer-profile-link')).toBe(true);
    // End key jumps focus to the last focusable — the footer profile link.
    focusables[0].focus();
    (fixture.nativeElement as HTMLElement).dispatchEvent(
      new KeyboardEvent('keydown', { key: 'End', bubbles: true }),
    );
    fixture.detectChanges();
    expect(document.activeElement).toBe(last);
  });

  it('cancelClose keeps the flyout open past the delay', () => {
    vi.useFakeTimers();
    const [firstGroup] = component.navGroups;
    component.openFlyout(firstGroup, document.createElement('button'));
    component.scheduleClose();
    component.cancelClose();
    vi.advanceTimersByTime(500);
    expect(component.activeGroup()).toBe(firstGroup);
    vi.useRealTimers();
  });
});
