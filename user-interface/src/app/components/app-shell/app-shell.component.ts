import { Component, ElementRef, HostListener, inject, QueryList, ViewChildren } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { trigger, state, style, transition, animate } from '@angular/animations';
import { ApiStatusWidgetComponent } from '../api-status-widget/api-status-widget.component';
import { BreadcrumbComponent } from '../../shared/breadcrumb/breadcrumb.component';
import { NavStateService } from '../../services/nav-state.service';
import { NAV_GROUPS, NavGroup, NavItem } from '../../models/navigation.model';

/**
 * Application shell with sidebar navigation and main content area.
 * Navigation is data-driven from NAV_GROUPS with collapsible groups and favorites.
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
    ApiStatusWidgetComponent,
    BreadcrumbComponent,
  ],
  templateUrl: './app-shell.component.html',
  styleUrl: './app-shell.component.scss',
  animations: [
    trigger('collapse', [
      state('open', style({ height: '*', opacity: 1 })),
      state('closed', style({ height: '0', opacity: 0, overflow: 'hidden' })),
      transition('open <=> closed', animate('200ms ease')),
    ]),
  ],
})
export class AppShellComponent {
  private readonly router = inject(Router);
  readonly navState = inject(NavStateService);
  readonly navGroups = NAV_GROUPS;

  /** All focusable elements in the nav for arrow-key navigation. */
  @ViewChildren('navFocusable') navFocusableElements!: QueryList<ElementRef<HTMLElement>>;

  /** Returns true if the given path is the current route (for aria-current). */
  isActive(path: string): boolean {
    return this.router.url.startsWith(path);
  }

  /** Keyboard navigation within the sidebar nav (WAI-ARIA disclosure pattern). */
  @HostListener('keydown', ['$event'])
  onNavKeydown(event: KeyboardEvent): void {
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

  trackByGroupKey(_index: number, group: NavGroup): string {
    return group.key;
  }

  trackByItemId(_index: number, item: NavItem): string {
    return item.id;
  }
}
