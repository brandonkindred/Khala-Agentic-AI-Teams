import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { TitleStrategy, provideRouter, withInMemoryScrolling } from '@angular/router';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';

import { routes } from './app.routes';
import { errorHandlerInterceptor } from './core/error-handler.interceptor';
import { KhalaTitleStrategy } from './core/khala-title.strategy';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(
      routes,
      // SPA navigation should behave like page loads: new views start at the
      // top, back/forward restore the prior scroll position.
      withInMemoryScrolling({ scrollPositionRestoration: 'enabled' })
    ),
    provideHttpClient(withInterceptors([errorHandlerInterceptor])),
    provideAnimations(),
    // Every route carries data.title; announce it in the tab title globally.
    { provide: TitleStrategy, useClass: KhalaTitleStrategy },
  ],
};
