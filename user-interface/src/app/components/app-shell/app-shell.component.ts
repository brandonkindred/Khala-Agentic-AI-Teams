import {
  Component,
  DestroyRef,
  ElementRef,
  HostListener,
  OnInit,
  QueryList,
  ViewChild,
  ViewChildren,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { NgTemplateOutlet } from '@angular/common';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { MatSidenav, MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatTooltipModule } from '@angular/material/tooltip';
import { BreakpointObserver } from '@angular/cdk/layout';
import { CdkConnectedOverlay, OverlayModule, ConnectedPosition } from '@angular/cdk/overlay';
import { filter, map } from 'rxjs/operators';
import { ApiStatusWidgetComponent } from '../api-status-widget/api-status-widget.component';
import { BreadcrumbComponent } from '../../shared/breadcrumb/breadcrumb.component';
import { InitialsAvatarComponent } from '../../shared/avatar/initials-avatar.component';
import { NavStateService } from '../../services/nav-state.service';
import { UserProfileStore } from '../../services/user-profile-store.service';
import { ALL_NAV_ITEMS, NAV_GROUPS, NavGroup, NavItem, findGroupForRoute } from '../../models/navigation.model';

/** Viewport below which the sidebar collapses into an overlay drawer. */
const HANDSET_QUERY = '(max-width: 959.98px)';

/**
 * Application shell with sidebar navigation and main content area.
 * Navigation is data-driven from NAV_GROUPS with flyout panels on hover/focus
 * and favorites. On handset-width viewports the rail becomes an overlay drawer
 * that closes on navigation. The sidenav footer shows the user's initials
 * avatar (linking to /user-profile); it carries the #navFocusable ref, so it
 * participates in the arrow-key nav as the last focusable element (DOM order).
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
    NgTemplateOutlet,
    ApiStatusWidgetComponent,
    BreadcrumbComponent,
    InitialsAvatarComponent,
  ],
  templateUrl: './app-shell.component.html',
  styleUrl: './app-shell.component.scss',
})
export class AppShellComponent implements OnInit {
  private readonly router = inject(Router);
  private readonly breakpoints = inject(BreakpointObserver);
  private readonly destroyRef = inject(DestroyRef);
  readonly navState = inject(NavStateService);
  readonly profileStore = inject(UserProfileStore);
  readonly navGroups = NAV_GROUPS;

  /** True on handset-width viewports (sidebar becomes an overlay drawer). */
  readonly isHandset = toSignal(
    this.breakpoints.observe(HANDSET_QUERY).pipe(map((state) => state.matches)),
    { initialValue: false },
  );

  /** The sidebar drawer, closed on navigation while in handset overlay mode. */
  @ViewChild('drawer') private drawer?: MatSidenav;

  /** The open group flyout, if any — repositioned when favorites reflow. */
  @ViewChild(CdkConnectedOverlay) private flyoutOverlay?: CdkConnectedOverlay;

  ngOnInit(): void {
    // Populate the footer avatar's identity once the shell mounts.
    this.profileStore.refresh();
    // In overlay mode, close the drawer after navigating so it doesn't cover
    // the page the user just chose.
    this.router.events
      .pipe(
        filter((e): e is NavigationEnd => e instanceof NavigationEnd),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe(() => {
        this.currentUrl.set(this.router.url);
        this.closeDrawerAfterHandsetNav();
      });
  }

  /**
   * Close the overlay drawer after a navigation on handset widths.
   *
   * Preconditions: called on NavigationEnd.
   * Postconditions: on handset viewports the drawer is closed so it doesn't
   * cover the just-navigated page; on wider viewports (fixed 'side' mode) it is
   * a no-op.
   */
  private closeDrawerAfterHandsetNav(): void {
    if (this.isHandset()) this.drawer?.close();
  }

  /** aria-label for a favorite toggle, phrased by current pinned state. */
  favoriteLabel(item: NavItem): string {
    return this.navState.isFavorite(item.id)
      ? `Remove ${item.label} from favorites`
      : `Add ${item.label} to favorites`;
  }

  /**
   * Pin/unpin a favorite, keeping an open flyout anchored to its trigger.
   *
   * Preconditions: `id` is a NavItem id.
   * Postconditions: the favorite is toggled (persisted by NavStateService).
   * Toggling changes the Favorites section height above the flyout's anchor,
   * so the open overlay is repositioned after the resulting layout settles —
   * otherwise it would stay at its old coordinates, detached from its trigger.
   */
  toggleFavorite(id: string): void {
    this.navState.toggleFavorite(id);
    // Defer past the change-detection + layout pass the toggle triggers, then
    // re-anchor the flyout to its (now shifted) trigger.
    setTimeout(() => this.flyoutOverlay?.overlayRef?.updatePosition());
  }

  /**
   * The footer profile link's route/icon/label come from the nav model so the
   * Settings-flyout entry and the footer can never drift apart.
   *
   * Invariants: the 'user-profile' NavItem exists in NAV_GROUPS (settings
   * group) — `requireNavItem` throws at construction if it is ever removed,
   * so the breakage is an explicit error instead of a broken footer link.
   */
  readonly profileNavItem: NavItem = AppShellComponent.requireNavItem('user-profile');

  /**
   * Look up a NavItem the shell template depends on.
   *
   * Preconditions: `id` names an item present in `ALL_NAV_ITEMS`.
   * Postconditions: returns that item; throws (real runtime check, not a
   * type-level assertion) when the item is missing, failing fast at shell
   * construction instead of during template rendering.
   */
  private static requireNavItem(id: string): NavItem {
    const item = ALL_NAV_ITEMS.find((navItem) => navItem.id === id);
    if (!item) {
      throw new Error(`NavItem '${id}' is missing from NAV_GROUPS but the app shell requires it`);
    }
    return item;
  }

  /** All focusable elements in the nav for arrow-key navigation. */
  @ViewChildren('navFocusable') navFocusableElements!: QueryList<ElementRef<HTMLElement>>;

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

  /** Current route URL, updated once per navigation (avoids re-serializing
   * `router.url` on every template active-state check). */
  private readonly currentUrl = signal(this.router.url);

  /** Nav group owning the current route, recomputed only when the URL changes. */
  private readonly activeGroupKey = computed(() => findGroupForRoute(this.currentUrl())?.key ?? null);

  /** Returns true if the given path is the current route (for aria-current). */
  isActive(path: string): boolean {
    return this.currentUrl().startsWith(path);
  }

  /** Returns true if the current route lives inside the given nav group. */
  isGroupActive(group: NavGroup): boolean {
    return this.activeGroupKey() === group.key;
  }

  /** Reveal the flyout for `group`, anchored to `origin`, and cancel any pending close. */
  openFlyout(group: NavGroup, origin: HTMLElement): void {
    this.cancelClose();
    this.lastOrigin = origin;
    this.activeOrigin.set(origin);
    this.activeGroup.set(group);
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
