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
