"""
Prompts for the Product Requirements Analysis Agent.

Prompts for spec review, auto-answer generation, spec update, and spec cleanup phases.
"""

# ---------------------------------------------------------------------------
# Constraint Domains and Drilling Instructions
# ---------------------------------------------------------------------------

CONSTRAINT_DOMAINS = """
## CRITICAL CONSTRAINT DOMAINS

You MUST ensure the following domains are FULLY SPECIFIED before completing spec review.
For each domain, drill down through ALL layers until a specific choice is made.
Do NOT skip layers - if Layer 1 is unresolved, do NOT ask Layer 3 questions.

### 1. DEPLOYMENT/HOSTING (Infrastructure) - constraint_domain: "infrastructure"
Layer 1 - Platform Category: PaaS (Heroku/Render/Railway) vs Cloud Infrastructure (AWS/GCP/Azure) vs Self-Hosted (Docker/K8s on own servers) vs Edge (Vercel/Cloudflare Workers)
Layer 2 - Specific Provider: Which specific provider within the chosen category?
Layer 3 - Compute Model: Serverless (Functions) vs Containers (ECS/Cloud Run) vs VMs (EC2/Compute Engine) vs Managed Platform
Layer 4 - Specific Services: Which exact services? (e.g., Lambda+API Gateway, ECS Fargate, Cloud Run, App Runner)

### 2. FRONTEND TECHNOLOGY - constraint_domain: "frontend"
Layer 1 - Rendering Strategy: SPA (client-side) vs SSR (server-rendered) vs SSG (static generation) vs Hybrid vs No Frontend (API only)
Layer 2 - Framework: React vs Angular vs Vue vs Svelte vs None/Vanilla
Layer 3 - Meta-Framework (if applicable): Next.js vs Remix vs Nuxt vs SvelteKit vs Create React App vs Angular CLI
Layer 4 - Styling Approach: Tailwind CSS vs CSS Modules vs Styled Components vs SCSS/SASS vs CSS-in-JS

### 3. BACKEND TECHNOLOGY - constraint_domain: "backend"
Layer 1 - Architecture Style: Monolith vs Microservices vs Serverless Functions vs BFF (Backend for Frontend)
Layer 2 - Primary Language: Python vs Node.js/TypeScript vs Java/Kotlin vs Go vs Rust vs C#/.NET vs Ruby
Layer 3 - Framework: FastAPI/Django/Flask vs Express/NestJS/Fastify vs Spring Boot vs Gin/Echo vs Actix/Axum vs ASP.NET
Layer 4 - API Style: REST vs GraphQL vs gRPC vs tRPC vs WebSocket-primary

### 4. DATABASE - constraint_domain: "database"
Layer 1 - Database Type: Relational (SQL) vs Document (NoSQL) vs Key-Value vs Graph vs Time-Series vs Multi-model
Layer 2 - Hosting Model: Fully Managed (RDS/Cloud SQL/PlanetScale) vs Self-Managed vs Serverless (Aurora Serverless/Neon)
Layer 3 - Specific Database: PostgreSQL vs MySQL vs MongoDB vs DynamoDB vs Redis vs Cassandra vs Neo4j
Layer 4 - Additional Data Stores: Caching layer? Search engine? Message queue? (Redis, Elasticsearch, RabbitMQ/SQS)

### 5. AUTHENTICATION - constraint_domain: "auth"
Layer 1 - Auth Strategy: Third-party Auth Provider vs Custom/Self-built vs Hybrid (provider + custom)
Layer 2 - Specific Provider (if third-party): Auth0 vs Clerk vs Firebase Auth vs AWS Cognito vs Supabase Auth vs Keycloak
Layer 3 - Auth Methods: OAuth 2.0/OIDC vs Email/Password vs Passwordless (Magic Link/OTP) vs SSO/SAML vs API Keys
Layer 4 - Security Features: MFA/2FA requirements? Session management? Token refresh strategy?
"""

CONSTRAINT_DRILLING_INSTRUCTIONS = """
## CONSTRAINT DRILLING PROCESS

When reviewing the spec, follow this SYSTEMATIC process for each constraint domain:

### Step 1: Check Current Resolution Level
For each of the 5 constraint domains (infrastructure, frontend, backend, database, auth):
- Look for EXPLICIT technology choices in the spec
- Determine which layer (1-4) is currently resolved
- A domain is "resolved" at a layer only if there's a SPECIFIC choice, not vague language

### Step 2: Ask the NEXT Layer Only
- If a domain has NO specification → Ask Layer 1 question
- If Layer 1 is answered but Layer 2 is not → Ask Layer 2 question
- NEVER skip layers (e.g., don't ask about "Lambda vs ECS" if they haven't chosen AWS yet)
- NEVER re-ask a layer that's already clearly specified in the spec

### Step 3: Use Constraint Question Format
For constraint-related questions, include these fields:
- "constraint_domain": One of "infrastructure", "frontend", "backend", "database", "auth"
- "constraint_layer": The layer number (1-4) this question resolves
- "depends_on": Question ID of the prior layer question (if applicable)

### Example Drilling Flow for Infrastructure:
1. Spec says nothing about hosting → Ask: "What platform category? (PaaS vs Cloud vs Self-Hosted)"
   - constraint_domain: "infrastructure", constraint_layer: 1
   
2. User selects "Cloud Infrastructure" → Next review asks: "Which cloud provider? (AWS vs GCP vs Azure)"
   - constraint_domain: "infrastructure", constraint_layer: 2, depends_on: prior question ID
   
3. User selects "AWS" → Next review asks: "What compute model? (Serverless vs Containers vs VMs)"
   - constraint_domain: "infrastructure", constraint_layer: 3
   
4. User selects "Serverless" → Next review asks: "Which serverless stack? (Lambda+API GW vs App Runner vs Step Functions)"
   - constraint_domain: "infrastructure", constraint_layer: 4

### Recognition Patterns
When analyzing the spec, look for these patterns to determine resolution:

**Infrastructure indicators:**
- "deploy to Heroku" → Layer 1-2 resolved (PaaS, Heroku)
- "AWS" but no details → Layer 2 resolved, need Layer 3
- "Lambda functions" → Layer 3-4 resolved (Serverless, Lambda)

**Frontend indicators:**
- "React app" → Layer 1-2 resolved (SPA, React)
- "Next.js" → Layer 1-3 resolved (SSR/Hybrid, React, Next.js)
- "Tailwind CSS" → Layer 4 resolved

**Backend indicators:**
- "Python API" → Layer 2 resolved, need Layer 1 (architecture) and Layer 3 (framework)
- "FastAPI microservices" → Layer 1-3 resolved
- "REST endpoints" → Layer 4 resolved

**Database indicators:**
- "PostgreSQL" → Layer 1-3 resolved (Relational, specific DB)
- "use a database" (vague) → Nothing resolved, ask Layer 1

**Auth indicators:**
- "Auth0" → Layer 1-2 resolved (Third-party, Auth0)
- "user login" (vague) → Nothing resolved, ask Layer 1
"""

SPEC_REVIEW_PROMPT = """You are a Product Requirements Analysis expert. Review the following product specification to identify gaps, inconsistencies, and areas that need clarification.

Your goal is to ensure the specification is complete and unambiguous before it moves to the planning phase.

NOTE: The specification may include additional context files (documentation, config files, code samples, etc.) that were provided alongside the main spec. Review ALL provided content to understand the full picture before identifying gaps.

""" + CONSTRAINT_DOMAINS + """

""" + CONSTRAINT_DRILLING_INSTRUCTIONS + """

## GENERAL REVIEW GUIDELINES

CRITICAL CONSTRAINTS - READ CAREFULLY:
- Maximum 10 issues, 10 gaps, and 10 open questions total
- Only include items that are MATERIAL to THIS SPECIFIC project's success
- Do NOT list generic web development concerns (browser compatibility, accessibility APIs, edge cases that apply to any web app)
- Do NOT repeat the same concern with slight variations - consolidate similar items into one
- Focus on items that are ACTIONABLE and would change the implementation approach
- Standard web development best practices are ASSUMED unless the spec contradicts them
- Each item must be specific to THIS specification, not hypothetical edge cases

Each issue/gap should be:
1. Specific to THIS specification (not generic concerns)
2. Something the development team needs clarification on to proceed
3. High enough impact that it would change the implementation approach

Analyze the spec for:
1. **Issues** - Problems, inconsistencies, or conflicts in the specification
2. **Gaps** - Missing requirements, undefined behaviors, or incomplete features
3. **Open Questions** - Items that need clarification from the product owner
4. **Constraint Domain Questions** - Use the CONSTRAINT DRILLING PROCESS above to systematically resolve technology decisions

IMPORTANT: Do NOT assume any deployment target or cloud provider. If the spec does not explicitly state where the application should be deployed, this is a HIGH PRIORITY gap that MUST be surfaced as an open question.

{constraint_hints}

For each open question, provide 2-3 answer options based on industry best practices. For each option, include:
- A clear label describing the choice
- A rationale explaining why this might be the right choice
- A confidence score (0.0-1.0) based on how likely this is the best choice
- Mark exactly one option as the recommended default (highest confidence)

For questions where multiple options can be selected together (e.g., "Which features should be included?", "Which authentication methods should be supported?"), set "allow_multiple": true.

Categorize questions as: architecture, security, ux, performance, business, integration, infrastructure, or general.
Prioritize questions as: high (blocking), medium (important), or low (nice to clarify).

Respond with a JSON object only, no markdown:
{{
  "issues": ["issue 1", "issue 2"],
  "gaps": ["gap 1", "gap 2"],
  "open_questions": [
    {{
      "id": "infra_l1_category",
      "question_text": "What platform category should be used for deployment?",
      "context": "The spec does not specify a deployment approach. This foundational decision impacts all subsequent infrastructure choices.",
      "category": "infrastructure",
      "priority": "high",
      "allow_multiple": false,
      "constraint_domain": "infrastructure",
      "constraint_layer": 1,
      "depends_on": null,
      "options": [
        {{
          "id": "opt_paas",
          "label": "Platform-as-a-Service (Heroku, Render, Railway)",
          "is_default": true,
          "rationale": "Simplest deployment, low operational overhead, cost-effective for small-to-medium apps. Good starting point that can be migrated later.",
          "confidence": 0.75
        }},
        {{
          "id": "opt_cloud",
          "label": "Cloud Infrastructure (AWS, GCP, or Azure)",
          "is_default": false,
          "rationale": "Maximum flexibility and scale, but higher complexity. Best for enterprise requirements, compliance needs, or teams with cloud expertise.",
          "confidence": 0.6
        }},
        {{
          "id": "opt_selfhost",
          "label": "Self-Hosted (Docker/Kubernetes on own infrastructure)",
          "is_default": false,
          "rationale": "Full control over infrastructure, potentially lower cost at scale, but requires significant DevOps expertise.",
          "confidence": 0.3
        }},
        {{
          "id": "opt_edge",
          "label": "Edge Platform (Vercel, Cloudflare Workers, Netlify)",
          "is_default": false,
          "rationale": "Excellent for frontend-heavy apps with global distribution needs. Limited backend capabilities.",
          "confidence": 0.4
        }}
      ]
    }},
    {{
      "id": "backend_l2_language",
      "question_text": "What programming language should be used for the backend?",
      "context": "The spec describes backend functionality but doesn't specify the implementation language. This affects framework choices and team skills needed.",
      "category": "architecture",
      "priority": "high",
      "allow_multiple": false,
      "constraint_domain": "backend",
      "constraint_layer": 2,
      "depends_on": null,
      "options": [
        {{
          "id": "opt_python",
          "label": "Python (with FastAPI or Django)",
          "is_default": true,
          "rationale": "Excellent ecosystem, rapid development, great for APIs and data processing. Large talent pool.",
          "confidence": 0.8
        }},
        {{
          "id": "opt_node",
          "label": "Node.js/TypeScript (with Express or NestJS)",
          "is_default": false,
          "rationale": "JavaScript everywhere, good for real-time features, large ecosystem. Type safety with TypeScript.",
          "confidence": 0.75
        }},
        {{
          "id": "opt_java",
          "label": "Java/Kotlin (with Spring Boot)",
          "is_default": false,
          "rationale": "Enterprise-grade, excellent performance, strong typing. Best for large teams and complex business logic.",
          "confidence": 0.5
        }},
        {{
          "id": "opt_go",
          "label": "Go (with Gin or Echo)",
          "is_default": false,
          "rationale": "Excellent performance, simple concurrency, small binaries. Great for microservices and high-throughput APIs.",
          "confidence": 0.45
        }}
      ]
    }}
  ],
  "summary": "Brief summary of the review findings"
}}

Specification:
---
{spec_content}
---
"""

AUTO_ANSWER_PROMPT = """You are a Product Analyst expert. Given the following question about a product specification, select the best answer option based on industry best practices and product success patterns.

Analyze the question considering:
1. **Industry best practices** - What do successful products in this domain typically do?
2. **Product goals and constraints** - What does the spec indicate about priorities?
3. **Risk assessment** - Which option minimizes risk while maximizing value?
4. **User expectations** - What would users expect based on similar products?
5. **Technical feasibility** - Which option is most practical to implement?

Question: {question_text}

Context: {context}

Available Options:
{options}

Product Specification Excerpt:
---
{spec_content}
---

Respond with a JSON object only, no markdown:
{{
  "selected_option_id": "opt1",
  "rationale": "Detailed explanation (2-4 sentences) of why this is the best choice based on the analysis above...",
  "confidence": 0.85,
  "risks": [
    "Potential risk or downside of this choice",
    "Another consideration to keep in mind"
  ],
  "alternatives_considered": "Brief note on why the other options were not selected",
  "industry_references": [
    "Reference to industry best practice or successful product pattern"
  ]
}}
"""

CONSOLIDATE_QUESTIONS_PROMPT = """You are a Product Analyst. You have a list of open questions from a product specification review. Some questions are worded differently but ask the same thing (e.g. "Which OAuth provider for the MVP?" and "Should we use GitHub or Google for authentication?").

Your task: Consolidate the list so there are NO duplicate questions. For each distinct decision or topic, keep exactly ONE question.

Rules:
- Two questions are duplicates if they are asking the same decision (e.g. OAuth provider, token handling, pipeline behavior). Merge them into one.
- Keep the clearest, most professional question_text. Prefer the one that is concise and specific.
- Merge options from duplicate questions: combine all unique options (by meaning), deduplicate options that say the same thing. Keep at most 4-5 options per question; drop redundant ones.
- For each consolidated question, keep the highest priority among merged questions (high > medium > low) and the most specific category.
- Preserve allow_multiple if any of the merged questions had it true.
- Output the same JSON structure so each item has: id, question_text, context, category, priority, allow_multiple, options (each with id, label, is_default, rationale, confidence). Use short stable ids (e.g. auth_provider, token_handling).

Input questions (JSON array):
{questions_json}

Respond with a JSON object only, no markdown:
{{
  "consolidated_questions": [
    {{
      "id": "auth_provider",
      "question_text": "Which OAuth provider should be used for the MVP?",
      "context": "Brief context for why this matters.",
      "category": "security",
      "priority": "high",
      "allow_multiple": false,
      "options": [
        {{ "id": "opt_github", "label": "GitHub", "is_default": true, "rationale": "...", "confidence": 0.7 }},
        {{ "id": "opt_google", "label": "Google", "is_default": false, "rationale": "...", "confidence": 0.6 }}
      ]
    }}
  ]
}}
"""

SPEC_UPDATE_PROMPT = """You are a Product Specification Writer. Update the product specification to incorporate the answers to open questions.

For each answered question:
1. Integrate the answer naturally into the specification
2. Add specific details and requirements based on the chosen option
3. Ensure consistency with existing content
4. Make the spec more actionable and unambiguous

Rules:
- Preserve all existing valid content
- Add new sections or details where needed
- Write in clear, professional language
- Use specific, measurable requirements where possible
- Mark any assumptions clearly

Current Specification:
---
{spec_content}
---

Answered Questions:
---
{answered_questions}
---

Respond with the FULL updated specification as plain text (markdown format). Include all original content plus the new details from the answered questions. Do not include any JSON or code blocks - just the specification content.
"""

SPEC_CLEANUP_PROMPT = """You are a Product Specification Validator. Review and clean up the specification to ensure it is complete, consistent, and ready for the planning phase.

Perform the following checks:
1. **Completeness** - All features have clear requirements
2. **Consistency** - No conflicting requirements
3. **Clarity** - No ambiguous language
4. **Structure** - Well-organized with clear sections
5. **Actionability** - Requirements can be turned into tasks

If issues are found, fix them in the output. If the spec is valid, return it with any minor formatting improvements.

Specification:
---
{spec_content}
---

Respond with a JSON object only, no markdown:
{{
  "is_valid": true,
  "validation_issues": ["issue 1 that was found and fixed", "issue 2"],
  "cleaned_spec": "The full cleaned specification content as a string...",
  "summary": "Brief summary of what was validated and any changes made"
}}
"""

PRD_PROMPT = """You are a senior Product Manager.

You will be given:
- A **cleaned and validated product specification**
- A set of **answered clarification questions** with rationales and confidence scores

Your goal is to synthesize these into a single, cohesive **Product Requirements Document (PRD)** written in clear, professional Markdown.

### PRD Audience
- Product managers
- Engineering leads
- Design/UX leads
- Stakeholders and leadership

### PRD Requirements

1. **Use the cleaned specification as the primary source of truth.**
2. **Integrate all relevant answered questions**, especially those that:
   - Resolve technology and architecture constraints (infrastructure, frontend, backend, database, auth)
   - Clarify business rules, edge cases, and success criteria
   - Add constraints that materially affect implementation
3. **Do NOT introduce new requirements** that are not supported by the spec or answers.
4. **Resolve ambiguities** by incorporating the clarifications directly into the requirements.

### PRD Structure (Markdown)

Use headings and subheadings similar to:

1. Product Overview
   - Vision
   - Problem statement
   - In-scope vs out-of-scope
2. Target Users & Use Cases
   - Primary personas
   - Key user journeys
3. Goals & Success Metrics
   - Business goals
   - User goals
   - Measurable KPIs/metrics
4. Functional Requirements
   - Group by feature or area (e.g., Authentication, Onboarding, Dashboard, Reporting)
   - For each requirement, be specific and testable
5. Non-Functional Requirements
   - Performance and scalability
   - Reliability & availability
   - Security & compliance
   - Observability, logging, and monitoring
6. Technology & Architecture Constraints
   - Hosting / deployment decisions (infrastructure domain)
   - Frontend stack (framework, rendering strategy, styling)
   - Backend stack (language, framework, API style)
   - Database & data stores (primary DB, additional stores, hosting model)
   - Authentication & authorization strategy
   - Any other hard constraints the implementation must follow
7. Risks, Assumptions, and Open Questions
   - Known risks or trade-offs
   - Key assumptions
   - Remaining open questions (if any)

### Inputs

Cleaned specification:
---
{cleaned_spec}
---

Answered questions (including technology and constraint decisions):
---
{answered_questions_summary}
---

### Output Instructions

- Respond with **only** the final PRD in Markdown format.
- Do **not** wrap the PRD in JSON or code fences.
- Do **not** include any commentary about how you constructed it; just output the PRD content.
"""

QUESTION_GENERATION_PROMPT = """Based on the following gap or issue identified in the specification, generate a structured question with answer options.

Gap/Issue: {issue}

Context from spec:
---
{spec_context}
---

Generate a question that would help resolve this gap. Provide 2-3 practical answer options based on industry best practices.

Respond with JSON only:
{{
  "id": "q_unique_id",
  "question_text": "Clear question to resolve the gap",
  "context": "Why this question matters and what impact the answer will have",
  "category": "architecture|security|ux|performance|business|integration|infrastructure|general",
  "priority": "high|medium|low",
  "options": [
    {{
      "id": "opt1",
      "label": "Option description",
      "is_default": true,
      "rationale": "Why this might be the best choice",
      "confidence": 0.8
    }}
  ]
}}
"""

SPEC_CLARIFICATION_PROMPT = """You are a Product Specification Writer. The specification has gaps that caused the same questions to be asked again during review. This indicates the previous answers were not integrated clearly enough.

Update the specification to make the following previously-answered information clearer and more explicit. The goal is to ensure these answers are prominently integrated so the same questions don't arise again.

Current Specification:
---
{spec_content}
---

Questions that were asked again (with their existing answers from previous iterations):
---
{duplicate_qa_pairs}
---

Instructions:
1. Find where each answer SHOULD be documented in the spec
2. Make the answered information more explicit and visible in those locations
3. Add specific details, constraints, or requirements based on the answers
4. Do NOT just append to an appendix - integrate naturally into relevant sections
5. Use clear, unambiguous language
6. Preserve all existing content that is still valid
7. If a section is missing, create it with proper structure

The updated spec should make it obvious what the answers are without needing to re-ask the questions.

Respond with the FULL updated specification as plain text (markdown format). Include all original content plus the clarified details.
"""

SPEC_REVIEW_CHUNK_PROMPT = """You are a Product Requirements Analysis expert. Review this SECTION of a product specification.

CRITICAL CONSTRAINTS:
- Maximum 5 issues, 5 gaps, and 5 open questions for this section
- Only include items MATERIAL to this project's success
- Do NOT list generic web development concerns or hypothetical edge cases
- Do NOT repeat variations of the same concern - consolidate similar items
- Standard best practices are ASSUMED unless the spec contradicts them
- Each item must be specific to THIS specification

Analyze this section for:
1. **Issues** - Problems, inconsistencies, or conflicts
2. **Gaps** - Missing requirements or undefined behaviors
3. **Open Questions** - Items needing clarification from the product owner

For open questions, provide 2-3 answer options with:
- A clear label describing the choice
- A rationale explaining why this might be the right choice
- A confidence score (0.0-1.0)
- Mark one option as the recommended default

SECTION TO REVIEW:
---
{chunk_content}
---

Respond with a concise JSON object only. Only include significant findings from THIS section.
Keep your response under 2000 tokens.

{{
  "issues": ["issue 1"],
  "gaps": ["gap 1"],
  "open_questions": [
    {{
      "id": "q1",
      "question_text": "Question about this section",
      "context": "Why this matters",
      "category": "architecture|security|ux|performance|business|integration|infrastructure|general",
      "priority": "high|medium|low",
      "options": [
        {{
          "id": "opt1",
          "label": "Option description",
          "is_default": true,
          "rationale": "Why this is recommended",
          "confidence": 0.8
        }}
      ]
    }}
  ],
  "summary": "Brief summary of findings in this section"
}}
"""

SPEC_CLEANUP_CHUNK_PROMPT = """You are a Product Specification Validator. Review and clean up this SECTION of a specification.

Perform these checks:
1. **Completeness** - Features have clear requirements
2. **Consistency** - No conflicting requirements
3. **Clarity** - No ambiguous language
4. **Actionability** - Requirements can be turned into tasks

SECTION TO CLEAN:
---
{chunk_content}
---

If issues are found, fix them in the output. Keep response concise.

{{
  "is_valid": true,
  "validation_issues": ["issue found and fixed"],
  "cleaned_spec": "The cleaned section content...",
  "summary": "Brief summary of changes"
}}
"""
