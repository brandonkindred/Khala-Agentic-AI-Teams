"""
Shared guidance for tool recommendations across all agents.

When agents recommend external tools, libraries, frameworks, or services,
they should follow this guidance to provide decision-relevant details.
"""

from typing import Optional

_tool_recommendation_guidance_cache: Optional[str] = None


def get_tool_recommendation_guidance_cached() -> str:
    """
    Return TOOL_RECOMMENDATION_GUIDANCE text. Cached per process.
    Use this when building prompts that need the full guidance.
    """
    global _tool_recommendation_guidance_cache
    if _tool_recommendation_guidance_cache is None:
        _tool_recommendation_guidance_cache = TOOL_RECOMMENDATION_GUIDANCE
    return _tool_recommendation_guidance_cache


TOOL_RECOMMENDATION_GUIDANCE = """
**TOOL RECOMMENDATION STANDARDS (all agents must follow when recommending tools/services):**

When recommending any external tool, library, framework, or service, you MUST provide structured details to help founders and technical leaders make informed decisions.

## Required Information

### 1. Pricing Information
- **Pricing model**: free, freemium, paid, enterprise, or usage-based
- **Specific pricing details**: free tier limits, base plan cost, per-seat pricing, overage charges
- **Estimated monthly cost**: approximate cost for the use case at hand (e.g., "$0", "$25-50/mo", "usage-based ~$100/mo at scale")

### 2. Licensing
- **License type**: MIT, Apache 2.0, GPL, BSD, proprietary, etc.
- **Open source**: whether the tool is open source
- **Source URL**: link to source code repository if open source

### 3. Adoption Considerations
- **Ease of integration**: how much work to add to existing stack (low/medium/high)
- **Learning curve**: time and effort to become productive (minimal/moderate/steep)
- **Documentation quality**: quality of official docs (poor/adequate/good/excellent)
- **Community size**: size and activity of user community (small/medium/large/massive)
- **Maturity level**: project maturity (emerging/growing/mature/legacy)

### 4. Risk Factors
- **Vendor lock-in risk**: risk of being locked into this vendor (none/low/medium/high)
- **Migration complexity**: difficulty of switching away later (trivial/moderate/complex)
- **Known limitations**: any gotchas, constraints, or issues to be aware of

### 5. Alternatives
- **Alternative options**: 1-3 alternative tools/services to consider
- **Comparison**: brief explanation of why the primary recommendation beats the alternatives for this use case

## Output Format

When making tool recommendations, output them using this JSON structure:

```json
{
  "name": "Tool Name",
  "category": "database|ci_cd|monitoring|framework|hosting|auth|cache|queue|etc",
  "description": "Brief description of what the tool does",
  "rationale": "Why this tool is recommended for this specific use case",
  "pricing_tier": "free|freemium|paid|enterprise|usage_based",
  "pricing_details": "Free tier: 10k events/mo; Pro: $25/mo; Enterprise: custom",
  "estimated_monthly_cost": "$0-50 for typical usage",
  "license_type": "mit|apache_2|gpl|bsd|proprietary|custom_oss|unknown",
  "is_open_source": true,
  "source_url": "https://github.com/org/repo",
  "ease_of_integration": "low|medium|high",
  "learning_curve": "minimal|moderate|steep",
  "documentation_quality": "poor|adequate|good|excellent",
  "community_size": "small|medium|large|massive",
  "maturity": "emerging|growing|mature|legacy",
  "vendor_lock_in_risk": "none|low|medium|high",
  "migration_complexity": "trivial|moderate|complex",
  "alternatives": ["Alternative 1", "Alternative 2"],
  "why_not_alternatives": "Brief explanation of tradeoffs",
  "confidence": 0.85
}
```

## Examples

### Example 1: Database Recommendation

```json
{
  "name": "PostgreSQL",
  "category": "database",
  "description": "Advanced open-source relational database with strong ACID compliance",
  "rationale": "Best fit for transactional workloads with complex queries; excellent ecosystem support",
  "pricing_tier": "free",
  "pricing_details": "Open source, free to use. Managed services: AWS RDS ~$15-200/mo, Supabase free tier available",
  "estimated_monthly_cost": "$0 self-hosted; $15-50/mo managed for small-medium apps",
  "license_type": "bsd",
  "is_open_source": true,
  "source_url": "https://github.com/postgres/postgres",
  "ease_of_integration": "high",
  "learning_curve": "moderate",
  "documentation_quality": "excellent",
  "community_size": "massive",
  "maturity": "mature",
  "vendor_lock_in_risk": "none",
  "migration_complexity": "moderate",
  "alternatives": ["MySQL", "SQLite", "CockroachDB"],
  "why_not_alternatives": "MySQL has weaker JSON support; SQLite not suitable for concurrent writes; CockroachDB adds complexity for single-node deployments",
  "confidence": 0.95
}
```

### Example 2: CI/CD Tool Recommendation

```json
{
  "name": "GitHub Actions",
  "category": "ci_cd",
  "description": "CI/CD platform integrated directly into GitHub repositories",
  "rationale": "Zero setup overhead for GitHub-hosted repos; generous free tier; extensive marketplace",
  "pricing_tier": "freemium",
  "pricing_details": "Free: 2,000 mins/mo (public unlimited); Team: $4/user/mo + mins; Enterprise: custom",
  "estimated_monthly_cost": "$0 for small teams; $20-100/mo for medium teams with heavy usage",
  "license_type": "proprietary",
  "is_open_source": false,
  "source_url": null,
  "ease_of_integration": "high",
  "learning_curve": "minimal",
  "documentation_quality": "excellent",
  "community_size": "massive",
  "maturity": "mature",
  "vendor_lock_in_risk": "medium",
  "migration_complexity": "moderate",
  "alternatives": ["GitLab CI", "CircleCI", "Jenkins"],
  "why_not_alternatives": "GitLab CI requires migration from GitHub; CircleCI has lower free tier; Jenkins requires self-hosting and maintenance",
  "confidence": 0.90
}
```

## Notes

- If current pricing is unknown, note "verify current pricing on vendor website"
- For rapidly changing tools (AI/ML services), note that pricing may have changed
- When multiple tools are needed (e.g., database + cache), provide separate recommendations for each
- Confidence score reflects certainty that this is the right choice for the specific use case
"""

TOOL_RECOMMENDATION_JSON_SCHEMA = """
When outputting tool recommendations, use this JSON schema:

{
  "tool_recommendations": [
    {
      "name": "string (required)",
      "category": "string (required)",
      "description": "string (required)",
      "rationale": "string (required)",
      "pricing_tier": "free|freemium|paid|enterprise|usage_based (required)",
      "pricing_details": "string (required)",
      "estimated_monthly_cost": "string or null",
      "license_type": "mit|apache_2|gpl|bsd|proprietary|custom_oss|unknown (required)",
      "is_open_source": "boolean (required)",
      "source_url": "string or null",
      "ease_of_integration": "low|medium|high",
      "learning_curve": "minimal|moderate|steep",
      "documentation_quality": "poor|adequate|good|excellent",
      "community_size": "small|medium|large|massive",
      "maturity": "emerging|growing|mature|legacy",
      "vendor_lock_in_risk": "none|low|medium|high",
      "migration_complexity": "trivial|moderate|complex",
      "alternatives": ["string"],
      "why_not_alternatives": "string",
      "confidence": "float 0.0-1.0"
    }
  ]
}
"""
