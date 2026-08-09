/**
 * Production environment configuration.
 * All API requests go directly to the unified API at port 8888 (no proxy).
 */
const apiBase = 'http://localhost:8888';
export const environment = {
  production: true,
  bloggingApiUrl: `${apiBase}/api/blogging`,
  softwareEngineeringApiUrl: `${apiBase}/api/software-engineering`,
  codingTeamApiUrl: `${apiBase}/api/coding-team`,
  planningApiUrl: `${apiBase}/api/planning`,
  marketResearchApiUrl: `${apiBase}/api/market-research`,
  soc2ComplianceApiUrl: `${apiBase}/api/soc2-compliance`,
  socialMarketingApiUrl: `${apiBase}/api/social-marketing`,
  brandingApiUrl: `${apiBase}/api/branding`,
  personalAssistantApiUrl: `${apiBase}/api/personal-assistant`,
  accessibilityApiUrl: `${apiBase}/api/accessibility-audit`,
  agentProvisioningApiUrl: `${apiBase}/api/agent-provisioning`,
  agentRegistryApiUrl: `${apiBase}/api/agents`,
  agentStudioApiUrl: `${apiBase}/api/agent-studio`,
  aiSystemsApiUrl: `${apiBase}/api/ai-systems`,
  investmentApiUrl: `${apiBase}/api/investment`,
  integrationsApiUrl: `${apiBase}/api/integrations`,
  llmConfigApiUrl: `${apiBase}/api/llm-config`,
  salesApiUrl: `${apiBase}/api/sales`,
  agenticTeamProvisioningApiUrl: `${apiBase}/api/agentic-team-provisioning`,
  startupAdvisorApiUrl: `${apiBase}/api/startup-advisor`,
  personaTestingApiUrl: `${apiBase}/api/user-agent-founder`,
  deepthoughtApiUrl: `${apiBase}/api/deepthought`,
  roadTripPlanningApiUrl: `${apiBase}/api/road-trip-planning`,
  productDeliveryApiUrl: `${apiBase}/api/product-delivery`,
  agentCognitionApiUrl: `${apiBase}/api/cognition`,
  userProfileApiUrl: `${apiBase}/api/user-profile`,
  jobMatchingApiUrl: `${apiBase}/api/job-matching`,
};
