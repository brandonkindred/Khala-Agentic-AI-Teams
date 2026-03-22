"""Standards mapping tools for accessibility audits."""

from .map_wcag import MapWcagInput, MapWcagOutput, map_wcag
from .tag_section508 import TagSection508Input, TagSection508Output, tag_section508

__all__ = [
    "map_wcag",
    "MapWcagInput",
    "MapWcagOutput",
    "tag_section508",
    "TagSection508Input",
    "TagSection508Output",
]
