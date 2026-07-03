import { Injectable, inject } from '@angular/core';
import { Title } from '@angular/platform-browser';
import {
  RouterStateSnapshot,
  TitleStrategy,
} from '@angular/router';

/** Suffix appended to every page title. */
const APP_NAME = 'Khala';

/**
 * Sets the browser tab title from the active route's `data.title` so wayfinding
 * (WCAG 2.4.2) works on every route, not only the ones wrapped in
 * `app-dashboard-shell`.
 *
 * Invariants: the document title is always either `"<title> | Khala"` when the
 * deepest activated route defines `data.title`, or `"Khala"` otherwise.
 */
@Injectable({ providedIn: 'root' })
export class AppTitleStrategy extends TitleStrategy {
  private readonly title = inject(Title);

  /**
   * Preconditions: `snapshot` is the router state for the navigation just
   * completed.
   * Postconditions: `document.title` reflects the deepest route's `data.title`
   * (with the app-name suffix) or the bare app name when none is declared.
   */
  override updateTitle(snapshot: RouterStateSnapshot): void {
    let route = snapshot.root;
    let pageTitle: string | undefined;
    while (route) {
      // Deepest declared title wins (child routes override their parents).
      if (typeof route.data?.['title'] === 'string') {
        pageTitle = route.data['title'];
      }
      route = route.firstChild!;
    }
    this.title.setTitle(pageTitle ? `${pageTitle} | ${APP_NAME}` : APP_NAME);
  }
}
