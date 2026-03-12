"""
Prompts for the Product Requirements Analysis Agent.

Prompts for spec review, auto-answer generation, spec update, and spec cleanup phases.
"""

# ---------------------------------------------------------------------------
# Context and Constraints Discovery (pre-review)
# ---------------------------------------------------------------------------

CONTEXT_CONSTRAINTS_QUESTIONS_PROMPT = """You are an expert Product Analyst. Before diving into detailed spec review, we need to uncover high-level project context and constraints that shape how the spec should be interpreted.

Generate 1-2 focused questions per category below. These are NOT feature-level details—they are outside constraints and mandates (who is this for, where will it run, what principles must the build follow, what organizational mandates apply).

Categories:
1. **Project context** – Who is this for? (e.g. startup vs enterprise, internal vs external product, regulatory environment) so MVP and requirements are appropriate.
2. **Deployment** – Where will it run? On-prem, cloud, or hybrid; if cloud: which provider (AWS, GCP, Azure, Rackspace, DigitalOcean, Heroku, other) and any constraints.
3. **Tenets** – What principles must the build follow? (e.g. event-driven, API-driven, serverless, agility, ease of change, user behavior analysis, security-first, cost-conscious). Allow multiple or single depending on question design.
4. **Organizational mandates** – Company-wide expectations: SLAs (e.g. 99.9% vs 99.999%), RTO/RPO, compliance (SOC2, HIPAA), or "none / standard." Do NOT ask about org structure, approval chains, or decision hierarchy; we only care about compliance/SLA/tenets that affect the build, not how decisions are made inside an organization.

For each question provide 2-4 options with: id, label, is_default (exactly one true), rationale, confidence (0.0-1.0).
Output must be valid JSON that can be parsed into a list of open questions. Use the same structure as spec review questions: id, question_text, context, category, priority, allow_multiple, options (each with id, label, is_default, rationale, confidence). Include constraint_domain: "" and constraint_layer: 0 for context questions. Use source: "context_discovery".

Specification excerpt (optional context):
---
{spec_excerpt}
---

Respond with a JSON object only, no markdown:
{{
  "open_questions": [
    {{
      "id": "ctx_project_type",
      "question_text": "What type of organization or product context is this?",
      "context": "This shapes MVP scope and governance expectations.",
      "category": "business",
      "priority": "high",
      "allow_multiple": false,
      "constraint_domain": "",
      "constraint_layer": 0,
      "options": [
        {{ "id": "opt_startup", "label": "Startup / early-stage (agility, speed, user behavior focus)", "is_default": true, "rationale": "Common for new products.", "confidence": 0.6 }},
        {{ "id": "opt_enterprise", "label": "Enterprise (governance, consistency, compliance)", "is_default": false, "rationale": "For established orgs.", "confidence": 0.5 }}
      ]
    }},
    {{
      "id": "ctx_deployment",
      "question_text": "Where will this be deployed?",
      "context": "Deployment model affects infrastructure and provider choices.",
      "category": "infrastructure",
      "priority": "high",
      "allow_multiple": false,
      "constraint_domain": "",
      "constraint_layer": 0,
      "options": [
        {{ "id": "opt_cloud", "label": "Cloud (AWS, GCP, Azure, etc.)", "is_default": true, "rationale": "Most common for new apps.", "confidence": 0.7 }},
        {{ "id": "opt_onprem", "label": "On-premises", "is_default": false, "rationale": "For air-gapped or regulated environments.", "confidence": 0.3 }},
        {{ "id": "opt_hybrid", "label": "Hybrid", "is_default": false, "rationale": "Mix of cloud and on-prem.", "confidence": 0.4 }}
      ]
    }}
  ]
}}
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

**SOURCE OF TRUTH:** The specification and any "Previously Answered Questions" below are the source of truth. Do NOT ask open questions about decisions that are already clearly specified in the spec or already answered in the Q&A. Only ask about topics that are genuinely unspecified, ambiguous, or conflicting. If something is already stated or answered, do not ask how it should be done.

NOTE: The specification may include additional context files (documentation, config files, code samples, etc.) that were provided alongside the main spec. Review ALL provided content to understand the full picture before identifying gaps.

**Do not ask organizational/process questions:** Do NOT ask about organizational structure, approval workflows, decision-making process, who has final say, consensus, product manager vs team, or stakeholder sign-off. The client/user is the source of truth: their feedback and direction define what should be done. Implementation is handled by AI agents; there are no human roles or hierarchies to clarify. Focus open questions on product and technical decisions (what to build, how it should behave, technology choices), not on how a company would run or who approves what.

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
      "blocking": true,
      "owner": "user",
      "section_impact": ["Technical Approach"],
      "due_date": "2026-03-06",
      "status": "open",
      "asked_via": ["web_ui"],
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
      "blocking": true,
      "owner": "user",
      "section_impact": ["Technical Approach"],
      "due_date": "2026-03-06",
      "status": "open",
      "asked_via": ["web_ui"],
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

CONSOLIDATE_QUESTIONS_PROMPT = """You are an expert Product Analyst. You have a list of open questions from a product specification review. Some questions are worded differently but ask the same thing (e.g. "Do you want Google only for OAuth?" and "What is the right provider? OAuth or Enterprise?").

Your task: Consolidate the list so there are NO duplicate questions. For each distinct decision or topic, keep exactly ONE question. Produce a single, thorough list with no repeated topics or near-duplicate phrasings.

Rules:
- Two questions are duplicates if they are asking the same decision (e.g. OAuth provider, token handling, pipeline behavior). Merge them into one.
- REPHRASE merged questions: do not just pick the better of two phrasings. Write one new question that gets at the "meat" of the decision so the user answers it once. Example: instead of keeping either "Do you want Google only for OAuth?" or "What is the right provider? OAuth or Enterprise?", ask: "Which authentication approach do you want: single OAuth provider (e.g. Google), multiple OAuth providers, or Enterprise SSO?"
- MERGE AND REWORD OPTIONS: When merging questions, combine all unique options from the merged questions by meaning; deduplicate options that say the same thing. Reword options so each is a distinct, substantive choice that still captures what the original questions were asking. Keep at most 4-5 options per question; drop redundant ones. Option labels must be clear, standalone choices (e.g. "Single OAuth provider (e.g. Google)" not just "Google" if the question is about approach).
- For each consolidated question, keep the highest priority among merged questions (high > medium > low) and the most specific category.
- Preserve allow_multiple if any of the merged questions had it true.
- Output the same JSON structure so each item preserves metadata fields used by orchestration: id, question_text, context, category, priority, allow_multiple, constraint_domain, constraint_layer, depends_on, blocking, owner, section_impact, due_date, status, asked_via, options (each with id, label, is_default, rationale, confidence). Use short stable ids (e.g. auth_provider, token_handling).

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
      "constraint_domain": "auth",
      "constraint_layer": 2,
      "depends_on": null,
      "blocking": true,
      "owner": "user",
      "section_impact": ["Technical Approach", "Security, Privacy, and Compliance"],
      "due_date": "2026-03-06",
      "status": "open",
      "asked_via": ["web_ui"],
      "options": [
        {{ "id": "opt_github", "label": "GitHub", "is_default": true, "rationale": "...", "confidence": 0.7 }},
        {{ "id": "opt_google", "label": "Google", "is_default": false, "rationale": "...", "confidence": 0.6 }}
      ]
    }}
  ]
}}
"""

REVIEW_QUESTIONS_ALIGNMENT_PROMPT = """You are an expert Product Analyst. You have a list of open questions that will be shown to the user. Before presenting them, you must ensure each question and its answer options make sense together.

Rules:
- For each question, classify whether it is OPEN-ENDED (e.g. "What do you think is the right way to do this?", "How should we handle X?") or CLOSED (e.g. "Should we use X?" with a clear yes/no intent).
- OPEN-ENDED questions must NOT have only "Yes" / "No" as options. The options must be substantive statements that answer the question (e.g. "Use OAuth with a single provider", "Use Enterprise SSO", "Support both"). If you see an open-ended question with Yes/No-only options, REWORD the option labels (and optionally rationales) so they are concrete, nuanced statements that match the question when read together. Alternatively, reword the question to be specific enough that the given options are appropriate.
- CLOSED questions (clear yes/no) may keep Yes/No options.
- Preserve all question ids and the same JSON structure. Output the full list of questions with any question_text or option label/rationale changes applied. Do not drop or add questions; only fix alignment.

Input questions (JSON array):
{questions_json}

Respond with a JSON object only, no markdown:
{{
  "aligned_questions": [
    {{
      "id": "auth_provider",
      "question_text": "Which authentication approach do you want?",
      "context": "...",
      "category": "security",
      "priority": "high",
      "allow_multiple": false,
      "constraint_domain": "auth",
      "constraint_layer": 2,
      "depends_on": null,
      "blocking": true,
      "owner": "user",
      "section_impact": ["Technical Approach"],
      "due_date": "2026-03-06",
      "status": "open",
      "asked_via": ["web_ui"],
      "options": [
        {{ "id": "opt_oauth_single", "label": "Single OAuth provider (e.g. Google)", "is_default": true, "rationale": "...", "confidence": 0.7 }},
        {{ "id": "opt_enterprise", "label": "Enterprise SSO", "is_default": false, "rationale": "...", "confidence": 0.5 }}
      ]
    }}
  ]
}}
"""

GENERATE_QUESTION_RECOMMENDATIONS_PROMPT = """You are an expert Product Analyst. For each of the following open questions, produce a short recommendation: which option to choose and why. Consider ALL options and trade-offs before recommending; your recommendation must be well-reasoned and consider alternatives.

For each question:
1. State which option you recommend (by its id or label).
2. Explain why in 2-4 sentences.
3. Briefly note alternatives considered and why they were not chosen.

Specification excerpt (for context):
---
{spec_excerpt}
---

Questions with options:
{questions_json}

Respond with a JSON object only, no markdown:
{{
  "recommendations": [
    {{ "id": "auth_provider", "recommendation": "We recommend Single OAuth provider (opt_oauth_single) because it is simplest to implement and sufficient for most MVPs. Enterprise SSO was considered but adds complexity and is better added later if needed." }},
    {{ "id": "infra_l1_category", "recommendation": "..." }}
  ]
}}
"""

SPEC_UPDATE_PROMPT = """You are an expert Product Specification Writer. Update the product specification to incorporate the answers to open questions.

For each answered question:
1. Integrate the answer naturally into the specification
2. Add specific details and requirements based on the chosen option
3. Ensure consistency with existing content
4. Make the spec more actionable and unambiguous

Rules:
- **The answers are the source of truth.** Where the spec contradicts an answer (e.g. spec says "HTTP-only cookies" but the answer is "stateless JWT"), REPLACE or REMOVE the conflicting statement so the spec reflects only the answer. Do not leave both options in the spec.
- Preserve all existing valid content that does not conflict with the answers
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

SPEC_CLEANUP_PROMPT = """You are an expert Product Specification Validator. Review and clean up the specification to ensure it is complete, consistent, and ready for the planning phase.

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

PRD_PROMPT = """You are an expert PRD Orchestrator for a hub-and-spoke PRD Factory.

You will be given:
- A **cleaned and validated product specification**
- A set of **answered clarification questions** with rationales and confidence scores

Your goal is to synthesize these inputs into a complete, implementation-ready **Product Requirements Document (PRD)** in professional Markdown.

## Core operating rules (non-negotiable)
1. The PRD **cannot be marked Final** if blocking open questions remain.
2. Every **Must** functional requirement must include explicit acceptance criteria.
3. Every requirement and major decision must be traceable to evidence, answers, or approved assumptions.
4. Use deterministic, inspectable structure. Avoid freeform sections outside the required template.
5. If information is missing, keep it as TBD only when linked to a blocking question or approved assumption.

## Required PRD template (use this ordering)
1. Title, Owner, Date, Status (Draft | Review | Final)
2. Executive Summary
3. Problem Statement
4. Goals and Non-Goals
5. Personas and Target Users
6. User Stories and Use Cases
7. Requirements
   - Functional Requirements (FR-###, with priority: must/should/could/wont)
   - Non-Functional Requirements (measurable targets or selectable tiers)
   - Constraints
8. Scope
   - In-scope
   - Out-of-scope
9. UX
   - Key workflows
   - Wireframe notes (text-only)
   - Accessibility considerations
   - Design system notes (components, interaction patterns, states)
   - Branding guidance (voice/tone, visual direction, key brand constraints)
10. Technical Approach (high level)
    - Components
    - Integrations
    - Data flows
    - Architecture overview
11. Data and Analytics
    - Events
    - Dashboards
    - KPIs tied to goals
12. Risks, Assumptions, Dependencies
13. Rollout Plan
    - Milestones
    - Feature flags
    - Migration/backfill
14. Acceptance Criteria
15. Open Questions (id, owner, due date, section impact, blocking/non-blocking, status)
16. Appendix
    - Glossary
    - References to source spec/evidence

## Team-of-agents simulation requirement
Draft content as if specialist spokes contributed to sections below. Ensure these perspectives are reflected:
- Requirements Analyst
- Personas and Use-Case
- Scope and Milestones Planner
- UX and Flows
- API and Integration
- Data and Analytics
- Non-Functional Requirements
- Security, Privacy, and Compliance
- Risks, Assumptions, Dependencies
- QA and Acceptance Criteria
- Editor and Consistency
- PRD Critic (quality gate findings)
- Question Concierge (human question management)

## Quality gates checklist (must be internally validated before output)
- Completeness: all required sections present.
- Consistency: no contradictions between scope, requirements, milestones.
- Testability: FR/NFR are measurable/verifiable.
- Traceability: requirements and decisions link to evidence or answers.
- Pragmatism: rollout is feasible; risks have mitigations and owners.

If a gate would fail, call it out explicitly in Open Questions and Risks/Assumptions/Dependencies.

## Inputs

Cleaned specification:
---
{cleaned_spec}
---

Answered questions (including technology and constraint decisions):
---
{answered_questions_summary}
---

Specialist collaboration recommendations (agents/tooling):
---
{specialist_collaboration_plan}
---

## Output instructions
- Respond with **only** the final PRD in Markdown format.
- Do **not** wrap output in code fences or JSON.
- Keep IDs stable and explicit where applicable (FR-###, Q-###).
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

SPEC_CLARIFICATION_PROMPT = """You are an expert Product Specification Writer. The specification has gaps that caused the same questions to be asked again during review. This indicates the previous answers were not integrated clearly enough.

Update the specification to make the following previously-answered information clearer and more explicit. The goal is to ensure these answers are prominently integrated so the same questions don't arise again.

**The answers below are the source of truth.** Where the spec contradicts an answer (e.g. spec says "HTTP-only cookies" but the answer is "stateless JWT"), REPLACE or REMOVE the conflicting statement so the spec reflects only the answer. Do not leave both options in the spec.

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
3. Replace any conflicting spec content with the answer; the answer wins
4. Add specific details, constraints, or requirements based on the answers
5. Do NOT just append to an appendix - integrate naturally into relevant sections
6. Use clear, unambiguous language
7. Preserve all existing content that does not conflict with the answers
8. If a section is missing, create it with proper structure

The updated spec should make it obvious what the answers are without needing to re-ask the questions.

Respond with the FULL updated specification as plain text (markdown format). Include all original content plus the clarified details.
"""

SPEC_CONSISTENCY_CLARIFICATION_PROMPT = """You are an expert Product Specification Editor. The specification was found to have many overlapping or duplicate open questions, which suggests it contains ambiguous or conflicting information.

Your task is to update the specification so that:
1. **Clarity**: Make it clearer what the answers are for decisions that are already implied or stated—so the same questions are not asked again.
2. **Consistency**: Remove or resolve any conflicting or opposing information within the spec. Where the spec contradicts itself, choose one consistent interpretation.
3. **Use QA as source of truth**: The following Q&A (from previous rounds with the product owner, or from qa_history) is the canonical source. Where the spec conflicts with these answers, update the spec to match the Q&A. Do not leave conflicting statements.

Current Specification:
---
{spec_content}
---

Canonical Q&A (use this to resolve conflicts and fill gaps):
---
{qa_source}
---

Instructions:
- Integrate the Q&A answers clearly into the relevant sections of the spec.
- If two parts of the spec contradict each other, replace with the version that matches the Q&A, or with a single consistent statement.
- Remove redundant or ambiguous phrasing that could lead to the same question being asked again.
- Preserve all other valid content. Output the FULL specification as plain text (markdown).
"""

SPEC_REVIEW_CHUNK_PROMPT = """You are a Product Requirements Analysis expert. Review this SECTION of a product specification.

CRITICAL CONSTRAINTS:
- Maximum 5 issues, 5 gaps, and 5 open questions for this section
- **Do NOT ask questions about topics already specified in the spec or already answered** (see "Already answered" below if present). The spec and prior Q&A are the source of truth.
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

SPEC_CLEANUP_CHUNK_PROMPT = """You are an expert Product Specification Validator. Review and clean up this SECTION of a specification.

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
