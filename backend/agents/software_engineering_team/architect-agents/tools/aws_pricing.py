"""AWS Pricing API wrapper for cost estimation."""

from __future__ import annotations

import json
from typing import Any

from strands import tool

# Fallback pricing (approximate USD/month) when Pricing API is unavailable
_FALLBACK_PRICES: dict[str, dict[str, Any]] = {
    "ec2": {
        "t3.micro": 7.5,
        "t3.small": 15,
        "t3.medium": 30,
        "t3.large": 60,
        "m5.large": 70,
        "m5.xlarge": 140,
    },
    "rds": {
        "db.t3.micro": 15,
        "db.t3.small": 30,
        "db.t3.medium": 60,
        "db.m5.large": 140,
    },
    "lambda": {"per_1m_requests": 0.20, "per_gb_second": 0.0000166667},
    "s3": {"per_gb_storage": 0.023, "per_1k_requests": 0.0004},
    "dynamodb": {"on_demand_per_1m_reads": 0.25, "on_demand_per_1m_writes": 1.25},
    "elasticache": {"cache.t3.micro": 12, "cache.t3.small": 24},
}


def _get_pricing_from_api(service: str, region: str, instance_type: str | None = None) -> dict[str, Any] | None:
    """Attempt to get pricing from AWS Pricing API. Returns None on failure."""
    try:
        import boto3
        from botocore.exceptions import ClientError

        client = boto3.client("pricing", region_name="us-east-1")
        if service.lower() == "ec2":
            response = client.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type or "t3.micro"},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                ],
                MaxResults=1,
            )
            if response.get("PriceList"):
                return json.loads(response["PriceList"][0])
        # Add more service mappings as needed
        return None
    except (ImportError, ClientError, Exception):
        return None


@tool
def aws_pricing_tool(
    service: str,
    region: str = "us-east-1",
    instance_type: str | None = None,
    quantity: int = 1,
) -> str:
    """Get estimated monthly AWS cost for a service.

    Use this to check current AWS pricing, service limits, and estimate
    infrastructure costs. Falls back to approximate pricing when the
    Pricing API is unavailable.

    Args:
        service: AWS service (ec2, rds, lambda, s3, dynamodb, elasticache).
        region: AWS region (default us-east-1).
        instance_type: For EC2/RDS/ElastiCache, the instance type
            (e.g. t3.micro, m5.large).
        quantity: Number of units (instances, GB, etc.) for cost scaling.

    Returns:
        JSON string with estimated monthly cost in USD and assumptions.
    """
    service_lower = service.lower()
    result: dict[str, Any] = {
        "service": service,
        "region": region,
        "estimated_monthly_usd": None,
        "assumptions": [],
        "source": "fallback",
    }

    # Try Pricing API first
    api_result = _get_pricing_from_api(service, region, instance_type)
    if api_result:
        # Parse PriceList structure - simplified; real parsing is complex
        result["source"] = "pricing_api"
        result["assumptions"].append("Parsed from AWS Pricing API")
        # Full parsing of PriceList would go here
        result["estimated_monthly_usd"] = "See raw response"
        result["raw"] = str(api_result)[:500]
        return json.dumps(result, indent=2)

    # Fallback to static estimates
    if service_lower in _FALLBACK_PRICES:
        prices = _FALLBACK_PRICES[service_lower]
        if instance_type and instance_type in prices:
            result["estimated_monthly_usd"] = round(prices[instance_type] * quantity, 2)
            result["assumptions"].append(f"{quantity}x {instance_type} (approximate)")
        elif service_lower == "lambda":
            result["estimated_monthly_usd"] = "Usage-based; see per_1m_requests, per_gb_second"
            result["assumptions"].append("On-demand pricing")
        elif service_lower == "s3":
            result["estimated_monthly_usd"] = "Usage-based; see per_gb_storage"
            result["assumptions"].append("Standard storage")
        elif service_lower == "dynamodb":
            result["estimated_monthly_usd"] = "Usage-based; on-demand pricing"
            result["assumptions"].append("On-demand mode")
        else:
            # Use first available instance type as default
            first_key = next(iter(prices))
            result["estimated_monthly_usd"] = round(prices[first_key] * quantity, 2)
            result["assumptions"].append(f"Default {first_key} x {quantity} (approximate)")
    else:
        result["assumptions"].append(f"No pricing data for {service}; check AWS calculator")

    return json.dumps(result, indent=2)
