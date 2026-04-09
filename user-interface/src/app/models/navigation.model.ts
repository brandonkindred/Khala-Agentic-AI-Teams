/**
 * Data-driven sidebar navigation model.
 *
 * Mirrors the structure previously hardcoded in AppShellComponent template.
 * Used by NavStateService for collapsible groups, favorites, and keyboard nav.
 */

export interface NavItem {
  /** Unique identifier, e.g. 'blogging', 'se-planning' */
  id: string;
  label: string;
  /** Material icon name */
  icon: string;
  /** Router path, e.g. '/blogging' */
  route: string;
  /** Parent group key */
  group: string;
  /** Indented under parent link */
  nested?: boolean;
  /** When true, routerLinkActive matches only the exact path */
  exact?: boolean;
}

export interface NavGroup {
  /** Unique key, e.g. 'content', 'development' */
  key: string;
  label: string;
  items: NavItem[];
}

// ---------------------------------------------------------------------------
// Full navigation registry — exact mirror of the previous hardcoded sidebar.
// ---------------------------------------------------------------------------

export const NAV_GROUPS: NavGroup[] = [
  {
    key: 'content',
    label: 'Content',
    items: [
      { id: 'blogging', label: 'Blogging', icon: 'article', route: '/blogging', group: 'content', exact: true },
      { id: 'blogging-dashboard', label: 'Pipeline Dashboard', icon: 'space_dashboard', route: '/blogging/dashboard', group: 'content', nested: true },
      { id: 'branding', label: 'Branding', icon: 'palette', route: '/branding', group: 'content' },
      { id: 'social-marketing', label: 'Social Marketing', icon: 'campaign', route: '/social-marketing', group: 'content' },
    ],
  },
  {
    key: 'development',
    label: 'Development',
    items: [
      { id: 'software-engineering', label: 'Software Engineering', icon: 'terminal', route: '/software-engineering', group: 'development', exact: true },
      { id: 'se-planning', label: 'Planning', icon: 'description', route: '/software-engineering/planning-v3', group: 'development', nested: true },
      { id: 'se-coding-team', label: 'Coding Team', icon: 'code', route: '/software-engineering/coding-team', group: 'development', nested: true },
      { id: 'persona-testing', label: 'Persona Testing', icon: 'science', route: '/persona-testing', group: 'development', nested: true },
    ],
  },
  {
    key: 'research',
    label: 'Research & Compliance',
    items: [
      { id: 'market-research', label: 'Market Research', icon: 'insights', route: '/market-research', group: 'research' },
      { id: 'soc2-compliance', label: 'SOC2 Compliance', icon: 'verified_user', route: '/soc2-compliance', group: 'research' },
      { id: 'accessibility', label: 'Accessibility Audit', icon: 'accessibility_new', route: '/accessibility', group: 'research' },
    ],
  },
  {
    key: 'finance',
    label: 'Finance',
    items: [
      { id: 'investment', label: 'Investment', icon: 'account_balance', route: '/investment', group: 'finance', exact: true },
      { id: 'investment-advisor', label: 'Advisor & IPS', icon: 'support_agent', route: '/investment/advisor', group: 'finance', nested: true },
      { id: 'investment-strategy-lab', label: 'Strategy Lab', icon: 'science', route: '/investment/strategy-lab', group: 'finance', nested: true },
      { id: 'startup-advisor', label: 'Startup Advisor', icon: 'rocket_launch', route: '/startup-advisor', group: 'finance' },
    ],
  },
  {
    key: 'revenue',
    label: 'Revenue',
    items: [
      { id: 'sales', label: 'Sales', icon: 'trending_up', route: '/sales', group: 'revenue' },
    ],
  },
  {
    key: 'personal',
    label: 'Personal',
    items: [
      { id: 'personal-assistant', label: 'Personal Assistant', icon: 'smart_toy', route: '/personal-assistant', group: 'personal' },
      { id: 'nutrition', label: 'Nutritionist', icon: 'restaurant_menu', route: '/nutrition', group: 'personal' },
    ],
  },
  {
    key: 'agentic-ai',
    label: 'Agentic AI',
    items: [
      { id: 'ai-systems', label: 'AI Systems', icon: 'psychology', route: '/ai-systems', group: 'agentic-ai' },
      { id: 'agent-provisioning', label: 'Agent Provisioning', icon: 'cloud_queue', route: '/agent-provisioning', group: 'agentic-ai' },
      { id: 'agentic-teams', label: 'Agentic Teams', icon: 'groups', route: '/agentic-teams', group: 'agentic-ai' },
      { id: 'deepthought', label: 'Deepthought', icon: 'psychology', route: '/deepthought', group: 'agentic-ai' },
    ],
  },
  {
    key: 'settings',
    label: 'Settings',
    items: [
      { id: 'integrations', label: 'Integrations', icon: 'integration_instructions', route: '/integrations', group: 'settings' },
    ],
  },
];

/** Flat list of all nav items across all groups. */
export const ALL_NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap(g => g.items);

/** Find the group that contains a given route. */
export function findGroupForRoute(route: string): NavGroup | undefined {
  return NAV_GROUPS.find(g => g.items.some(item => route.startsWith(item.route)));
}
