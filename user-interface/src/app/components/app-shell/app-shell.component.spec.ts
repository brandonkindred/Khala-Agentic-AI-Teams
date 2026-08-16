import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NavigationEnd, NavigationStart, provideRouter } from '@angular/router';
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

  it('isActive matches whole path segments, ignoring query and fragment', () => {
    (component as any).router = { url: '/dashboard' };
    expect(component.isActive('/dashboard')).toBe(true);
    // Not a segment match — '/dash' must not claim '/dashboard'.
    expect(component.isActive('/dash')).toBe(false);

    (component as any).router = { url: '/job-matching?tab=profile' };
    expect(component.isActive('/job-matching')).toBe(true);

    (component as any).router = { url: '/agent-studio/provisioning' };
    expect(component.isActive('/agent-studio')).toBe(true);
  });

  it('isActive should return false when router url does not start with path', () => {
    (component as any).router = { url: '/dashboard' };
    expect(component.isActive('/software-engineering')).toBe(false);
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

  it('marks the footer profile link as current when on /user-profile', () => {
    (component as any).router = { url: '/user-profile' };
    fixture.detectChanges();
    const link = (fixture.nativeElement as HTMLElement).querySelector(
      '.footer-profile-link',
    ) as HTMLAnchorElement;
    expect(link.getAttribute('aria-current')).toBe('page');
  });

  it('requireNavItem throws when the nav model is missing a required item', () => {
    // Real fail-fast: a removed 'user-profile' NavItem must be an explicit
    // construction error, not an undefined that crashes template rendering.
    expect(() => (AppShellComponent as any).requireNavItem('does-not-exist')).toThrowError(
      /does-not-exist/,
    );
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

  describe('route-change focus and scroll', () => {
    const nav = (url: string) => new NavigationEnd(1, url, url);

    it('focuses main content and scrolls to top only when the path changes', () => {
      // The shell's own template renders #main-content; assert against it.
      const main = document.getElementById('main-content') as HTMLElement;
      expect(main).toBeTruthy();
      const scrollTo = vi.fn();
      component.sidenavContent = {
        getElementRef: () => ({ nativeElement: { scrollTo } }),
      } as never;

      component.onNavigationEnd(nav('/dashboard')); // initial load — no focus move
      expect(document.activeElement).not.toBe(main);

      component.onNavigationEnd(nav('/job-matching')); // path change
      expect(document.activeElement).toBe(main);
      expect(scrollTo).toHaveBeenCalledWith({ top: 0 });

      (document.activeElement as HTMLElement).blur();
      scrollTo.mockClear();
      // Query-param-only navigation (tab mirroring) must not steal focus.
      component.onNavigationEnd(nav('/job-matching?tab=profile'));
      expect(document.activeElement).not.toBe(main);
      expect(scrollTo).not.toHaveBeenCalled();
    });

    it('leaves focus and scroll alone on browser back/forward (popstate)', () => {
      const main = document.getElementById('main-content') as HTMLElement;
      const scrollTo = vi.fn();
      component.sidenavContent = {
        getElementRef: () => ({ nativeElement: { scrollTo } }),
      } as never;

      component.onNavigationEnd(nav('/dashboard')); // initial load
      // History traversal returns the user to a known place — do not fight it.
      component.onNavigationStart(new NavigationStart(2, '/job-matching', 'popstate'));
      component.onNavigationEnd(nav('/job-matching'));
      expect(document.activeElement).not.toBe(main);
      expect(scrollTo).not.toHaveBeenCalled();

      // The next imperative navigation behaves normally again.
      component.onNavigationStart(new NavigationStart(3, '/dashboard', 'imperative'));
      component.onNavigationEnd(nav('/dashboard'));
      expect(document.activeElement).toBe(main);
      expect(scrollTo).toHaveBeenCalledWith({ top: 0 });
      (document.activeElement as HTMLElement).blur();
    });
  });

  describe('flyout keyboard access', () => {
    afterEach(() => {
      document.querySelectorAll('.cdk-overlay-container').forEach((el) => el.remove());
      vi.useRealTimers();
    });

    function openWithKeyboard(): HTMLElement {
      const trigger = fixture.nativeElement.querySelector('.nav-group-trigger') as HTMLElement;
      component.onTriggerKeydown(
        new KeyboardEvent('keydown', { key: 'Enter' }),
        component.navGroups[0],
        trigger
      );
      fixture.detectChanges();
      vi.runAllTimers();
      return trigger;
    }

    it('Enter on a trigger opens the flyout and focuses its first link', () => {
      vi.useFakeTimers();
      openWithKeyboard();
      const firstLink = document.querySelector<HTMLElement>('.nav-flyout .nav-flyout-link');
      expect(firstLink).toBeTruthy();
      expect(document.activeElement).toBe(firstLink);
    });

    it('keeps the flyout open when focus moves into it, closes when it leaves', () => {
      vi.useFakeTimers();
      openWithKeyboard();
      const firstLink = document.querySelector<HTMLElement>('.nav-flyout .nav-flyout-link')!;

      // Focus moving into the (body-portalled) flyout must NOT close it —
      // this was the bug that made the nav mouse-only.
      component.onNavFocusOut({ relatedTarget: firstLink } as unknown as FocusEvent);
      vi.advanceTimersByTime(500);
      expect(component.activeGroup()).not.toBeNull();

      // Focus leaving both trigger and flyout closes it.
      component.onNavFocusOut({ relatedTarget: document.body } as unknown as FocusEvent);
      vi.advanceTimersByTime(500);
      expect(component.activeGroup()).toBeNull();
    });

    it('arrow keys rove across the flyout links; ArrowLeft returns to the trigger', () => {
      vi.useFakeTimers();
      const trigger = openWithKeyboard();
      const links = Array.from(
        document.querySelectorAll<HTMLElement>('.nav-flyout .nav-flyout-link')
      );
      expect(links.length).toBeGreaterThan(0);

      component.onFlyoutKeydown(new KeyboardEvent('keydown', { key: 'ArrowDown' }));
      expect(document.activeElement).toBe(links[Math.min(1, links.length - 1)]);

      component.onFlyoutKeydown(new KeyboardEvent('keydown', { key: 'End' }));
      expect(document.activeElement).toBe(links[links.length - 1]);

      component.onFlyoutKeydown(new KeyboardEvent('keydown', { key: 'Home' }));
      expect(document.activeElement).toBe(links[0]);

      component.onFlyoutKeydown(new KeyboardEvent('keydown', { key: 'ArrowLeft' }));
      expect(component.activeGroup()).toBeNull();
      expect(document.activeElement).toBe(trigger);
    });

    it('renders a pin toggle per flyout item that flips the favorite state', () => {
      vi.useFakeTimers();
      localStorage.removeItem('kh-nav-favorites');
      openWithKeyboard();
      const star = document.querySelector<HTMLButtonElement>('.nav-flyout .nav-link-star')!;
      expect(star).toBeTruthy();
      expect(star.getAttribute('aria-pressed')).toBe('false');
      const itemId = component.navGroups[0].items[0].id;

      star.click();
      expect(component.navState.isFavorite(itemId)).toBe(true);

      component.navState.toggleFavorite(itemId); // reset persisted state
      localStorage.removeItem('kh-nav-favorites');
    });
  });
});
