/**
 * Production environment configuration.
 * API base is the strands-agents Docker container (port 8888); override via build-time replacement if needed.
 */
const apiBase = 'http://localhost:8888';
export const environment = {
  production: true,
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
};
