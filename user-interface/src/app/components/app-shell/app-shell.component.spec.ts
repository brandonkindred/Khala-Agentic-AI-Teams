import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { BreakpointObserver } from '@angular/cdk/layout';
import { of } from 'rxjs';
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

  it('isActive should return true when the current url starts with path', () => {
    (component as any).currentUrl.set('/dashboard');
    expect(component.isActive('/dashboard')).toBe(true);
    expect(component.isActive('/')).toBe(true);
  });

  it('isActive should return false when the current url does not start with path', () => {
    (component as any).currentUrl.set('/dashboard');
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
    // Route/label derive from the 'user-profile' NavItem in navigation.model.ts.
    expect(link.getAttribute('href')).toBe('/user-profile');
    expect(link.getAttribute('aria-label')).toBe('User Profile');
    // Not the current page by default, so no aria-current.
    expect(link.getAttribute('aria-current')).toBeNull();
  });

  it('requireNavItem throws when the nav model is missing a required item', () => {
    // Real fail-fast: a removed 'user-profile' NavItem must be an explicit
    // construction error, not an undefined that crashes template rendering.
    expect(() => (AppShellComponent as any).requireNavItem('does-not-exist')).toThrowError(
      /does-not-exist/,
    );
  });

  it('marks the footer profile link as current when on /user-profile', () => {
    (component as any).currentUrl.set('/user-profile');
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

  it('phrases the favorite toggle label by pinned state', () => {
    const item = component.navGroups[0].items[0];
    expect(component.favoriteLabel(item)).toBe(`Add ${item.label} to favorites`);
    component.toggleFavorite(item.id);
    expect(component.favoriteLabel(item)).toBe(`Remove ${item.label} from favorites`);
    component.toggleFavorite(item.id); // reset shared localStorage state
  });

  it('renders the footer as a generic icon until an identity is known', () => {
    // Store starts with no name (getProfile request is not flushed here).
    expect(component.profileStore.hasIdentity()).toBe(false);
    const link = (fixture.nativeElement as HTMLElement).querySelector('.footer-profile-link');
    expect(link?.querySelector('app-initials-avatar')).toBeNull();
    expect(link?.querySelector('mat-icon')).toBeTruthy();
  });

  it('shows the initials avatar in the footer once an identity is known', () => {
    component.profileStore.set('Grace Hopper', 'blue');
    fixture.detectChanges();
    const link = (fixture.nativeElement as HTMLElement).querySelector('.footer-profile-link');
    expect(link?.querySelector('app-initials-avatar')).toBeTruthy();
  });

  it('defaults to desktop (non-handset) layout', () => {
    expect(component.isHandset()).toBe(false);
  });

  it('leaves the drawer open after navigating on desktop', () => {
    const drawer = { close: vi.fn() };
    (component as any).drawer = drawer;
    (component as any).closeDrawerAfterHandsetNav();
    expect(drawer.close).not.toHaveBeenCalled();
  });
});

describe('AppShellComponent responsive drawer', () => {
  it('closes the overlay drawer after navigating on handset widths', async () => {
    await TestBed.configureTestingModule({
      imports: [AppShellComponent, NoopAnimationsModule],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: BreakpointObserver, useValue: { observe: () => of({ matches: true, breakpoints: {} }) } },
      ],
    }).compileComponents();
    const fixture = TestBed.createComponent(AppShellComponent);
    const component = fixture.componentInstance;
    fixture.detectChanges();
    expect(component.isHandset()).toBe(true);
    const drawer = { close: vi.fn() };
    (component as any).drawer = drawer;
    (component as any).closeDrawerAfterHandsetNav();
    expect(drawer.close).toHaveBeenCalledTimes(1);
    TestBed.resetTestingModule();
  });
});
