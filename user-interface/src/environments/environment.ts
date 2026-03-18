/**
 * Development environment configuration.
 * All API requests go directly to the unified API at port 8888 (no proxy).
 */
const apiBase = 'http://localhost:8888';
export const environment = {
  production: false,
  bloggingApiUrl: `${apiBase}/api/blogging`,
  softwareEngineeringApiUrl: `${apiBase}/api/software-engineering`,
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
  integrationsApiUrl: `${apiBase}/api/integrations`,
  /** StudioGrid design-system workflow API */
  studioGridApiUrl: `${apiBase}/api/studio-grid`,
  salesApiUrl: `${apiBase}/api/sales`,
};

