import { Injectable, inject } from '@angular/core';
import { Title } from '@angular/platform-browser';
import { RouterStateSnapshot, TitleStrategy } from '@angular/router';

/**
 * Sets the document title from the deepest route's `data.title` on every
 * navigation, as "<title> | Khala" (WCAG 2.4.2). Routes without a title
 * fall back to the app name. DashboardShellComponent may still override the
 * title after render — it sets the same "<title> | Khala" format, so the two
 * sources agree on shell pages.
 */
@Injectable({ providedIn: 'root' })
export class KhalaTitleStrategy extends TitleStrategy {
  private readonly title = inject(Title);

  override updateTitle(snapshot: RouterStateSnapshot): void {
    let route = snapshot.root;
    let pageTitle: string | undefined;
    while (route.firstChild) {
      route = route.firstChild;
      pageTitle = (route.data?.['title'] as string | undefined) ?? pageTitle;
    }
    this.title.setTitle(pageTitle ? `${pageTitle} | Khala` : 'Khala');
  }
}
