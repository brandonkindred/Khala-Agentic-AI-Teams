import { Component, inject, signal } from '@angular/core';
import { ActivatedRoute, NavigationEnd, Router, RouterLink } from '@angular/router';
import { MatIconModule } from '@angular/material/icon';
import { filter } from 'rxjs/operators';

export interface Breadcrumb {
  label: string;
  url: string;
}

/**
 * Route-aware breadcrumb trail following the WAI-ARIA breadcrumb pattern.
 * Reads `data.breadcrumb` from each activated route segment.
 *
 * @see https://www.w3.org/WAI/ARIA/apg/patterns/breadcrumb/
 */
@Component({
  selector: 'app-breadcrumb',
  standalone: true,
  imports: [RouterLink, MatIconModule],
  templateUrl: './breadcrumb.component.html',
  styleUrl: './breadcrumb.component.scss',
})
export class BreadcrumbComponent {
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  breadcrumbs = signal<Breadcrumb[]>([]);

  constructor() {
    // Build on initial load
    this.breadcrumbs.set(this.buildBreadcrumbs(this.route.root));

    // Rebuild on every navigation
    this.router.events.pipe(
      filter((e): e is NavigationEnd => e instanceof NavigationEnd),
    ).subscribe(() => {
      this.breadcrumbs.set(this.buildBreadcrumbs(this.route.root));
    });
  }

  private buildBreadcrumbs(route: ActivatedRoute, url = '', crumbs: Breadcrumb[] = []): Breadcrumb[] {
    const child = route.children[0];
    if (!child?.snapshot) {
      return crumbs;
    }

    const segments = child.snapshot.url.map(s => s.path);
    if (segments.length > 0) {
      url += '/' + segments.join('/');
    }

    const label = child.snapshot.data['breadcrumb'];
    if (label) {
      crumbs.push({ label, url });
    }

    return this.buildBreadcrumbs(child, url, crumbs);
  }
}
