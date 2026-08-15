import {
  Component,
  DestroyRef,
  ElementRef,
  HostListener,
  inject,
  QueryList,
  signal,
  ViewChild,
  ViewChildren,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import {
  NavigationEnd,
  NavigationStart,
  Router,
  RouterLink,
  RouterLinkActive,
  RouterOutlet,
} from '@angular/router';
import { MatSidenavContent, MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { OverlayModule, ConnectedPosition } from '@angular/cdk/overlay';
import { ApiStatusWidgetComponent } from '../api-status-widget/api-status-widget.component';
import { BreadcrumbComponent } from '../../shared/breadcrumb/breadcrumb.component';
import { InitialsAvatarComponent } from '../../shared/avatar/initials-avatar.component';
import { NavStateService } from '../../services/nav-state.service';
import { UserProfileStore } from '../../services/user-profile-store.service';
import { ALL_NAV_ITEMS, NAV_GROUPS, NavGroup, NavItem, findGroupForRoute } from '../../models/navigation.model';

/**
 * Application shell with sidebar navigation and main content area.
 * Navigation is data-driven from NAV_GROUPS. Each group opens a flyout panel
 * following the WAI-ARIA disclosure-navigation pattern: hover or click opens
 * it for mouse users; Enter/Space/ArrowRight open it and move focus to the
 * first link for keyboard users; arrows rove within it; Escape/ArrowLeft
 * close it and return focus to the trigger. Items can be pinned to the
 * sidebar as favorites. After each navigation, focus moves to the main
 * content region so keyboard/screen-reader context follows the route.
 */
@Component({
  selector: 'app-app-shell',
  standalone: true,
  imports: [
    RouterOutlet,
    RouterLink,
    RouterLinkActive,
    MatSidenavModule,
    MatToolbarModule,
    MatButtonModule,
    MatIconModule,
    MatTooltipModule,
    OverlayModule,
    ApiStatusWidgetComponent,
    BreadcrumbComponent,
    InitialsAvatarComponent,
  ],
  templateUrl: './app-shell.component.html',
  styleUrl: './app-shell.component.scss',
})
export class AppShellComponent {
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  readonly navState = inject(NavStateService);
  readonly profileStore = inject(UserProfileStore);
  readonly navGroups = NAV_GROUPS;

  /**
   * The footer profile link's route/icon/label come from the nav model so the
   * Settings-flyout entry and the footer can never drift apart. `requireNavItem`
   * throws at construction if the 'user-profile' item is ever removed, so the
   * breakage is an explicit error instead of a silently broken footer link.
   */
  readonly profileNavItem: NavItem = AppShellComponent.requireNavItem('user-profile');

  private static requireNavItem(id: string): NavItem {
    const item = ALL_NAV_ITEMS.find((navItem) => navItem.id === id);
    if (!item) {
      throw new Error(`AppShellComponent: required nav item '${id}' is missing from the nav model.`);
    }
    return item;
  }

  /** All focusable elements in the nav for arrow-key navigation. */
  @ViewChildren('navFocusable') navFocusableElements!: QueryList<ElementRef<HTMLElement>>;

  /** The app's real scroll container (the window never overflows in this layout). */
  @ViewChild(MatSidenavContent) sidenavContent?: MatSidenavContent;

  /** Which nav group is currently revealed in the flyout overlay, if any. */
  readonly activeGroup = signal<NavGroup | null>(null);
  /** Trigger element the flyout should anchor to. */
  readonly activeOrigin = signal<HTMLElement | null>(null);

  /** CDK connected-overlay positions: flyout to the right of the sidebar rail. */
  readonly flyoutPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'top', overlayX: 'start', overlayY: 'top', offsetX: 8 },
    { originX: 'end', originY: 'bottom', overlayX: 'start', overlayY: 'bottom', offsetX: 8 },
  ];

  private closeTimer: ReturnType<typeof setTimeout> | null = null;
  private lastOrigin: HTMLElement | null = null;

  private previousPath: string | null = null;
  /** Trigger of the navigation currently completing (recorded at NavigationStart). */
  private navigationTrigger: NavigationStart['navigationTrigger'] = 'imperative';

  constructor() {
    // Populate the footer avatar's identity once the shell mounts.
    this.profileStore.refresh();
    this.router.events.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((event) => {
      if (event instanceof NavigationStart) {
        this.onNavigationStart(event);
      } else if (event instanceof NavigationEnd) {
        this.onNavigationEnd(event);
      }
    });
  }

  /** Record how the navigation was initiated; NavigationEnd carries no trigger. */
  onNavigationStart(event: NavigationStart): void {
    this.navigationTrigger = event.navigationTrigger;
  }

  /**
   * Keep keyboard/screen-reader context in step with SPA navigation: when the
   * route PATH changes (not on query-param-only navigations, which dashboards
   * use to mirror in-page state like the active tab — stealing focus there
   * would break the very control being operated), move focus to the main
   * content region and start the new view at the top. The window never
   * scrolls in this layout, so scroll-to-top targets the sidenav content —
   * the app's actual scroll container. Browser back/forward (popstate) is a
   * *return* to a place the user has already been — forcing top-of-page and
   * stealing focus there would fight history traversal, so it is exempt.
   */
  onNavigationEnd(event: NavigationEnd): void {
    const path = event.urlAfterRedirects.split(/[?#]/)[0];
    const pathChanged = this.previousPath !== null && this.previousPath !== path;
    this.previousPath = path;
    if (!pathChanged || this.navigationTrigger === 'popstate') {
      return; // initial load, in-page query-param/fragment update, or history traversal
    }
    document.getElementById('main-content')?.focus({ preventScroll: true });
    this.sidenavContent?.getElementRef().nativeElement.scrollTo({ top: 0 });
  }

  /** Returns true if the given path is the current route (for aria-current).
   *
   * Matches whole path segments against the URL stripped of query/fragment,
   * so `/agent-studio` is active for `/agent-studio/provisioning` but never for
   * `/agent-studio-x`, and only one nav item claims `aria-current` at a time.
   */
  isActive(path: string): boolean {
    const url = this.router.url.split(/[?#]/)[0];
    return url === path || url.startsWith(path + '/');
  }

  /** Returns true if the current route lives inside the given nav group. */
  isGroupActive(group: NavGroup): boolean {
    return findGroupForRoute(this.router.url)?.key === group.key;
  }

  /** Reveal the flyout for `group`, anchored to `origin`, and cancel any pending close. */
  openFlyout(group: NavGroup, origin: HTMLElement): void {
    this.cancelClose();
    this.lastOrigin = origin;
    this.activeOrigin.set(origin);
    this.activeGroup.set(group);
  }

  /** Open the flyout and move focus to its first link (keyboard invocation). */
  openFlyoutAndFocus(group: NavGroup, origin: HTMLElement): void {
    this.openFlyout(group, origin);
    // The overlay renders on the next tick; focus the first link once present.
    setTimeout(() => this.flyoutLinks()[0]?.focus());
  }

  /** Keyboard handling on a group trigger (disclosure-navigation pattern). */
  onTriggerKeydown(event: KeyboardEvent, group: NavGroup, origin: HTMLElement): void {
    if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowRight') {
      event.preventDefault();
      this.openFlyoutAndFocus(group, origin);
    }
  }

  /**
   * Close only when focus truly leaves the trigger/flyout pair. Focus moving
   * from the trigger into the portalled overlay (or between flyout links)
   * must not close the panel — that was what made the flyout
   * keyboard-inaccessible before.
   */
  onNavFocusOut(event: FocusEvent): void {
    const next = event.relatedTarget as HTMLElement | null;
    if (next && (next.closest('.nav-flyout') || next === this.lastOrigin)) {
      return;
    }
    this.scheduleClose();
  }

  /** Arrow-key roving across the flyout's links; ArrowLeft returns to the trigger. */
  onFlyoutKeydown(event: KeyboardEvent): void {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      this.closeFlyout(true);
      return;
    }
    const links = this.flyoutLinks();
    if (!links.length) return;
    const currentIndex = links.indexOf(document.activeElement as HTMLElement);
    let nextIndex: number | null = null;
    switch (event.key) {
      case 'ArrowDown':
        nextIndex = currentIndex < 0 ? 0 : Math.min(currentIndex + 1, links.length - 1);
        break;
      case 'ArrowUp':
        nextIndex = currentIndex < 0 ? 0 : Math.max(currentIndex - 1, 0);
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = links.length - 1;
        break;
      default:
        return;
    }
    event.preventDefault();
    links[nextIndex]?.focus();
  }

  private flyoutLinks(): HTMLElement[] {
    // The overlay is portalled to the document body, not this component's DOM.
    return Array.from(document.querySelectorAll<HTMLElement>('.nav-flyout .nav-flyout-link'));
  }

  /** Schedule the flyout to close after a short delay (tolerates trigger→panel gap). */
  scheduleClose(): void {
    this.cancelClose();
    this.closeTimer = setTimeout(() => {
      this.activeGroup.set(null);
      this.closeTimer = null;
    }, 150);
  }

  /** Cancel a pending close (e.g. when cursor re-enters trigger or flyout). */
  cancelClose(): void {
    if (this.closeTimer !== null) {
      clearTimeout(this.closeTimer);
      this.closeTimer = null;
    }
  }

  /** Close the flyout immediately, optionally returning focus to the origin trigger. */
  closeFlyout(returnFocus = false): void {
    this.cancelClose();
    this.activeGroup.set(null);
    if (returnFocus) {
      this.lastOrigin?.focus();
    }
  }

  /** Keyboard navigation within the sidebar nav (WAI-ARIA disclosure pattern). */
  @HostListener('keydown', ['$event'])
  onNavKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape' && this.activeGroup() !== null) {
      event.preventDefault();
      this.closeFlyout(true);
      return;
    }

    const focusables = this.navFocusableElements?.toArray().map(el => el.nativeElement);
    if (!focusables?.length) return;

    const active = document.activeElement as HTMLElement;
    const currentIndex = focusables.indexOf(active);
    if (currentIndex === -1) return;

    let nextIndex: number | null = null;
    switch (event.key) {
      case 'ArrowDown':
        nextIndex = Math.min(currentIndex + 1, focusables.length - 1);
        break;
      case 'ArrowUp':
        nextIndex = Math.max(currentIndex - 1, 0);
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = focusables.length - 1;
        break;
      default:
        return; // Don't prevent default for other keys
    }

    event.preventDefault();
    focusables[nextIndex]?.focus();
  }

  trackByItemId(_index: number, item: NavItem): string {
    return item.id;
  }
}
