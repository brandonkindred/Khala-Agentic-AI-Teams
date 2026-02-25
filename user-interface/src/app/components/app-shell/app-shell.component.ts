import { Component } from '@angular/core';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { MatSidenavModule } from '@angular/material/sidenav';
import { MatToolbarModule } from '@angular/material/toolbar';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { ApiStatusWidgetComponent } from '../api-status-widget/api-status-widget.component';

/**
 * Application shell with sidebar navigation and main content area.
 * Provides links to all available agent API features.
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
  ],
  templateUrl: './app-shell.component.html',
  styleUrl: './app-shell.component.scss',
})
export class AppShellComponent {
  constructor(private readonly router: Router) {}

  /** Returns true if the given path is the current route (for aria-current). */
  isActive(path: string): boolean {
    return this.router.url.startsWith(path);
  }
}
