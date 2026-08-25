import { ALL_NAV_ITEMS, NAV_GROUPS, findGroupForRoute } from './navigation.model';

describe('NAV_GROUPS Cognition entry', () => {
  it('registers Cognition as a top-level agentic-ai item at /cognition', () => {
    const item = ALL_NAV_ITEMS.find((i) => i.id === 'cognition');
    expect(item).toEqual({
      id: 'cognition',
      label: 'Cognition',
      icon: 'psychology',
      route: '/cognition',
      group: 'agentic-ai',
    });
  });
});

describe('NAV_GROUPS Studio-only journey entry', () => {
  const agenticGroup = NAV_GROUPS.find((g) => g.key === 'agentic-ai');

  it('has an agentic-ai navigation group', () => {
    expect(agenticGroup).toBeDefined();
  });

  it('uses Agent Studio as the sole exact-match entry point for the agentic-ai group', () => {
    const item = ALL_NAV_ITEMS.find((i) => i.id === 'agent-studio');
    expect(item).toEqual({
      id: 'agent-studio',
      label: 'Agent Studio',
      icon: 'smart_toy',
      route: '/agent-studio',
      group: 'agentic-ai',
      exact: true,
    });
  });

  it('nests Provisioning under Agent Studio', () => {
    const item = ALL_NAV_ITEMS.find((i) => i.id === 'agent-studio-provisioning');
    expect(item).toEqual({
      id: 'agent-studio-provisioning',
      label: 'Provisioning',
      icon: 'dns',
      route: '/agent-studio/provisioning',
      group: 'agentic-ai',
      nested: true,
    });
  });

  it('nests Metrics under Agent Studio', () => {
    const item = ALL_NAV_ITEMS.find((i) => i.id === 'agent-studio-metrics');
    expect(item).toEqual({
      id: 'agent-studio-metrics',
      label: 'Metrics',
      icon: 'monitoring',
      route: '/agent-studio/metrics',
      group: 'agentic-ai',
      nested: true,
    });
  });

  it('Agent Studio is the first non-nested item in the agentic-ai group (#6525)', () => {
    const topLevel = agenticGroup!.items.filter((i) => !i.nested);
    expect(topLevel[0].id).toBe('agent-studio');
  });

  it('does not expose legacy Console, Teams, or Personas anywhere in the global nav', () => {
    // Check IDs globally — catches items reintroduced under any group
    expect(ALL_NAV_ITEMS.some((i) => i.id === 'agent-console')).toBe(false);
    expect(ALL_NAV_ITEMS.some((i) => i.id === 'agentic-teams')).toBe(false);
    expect(ALL_NAV_ITEMS.some((i) => i.id === 'persona-testing')).toBe(false);
    // Check routes globally — catches renamed IDs that reuse legacy URLs
    expect(ALL_NAV_ITEMS.some((i) => i.route === '/agent-console')).toBe(false);
    expect(ALL_NAV_ITEMS.some((i) => i.route === '/agentic-teams')).toBe(false);
    expect(ALL_NAV_ITEMS.some((i) => i.route === '/persona-testing')).toBe(false);
  });

  it('only exposes Studio-based routes as navigable /agent-studio paths', () => {
    // Check globally so routes added under any group are caught
    const studioRoutes = ALL_NAV_ITEMS.filter((i) => i.route.startsWith('/agent-studio'));
    expect(studioRoutes.map((r) => r.id).sort()).toEqual([
      'agent-studio',
      'agent-studio-metrics',
      'agent-studio-provisioning',
    ]);
  });
});

describe('findGroupForRoute – deep-link resolution', () => {
  it('resolves /agent-studio to the agentic-ai group', () => {
    expect(findGroupForRoute('/agent-studio')?.key).toBe('agentic-ai');
  });

  it('resolves /agent-studio/provisioning to the agentic-ai group', () => {
    expect(findGroupForRoute('/agent-studio/provisioning')?.key).toBe('agentic-ai');
  });

  it('resolves /agent-studio/metrics to the agentic-ai group', () => {
    expect(findGroupForRoute('/agent-studio/metrics')?.key).toBe('agentic-ai');
  });

  it('returns undefined for removed legacy routes', () => {
    expect(findGroupForRoute('/agent-console')).toBeUndefined();
    expect(findGroupForRoute('/agentic-teams')).toBeUndefined();
    expect(findGroupForRoute('/persona-testing')).toBeUndefined();
  });
});
