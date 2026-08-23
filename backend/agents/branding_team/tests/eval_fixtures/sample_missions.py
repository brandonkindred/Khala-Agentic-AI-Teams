"""Sample mission corpus for selective-context eval comparison.

Provides a small, diverse set of ``BrandingMission`` fixtures — varying
industry, target audience, and brand maturity/completeness — for use by a
future eval script that compares branding pipeline output quality with and
without selective context filtering. This module only builds the corpus; it
does not run the pipeline or generate golden outputs.

Postconditions:
    ``SAMPLE_MISSIONS`` is a non-empty list of valid ``BrandingMission``
    instances spanning at least tech, consumer, and B2B industries, and
    both minimal-field and full-field completeness levels.
"""

from __future__ import annotations

from branding_team.models import BrandingMission

# Tech / startup, full fields — a mature brand with every optional field
# (including visual identity) populated.
_TECH_STARTUP_FULL = BrandingMission(
    company_name="Northwind Analytics",
    company_description="A SaaS platform that turns raw product telemetry into real-time growth insights.",
    target_audience="data-driven product managers at Series A-C SaaS startups",
    values=["clarity", "rigor", "speed"],
    differentiators=[
        "real-time anomaly detection",
        "no-code dashboard builder",
        "usage-based pricing",
    ],
    desired_voice="sharp, technical, confident",
    existing_brand_material=["pitch deck v3", "landing page copy", "investor one-pager"],
    color_inspiration=["deep indigo", "electric cyan"],
    visual_style="minimalist",
    typography_preference="geometric sans-serif",
    interface_density="spacious/minimalist",
)

# Consumer / minimal fields — an early-stage DTC brand supplying only the
# three required fields; everything else defaults.
_CONSUMER_DTC_MINIMAL = BrandingMission(
    company_name="Petal & Pine",
    company_description="A direct-to-consumer subscription box for locally sourced seasonal flowers.",
    target_audience="urban millennials who want fresh flowers without the flower-shop trip",
)

# B2B / partial fields — a mid-maturity enterprise services brand with some
# optional fields set but no visual-identity work done yet.
_B2B_ENTERPRISE_PARTIAL = BrandingMission(
    company_name="Ironclad Compliance Partners",
    company_description="A consultancy that helps mid-market financial firms pass SOC2 and ISO 27001 audits.",
    target_audience="compliance and risk officers at mid-market financial services firms",
    values=["trust", "precision", "accountability"],
    differentiators=["audit-ready in 90 days", "dedicated former auditors on staff"],
    existing_brand_material=["client case studies"],
)

# Nonprofit / minimal-to-partial fields — another industry and maturity
# combination for extra corpus diversity.
_NONPROFIT_MINIMAL = BrandingMission(
    company_name="Harborline Relief",
    company_description="A disaster-response nonprofit coordinating volunteer logistics for coastal communities.",
    target_audience="donors and volunteer coordinators in coastal disaster-prone regions",
    values=["urgency", "transparency"],
    desired_voice="warm, direct, urgent",
)

SAMPLE_MISSIONS: list[BrandingMission] = [
    _TECH_STARTUP_FULL,
    _CONSUMER_DTC_MINIMAL,
    _B2B_ENTERPRISE_PARTIAL,
    _NONPROFIT_MINIMAL,
]
