import { ALL_NAV_ITEMS } from './navigation.model';

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
