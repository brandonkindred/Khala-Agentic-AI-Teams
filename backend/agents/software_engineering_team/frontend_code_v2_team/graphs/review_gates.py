"""Frontend Code V2 review gates graph.

Same topology as backend review gates — code_review → qa → security →
documentation — but with frontend-specific system prompts.
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_review_gates_graph(*, max_fix_retries: int = 3) -> Graph:
    """Build the frontend review gates sequential graph."""
    return build_sequential(
        stages=[
            ("code_review", build_agent(
                name="frontend_code_review_gate",
                system_prompt=(
                    "You are a senior frontend code reviewer. Review the implementation for:\n"
                    "1. Component architecture and reusability\n"
                    "2. State management patterns\n"
                    "3. Accessibility (WCAG compliance)\n"
                    "4. Performance (bundle size, render efficiency)\n"
                    "5. Framework best practices (Angular/React/Vue)\n\n"
                    "Return JSON with: approved (bool), issues array, summary."
                ),
                agent_key="coding_team",
                description="Reviews frontend code quality",
            )),
            ("qa_testing", build_agent(
                name="frontend_qa_gate",
                system_prompt=(
                    "You are a frontend QA engineer. Verify:\n"
                    "1. Component unit tests with proper assertions\n"
                    "2. Integration tests for user flows\n"
                    "3. Responsive design across breakpoints\n"
                    "4. Cross-browser compatibility considerations\n\n"
                    "Return JSON with: approved (bool), bugs_found array, tests, summary."
                ),
                agent_key="coding_team",
                description="Runs frontend QA testing",
            )),
            ("security_testing", build_agent(
                name="frontend_security_gate",
                system_prompt=(
                    "You are a frontend security specialist. Check for:\n"
                    "1. XSS vulnerabilities (DOM and reflected)\n"
                    "2. CSRF protection\n"
                    "3. Sensitive data in client-side code\n"
                    "4. Proper auth token handling\n"
                    "5. Content Security Policy compliance\n\n"
                    "Return JSON with: vulnerabilities array, summary."
                ),
                agent_key="coding_team",
                description="Frontend security review",
            )),
            ("documentation", build_agent(
                name="frontend_documentation_gate",
                system_prompt=(
                    "You are a frontend documentation specialist. Update:\n"
                    "1. Component documentation and usage examples\n"
                    "2. Storybook stories if applicable\n"
                    "3. README for UI features\n"
                    "4. Style guide updates\n\n"
                    "Return JSON with: readme_content, readme_changed (bool), summary."
                ),
                agent_key="coding_team",
                description="Updates frontend documentation",
            )),
        ],
        graph_id="frontend_review_gates",
        execution_timeout=600.0,
        node_timeout=180.0,
    )
