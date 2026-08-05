import { TestBed } from '@angular/core/testing';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { of, throwError } from 'rxjs';
import { vi } from 'vitest';
import { AgentCatalogApiService } from '../../../../services/agent-catalog-api.service';
import { AgentCatalogComponent } from './agent-catalog.component';
import { expectNoAxeViolations } from '../../../../testing/a11y';

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

  const setup = async (overrides: Record<string, unknown> = {}) => {
    const api = {
      listAgents: vi.fn().mockReturnValue(of([agent])),
      listTeams: vi.fn().mockReturnValue(of([teamGroup])),
      getAgent: vi.fn(),
      getInputSchema: vi.fn(),
      getOutputSchema: vi.fn(),
      ...overrides,
    };
    await TestBed.configureTestingModule({
      imports: [AgentCatalogComponent, NoopAnimationsModule],
      providers: [{ provide: AgentCatalogApiService, useValue: api }],
    }).compileComponents();
  };

  const render = async () => {
    const fixture = TestBed.createComponent(AgentCatalogComponent);
    fixture.detectChanges();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    fixture.detectChanges();
    return fixture;
  };

  it('has no axe violations with agent cards rendered', async () => {
    await setup();
    const fixture = await render();

    // Guard: a catalog card is in the DOM so axe audits the real grid.
    expect(fixture.nativeElement.querySelector('.agent-card')).toBeTruthy();

    await expectNoAxeViolations(fixture.nativeElement);
  });

  it('has no axe violations in the empty (no agents) state', async () => {
    await setup({ listAgents: vi.fn().mockReturnValue(of([])), listTeams: vi.fn().mockReturnValue(of([])) });
    const fixture = await render();

    // Guard: the empty-state panel is what axe should be auditing.
    expect(fixture.nativeElement.querySelector('.agent-catalog__empty-state')).toBeTruthy();

    await expectNoAxeViolations(fixture.nativeElement);
  });

  it('has no axe violations in the error state', async () => {
    await setup({
      listAgents: vi.fn().mockReturnValue(throwError(() => ({ message: 'Failed to load agents' }))),
    });
    const fixture = await render();

    // Guard: the role="alert" error banner is rendered.
    expect(fixture.nativeElement.querySelector('.agent-catalog__error')).toBeTruthy();

    await expectNoAxeViolations(fixture.nativeElement);
  });
});
