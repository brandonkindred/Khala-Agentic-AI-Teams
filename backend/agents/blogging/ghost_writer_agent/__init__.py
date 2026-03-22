"""Ghost writer story elicitation agent for the blogging pipeline."""

from .agent import GhostWriterElicitationAgent
from .models import StoryGap, StoryElicitationResult

__all__ = ["GhostWriterElicitationAgent", "StoryGap", "StoryElicitationResult"]
