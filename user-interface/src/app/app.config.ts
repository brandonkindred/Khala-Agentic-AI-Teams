import { ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { TitleStrategy, provideRouter } from '@angular/router';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { provideAnimations } from '@angular/platform-browser/animations';

import { routes } from './app.routes';
import { errorHandlerInterceptor } from './core/error-handler.interceptor';
import { AppTitleStrategy } from './core/app-title-strategy';

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    provideHttpClient(withInterceptors([errorHandlerInterceptor])),
    provideAnimations(),
    // Sync the browser tab title from each route's `data.title` (WCAG 2.4.2).
    { provide: TitleStrategy, useClass: AppTitleStrategy },
  ],
};
