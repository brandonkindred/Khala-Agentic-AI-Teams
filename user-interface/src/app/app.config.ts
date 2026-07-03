import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { TitleStrategy, provideRouter } from '@angular/router';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';

import { routes } from './app.routes';
import { errorHandlerInterceptor } from './core/error-handler.interceptor';
import { KhalaTitleStrategy } from './core/khala-title.strategy';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    // No withInMemoryScrolling: the router's ViewportScroller targets the
    // window, which never overflows in this layout — the shell scrolls its
    // sidenav content to the top on path changes instead (AppShellComponent).
    provideRouter(routes),
    provideHttpClient(withInterceptors([errorHandlerInterceptor])),
    provideAnimations(),
    // Every route carries data.title; announce it in the tab title globally.
    { provide: TitleStrategy, useClass: KhalaTitleStrategy },
  ],
};
