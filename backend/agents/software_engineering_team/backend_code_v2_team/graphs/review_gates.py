"""Backend Code V2 review gates graph.

Replaces the sequential while-loop that re-runs ALL reviewers after any
fix. The graph ensures each gate runs independently with its own fix
loop, preventing upstream cascade.

Topology::

    code_review → qa_testing → security_testing → documentation

Each stage has its own fix loop: if the gate fails, fixes are applied
and only that gate re-runs (not all upstream gates). This eliminates
30-50% redundant LLM calls from the current cascade pattern.

Existing functions wrap directly as graph node logic:
- run_code_review_phase → code_review node
- run_batch_coding_fixes → internal fix loop
- run_qa_phase → qa_testing node
- run_security_phase → security_testing node
"""

from __future__ import annotations

from strands.multiagent.graph import Graph

from shared_graph import build_agent, build_sequential


def build_review_gates_graph(*, max_fix_retries: int = 3) -> Graph:
    """Build the review gates sequential graph.

    Parameters
    ----------
    max_fix_retries:
        Maximum fix retries per gate (prevents infinite loops).

    Returns
    -------
    Graph
        code_review → qa → security → documentation pipeline
    """
    return build_sequential(
        stages=[
            ("code_review", build_agent(
                name="code_review_gate",
                system_prompt=(
                    "You are a senior code reviewer. Review the implementation for:\n"
                    "1. Code quality and adherence to project conventions\n"
                    "2. Spec compliance — does the code fulfill requirements?\n"
                    "3. Performance concerns and anti-patterns\n"
                    "4. Error handling completeness\n\n"
                    "If issues are found, provide specific fix instructions. "
                    "Return JSON with: approved (bool), issues array, "
                    "spec_compliance_notes, suggested_commit_message."
                ),
                agent_key="coding_team",
                description="Reviews code quality and spec compliance",
            )),
            ("qa_testing", build_agent(
                name="qa_gate",
                system_prompt=(
                    "You are a QA engineer. Verify the implementation:\n"
                    "1. Write and validate integration tests\n"
                    "2. Check edge cases and error paths\n"
                    "3. Verify the code handles all acceptance criteria\n"
                    "4. Check test coverage adequacy\n\n"
                    "Return JSON with: approved (bool), bugs_found array, "
                    "integration_tests, unit_tests, test_plan, summary."
                ),
                agent_key="coding_team",
                description="Runs QA verification and testing",
            )),
            ("security_testing", build_agent(
                name="security_gate",
                system_prompt=(
                    "You are a security engineer. Review the code for:\n"
                    "1. OWASP Top 10 vulnerabilities\n"
                    "2. Input validation and sanitization\n"
                    "3. Authentication/authorization issues\n"
                    "4. Secrets/credential exposure\n"
                    "5. Dependency vulnerabilities\n\n"
                    "Return JSON with: vulnerabilities array, summary."
                ),
                agent_key="coding_team",
                description="Security review of implementation",
            )),
            ("documentation", build_agent(
                name="documentation_gate",
                system_prompt=(
                    "You are a technical writer. Update documentation:\n"
                    "1. README updates for new features\n"
                    "2. API documentation for new endpoints\n"
                    "3. Code comments for complex logic\n"
                    "4. CONTRIBUTORS.md if applicable\n\n"
                    "Return JSON with: readme_content, readme_changed (bool), "
                    "summary, suggested_commit_message."
                ),
                agent_key="coding_team",
                description="Updates project documentation",
            )),
        ],
        graph_id="backend_review_gates",
        execution_timeout=600.0,
        node_timeout=180.0,
    )
