/**
 * Development environment configuration.
 * All APIs are served through the unified gateway at port 8080.
 */
export const environment = {
  production: false,
  /** Blogging API (research-and-review, full-pipeline) */
  bloggingApiUrl: 'http://localhost:8080/api/blogging',
  /** Software Engineering Team API */
  softwareEngineeringApiUrl: 'http://localhost:8080/api/software-engineering',
  /** Planning V3 Team API (client-facing discovery / PRD) */
  planningV3ApiUrl: 'http://localhost:8080/api/planning-v3',
  /** Market Research Team API */
  marketResearchApiUrl: 'http://localhost:8080/api/market-research',
  /** SOC2 Compliance Team API */
  soc2ComplianceApiUrl: 'http://localhost:8080/api/soc2-compliance',
  /** Social Media Marketing Team API */
  socialMarketingApiUrl: 'http://localhost:8080/api/social-marketing',
  /** Branding Team API */
  brandingApiUrl: 'http://localhost:8080/api/branding',
  /** Personal Assistant Team API */
  personalAssistantApiUrl: 'http://localhost:8080/api/personal-assistant',
  /** Accessibility Audit Team API */
  accessibilityApiUrl: 'http://localhost:8080/api/accessibility-audit',
  /** Agent Provisioning Team API */
  agentProvisioningApiUrl: 'http://localhost:8080/api/agent-provisioning',
  /** AI Systems Team API */
  aiSystemsApiUrl: 'http://localhost:8080/api/ai-systems',
  /** Investment Team API */
  investmentApiUrl: 'http://localhost:8080/api/investment',
  /** Integrations API (Slack, etc.) - unified API */
  integrationsApiUrl: 'http://localhost:8080/api/integrations',
  /** StudioGrid design-system workflow API */
  studioGridApiUrl: 'http://localhost:8080/api/studio-grid',
};
