"""
WCAG 2.2 Success Criteria definitions.

Provides structured access to all WCAG 2.2 Level A, AA, and AAA
success criteria for accessibility auditing.
"""

import functools
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class WCAGLevel(str, Enum):
    """WCAG conformance level."""

    A = "A"
    AA = "AA"
    AAA = "AAA"


class WCAGPrinciple(str, Enum):
    """WCAG guiding principles (POUR)."""

    PERCEIVABLE = "Perceivable"
    OPERABLE = "Operable"
    UNDERSTANDABLE = "Understandable"
    ROBUST = "Robust"


class SuccessCriterion(BaseModel):
    """A single WCAG success criterion."""

    sc: str = Field(..., description="Success criterion number, e.g., 1.1.1")
    name: str = Field(..., description="SC name")
    level: WCAGLevel
    principle: WCAGPrinciple
    guideline: str = Field(..., description="Guideline number, e.g., 1.1")
    guideline_name: str
    description: str = Field(default="", description="Brief description of requirement")
    techniques: List[str] = Field(default_factory=list, description="Common sufficient techniques")
    failures: List[str] = Field(default_factory=list, description="Common failure patterns")
    new_in_22: bool = Field(default=False, description="True if new in WCAG 2.2")
    new_in_21: bool = Field(default=False, description="True if new in WCAG 2.1")


# ---------------------------------------------------------------------------
# WCAG 2.2 Success Criteria Database
# ---------------------------------------------------------------------------

WCAG_22_CRITERIA: Dict[str, SuccessCriterion] = {
    # Principle 1: Perceivable
    # Guideline 1.1 Text Alternatives
    "1.1.1": SuccessCriterion(
        sc="1.1.1",
        name="Non-text Content",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.1",
        guideline_name="Text Alternatives",
        description="All non-text content has a text alternative that serves the equivalent purpose.",
        techniques=["G94", "G95", "H37", "H36", "H67", "ARIA6", "ARIA10"],
        failures=["F3", "F13", "F20", "F30", "F38", "F39", "F65", "F67", "F71", "F72"],
    ),
    # Guideline 1.2 Time-based Media
    "1.2.1": SuccessCriterion(
        sc="1.2.1",
        name="Audio-only and Video-only (Prerecorded)",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.2",
        guideline_name="Time-based Media",
        description="Prerecorded audio-only and video-only content has alternatives.",
        techniques=["G158", "G159", "G166"],
        failures=["F30", "F67"],
    ),
    "1.2.2": SuccessCriterion(
        sc="1.2.2",
        name="Captions (Prerecorded)",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.2",
        guideline_name="Time-based Media",
        description="Captions are provided for all prerecorded audio content in synchronized media.",
        techniques=["G87", "G93", "H95"],
        failures=["F8", "F75", "F74"],
    ),
    "1.2.3": SuccessCriterion(
        sc="1.2.3",
        name="Audio Description or Media Alternative (Prerecorded)",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.2",
        guideline_name="Time-based Media",
        description="Audio description or full text alternative for prerecorded video.",
        techniques=["G69", "G58", "G78", "G173", "G8"],
        failures=["F30", "F67"],
    ),
    "1.2.4": SuccessCriterion(
        sc="1.2.4",
        name="Captions (Live)",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.2",
        guideline_name="Time-based Media",
        description="Captions are provided for all live audio content in synchronized media.",
        techniques=["G9", "G93"],
        failures=["F8", "F75"],
    ),
    "1.2.5": SuccessCriterion(
        sc="1.2.5",
        name="Audio Description (Prerecorded)",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.2",
        guideline_name="Time-based Media",
        description="Audio description is provided for all prerecorded video content.",
        techniques=["G78", "G173", "G8"],
        failures=["F30"],
    ),
    # Guideline 1.3 Adaptable
    "1.3.1": SuccessCriterion(
        sc="1.3.1",
        name="Info and Relationships",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.3",
        guideline_name="Adaptable",
        description="Information, structure, and relationships conveyed through presentation can be programmatically determined.",
        techniques=["G115", "G117", "G140", "H42", "H48", "H51", "H63", "H71", "ARIA11", "ARIA12"],
        failures=["F2", "F33", "F34", "F42", "F43", "F46", "F48", "F87", "F90", "F91", "F92"],
    ),
    "1.3.2": SuccessCriterion(
        sc="1.3.2",
        name="Meaningful Sequence",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.3",
        guideline_name="Adaptable",
        description="When the sequence in which content is presented affects its meaning, a correct reading sequence can be programmatically determined.",
        techniques=["G57", "C6", "C8", "C27"],
        failures=["F1", "F32", "F34", "F49"],
    ),
    "1.3.3": SuccessCriterion(
        sc="1.3.3",
        name="Sensory Characteristics",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.3",
        guideline_name="Adaptable",
        description="Instructions do not rely solely on sensory characteristics like shape, size, visual location, orientation, or sound.",
        techniques=["G96"],
        failures=["F14", "F26"],
    ),
    "1.3.4": SuccessCriterion(
        sc="1.3.4",
        name="Orientation",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.3",
        guideline_name="Adaptable",
        description="Content does not restrict its view and operation to a single display orientation unless essential.",
        techniques=["G214"],
        failures=["F97"],
        new_in_21=True,
    ),
    "1.3.5": SuccessCriterion(
        sc="1.3.5",
        name="Identify Input Purpose",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.3",
        guideline_name="Adaptable",
        description="The purpose of input fields collecting user information can be programmatically determined.",
        techniques=["H98"],
        failures=["F107"],
        new_in_21=True,
    ),
    # Guideline 1.4 Distinguishable
    "1.4.1": SuccessCriterion(
        sc="1.4.1",
        name="Use of Color",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="Color is not used as the only visual means of conveying information.",
        techniques=["G14", "G111", "G182", "G183"],
        failures=["F13", "F73", "F81"],
    ),
    "1.4.2": SuccessCriterion(
        sc="1.4.2",
        name="Audio Control",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="A mechanism is available to pause, stop, or control audio volume that plays automatically.",
        techniques=["G60", "G170", "G171"],
        failures=["F23", "F93"],
    ),
    "1.4.3": SuccessCriterion(
        sc="1.4.3",
        name="Contrast (Minimum)",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="Text and images of text have a contrast ratio of at least 4.5:1 (3:1 for large text).",
        techniques=["G18", "G145", "G148", "G174"],
        failures=["F24", "F83"],
    ),
    "1.4.4": SuccessCriterion(
        sc="1.4.4",
        name="Resize Text",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="Text can be resized up to 200% without loss of content or functionality.",
        techniques=["G142", "G146", "G178", "G179", "C12", "C13", "C14"],
        failures=["F69", "F80", "F94"],
    ),
    "1.4.5": SuccessCriterion(
        sc="1.4.5",
        name="Images of Text",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="Text is used to convey information rather than images of text.",
        techniques=["C22", "C30", "G140"],
        failures=["F3"],
    ),
    "1.4.10": SuccessCriterion(
        sc="1.4.10",
        name="Reflow",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="Content can be presented without loss of information or functionality at 320 CSS pixels without horizontal scrolling.",
        techniques=["C31", "C32", "C33", "C38"],
        failures=["F102"],
        new_in_21=True,
    ),
    "1.4.11": SuccessCriterion(
        sc="1.4.11",
        name="Non-text Contrast",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="UI components and graphical objects have a contrast ratio of at least 3:1.",
        techniques=["G195", "G207", "G209"],
        failures=["F78"],
        new_in_21=True,
    ),
    "1.4.12": SuccessCriterion(
        sc="1.4.12",
        name="Text Spacing",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="No loss of content or functionality occurs when text spacing is adjusted.",
        techniques=["C35", "C36"],
        failures=["F104"],
        new_in_21=True,
    ),
    "1.4.13": SuccessCriterion(
        sc="1.4.13",
        name="Content on Hover or Focus",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.PERCEIVABLE,
        guideline="1.4",
        guideline_name="Distinguishable",
        description="Where hover or focus triggers additional content, it is dismissible, hoverable, and persistent.",
        techniques=["SCR39"],
        failures=["F95"],
        new_in_21=True,
    ),
    # Principle 2: Operable
    # Guideline 2.1 Keyboard Accessible
    "2.1.1": SuccessCriterion(
        sc="2.1.1",
        name="Keyboard",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.1",
        guideline_name="Keyboard Accessible",
        description="All functionality is operable through a keyboard interface.",
        techniques=["G202", "H91", "SCR2", "SCR20", "SCR35"],
        failures=["F42", "F54", "F55"],
    ),
    "2.1.2": SuccessCriterion(
        sc="2.1.2",
        name="No Keyboard Trap",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.1",
        guideline_name="Keyboard Accessible",
        description="Keyboard focus can be moved away from any component using only the keyboard.",
        techniques=["G21"],
        failures=["F10"],
    ),
    "2.1.4": SuccessCriterion(
        sc="2.1.4",
        name="Character Key Shortcuts",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.1",
        guideline_name="Keyboard Accessible",
        description="Single character key shortcuts can be turned off or remapped.",
        techniques=["G217"],
        failures=["F99"],
        new_in_21=True,
    ),
    # Guideline 2.2 Enough Time
    "2.2.1": SuccessCriterion(
        sc="2.2.1",
        name="Timing Adjustable",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.2",
        guideline_name="Enough Time",
        description="Users can turn off, adjust, or extend time limits.",
        techniques=["G133", "G180", "G198", "SCR16", "SCR1", "SCR33"],
        failures=["F40", "F41", "F58"],
    ),
    "2.2.2": SuccessCriterion(
        sc="2.2.2",
        name="Pause, Stop, Hide",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.2",
        guideline_name="Enough Time",
        description="Moving, blinking, scrolling, or auto-updating content can be paused, stopped, or hidden.",
        techniques=["G4", "G11", "G152", "G186", "G187", "G191", "SCR22", "SCR33"],
        failures=["F4", "F7", "F16", "F47", "F50"],
    ),
    # Guideline 2.3 Seizures and Physical Reactions
    "2.3.1": SuccessCriterion(
        sc="2.3.1",
        name="Three Flashes or Below Threshold",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.3",
        guideline_name="Seizures and Physical Reactions",
        description="Content does not contain anything that flashes more than three times per second.",
        techniques=["G15", "G19", "G176"],
        failures=["F23"],
    ),
    # Guideline 2.4 Navigable
    "2.4.1": SuccessCriterion(
        sc="2.4.1",
        name="Bypass Blocks",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="A mechanism is available to bypass blocks of content that are repeated.",
        techniques=["G1", "G123", "G124", "H69", "ARIA11"],
        failures=["F87"],
    ),
    "2.4.2": SuccessCriterion(
        sc="2.4.2",
        name="Page Titled",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="Web pages have titles that describe topic or purpose.",
        techniques=["G88", "G127", "H25"],
        failures=["F25"],
    ),
    "2.4.3": SuccessCriterion(
        sc="2.4.3",
        name="Focus Order",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="Components receive focus in an order that preserves meaning and operability.",
        techniques=["G59", "H4", "C27", "SCR26", "SCR27", "SCR37"],
        failures=["F10", "F44", "F85"],
    ),
    "2.4.4": SuccessCriterion(
        sc="2.4.4",
        name="Link Purpose (In Context)",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="The purpose of each link can be determined from the link text alone or together with context.",
        techniques=["G53", "G91", "H24", "H30", "H33", "ARIA7", "ARIA8"],
        failures=["F63", "F89"],
    ),
    "2.4.5": SuccessCriterion(
        sc="2.4.5",
        name="Multiple Ways",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="More than one way is available to locate a Web page within a set of Web pages.",
        techniques=["G63", "G64", "G125", "G126", "G161", "G185"],
        failures=[],
    ),
    "2.4.6": SuccessCriterion(
        sc="2.4.6",
        name="Headings and Labels",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="Headings and labels describe topic or purpose.",
        techniques=["G130", "G131"],
        failures=[],
    ),
    "2.4.7": SuccessCriterion(
        sc="2.4.7",
        name="Focus Visible",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="Any keyboard operable user interface has a mode of operation where the keyboard focus indicator is visible.",
        techniques=["G149", "G165", "G195", "C15", "C40"],
        failures=["F55", "F78"],
    ),
    "2.4.11": SuccessCriterion(
        sc="2.4.11",
        name="Focus Not Obscured (Minimum)",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="When a UI component receives keyboard focus, it is not entirely hidden.",
        techniques=["C43"],
        failures=["F110"],
        new_in_22=True,
    ),
    "2.4.12": SuccessCriterion(
        sc="2.4.12",
        name="Focus Not Obscured (Enhanced)",
        level=WCAGLevel.AAA,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="When a UI component receives keyboard focus, no part of it is hidden.",
        techniques=["C43"],
        failures=["F110"],
        new_in_22=True,
    ),
    "2.4.13": SuccessCriterion(
        sc="2.4.13",
        name="Focus Appearance",
        level=WCAGLevel.AAA,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.4",
        guideline_name="Navigable",
        description="Focus indicator meets minimum size and contrast requirements.",
        techniques=["G195", "C40", "C41"],
        failures=["F78"],
        new_in_22=True,
    ),
    # Guideline 2.5 Input Modalities
    "2.5.1": SuccessCriterion(
        sc="2.5.1",
        name="Pointer Gestures",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.5",
        guideline_name="Input Modalities",
        description="Multipoint or path-based gestures have single-pointer alternatives.",
        techniques=["G215", "G216"],
        failures=["F105"],
        new_in_21=True,
    ),
    "2.5.2": SuccessCriterion(
        sc="2.5.2",
        name="Pointer Cancellation",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.5",
        guideline_name="Input Modalities",
        description="Functions triggered by pointer can be aborted or undone.",
        techniques=["G210", "G211", "G212"],
        failures=["F101"],
        new_in_21=True,
    ),
    "2.5.3": SuccessCriterion(
        sc="2.5.3",
        name="Label in Name",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.5",
        guideline_name="Input Modalities",
        description="For components with visible text labels, the accessible name contains the visible text.",
        techniques=["G208", "G211"],
        failures=["F96"],
        new_in_21=True,
    ),
    "2.5.4": SuccessCriterion(
        sc="2.5.4",
        name="Motion Actuation",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.5",
        guideline_name="Input Modalities",
        description="Functionality triggered by motion can be operated by UI components.",
        techniques=["G213"],
        failures=["F106"],
        new_in_21=True,
    ),
    "2.5.7": SuccessCriterion(
        sc="2.5.7",
        name="Dragging Movements",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.5",
        guideline_name="Input Modalities",
        description="Functionality that uses dragging has single pointer alternatives.",
        techniques=["G219"],
        failures=["F108"],
        new_in_22=True,
    ),
    "2.5.8": SuccessCriterion(
        sc="2.5.8",
        name="Target Size (Minimum)",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.OPERABLE,
        guideline="2.5",
        guideline_name="Input Modalities",
        description="Targets are at least 24x24 CSS pixels or have sufficient spacing.",
        techniques=["C42"],
        failures=["F109"],
        new_in_22=True,
    ),
    # Principle 3: Understandable
    # Guideline 3.1 Readable
    "3.1.1": SuccessCriterion(
        sc="3.1.1",
        name="Language of Page",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.1",
        guideline_name="Readable",
        description="The default human language of each Web page can be programmatically determined.",
        techniques=["H57"],
        failures=["F101"],
    ),
    "3.1.2": SuccessCriterion(
        sc="3.1.2",
        name="Language of Parts",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.1",
        guideline_name="Readable",
        description="The human language of each passage or phrase can be programmatically determined.",
        techniques=["H58"],
        failures=[],
    ),
    # Guideline 3.2 Predictable
    "3.2.1": SuccessCriterion(
        sc="3.2.1",
        name="On Focus",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.2",
        guideline_name="Predictable",
        description="Receiving focus does not initiate a change of context.",
        techniques=["G107"],
        failures=["F52", "F55"],
    ),
    "3.2.2": SuccessCriterion(
        sc="3.2.2",
        name="On Input",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.2",
        guideline_name="Predictable",
        description="Changing a UI component setting does not automatically cause a change of context.",
        techniques=["G80", "G13", "SCR19"],
        failures=["F36", "F37"],
    ),
    "3.2.3": SuccessCriterion(
        sc="3.2.3",
        name="Consistent Navigation",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.2",
        guideline_name="Predictable",
        description="Navigation mechanisms repeated across pages occur in the same relative order.",
        techniques=["G61"],
        failures=["F66"],
    ),
    "3.2.4": SuccessCriterion(
        sc="3.2.4",
        name="Consistent Identification",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.2",
        guideline_name="Predictable",
        description="Components with the same functionality are identified consistently.",
        techniques=["G197"],
        failures=["F31"],
    ),
    "3.2.6": SuccessCriterion(
        sc="3.2.6",
        name="Consistent Help",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.2",
        guideline_name="Predictable",
        description="If help mechanisms exist, they are in a consistent location across pages.",
        techniques=["G220"],
        failures=[],
        new_in_22=True,
    ),
    # Guideline 3.3 Input Assistance
    "3.3.1": SuccessCriterion(
        sc="3.3.1",
        name="Error Identification",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.3",
        guideline_name="Input Assistance",
        description="Input errors are automatically detected and described in text.",
        techniques=["G83", "G84", "G85", "ARIA18", "ARIA19", "ARIA21", "SCR18", "SCR32"],
        failures=["F69"],
    ),
    "3.3.2": SuccessCriterion(
        sc="3.3.2",
        name="Labels or Instructions",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.3",
        guideline_name="Input Assistance",
        description="Labels or instructions are provided when content requires user input.",
        techniques=["G89", "G131", "G162", "G167", "H44", "H71", "ARIA1", "ARIA9"],
        failures=["F82"],
    ),
    "3.3.3": SuccessCriterion(
        sc="3.3.3",
        name="Error Suggestion",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.3",
        guideline_name="Input Assistance",
        description="If an error is detected, suggestions for correction are provided.",
        techniques=["G83", "G84", "G85", "G177", "ARIA18"],
        failures=[],
    ),
    "3.3.4": SuccessCriterion(
        sc="3.3.4",
        name="Error Prevention (Legal, Financial, Data)",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.3",
        guideline_name="Input Assistance",
        description="For pages with legal/financial data, submissions are reversible, checked, or confirmed.",
        techniques=["G98", "G99", "G155", "G164", "G168"],
        failures=[],
    ),
    "3.3.7": SuccessCriterion(
        sc="3.3.7",
        name="Redundant Entry",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.3",
        guideline_name="Input Assistance",
        description="Information previously entered is auto-populated or available for selection.",
        techniques=["G221"],
        failures=[],
        new_in_22=True,
    ),
    "3.3.8": SuccessCriterion(
        sc="3.3.8",
        name="Accessible Authentication (Minimum)",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.3",
        guideline_name="Input Assistance",
        description="Cognitive function tests are not required for authentication unless alternatives exist.",
        techniques=["G218", "H100"],
        failures=["F109"],
        new_in_22=True,
    ),
    "3.3.9": SuccessCriterion(
        sc="3.3.9",
        name="Accessible Authentication (Enhanced)",
        level=WCAGLevel.AAA,
        principle=WCAGPrinciple.UNDERSTANDABLE,
        guideline="3.3",
        guideline_name="Input Assistance",
        description="Cognitive function tests are not required for any authentication step.",
        techniques=["G218", "H100"],
        failures=["F109"],
        new_in_22=True,
    ),
    # Principle 4: Robust
    # Guideline 4.1 Compatible
    "4.1.1": SuccessCriterion(
        sc="4.1.1",
        name="Parsing",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.ROBUST,
        guideline="4.1",
        guideline_name="Compatible",
        description="Elements have complete start and end tags, are nested correctly, and have unique IDs.",
        techniques=["G134", "G192", "H74", "H75", "H88", "H93", "H94"],
        failures=["F17", "F62", "F70", "F77"],
    ),
    "4.1.2": SuccessCriterion(
        sc="4.1.2",
        name="Name, Role, Value",
        level=WCAGLevel.A,
        principle=WCAGPrinciple.ROBUST,
        guideline="4.1",
        guideline_name="Compatible",
        description="For all UI components, name and role can be programmatically determined.",
        techniques=[
            "G10",
            "G108",
            "G135",
            "H64",
            "H65",
            "H88",
            "H91",
            "ARIA4",
            "ARIA5",
            "ARIA14",
            "ARIA16",
        ],
        failures=["F15", "F20", "F59", "F68", "F79", "F86", "F89"],
    ),
    "4.1.3": SuccessCriterion(
        sc="4.1.3",
        name="Status Messages",
        level=WCAGLevel.AA,
        principle=WCAGPrinciple.ROBUST,
        guideline="4.1",
        guideline_name="Compatible",
        description="Status messages can be programmatically determined without receiving focus.",
        techniques=["ARIA19", "ARIA22", "ARIA23", "G199"],
        failures=["F103"],
        new_in_21=True,
    ),
}


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def get_criterion(sc: str) -> Optional[SuccessCriterion]:
    """Get a success criterion by its number."""
    return WCAG_22_CRITERIA.get(sc)


@functools.lru_cache(maxsize=8)
def get_criteria_by_level(level: WCAGLevel) -> List[SuccessCriterion]:
    """Get all success criteria for a given conformance level."""
    return [sc for sc in WCAG_22_CRITERIA.values() if sc.level == level]


@functools.lru_cache(maxsize=8)
def get_criteria_by_principle(principle: WCAGPrinciple) -> List[SuccessCriterion]:
    """Get all success criteria for a given principle."""
    return [sc for sc in WCAG_22_CRITERIA.values() if sc.principle == principle]


@functools.lru_cache(maxsize=1)
def get_level_a_aa_criteria() -> List[SuccessCriterion]:
    """Get all Level A and AA success criteria (typical conformance target)."""
    return [sc for sc in WCAG_22_CRITERIA.values() if sc.level in (WCAGLevel.A, WCAGLevel.AA)]


@functools.lru_cache(maxsize=1)
def get_new_in_22_criteria() -> List[SuccessCriterion]:
    """Get all success criteria new in WCAG 2.2."""
    return [sc for sc in WCAG_22_CRITERIA.values() if sc.new_in_22]


@functools.lru_cache(maxsize=1)
def get_new_in_21_criteria() -> List[SuccessCriterion]:
    """Get all success criteria new in WCAG 2.1."""
    return [sc for sc in WCAG_22_CRITERIA.values() if sc.new_in_21]


def get_all_sc_numbers() -> List[str]:
    """Get all success criterion numbers."""
    return list(WCAG_22_CRITERIA.keys())


def get_guideline_criteria(guideline: str) -> List[SuccessCriterion]:
    """Get all success criteria for a given guideline number (e.g., '2.4')."""
    return [sc for sc in WCAG_22_CRITERIA.values() if sc.guideline == guideline]
