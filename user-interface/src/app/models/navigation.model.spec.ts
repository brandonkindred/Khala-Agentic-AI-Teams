import { ALL_NAV_ITEMS, NAV_GROUPS } from './navigation.model';

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

describe('NAV_GROUPS Agent Console retirement', () => {
  it('no longer lists Agent Console as a peer product', () => {
    expect(ALL_NAV_ITEMS.some((i) => i.id === 'agent-console')).toBe(false);
    expect(ALL_NAV_ITEMS.some((i) => i.route === '/agent-console')).toBe(false);
  });

  it('marks Agent Studio as an exact-match parent so child routes do not co-activate it', () => {
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

  it('relocates Provisioning under Agent Studio as a nested route', () => {
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

  it('relocates Metrics under Agent Studio as a nested route', () => {
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
});

describe('NAV_GROUPS Agent Studio is the sole product entry (#6525)', () => {
  it('Agent Studio is the first non-nested item in the agentic-ai group', () => {
    const agenticGroup = NAV_GROUPS.find((g) => g.key === 'agentic-ai');
    expect(agenticGroup).toBeDefined();
    const topLevel = agenticGroup!.items.filter((i) => !i.nested);
    expect(topLevel[0].id).toBe('agent-studio');
  });
});

describe('NAV_GROUPS Agentic Teams / Testing Personas retirement', () => {
  it('no longer lists Agentic Teams as a peer product', () => {
    expect(ALL_NAV_ITEMS.some((i) => i.id === 'agentic-teams')).toBe(false);
    expect(ALL_NAV_ITEMS.some((i) => i.route === '/agentic-teams')).toBe(false);
  });

  it('no longer lists Testing Personas as a peer product', () => {
    expect(ALL_NAV_ITEMS.some((i) => i.id === 'persona-testing')).toBe(false);
    expect(ALL_NAV_ITEMS.some((i) => i.route === '/persona-testing')).toBe(false);
  });
});
