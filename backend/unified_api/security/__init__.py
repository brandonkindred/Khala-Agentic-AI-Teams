"""
Security agent for the API gateway.

Exposes scan() and ScanResult for request validation before forwarding to team APIs.
"""

from unified_api.security.agent import ScanResult, scan

__all__ = ["scan", "ScanResult"]
