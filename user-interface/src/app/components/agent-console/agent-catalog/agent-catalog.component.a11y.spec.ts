import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of } from 'rxjs';
import { vi } from 'vitest';
import { axe } from 'vitest-axe';
import { AgentCatalogApiService } from '../../../services/agent-catalog-api.service';
import { AgentCatalogComponent } from './agent-catalog.component';

// `color-contrast` disabled — jsdom can't compute composited colours. Contrast
// is enforced by src/styles/scss-contrast-guard.spec.ts + browser axe DevTools.
const axeOptions = {
  rules: {
    'color-contrast': { enabled: false },
  },
};

describe('AgentCatalogComponent a11y', () => {
  const agent = {
    id: 'blogging.writer',
    team: 'blogging',
    name: 'Writer',
    summary: 'Drafts long-form posts.',
    tags: ['content'],
    has_input_schema: true,
    has_output_schema: true,
    has_invoke: true,
  };
  const teamGroup = { team: 'blogging', display_name: 'Blogging', agent_count: 1, tags: ['content'] };

  const setup = async () => {
    const api = {
      listAgents: vi.fn().mockReturnValue(of([agent])),
      listTeams: vi.fn().mockReturnValue(of([teamGroup])),
      getAgent: vi.fn(),
      getInputSchema: vi.fn(),
      getOutputSchema: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [AgentCatalogComponent, NoopAnimationsModule],
      providers: [{ provide: AgentCatalogApiService, useValue: api }],
    }).compileComponents();
  };

  it('has no axe violations with agent cards rendered', async () => {
    await setup();

    const fixture = TestBed.createComponent(AgentCatalogComponent);
    fixture.detectChanges();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();

    // Guard: a catalog card is in the DOM so axe audits the real grid.
    expect(fixture.nativeElement.querySelector('.agent-card')).toBeTruthy();

    const results = await axe(fixture.nativeElement, axeOptions);
    expect(results).toHaveNoViolations();
  });
});
