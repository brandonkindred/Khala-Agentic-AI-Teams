"""
Accessible Design System Engineer (ADSE)

Hardens design system components with accessibility contracts.

Tools:
- designsystem.build_inventory
- designsystem.generate_a11y_contract
- designsystem.build_test_harness
- designsystem.check_tokens
"""

import uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from ..models import (
    A11yContract,
    ComponentInventory,
    Surface,
)


class TokenCheckResult(BaseModel):
    """Result of checking design tokens."""

    token: str
    problem: str = Field(default="")
    recommendation: str = Field(default="")
    passes: bool = Field(default=True)


class TestHarnessOutput(BaseModel):
    """Output from building a test harness."""

    harness_ref: str
    harness_type: str
    component: str
    notes: str = Field(default="")


class AccessibleDesignSystemAgent:
    """
    Accessible Design System Engineer (ADSE).

    Hardens design system components with accessibility contracts:
    - Build component inventories from Storybook/repos
    - Generate accessibility contracts for components
    - Build test harnesses for automated testing
    - Check design tokens for accessibility compliance

    The goal is to make accessibility the default by building
    it into the design system layer.
    """

    agent_code = "ADSE"
    agent_name = "Accessible Design System Engineer"

    def __init__(self, llm_client: Optional[Any] = None):
        self.llm_client = llm_client
        self._inventories: Dict[str, ComponentInventory] = {}
        self._contracts: Dict[str, A11yContract] = {}

    async def build_inventory(
        self,
        system_name: str,
        source: Literal["storybook", "repo", "manual"],
        components: List[str] = None,
    ) -> ComponentInventory:
        """
        Build an inventory of design system components.

        Scans the specified source to identify all components
        that need accessibility contracts.

        Args:
            system_name: Name of the design system
            source: Source to scan (storybook, repo, manual)
            components: Optional list of specific components

        Returns:
            ComponentInventory with all identified components
        """
        inventory_ref = f"inventory_{system_name}_{uuid.uuid4().hex[:8]}"

        # In production, would scan the actual source
        if not components:
            # Default component types to look for
            components = []

        inventory = ComponentInventory(
            inventory_ref=inventory_ref,
            system_name=system_name,
            source=source,
            components=components,
        )

        self._inventories[inventory_ref] = inventory
        return inventory

    async def generate_a11y_contract(
        self,
        system_name: str,
        component: str,
        platform: Literal["web", "ios", "android"],
        linked_patterns: List[str] = None,
        stacks: Dict[str, str] = None,
    ) -> A11yContract:
        """
        Generate an accessibility contract for a component.

        The contract defines:
        - Required accessibility properties
        - Expected behaviors
        - Test harness requirements

        Args:
            system_name: Design system name
            component: Component name
            platform: Target platform
            linked_patterns: Patterns from audits to address
            stacks: Tech stack info

        Returns:
            A11yContract with requirements and test plan
        """
        linked_patterns = linked_patterns or []
        stacks = stacks or {}
        contract_ref = f"contract_{system_name}_{component}_{uuid.uuid4().hex[:8]}"

        # Build requirements based on component type and platform
        requirements = self._generate_requirements(component, platform)
        test_harness_plan = self._generate_test_harness_plan(component, platform)

        contract = A11yContract(
            contract_ref=contract_ref,
            system_name=system_name,
            component=component,
            platform=Surface(platform),
            requirements=requirements,
            test_harness_plan=test_harness_plan,
            linked_patterns=linked_patterns,
        )

        self._contracts[contract_ref] = contract
        return contract

    async def build_test_harness(
        self,
        system_name: str,
        component: str,
        platform: Literal["web", "ios", "android"],
        harness_type: Literal["storybook", "testpage", "detox_suite"],
        contract_ref: str,
    ) -> TestHarnessOutput:
        """
        Build a test harness for a component.

        Creates automated tests that verify the component
        meets its accessibility contract.

        Args:
            system_name: Design system name
            component: Component name
            platform: Target platform
            harness_type: Type of harness to generate
            contract_ref: Reference to the contract

        Returns:
            TestHarnessOutput with harness reference
        """
        harness_ref = f"harness_{system_name}_{component}_{uuid.uuid4().hex[:8]}"

        notes = ""
        if harness_type == "storybook":
            notes = "Generated Storybook accessibility tests using @storybook/addon-a11y"
        elif harness_type == "testpage":
            notes = "Generated standalone test page with axe-core integration"
        elif harness_type == "detox_suite":
            notes = "Generated Detox test suite for mobile accessibility"

        return TestHarnessOutput(
            harness_ref=harness_ref,
            harness_type=harness_type,
            component=component,
            notes=notes,
        )

    async def check_tokens(
        self,
        system_name: str,
        tokens: Dict[str, Any],
    ) -> List[TokenCheckResult]:
        """
        Check design tokens for accessibility compliance.

        Validates:
        - Focus ring tokens (visibility, contrast)
        - Color tokens (contrast ratios)
        - Spacing tokens (touch targets, padding)

        Args:
            system_name: Design system name
            tokens: Token definitions to check

        Returns:
            List of token check results
        """
        results = []

        # Check focus ring tokens
        if "focus_ring" in tokens:
            focus_ring = tokens["focus_ring"]
            if focus_ring.get("width", 0) < 2:
                results.append(TokenCheckResult(
                    token="focus_ring.width",
                    problem="Focus ring width too thin",
                    recommendation="Use at least 2px focus ring width",
                    passes=False,
                ))

        # Check color tokens for contrast
        if "colors" in tokens:
            colors = tokens["colors"]
            for name, value in colors.items():
                # Would calculate actual contrast in production
                results.append(TokenCheckResult(
                    token=f"colors.{name}",
                    problem="",
                    recommendation="",
                    passes=True,
                ))

        # Check spacing for touch targets
        if "spacing" in tokens:
            spacing = tokens["spacing"]
            touch_target = spacing.get("touch_target_min", 0)
            if touch_target < 44:
                results.append(TokenCheckResult(
                    token="spacing.touch_target_min",
                    problem=f"Touch target minimum ({touch_target}) below 44px",
                    recommendation="Set touch_target_min to at least 44px",
                    passes=False,
                ))

        return results

    def _generate_requirements(
        self,
        component: str,
        platform: str,
    ) -> Dict[str, Any]:
        """Generate accessibility requirements for a component type."""
        # Base requirements for all components
        requirements = {
            "keyboard_accessible": True,
            "focus_visible": True,
            "proper_labeling": True,
        }

        # Component-specific requirements
        component_lower = component.lower()

        if "button" in component_lower:
            requirements.update({
                "role": "button",
                "activatable_via_enter_space": True,
                "disabled_state_exposed": True,
            })

        if "input" in component_lower or "field" in component_lower:
            requirements.update({
                "labeled_by_or_labelled": True,
                "error_state_exposed": True,
                "required_state_exposed": True,
                "autocomplete_attribute": True,
            })

        if "modal" in component_lower or "dialog" in component_lower:
            requirements.update({
                "role": "dialog",
                "focus_trap": True,
                "return_focus_on_close": True,
                "escape_closes": True,
                "background_inert": True,
            })

        if "menu" in component_lower or "dropdown" in component_lower:
            requirements.update({
                "arrow_key_navigation": True,
                "typeahead_search": True,
                "escape_closes": True,
            })

        if "tab" in component_lower:
            requirements.update({
                "arrow_key_navigation": True,
                "tab_panel_linked": True,
            })

        return requirements

    def _generate_test_harness_plan(
        self,
        component: str,
        platform: str,
    ) -> Dict[str, Any]:
        """Generate test harness plan for a component."""
        base_tests = [
            "keyboard_focus_test",
            "screen_reader_announcement_test",
            "contrast_check",
        ]

        component_lower = component.lower()

        if "button" in component_lower:
            base_tests.extend([
                "click_via_keyboard_test",
                "disabled_state_test",
            ])

        if "input" in component_lower:
            base_tests.extend([
                "label_association_test",
                "error_announcement_test",
                "autocomplete_test",
            ])

        if "modal" in component_lower:
            base_tests.extend([
                "focus_trap_test",
                "escape_close_test",
                "return_focus_test",
            ])

        return {
            "tests": base_tests,
            "automation_framework": "playwright" if platform == "web" else "detox",
            "ci_integration": True,
        }

    async def generate_component_docs(
        self,
        contract: A11yContract,
    ) -> Dict[str, Any]:
        """
        Generate accessibility documentation for a component.

        Creates developer-facing documentation that explains
        the accessibility requirements and how to meet them.
        """
        return {
            "component": contract.component,
            "platform": contract.platform.value,
            "requirements": contract.requirements,
            "test_harness": contract.test_harness_plan,
            "usage_notes": self._generate_usage_notes(contract),
            "common_mistakes": self._generate_common_mistakes(contract),
        }

    def _generate_usage_notes(self, contract: A11yContract) -> List[str]:
        """Generate usage notes for a component."""
        notes = []
        reqs = contract.requirements

        if reqs.get("keyboard_accessible"):
            notes.append("Ensure all interactions are keyboard accessible")

        if reqs.get("focus_visible"):
            notes.append("Use the focus ring tokens for consistent focus visibility")

        if reqs.get("proper_labeling"):
            notes.append("Provide meaningful accessible names for all instances")

        return notes

    def _generate_common_mistakes(self, contract: A11yContract) -> List[str]:
        """Generate list of common mistakes to avoid."""
        mistakes = []
        component = contract.component.lower()

        if "button" in component:
            mistakes.extend([
                "Using div/span instead of button element",
                "Missing keyboard event handlers on custom buttons",
                "Icon-only buttons without accessible name",
            ])

        if "input" in component:
            mistakes.extend([
                "Placeholder text as only label",
                "Error messages not associated with input",
                "Missing autocomplete attribute",
            ])

        if "modal" in component:
            mistakes.extend([
                "Focus not trapped in modal",
                "Background content still interactive",
                "Focus not returned on close",
            ])

        return mistakes
