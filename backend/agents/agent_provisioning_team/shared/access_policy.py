"""
Access policy definitions for permission tiers.

Maps access tiers to specific permissions for each tool type.
"""

from typing import Dict, List, Set, Tuple

from ..models import AccessTier


POSTGRES_PERMISSIONS: Dict[AccessTier, List[str]] = {
    AccessTier.MINIMAL: ["SELECT"],
    AccessTier.STANDARD: ["SELECT", "INSERT", "UPDATE", "DELETE"],
    AccessTier.ELEVATED: ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "INDEX"],
    AccessTier.FULL: ["ALL PRIVILEGES"],
}

REDIS_PERMISSIONS: Dict[AccessTier, List[str]] = {
    AccessTier.MINIMAL: ["GET", "KEYS", "EXISTS", "TYPE"],
    AccessTier.STANDARD: ["GET", "SET", "DEL", "KEYS", "EXISTS", "TYPE", "EXPIRE", "TTL"],
    AccessTier.ELEVATED: ["GET", "SET", "DEL", "KEYS", "EXISTS", "TYPE", "EXPIRE", "TTL", "PUBLISH", "SUBSCRIBE"],
    AccessTier.FULL: ["+@all"],
}

GIT_PERMISSIONS: Dict[AccessTier, List[str]] = {
    AccessTier.MINIMAL: ["read"],
    AccessTier.STANDARD: ["read", "write"],
    AccessTier.ELEVATED: ["read", "write", "admin"],
    AccessTier.FULL: ["read", "write", "admin", "delete"],
}

DOCKER_PERMISSIONS: Dict[AccessTier, List[str]] = {
    AccessTier.MINIMAL: ["inspect", "logs"],
    AccessTier.STANDARD: ["inspect", "logs", "exec"],
    AccessTier.ELEVATED: ["inspect", "logs", "exec", "start", "stop", "restart"],
    AccessTier.FULL: ["all"],
}


def get_permissions(tool_type: str, access_tier: AccessTier) -> List[str]:
    """Get the list of permissions for a tool type and access tier.
    
    Args:
        tool_type: Type of tool (postgresql, redis, git, docker)
        access_tier: Requested access tier
    
    Returns:
        List of permission strings for the tool
    """
    permission_maps = {
        "postgresql": POSTGRES_PERMISSIONS,
        "postgres": POSTGRES_PERMISSIONS,
        "redis": REDIS_PERMISSIONS,
        "git": GIT_PERMISSIONS,
        "docker": DOCKER_PERMISSIONS,
    }
    
    perm_map = permission_maps.get(tool_type.lower())
    if perm_map is None:
        return ["standard"]
    
    return perm_map.get(access_tier, perm_map[AccessTier.STANDARD])


def validate_permissions(
    tool_type: str,
    access_tier: AccessTier,
    actual_permissions: List[str],
) -> Tuple[bool, List[str]]:
    """Validate that actual permissions match expected for the access tier.
    
    Args:
        tool_type: Type of tool
        access_tier: Requested access tier
        actual_permissions: Permissions actually granted
    
    Returns:
        Tuple of (passed, warnings)
    """
    expected = set(get_permissions(tool_type, access_tier))
    actual = set(actual_permissions)
    
    warnings: List[str] = []
    
    over_permissions = actual - expected
    if over_permissions:
        warnings.append(
            f"Over-permissioned: {tool_type} has {over_permissions} beyond {access_tier.value} tier"
        )
    
    missing = expected - actual
    if missing:
        warnings.append(
            f"Under-permissioned: {tool_type} missing {missing} for {access_tier.value} tier"
        )
    
    passed = len(over_permissions) == 0
    return passed, warnings


def is_tier_sufficient(
    requested_tier: AccessTier,
    required_tier: AccessTier,
) -> bool:
    """Check if requested tier is sufficient for required tier.
    
    Tier hierarchy: MINIMAL < STANDARD < ELEVATED < FULL
    """
    tier_order = [AccessTier.MINIMAL, AccessTier.STANDARD, AccessTier.ELEVATED, AccessTier.FULL]
    return tier_order.index(requested_tier) >= tier_order.index(required_tier)
