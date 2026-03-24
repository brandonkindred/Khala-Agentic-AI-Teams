"""
Section 508 Standards definitions and WCAG crosswalk.

Provides structured access to Section 508 requirements and their
mapping to WCAG 2.x success criteria for accessibility auditing.

Section 508 was refreshed in 2017 to align with WCAG 2.0 Level A/AA.
This module provides the crosswalk between 508 requirements and WCAG SC.
"""

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class Section508Category(str, Enum):
    """Section 508 requirement categories."""

    GENERAL = "General"
    WEB = "Web"
    SOFTWARE = "Software"
    HARDWARE = "Hardware"
    SUPPORT_DOCS = "Support Documentation and Services"
    ICT = "Information and Communication Technology"


class ApplicablePlatform(str, Enum):
    """Platforms to which a 508 requirement applies."""

    WEB = "web"
    SOFTWARE = "software"
    MOBILE_APP = "mobile_app"
    DOCUMENT = "document"
    HARDWARE = "hardware"


class Section508Requirement(BaseModel):
    """A single Section 508 requirement with WCAG mapping."""

    section: str = Field(..., description="Section number, e.g., E205.4")
    name: str = Field(..., description="Requirement name")
    category: Section508Category
    description: str = Field(default="", description="Requirement description")
    wcag_mappings: List[str] = Field(
        default_factory=list, description="Mapped WCAG 2.x success criteria"
    )
    platforms: List[ApplicablePlatform] = Field(
        default_factory=list, description="Applicable platforms"
    )
    notes: str = Field(default="", description="Additional compliance notes")


# ---------------------------------------------------------------------------
# Section 508 Requirements Database (2017 Refresh)
# ---------------------------------------------------------------------------

SECTION_508_REQUIREMENTS: Dict[str, Section508Requirement] = {
    # Chapter 2: Scoping Requirements
    "E205.4": Section508Requirement(
        section="E205.4",
        name="Accessibility Standard",
        category=Section508Category.GENERAL,
        description="Electronic content shall conform to Level A and Level AA Success Criteria and Conformance Requirements in WCAG 2.0.",
        wcag_mappings=["all_level_a", "all_level_aa"],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.DOCUMENT,
        ],
    ),
    # Chapter 3: Functional Performance Criteria
    "302.1": Section508Requirement(
        section="302.1",
        name="Without Vision",
        category=Section508Category.GENERAL,
        description="ICT shall provide at least one mode of operation that does not require user vision.",
        wcag_mappings=["1.1.1", "1.3.1", "1.3.2", "1.4.1", "4.1.2"],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
        ],
        notes="Screen reader compatibility is primary method of compliance.",
    ),
    "302.2": Section508Requirement(
        section="302.2",
        name="With Limited Vision",
        category=Section508Category.GENERAL,
        description="ICT shall provide at least one mode of operation that enables users with limited vision to make use of ICT.",
        wcag_mappings=["1.4.3", "1.4.4", "1.4.10", "1.4.11", "1.4.12"],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
        ],
        notes="Includes contrast, resize, zoom, and reflow requirements.",
    ),
    "302.3": Section508Requirement(
        section="302.3",
        name="Without Perception of Color",
        category=Section508Category.GENERAL,
        description="ICT shall provide at least one mode of operation that does not require user perception of color.",
        wcag_mappings=["1.4.1"],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
        ],
    ),
    "302.4": Section508Requirement(
        section="302.4",
        name="Without Hearing",
        category=Section508Category.GENERAL,
        description="ICT shall provide at least one mode of operation that does not require user hearing.",
        wcag_mappings=["1.2.1", "1.2.2", "1.2.3", "1.2.4", "1.2.5"],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
        ],
        notes="Captions and transcripts required for audio content.",
    ),
    "302.5": Section508Requirement(
        section="302.5",
        name="With Limited Hearing",
        category=Section508Category.GENERAL,
        description="ICT shall provide at least one mode of operation that enables users with limited hearing to make use of ICT.",
        wcag_mappings=["1.2.2", "1.2.4", "1.4.2"],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
        ],
    ),
    "302.6": Section508Requirement(
        section="302.6",
        name="Without Speech",
        category=Section508Category.GENERAL,
        description="ICT shall provide at least one mode of operation that does not require user speech.",
        wcag_mappings=[],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
        ],
        notes="Alternative input methods must be available.",
    ),
    "302.7": Section508Requirement(
        section="302.7",
        name="With Limited Manipulation",
        category=Section508Category.GENERAL,
        description="ICT shall provide at least one mode of operation that does not require fine motor control or simultaneous manual operations.",
        wcag_mappings=["2.1.1", "2.1.2", "2.5.1", "2.5.2", "2.5.4"],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
        ],
    ),
    "302.8": Section508Requirement(
        section="302.8",
        name="With Limited Reach and Strength",
        category=Section508Category.GENERAL,
        description="ICT shall provide at least one mode of operation that is operable with limited reach and limited strength.",
        wcag_mappings=["2.5.8"],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
            ApplicablePlatform.HARDWARE,
        ],
    ),
    "302.9": Section508Requirement(
        section="302.9",
        name="With Limited Language, Cognitive, and Learning Abilities",
        category=Section508Category.GENERAL,
        description="ICT shall provide features making content usable by individuals with limited cognitive, language, and learning abilities.",
        wcag_mappings=[
            "3.1.1",
            "3.1.2",
            "3.2.1",
            "3.2.2",
            "3.2.3",
            "3.2.4",
            "3.3.1",
            "3.3.2",
            "3.3.3",
            "3.3.4",
            "3.3.7",
            "3.3.8",
        ],
        platforms=[
            ApplicablePlatform.WEB,
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
        ],
    ),
    # Chapter 4: Hardware
    "402.1": Section508Requirement(
        section="402.1",
        name="General - Speech Output",
        category=Section508Category.HARDWARE,
        description="ICT with a display screen shall provide speech output capability.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.HARDWARE],
    ),
    "402.2": Section508Requirement(
        section="402.2",
        name="Volume",
        category=Section508Category.HARDWARE,
        description="ICT providing audio output shall allow the user to adjust the volume.",
        wcag_mappings=["1.4.2"],
        platforms=[ApplicablePlatform.HARDWARE, ApplicablePlatform.SOFTWARE],
    ),
    # Chapter 5: Software
    "501.1": Section508Requirement(
        section="501.1",
        name="Scope - Software",
        category=Section508Category.SOFTWARE,
        description="The requirements of Chapter 5 shall apply to software.",
        wcag_mappings=["all_level_a", "all_level_aa"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.2.1": Section508Requirement(
        section="502.2.1",
        name="User Control of Accessibility Features",
        category=Section508Category.SOFTWARE,
        description="Platform software shall provide user control over accessibility features.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.SOFTWARE],
    ),
    "502.2.2": Section508Requirement(
        section="502.2.2",
        name="No Disruption of Accessibility Features",
        category=Section508Category.SOFTWARE,
        description="Software shall not disrupt platform accessibility features.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.1": Section508Requirement(
        section="502.3.1",
        name="Object Information",
        category=Section508Category.SOFTWARE,
        description="The object role, states, properties, boundary, name, and description shall be programmatically determinable.",
        wcag_mappings=["4.1.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.2": Section508Requirement(
        section="502.3.2",
        name="Modification of Object Information",
        category=Section508Category.SOFTWARE,
        description="States and properties that can be set by the user shall be capable of being set programmatically.",
        wcag_mappings=["4.1.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.3": Section508Requirement(
        section="502.3.3",
        name="Row, Column, and Headers",
        category=Section508Category.SOFTWARE,
        description="Data tables shall expose row and column headers programmatically.",
        wcag_mappings=["1.3.1"],
        platforms=[
            ApplicablePlatform.SOFTWARE,
            ApplicablePlatform.MOBILE_APP,
            ApplicablePlatform.WEB,
        ],
    ),
    "502.3.4": Section508Requirement(
        section="502.3.4",
        name="Values",
        category=Section508Category.SOFTWARE,
        description="Any current values and any allowable ranges shall be programmatically determinable.",
        wcag_mappings=["4.1.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.5": Section508Requirement(
        section="502.3.5",
        name="Modification of Values",
        category=Section508Category.SOFTWARE,
        description="Values that can be set by the user shall be capable of being set programmatically.",
        wcag_mappings=["4.1.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.6": Section508Requirement(
        section="502.3.6",
        name="Label Relationships",
        category=Section508Category.SOFTWARE,
        description="Any relationship between a UI component and any labels shall be programmatically exposed.",
        wcag_mappings=["1.3.1", "3.3.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.7": Section508Requirement(
        section="502.3.7",
        name="Hierarchical Relationships",
        category=Section508Category.SOFTWARE,
        description="Any hierarchical relationship between UI components shall be exposed.",
        wcag_mappings=["1.3.1"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.8": Section508Requirement(
        section="502.3.8",
        name="Text",
        category=Section508Category.SOFTWARE,
        description="The content of text objects and text attributes shall be programmatically determinable.",
        wcag_mappings=["1.3.1", "4.1.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.9": Section508Requirement(
        section="502.3.9",
        name="Modification of Text",
        category=Section508Category.SOFTWARE,
        description="Text that can be set by the user shall be capable of being set programmatically.",
        wcag_mappings=["4.1.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.10": Section508Requirement(
        section="502.3.10",
        name="List of Actions",
        category=Section508Category.SOFTWARE,
        description="A list of all actions that can be executed shall be programmatically determinable.",
        wcag_mappings=["4.1.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.11": Section508Requirement(
        section="502.3.11",
        name="Actions on Objects",
        category=Section508Category.SOFTWARE,
        description="Applications shall allow assistive technology to programmatically execute available actions.",
        wcag_mappings=["4.1.2"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.12": Section508Requirement(
        section="502.3.12",
        name="Focus Cursor",
        category=Section508Category.SOFTWARE,
        description="Applications shall expose information and mechanisms for tracking focus and text insertion point.",
        wcag_mappings=["2.4.7"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.13": Section508Requirement(
        section="502.3.13",
        name="Modification of Focus Cursor",
        category=Section508Category.SOFTWARE,
        description="Focus and text insertion point shall be capable of being set programmatically.",
        wcag_mappings=["2.4.3"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.3.14": Section508Requirement(
        section="502.3.14",
        name="Event Notification",
        category=Section508Category.SOFTWARE,
        description="Notification of events relevant to user interactions shall be available to assistive technology.",
        wcag_mappings=["4.1.3"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "502.4": Section508Requirement(
        section="502.4",
        name="Platform Accessibility Features",
        category=Section508Category.SOFTWARE,
        description="Software shall conform to platform accessibility services documented in platform documentation.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "503.2": Section508Requirement(
        section="503.2",
        name="User Preferences",
        category=Section508Category.SOFTWARE,
        description="Applications shall permit user preferences from platform settings for color, contrast, font type, size, and focus cursor.",
        wcag_mappings=["1.4.3", "1.4.4", "1.4.11", "2.4.7"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "503.3": Section508Requirement(
        section="503.3",
        name="Alternative User Interfaces",
        category=Section508Category.SOFTWARE,
        description="Where an application provides an alternative user interface for assistive technology, it shall use platform accessibility services.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "503.4": Section508Requirement(
        section="503.4",
        name="User Controls for Captions and Audio Description",
        category=Section508Category.SOFTWARE,
        description="User controls for captions and audio descriptions shall be provided at the same menu level as volume or program selection.",
        wcag_mappings=["1.2.2", "1.2.5"],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.MOBILE_APP],
    ),
    "504.2": Section508Requirement(
        section="504.2",
        name="Content Creation or Editing",
        category=Section508Category.SOFTWARE,
        description="Authoring tools shall provide means to create web content that conforms to Level A and AA WCAG success criteria.",
        wcag_mappings=["all_level_a", "all_level_aa"],
        platforms=[ApplicablePlatform.SOFTWARE],
    ),
    "504.2.1": Section508Requirement(
        section="504.2.1",
        name="Preservation of Information Provided for Accessibility",
        category=Section508Category.SOFTWARE,
        description="Authoring tools shall preserve accessibility information provided for WCAG conformance.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.SOFTWARE],
    ),
    "504.2.2": Section508Requirement(
        section="504.2.2",
        name="PDF Export",
        category=Section508Category.SOFTWARE,
        description="Authoring tools capable of exporting PDF shall export PDF that conforms to PDF/UA-1.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.SOFTWARE, ApplicablePlatform.DOCUMENT],
    ),
    "504.3": Section508Requirement(
        section="504.3",
        name="Prompts",
        category=Section508Category.SOFTWARE,
        description="Authoring tools shall prompt authors to create web content that conforms to WCAG Level A and Level AA.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.SOFTWARE],
    ),
    "504.4": Section508Requirement(
        section="504.4",
        name="Templates",
        category=Section508Category.SOFTWARE,
        description="Authoring tools shall provide templates conforming to Level A and Level AA.",
        wcag_mappings=["all_level_a", "all_level_aa"],
        platforms=[ApplicablePlatform.SOFTWARE],
    ),
    # Chapter 6: Support Documentation and Services
    "602.2": Section508Requirement(
        section="602.2",
        name="Accessibility and Compatibility Features",
        category=Section508Category.SUPPORT_DOCS,
        description="Documentation shall list and describe accessibility and compatibility features.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.DOCUMENT],
    ),
    "602.3": Section508Requirement(
        section="602.3",
        name="Documentation Accessibility",
        category=Section508Category.SUPPORT_DOCS,
        description="Documentation shall conform to Level A and Level AA WCAG Success Criteria.",
        wcag_mappings=["all_level_a", "all_level_aa"],
        platforms=[ApplicablePlatform.DOCUMENT, ApplicablePlatform.WEB],
    ),
    "602.4": Section508Requirement(
        section="602.4",
        name="Alternate Formats for Non-Electronic Support Documentation",
        category=Section508Category.SUPPORT_DOCS,
        description="Support documentation in alternate formats shall address accessibility and compatibility features.",
        wcag_mappings=[],
        platforms=[ApplicablePlatform.DOCUMENT],
    ),
}


# ---------------------------------------------------------------------------
# WCAG to Section 508 Reverse Mapping
# ---------------------------------------------------------------------------


def get_508_requirements_for_wcag(wcag_sc: str) -> List[Section508Requirement]:
    """Get all Section 508 requirements that map to a given WCAG SC."""
    return [req for req in SECTION_508_REQUIREMENTS.values() if wcag_sc in req.wcag_mappings]


def get_508_tags_for_wcag_list(wcag_scs: List[str]) -> List[str]:
    """
    Get Section 508 tags for a list of WCAG success criteria.

    Returns a deduplicated list of 508 section numbers.
    """
    tags = set()
    for sc in wcag_scs:
        for req in SECTION_508_REQUIREMENTS.values():
            if sc in req.wcag_mappings:
                tags.add(req.section)
    return sorted(list(tags))


def get_requirement(section: str) -> Optional[Section508Requirement]:
    """Get a Section 508 requirement by its section number."""
    return SECTION_508_REQUIREMENTS.get(section)


def get_requirements_by_category(
    category: Section508Category,
) -> List[Section508Requirement]:
    """Get all requirements for a given category."""
    return [req for req in SECTION_508_REQUIREMENTS.values() if req.category == category]


def get_requirements_by_platform(
    platform: ApplicablePlatform,
) -> List[Section508Requirement]:
    """Get all requirements applicable to a given platform."""
    return [req for req in SECTION_508_REQUIREMENTS.values() if platform in req.platforms]


def get_web_requirements() -> List[Section508Requirement]:
    """Get all requirements applicable to web content."""
    return get_requirements_by_platform(ApplicablePlatform.WEB)


def get_mobile_requirements() -> List[Section508Requirement]:
    """Get all requirements applicable to mobile apps."""
    return get_requirements_by_platform(ApplicablePlatform.MOBILE_APP)


def get_software_requirements() -> List[Section508Requirement]:
    """Get all requirements applicable to software."""
    return get_requirements_by_platform(ApplicablePlatform.SOFTWARE)


def get_all_section_numbers() -> List[str]:
    """Get all Section 508 requirement section numbers."""
    return list(SECTION_508_REQUIREMENTS.keys())
