/* Khala — site interactivity
 * Roster rendering + filtering, nav scroll state, scroll reveals, mobile menu.
 * Vanilla JS, no build step. Roster data lives inline — kept in sync with
 * backend/unified_api/config.py by hand; this is the canonical README roster.
 */

const TEAMS = [
  // Core Dev
  { cell: 'core', name: 'Software Engineering', route: '/api/software-engineering',
    desc: 'Full dev-team simulation: architecture, planning, coding, review, release.' },
  { cell: 'core', name: 'Planning V3', route: '/api/planning-v3',
    desc: 'Client-facing discovery and PRDs; hands off to dev and UX.' },
  { cell: 'core', name: 'Coding Team', route: '/api/coding-team',
    desc: 'SE sub-team: tech lead plus stack specialists with a task graph.' },
  { cell: 'core', name: 'AI Systems', route: '/api/ai-systems',
    desc: 'Spec-driven factory that builds new AI agent systems.' },
  { cell: 'core', name: 'Agent Provisioning', route: '/api/agent-provisioning',
    desc: 'Stands up agent environments — databases, git, docker.' },
  { cell: 'core', name: 'Agentic Team Provisioning', route: '/api/agentic-team-provisioning',
    desc: 'Designs new teams and their processes by conversation.' },
  { cell: 'core', name: 'User Agent Founder', route: '/api/user-agent-founder',
    desc: 'Autonomous founder agent that drives the SE team end-to-end.' },
  { cell: 'core', name: 'Deepthought', route: '/api/deepthought',
    desc: 'Recursive self-organizing agent that spawns its own sub-agents.' },

  // Business
  { cell: 'business', name: 'Market Research', route: '/api/market-research',
    desc: 'User discovery and product-concept viability research.' },
  { cell: 'business', name: 'SOC2 Compliance', route: '/api/soc2-compliance',
    desc: 'Audit workflow through SOC2 certification.' },
  { cell: 'business', name: 'Investment', route: '/api/investment',
    desc: 'Financial advisor (IPS, proposals) + Strategy Lab (ideation, backtests).' },
  { cell: 'business', name: 'AI Sales Team', route: '/api/sales',
    desc: 'Full B2B sales pod: prospect → qualify → nurture → close.' },
  { cell: 'business', name: 'Startup Advisor', route: '/api/startup-advisor',
    desc: 'Persistent conversational advisor with probing dialogue.' },

  // Content
  { cell: 'content', name: 'Blogging', route: '/api/blogging',
    desc: 'Research → planning → draft → copy-edit → publish pipeline.' },
  { cell: 'content', name: 'Social Marketing', route: '/api/social-marketing',
    desc: 'Cross-platform campaigns with per-platform specialists.' },
  { cell: 'content', name: 'Branding', route: '/api/branding',
    desc: 'Brand strategy, moodboards, and design/writing standards.' },

  // Personal
  { cell: 'personal', name: 'Personal Assistant', route: '/api/personal-assistant',
    desc: 'Email, calendar, tasks, deals, reservations.' },
  { cell: 'personal', name: 'Accessibility Audit', route: '/api/accessibility-audit',
    desc: 'WCAG 2.2 and Section 508 auditing for web and mobile.' },
  { cell: 'personal', name: 'Nutrition & Meal Planning', route: '/api/nutrition-meal-planning',
    desc: 'Personalized meal plans that learn from your feedback.' },
  { cell: 'personal', name: 'Road Trip Planning', route: '/api/road-trip-planning',
    desc: 'Profiling, route optimization, activity recs, logistics.' }
];

const CELL_LABEL = {
  core: 'Core Dev',
  business: 'Business',
  content: 'Content',
  personal: 'Personal'
};

/* ---------- roster render ---------- */
function renderRoster(filter = 'all') {
  const grid = document.getElementById('roster-grid');
  if (!grid) return;

  const items = filter === 'all' ? TEAMS : TEAMS.filter(t => t.cell === filter);

  grid.innerHTML = items.map(t => `
    <article class="team-card reveal" data-cell="${t.cell}">
      <div class="team-head">
        <h3 class="team-name">${t.name}</h3>
        <span class="team-tag">${CELL_LABEL[t.cell]}</span>
      </div>
      <p class="team-route">${t.route}</p>
      <p class="team-desc">${t.desc}</p>
    </article>
  `).join('');

  // Re-arm reveal on new cards
  requestAnimationFrame(() => {
    grid.querySelectorAll('.team-card').forEach(card => card.classList.add('is-visible'));
  });
}

/* ---------- tab switching ---------- */
function initTabs() {
  const tabs = document.querySelectorAll('.roster-tabs .chip');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => {
        t.classList.remove('is-active');
        t.setAttribute('aria-selected', 'false');
      });
      tab.classList.add('is-active');
      tab.setAttribute('aria-selected', 'true');
      renderRoster(tab.dataset.filter);
    });
  });
}

/* ---------- nav scroll state ---------- */
function initNav() {
  const nav = document.getElementById('nav');
  if (!nav) return;

  const setScrolled = () => {
    if (window.scrollY > 16) nav.classList.add('is-scrolled');
    else nav.classList.remove('is-scrolled');
  };
  setScrolled();
  window.addEventListener('scroll', setScrolled, { passive: true });

  // Mobile toggle
  const toggle = nav.querySelector('.nav-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const open = nav.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', String(open));
    });
    // Close menu when a link is clicked
    nav.querySelectorAll('.nav-links a').forEach(a => {
      a.addEventListener('click', () => {
        nav.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
      });
    });
  }
}

/* ---------- scroll reveals ---------- */
function initReveals() {
  // Tag candidates
  const selectors = [
    '.trio-card', '.why-card', '.adr', '.start-card',
    '.timeline li', '.warning', '.add-team', '.hero-stats > div'
  ];
  const nodes = document.querySelectorAll(selectors.join(','));
  nodes.forEach(n => n.classList.add('reveal'));

  if (!('IntersectionObserver' in window)) {
    // Fallback: show everything immediately
    nodes.forEach(n => n.classList.add('is-visible'));
    return;
  }

  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('is-visible');
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.08, rootMargin: '0px 0px -40px 0px' });

  nodes.forEach(n => io.observe(n));
}

/* ---------- boot ---------- */
document.addEventListener('DOMContentLoaded', () => {
  renderRoster('all');
  initTabs();
  initNav();
  initReveals();
});
