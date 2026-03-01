/**
 * Tool Recommendation Models
 *
 * Structured recommendation data for tools, libraries, frameworks, and services.
 * Provides decision-relevant details for founders and technical leaders.
 */

/** Pricing model for a tool or service. */
export type PricingTier = 'free' | 'freemium' | 'paid' | 'enterprise' | 'usage_based';

/** License type for a tool or library. */
export type LicenseType =
  | 'mit'
  | 'apache_2'
  | 'gpl'
  | 'bsd'
  | 'proprietary'
  | 'custom_oss'
  | 'unknown';

/** Ease of integration level. */
export type EaseOfIntegration = 'low' | 'medium' | 'high';

/** Learning curve level. */
export type LearningCurve = 'minimal' | 'moderate' | 'steep';

/** Documentation quality level. */
export type DocumentationQuality = 'poor' | 'adequate' | 'good' | 'excellent';

/** Community size level. */
export type CommunitySize = 'small' | 'medium' | 'large' | 'massive';

/** Maturity level. */
export type MaturityLevel = 'emerging' | 'growing' | 'mature' | 'legacy';

/** Vendor lock-in risk level. */
export type VendorLockInRisk = 'none' | 'low' | 'medium' | 'high';

/** Migration complexity level. */
export type MigrationComplexity = 'trivial' | 'moderate' | 'complex';

/**
 * Structured recommendation for a tool, library, framework, or service.
 *
 * Provides decision-relevant details for founders and technical leaders:
 * pricing, licensing, adoption complexity, and risk factors.
 */
export interface ToolRecommendation {
  /** Name of the tool/service. */
  name: string;

  /** Category: database, ci_cd, monitoring, framework, hosting, auth, cache, queue, etc. */
  category: string;

  /** Brief description of what the tool does. */
  description: string;

  /** Why this tool is recommended for this use case. */
  rationale: string;

  /** Pricing model. */
  pricing_tier: PricingTier;

  /** Specific pricing info, e.g. "Free tier: 10k events/mo; Pro: $25/mo". */
  pricing_details: string;

  /** Estimated monthly cost for the use case, e.g. "$0-50", "usage-based". */
  estimated_monthly_cost?: string;

  /** License type. */
  license_type: LicenseType;

  /** Whether the tool is open source. */
  is_open_source: boolean;

  /** URL to source code if open source (GitHub, GitLab, etc.). */
  source_url?: string;

  /** Ease of integration: low, medium, high. */
  ease_of_integration: EaseOfIntegration;

  /** Learning curve: minimal, moderate, steep. */
  learning_curve: LearningCurve;

  /** Documentation quality: poor, adequate, good, excellent. */
  documentation_quality: DocumentationQuality;

  /** Community size: small, medium, large, massive. */
  community_size: CommunitySize;

  /** Maturity level: emerging, growing, mature, legacy. */
  maturity: MaturityLevel;

  /** Vendor lock-in risk: none, low, medium, high. */
  vendor_lock_in_risk: VendorLockInRisk;

  /** Migration complexity if switching away: trivial, moderate, complex. */
  migration_complexity: MigrationComplexity;

  /** 1-3 alternative tools/services. */
  alternatives: string[];

  /** Brief explanation of why the primary recommendation beats alternatives. */
  why_not_alternatives: string;

  /** Confidence score (0.0-1.0) that this is the right choice. */
  confidence: number;
}

/**
 * Summary view of a tool recommendation for list displays.
 */
export interface ToolRecommendationSummary {
  name: string;
  category: string;
  pricing_tier: PricingTier;
  is_open_source: boolean;
  maturity: MaturityLevel;
  confidence: number;
}

/**
 * Helper to convert a full ToolRecommendation to a summary.
 */
export function toToolRecommendationSummary(
  rec: ToolRecommendation
): ToolRecommendationSummary {
  return {
    name: rec.name,
    category: rec.category,
    pricing_tier: rec.pricing_tier,
    is_open_source: rec.is_open_source,
    maturity: rec.maturity,
    confidence: rec.confidence,
  };
}

/**
 * Display labels for pricing tiers.
 */
export const PRICING_TIER_LABELS: Record<PricingTier, string> = {
  free: 'Free',
  freemium: 'Freemium',
  paid: 'Paid',
  enterprise: 'Enterprise',
  usage_based: 'Usage-Based',
};

/**
 * Display labels for license types.
 */
export const LICENSE_TYPE_LABELS: Record<LicenseType, string> = {
  mit: 'MIT',
  apache_2: 'Apache 2.0',
  gpl: 'GPL',
  bsd: 'BSD',
  proprietary: 'Proprietary',
  custom_oss: 'Custom OSS',
  unknown: 'Unknown',
};

/**
 * Display labels for maturity levels.
 */
export const MATURITY_LABELS: Record<MaturityLevel, string> = {
  emerging: 'Emerging',
  growing: 'Growing',
  mature: 'Mature',
  legacy: 'Legacy',
};

/**
 * Display labels for vendor lock-in risk.
 */
export const LOCK_IN_RISK_LABELS: Record<VendorLockInRisk, string> = {
  none: 'None',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
};
