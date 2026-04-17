/**
 * Development environment configuration.
 * All API requests go directly to the unified API at port 8888 (no proxy).
 */
const apiBase = 'http://localhost:8888';
export const environment = {
  production: false,
  bloggingApiUrl: `${apiBase}/api/blogging`,
  softwareEngineeringApiUrl: `${apiBase}/api/software-engineering`,
  /** Coding Team — Software Engineering sub-team (Task Graph, Senior SWEs) */
  codingTeamApiUrl: `${apiBase}/api/coding-team`,
  planningV3ApiUrl: `${apiBase}/api/planning-v3`,
  marketResearchApiUrl: `${apiBase}/api/market-research`,
  soc2ComplianceApiUrl: `${apiBase}/api/soc2-compliance`,
  socialMarketingApiUrl: `${apiBase}/api/social-marketing`,
  brandingApiUrl: `${apiBase}/api/branding`,
  personalAssistantApiUrl: `${apiBase}/api/personal-assistant`,
  accessibilityApiUrl: `${apiBase}/api/accessibility-audit`,
  agentProvisioningApiUrl: `${apiBase}/api/agent-provisioning`,
  aiSystemsApiUrl: `${apiBase}/api/ai-systems`,
  investmentApiUrl: `${apiBase}/api/investment`,
  nutritionApiUrl: `${apiBase}/api/nutrition-meal-planning`,
  integrationsApiUrl: `${apiBase}/api/integrations`,
  salesApiUrl: `${apiBase}/api/sales`,
  agenticTeamProvisioningApiUrl: `${apiBase}/api/agentic-team-provisioning`,
  startupAdvisorApiUrl: `${apiBase}/api/startup-advisor`,
  personaTestingApiUrl: `${apiBase}/api/user-agent-founder`,
  deepthoughtApiUrl: `${apiBase}/api/deepthought`,
  roadTripPlanningApiUrl: `${apiBase}/api/road-trip-planning`,
};

