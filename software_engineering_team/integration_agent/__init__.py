"""Integration / API-contract agent: validates full-stack backend-frontend alignment."""

from .agent import IntegrationAgent
from .models import IntegrationInput, IntegrationIssue, IntegrationOutput

__all__ = ["IntegrationAgent", "IntegrationInput", "IntegrationIssue", "IntegrationOutput"]
