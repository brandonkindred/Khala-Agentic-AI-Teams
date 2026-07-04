import { Type, Provider } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { provideHttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { TeamAssistantApiService } from '../services/team-assistant-api.service';
import { createTeamAssistantApiMock } from './team-assistant.mock';

/**
 * Renders a team `*-dashboard` component for an a11y audit.
 *
 * Every team dashboard is a `DashboardShellComponent` that embeds
 * `<app-team-assistant-chat [teamApiUrl]>`; this harness supplies the router +
 * http + a stubbed TeamAssistantApiService so the embedded chat loads a
 * conversation deterministically. Pass the dashboard's own feature-API-service
 * provider (if any) via `extraProviders`.
 *
 * Preconditions: `component` is a standalone dashboard component; any service it
 *   injects beyond TeamAssistantApiService is supplied in `extraProviders`.
 * Postconditions: returns a change-detected fixture ready for guard queries + axe.
 */
export async function renderDashboardShellA11y<T>(
  component: Type<T>,
  extraProviders: Provider[] = [],
): Promise<ComponentFixture<T>> {
  await TestBed.configureTestingModule({
    imports: [component, NoopAnimationsModule],
    providers: [
      provideHttpClient(),
      provideRouter([]),
      { provide: TeamAssistantApiService, useValue: createTeamAssistantApiMock() },
      ...extraProviders,
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(component);
  fixture.detectChanges();
  return fixture;
}
