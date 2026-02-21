/**
 * Development environment configuration.
 * API base URLs for each agent service (default ports per agents/README.md).
 */
export const environment = {
  production: false,
  /** Blogging API (research-and-review, full-pipeline) - default port 8000 */
  bloggingApiUrl: 'http://localhost:8000',
  /** Software Engineering Team API - default port 8000 */
  softwareEngineeringApiUrl: 'http://localhost:8000',
  /** Market Research Team API - default port 8011 */
  marketResearchApiUrl: 'http://localhost:8011',
  /** SOC2 Compliance Team API - default port 8020 */
  soc2ComplianceApiUrl: 'http://localhost:8020',
  /** Social Media Marketing Team API - default port 8010 */
  socialMarketingApiUrl: 'http://localhost:8010',
  /** Branding Team API - default port 8012 */
  brandingApiUrl: 'http://localhost:8012',
};
