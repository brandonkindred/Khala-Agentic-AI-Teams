import { Injectable, inject } from '@angular/core';
import { Title } from '@angular/platform-browser';
import {
  RouterStateSnapshot,
  TitleStrategy,
} from '@angular/router';

/** Suffix appended to every page title. */
const APP_NAME = 'Khala';

/**
 * Sets the browser tab title from the active route's `title` so wayfinding
 * (WCAG 2.4.2) works on every route, not only the ones wrapped in
 * `app-dashboard-shell`. Delegates the deepest-title resolution to the base
 * `TitleStrategy.buildTitle()` and only adds the app-name suffix.
 *
 * Invariants: the document title is always either `"<title> | Khala"` when the
 * deepest activated route declares a `title`, or `"Khala"` otherwise.
 */
@Injectable({ providedIn: 'root' })
export class AppTitleStrategy extends TitleStrategy {
  private readonly title = inject(Title);

  /**
   * Preconditions: `snapshot` is the router state for the navigation just
   * completed.
   * Postconditions: `document.title` reflects the deepest route's `title` (with
   * the app-name suffix) or the bare app name when none is declared.
   */
  override updateTitle(snapshot: RouterStateSnapshot): void {
    const pageTitle = this.buildTitle(snapshot);
    this.title.setTitle(pageTitle ? `${pageTitle} | ${APP_NAME}` : APP_NAME);
  }
}
