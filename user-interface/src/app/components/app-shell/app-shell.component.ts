import { Component, ElementRef, HostListener, inject, QueryList, signal, ViewChildren } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { OverlayModule, ConnectedPosition } from '@angular/cdk/overlay';
import { ApiStatusWidgetComponent } from '../api-status-widget/api-status-widget.component';
import { BreadcrumbComponent } from '../../shared/breadcrumb/breadcrumb.component';
import { NavStateService } from '../../services/nav-state.service';
import { NAV_GROUPS, NavGroup, NavItem, findGroupForRoute } from '../../models/navigation.model';

/**
 * Application shell with sidebar navigation and main content area.
 * Navigation is data-driven from NAV_GROUPS with flyout panels on hover/focus
 * and favorites.
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
    OverlayModule,
    ApiStatusWidgetComponent,
    BreadcrumbComponent,
  ],
  templateUrl: './app-shell.component.html',
  styleUrl: './app-shell.component.scss',
})
export class AppShellComponent {
  private readonly router = inject(Router);
  readonly navState = inject(NavStateService);
  readonly navGroups = NAV_GROUPS;

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

  /** Returns true if the given path is the current route (for aria-current). */
  isActive(path: string): boolean {
    return this.router.url.startsWith(path);
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
