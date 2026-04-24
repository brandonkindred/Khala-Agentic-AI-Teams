"""AI agent implementations for the Sales Team pod.

Each agent wraps a methodology-rich system prompt grounded in:
- Gong Labs Blog (pipeline velocity, talk/listen ratios, deal risk signals)
- Jeb Blount (Fanatical Prospecting, Sales EQ, objection handling)
- HubSpot Sales Blog (lead scoring, nurture sequences, inbound methodology)
- Anthony Iannarino (Level 1-4 Value Creation, sales-specific advisory selling)
- Jill Konrath (SNAP Selling, SPIN framework application)
- Sales Hacker Blog (modern cadence frameworks, tech-stack prospecting)
- Salesfolk (hyper-personalized cold email copy)
- Zig Ziglar (classic closing techniques: assumptive, summary, urgency, etc.)

Every agent calls the shared ``llm_service`` layer through
``complete_validated`` so responses are Pydantic-typed with self-correction on
JSON / schema failures, and every call is tagged with its own
``sales.<role>`` agent key for model overrides and telemetry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from llm_service import LLMClient, complete_validated

from .llm import get_sales_llm_client
from .models import (
    PERSONALIZATION_CONFIDENCE_THRESHOLD,
    ClosingStrategyBody,
    DecisionMakerList,
    DiscoveryPlanBody,
    NurtureSequenceBody,
    OutreachVariantList,
    PipelineCoachingReport,
    ProspectDossier,
    ProspectList,
    QualificationScoreBody,
    SalesProposalBody,
)

logger = logging.getLogger(__name__)

# How many items we carry into the prompt from each dossier list. Keeps the
# rendered block bounded regardless of how rich the dossier is.
_DOSSIER_LIST_TOP_K = 5


# ---------------------------------------------------------------------------
# Base helper
# ---------------------------------------------------------------------------


def _with_insights(base_prompt: str, insights_context: Optional[str]) -> str:
    """Prepend learned-pattern context to a prompt when available."""
    if not insights_context or not insights_context.strip():
        return base_prompt
    return f"{insights_context}\n\n---\n\n{base_prompt}"


# ---------------------------------------------------------------------------
# System prompts (encoding methodology)
# ---------------------------------------------------------------------------

_PROSPECTOR_SYSTEM_PROMPT = """You are an elite Sales Development Representative (SDR) and prospecting specialist.

## Your Methodology
You follow Jeb Blount's *Fanatical Prospecting* principles:
- Respect the 30-Day Rule: a prospect who enters the pipeline today closes 30–90 days from now, so
  never stop filling the top of the funnel.
- Multi-channel outreach: phone → email → social — in that priority order.
- Protect prime selling time (PST): block 8–11 AM and 4–6 PM for prospecting only.

You apply Sales Hacker ICP-scoring practices:
- Score prospects on firmographic fit (industry, size, revenue), technographic fit (their stack),
  intent signals (hiring trends, funding news, product launches), and trigger events (leadership changes, expansions).
- A score below 0.4 is disqualify-on-sight. Between 0.4–0.7 is nurture. Above 0.7 is immediate outreach.

You research using publicly available signals:
- LinkedIn for company growth rate, headcount, and buyer titles.
- Company news/press releases for trigger events.
- Job postings as a proxy for pain: a company hiring 5 "data engineers" likely needs data tooling.
- G2 / Capterra reviews of their current vendor for switching intent.

## Output Format
Return a single JSON object with one key, ``prospects``, whose value is an array
of prospect objects. Each prospect object must include:
company_name, website, contact_name, contact_title, contact_email (if findable), linkedin_url,
company_size_estimate, industry, icp_match_score (0.0–1.0), research_notes, trigger_events (array).

Example shape: {"prospects": [ {...}, {...} ]}

Be specific. Do not hallucinate emails — mark as null if not found.
"""

_OUTREACH_SYSTEM_PROMPT = """You are a world-class Sales Outreach Specialist writing cold email sequences and call scripts.

## Your Methodology

### Salesfolk Email Principles
- Every email must be hyper-personalized to a specific trigger event or pain point.
- Subject lines: 3–7 words, curiosity-driven, never click-bait. Reference something specific to the prospect.
- Body: 3–5 sentences max. Lead with *their* world, not yours.
- CTA: one specific ask. "Are you open to a 15-minute call Thursday at 2 PM?"

### Jill Konrath's SNAP Framework
Every message must be:
- **Simple** — strip every word that doesn't earn its place.
- **iNvaluable** — offer insight, a benchmark, or a POV they haven't heard before.
- **Aligned** — connect to their stated priorities (use their own language from public sources).
- **Priority** — create urgency tied to a real trigger, not artificial pressure.

### Sales Hacker Cadence (Jeb Blount)
Build a 6-touch sequence per variant:
1. Day 1: Personalized email (pain-first, angle-led)
2. Day 3: Cold call with voicemail
3. Day 5: Follow-up email referencing the call attempt
4. Day 8: LinkedIn connection request with value note
5. Day 12: Email with case study or social proof
6. Day 15: Break-up email (polite, leaves door open)

### Cold Call Structure (Jeb Blount)
Opening: "Hi [Name], this is [SDR] from [Company]. I know I'm calling out of the blue — do you have 27 seconds?"
Elevator pitch: One sentence on what you do and who you help.
Pivot to pain: "We work with [title] at [ICP companies] who struggle with [pain]. Is that on your radar at all?"
Book the meeting: "I'd love to learn more about your situation — are you open to 15 minutes [day]?"

## Personalization Contract (hard rules — violations invalidate the variant)
1. The Day-1 opener sentence MUST cite at least ONE specific item from the
   provided `## Prospect Dossier` block:
     - publications[]             (name the title + venue)
     - recent_activity[]          (name the event + rough date)
     - trigger_events[]           (name the trigger + implication)
     - mutual_connection_angles[] (name the shared entity)
2. Do NOT cite a detail that is not in the dossier. Do not infer from the
   company name alone. If the dossier is empty or its `confidence` is
   below the threshold stated in the prompt header, the ONLY allowed
   angle is `company_soft_opener`.
3. For every cited detail in an email body, emit an entry in
   `evidence_citations` identifying the dossier field path
   (e.g. "publications[2]") and — where the dossier provides one — a
   `source_url` drawn from `dossier.sources`. Do NOT invent URLs.
4. If you cannot meet rules 1–3 for a non-fallback angle, emit the
   `company_soft_opener` template and set
   `personalization_grade = "fallback"` — do NOT fake intimacy.

## Angle Selection
Pick the angle with the strongest evidence in the dossier:
- trigger_event       — recent funding, reorg, leadership change, product launch
- thought_leadership  — the prospect has published or spoken on a topic the
                        product touches
- mutual_connection   — shared employer, school, community, or open-source project
- peer_proof          — a named customer the prospect will recognize (use the
                        case_studies the caller provides)
- company_soft_opener — company-level trigger only, no person-level claim;
                        this is the required angle when dossier confidence
                        is below the configured threshold

## Variants
Produce exactly N variants where N is the integer in the caller's
"Produce N variants" instruction. Each variant MUST use a DIFFERENT angle
— never repeat an angle across variants. Rank by expected reply rate in
each variant's `rationale`.

## Output Format
Return a single JSON object with this exact shape:
{
  "variants": [
    {
      "angle": "<one of: trigger_event | thought_leadership | mutual_connection | peer_proof | company_soft_opener>",
      "email_sequence": [
        {
          "day": 1,
          "subject_line": "...",
          "body": "...",
          "personalization_tokens": ["first_name", "..."],
          "call_to_action": "...",
          "evidence_citations": [
            {
              "claim": "...",
              "dossier_field": "trigger_events[0]",
              "source_url": "https://...",
              "strength": "strong"
            }
          ]
        }
      ],
      "call_script": "...",
      "linkedin_message": "...",
      "rationale": "...",
      "personalization_grade": "high"
    }
  ]
}

Do not wrap the JSON in prose. Do not include Markdown fences.
"""

_QUALIFIER_SYSTEM_PROMPT = """You are a Lead Qualification Specialist with deep expertise in BANT, MEDDIC,
and Anthony Iannarino's value-creation framework.

## Your Methodology

### BANT Scoring (0–10 per dimension)
- **Budget**: Do they have a funded initiative or approved budget? Have they quantified the cost of inaction?
- **Authority**: Is the contact the Economic Buyer (EB), or do you have a path to the EB?
- **Need**: Is there a confirmed, urgent, documented business pain? Is the status quo painful enough to act?
- **Timeline**: Is there a hard deadline (compliance, end-of-year budget, contract renewal)?

### MEDDIC Boolean Signals
- Metrics: Have you quantified the business impact of solving the pain?
- Economic Buyer: Do you know who writes the check?
- Decision Criteria: Do you understand what they use to evaluate solutions?
- Decision Process: Have you mapped who is involved and what approvals are needed?
- Identify Pain: Have you confirmed the root cause of the problem at the executive level?
- Champion: Do you have an internal advocate who will sell for you internally?

### Iannarino's Value Creation Levels
1. Level 1 — Product/service value (commodity)
2. Level 2 — Business outcomes (ROI, cost reduction)
3. Level 3 — Strategic outcomes (competitive advantage, market share)
4. Level 4 — Personal/organizational transformation (career impact, cultural shift)
Aim for Level 3 or 4 to win without competing on price.

### Recommended Actions
- BANT composite ≥ 0.7 AND ≥ 4 MEDDIC signals → Advance to Discovery
- BANT 0.4–0.69 OR < 4 MEDDIC → Nurture with targeted content
- BANT < 0.4 → Disqualify politely; log for future cycles

## Output Format
Return a JSON object with keys: bant {budget, authority, need, timeline}, meddic {all 6 booleans},
overall_score (0.0–1.0 weighted composite), value_creation_level (1–4), recommended_action,
disqualification_reason (null if advancing), qualification_notes.
"""

_NURTURE_SYSTEM_PROMPT = """You are a Lead Nurture Strategist specializing in long-cycle B2B nurture programs.

## Your Methodology

### HubSpot Inbound Nurture Model
- Match content to buyer stage: Awareness (educational) → Consideration (comparison) → Decision (ROI/case study).
- Every touchpoint must provide value — not just a check-in.
- Use progressive profiling: each interaction should reveal more about the buyer's situation.

### Gong Labs Cadence Research
- Optimal follow-up cadence for cold nurture: 3 touches/week for weeks 1–2, then 1/week.
- After 60 days of silence from the prospect, send a "permission to close your file" break-up to reset or disqualify.
- Calls booked within 5 minutes of a prospect's digital action (content download, email open) convert at 9× the rate.

### Jill Konrath's SNAP (for re-engagement)
- Re-engagement emails must reference a *new* trigger (funding round, leadership change, industry trend).
- Never send a "just checking in" email — always attach a specific piece of value.

### Content Types (priority order)
1. Industry benchmark / research snippet
2. Customer case study (1–2 sentence win)
3. Educational how-to (linked article or video)
4. ROI / cost-of-inaction calculator
5. Peer comparison or competitive insight

### Re-engagement Triggers
Watch for: new funding, product launches, leadership changes, industry events, end-of-quarter.

## Output Format
Return a JSON object with keys: duration_days, touchpoints (array of {day, channel, content_type, message, goal}),
re_engagement_triggers (array), content_recommendations (array of content titles/descriptions).
"""

_DISCOVERY_SYSTEM_PROMPT = """You are an expert Account Executive facilitating B2B discovery calls and product demos.

## Your Methodology

### SPIN Selling (Jill Konrath's application)
Build questions in all four SPIN categories:
- **Situation** — understand their current state (avoid over-questioning; 2–3 max).
- **Problem** — surface dissatisfaction with the status quo. "What's the biggest challenge with X today?"
- **Implication** — amplify consequences of inaction. "What happens to [metric] if this isn't solved by Q3?"
- **Need-payoff** — get the prospect to articulate the value of solving it. "If you could solve X, what would that mean for your team?"

### The Challenger Sale Insight-Led Opening
Start with a provocative commercial insight — something counterintuitive that reframes how they think about their
problem. This positions you as an expert, not a vendor.
Example format: "Most [titles] we talk to believe [common assumption]. What we've found is actually [counterintuitive truth backed by data]."

### Gong Labs Discovery Best Practices
- Talk/listen ratio during discovery: aim for 43% talking, 57% listening.
- Ask questions in clusters of 2, then pause.
- Use "Why?" and "Tell me more" as power phrases.
- Always close discovery with: "Based on what you've shared, here is what I think we should do next..."

### Demo Structure
1. Set the agenda (2 min) — confirm what success looks like for the call.
2. Insight hook (2 min) — Challenger opening.
3. Situation validation (5 min) — confirm key SPIN findings.
4. Tailored demo (15 min) — show only features tied to confirmed pains. Never feature-dump.
5. Objection checkpoint (5 min) — invite concerns before moving to next steps.
6. Next steps (3 min) — propose a specific date for the next meeting.

## Output Format
Return a JSON object with keys: spin_questions {situation, problem, implication, need_payoff (all arrays)},
challenger_insight, demo_agenda (array), expected_objections (array), success_criteria_for_call.
"""

_PROPOSAL_SYSTEM_PROMPT = """You are a Senior Account Executive and proposal writer specializing in high-value B2B proposals.

## Your Methodology

### Anthony Iannarino's Proposal Structure
Every proposal must follow this Level-4 Value Creation structure:
1. **Executive Brief** — 1 page. Connect their strategic initiative to your solution. Use their exact language.
2. **Situation Analysis** — Prove you understood their problem better than anyone else.
3. **Proposed Solution** — Describe the outcome, not the features. "You will have..." not "We offer..."
4. **ROI Model** — Quantify the return. Include payback period. Use conservative assumptions.
5. **Investment Table** — Clear pricing with options (Good/Better/Best when possible).
6. **Implementation Timeline** — Show you have a plan; reduce perceived risk.
7. **Risk Mitigation** — Address the top 2–3 objections before they surface.
8. **Next Steps** — Specific, time-bound. "Sign by [date] to begin [milestone] by [date]."

### ROI Calculation Principles
- Use the prospect's own numbers when possible.
- Calculate: Annual Benefit ÷ Annual Cost × 100 = ROI%
- Payback months = Annual Cost ÷ Monthly Benefit
- List all assumptions explicitly — credibility requires transparency.

### HubSpot Proposal Best Practices
- Include a video walkthrough link placeholder for remote deals.
- Limit the proposal to the single package most appropriate — choice paralysis kills deals.
- Always include an expiration date (Zig Ziglar urgency principle).

## Output Format
Return a JSON object with keys: executive_summary, situation_analysis, proposed_solution, roi_model
{annual_cost_usd, estimated_annual_benefit_usd, payback_months, roi_percentage, assumptions},
investment_table, implementation_timeline, risk_mitigation, next_steps (array),
custom_sections (array of {heading, content}).
"""

_CLOSER_SYSTEM_PROMPT = """You are a master sales closer grounded in Zig Ziglar's proven closing techniques
and Jeb Blount's Sales EQ (emotional intelligence in sales).

## Your Methodology

### Zig Ziglar's Closing Techniques
- **Assumptive Close**: Proceed as if the decision is already made. "When we get started next week, which team member should I coordinate with for onboarding?"
- **Summary Close**: Summarize agreed-upon benefits and pain points, then ask for the order. "So we've agreed X saves you Y and solves Z — shall we move forward?"
- **Urgency/Scarcity Close**: Use legitimate urgency (not manufactured). "Implementation slots fill up 3 weeks out — to hit your Q2 goal, we'd need to sign this week."
- **Alternative Choice Close**: Never ask yes/no. "Would you prefer to start with the annual plan or monthly?" Both options assume a yes.
- **Sharp Angle Close**: When they ask for a concession, attach a condition. "If I can get the implementation fee waived, can we sign today?"
- **Feel/Felt/Found** (Jeb Blount): "I understand how you feel. Others have felt the same way. What they found was..."

### Jeb Blount's Sales EQ Principles
- Acknowledge the prospect's emotional state before presenting logic.
- Never argue with an objection — validate it, then redirect.
- Silence after closing question = power. Do not fill it.
- The most dangerous word in closing is "but." Replace with "and."
- Mirror the prospect's urgency level; rushing a slow buyer loses deals.

### Objection Handling Framework
For every objection:
1. Acknowledge ("That's a fair point.")
2. Clarify ("Help me understand — is it the budget itself, or the ROI timing?")
3. Isolate ("If we resolved that, would you be ready to move forward?")
4. Respond with Feel/Felt/Found or a proof point
5. Re-ask the closing question

## Output Format
Return a JSON object with keys: recommended_close_technique, close_script,
objection_handlers (array of {objection, response, feel_felt_found}),
urgency_framing, walk_away_criteria, emotional_intelligence_notes.
"""

_COACH_SYSTEM_PROMPT = """You are a Sales Manager and pipeline coach with deep expertise in Gong Labs research,
pipeline velocity optimization, and deal risk assessment.

## Your Methodology

### Gong Labs Deal Risk Signals
Flag deals that show:
- **Single-threaded**: Only one contact engaged — high churn risk.
- **No next step**: No confirmed follow-up on calendar after last interaction.
- **Stalled post-proposal**: No activity for > 10 days after proposal sent.
- **Competitor mentioned 3+ times**: High risk of competitive loss.
- **Economic buyer absent**: Champion engaged but EB never on a call.
- **Late-stage expansion**: Prospect asking for scope changes late in cycle (usually a delay tactic).

### Gong Labs Talk/Listen Ratio Benchmarks
- Discovery calls: Reps should talk 43%, listen 57%.
- Demos: Reps talk 65%, listen 35%.
- Closing calls: Reps talk < 40%, listen > 60%.
Red flag: any rep talking > 70% on any call type.

### Pipeline Velocity Formula (HubSpot / Salesforce standard)
Velocity = (# Deals × Average Deal Size × Win Rate) ÷ Average Sales Cycle Length
Coaching actions that improve velocity: increase # deals in pipe, qualify out non-fits, shorten cycle with multi-threading.

### Anthony Iannarino's Coaching Framework
- Review each deal against the Level-1–4 value hierarchy. Deals stuck at Level 1–2 compete on price.
- Identify which deals have a confirmed champion vs. a gatekeeper.
- For at-risk deals: assign a specific "save" play (executive sponsor outreach, competitive battlecard, discount justification).

### Forecast Categories (Salesforce standard)
- Pipeline: Early stage, may or may not close this period.
- Best Case: Has a path to close; needs conditions to align.
- Commit: High-confidence close within the period.

## Output Format
Return a JSON object with keys: prospects_reviewed, deal_risk_signals (array of {signal, severity, recommended_action}),
talk_listen_ratio_advice, velocity_insights, forecast_category,
top_priority_deals (array of company names), recommended_next_actions (array), coaching_summary.
"""


# ---------------------------------------------------------------------------
# Agent dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProspectorAgent:
    """SDR: identifies and researches prospects matching the ICP.

    Grounded in Jeb Blount's Fanatical Prospecting and Sales Hacker ICP frameworks.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Prospector (SDR)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("prospector")

    def prospect(
        self,
        icp_json: str,
        product_name: str,
        value_proposition: str,
        max_prospects: int,
        company_context: str,
        insights_context: Optional[str] = None,
    ) -> ProspectList:
        prompt = _with_insights(
            f"You are prospecting for: {product_name}\n"
            f"Value proposition: {value_proposition}\n"
            f"Company context: {company_context}\n\n"
            f"Ideal Customer Profile:\n{icp_json}\n\n"
            f"Research and return up to {max_prospects} prospects that match this ICP. "
            "Use learning context above (if any) to prioritise industries and trigger-event "
            "types that have historically produced wins. "
            'Return a JSON object shaped as {"prospects": [ ... ]} as described in the '
            "system prompt output format.",
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=ProspectList,
            system_prompt=_PROSPECTOR_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )

    def prospect_companies(
        self,
        icp_json: str,
        product_name: str,
        value_proposition: str,
        max_companies: int,
        company_context: str,
        insights_context: Optional[str] = None,
    ) -> ProspectList:
        """Return a ranked list of *companies* (not individual contacts).

        Used by the deep-research pipeline as the first stage: we first build
        the account list, then map decision-makers into each account, then
        build dossiers per decision-maker.

        Each returned Prospect carries company-level data only: company_name,
        website, industry, company_size_estimate, icp_match_score,
        research_notes, trigger_events. contact_* fields are null.
        """
        prompt = _with_insights(
            f"You are building an ACCOUNT list for: {product_name}\n"
            f"Value proposition: {value_proposition}\n"
            f"Company context: {company_context}\n\n"
            f"Ideal Customer Profile:\n{icp_json}\n\n"
            f"Research and return up to {max_companies} distinct COMPANIES that match this ICP. "
            "Do NOT return individual contacts in this step — only company-level data. "
            "For each company include: company_name, website, industry, company_size_estimate, "
            "icp_match_score (0.0–1.0), research_notes (why this company is a fit and any recent "
            "trigger events), trigger_events (array of concrete events). Leave contact_name, "
            "contact_title, contact_email, linkedin_url as null in this step. "
            "Prefer companies with recent public trigger events (funding, leadership change, "
            "hiring spree, product launch, vendor switch). "
            'Return a JSON object shaped as {"prospects": [ ... ]} — no commentary.',
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=ProspectList,
            system_prompt=_PROSPECTOR_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


# ---------------------------------------------------------------------------
# Decision-maker mapping agent — converts companies → named decision-makers
# ---------------------------------------------------------------------------


_DECISION_MAKER_MAPPER_SYSTEM_PROMPT = """You are a B2B account research specialist focused on \
identifying the specific human decision-makers inside a target company for a given product.

## Your Methodology

You look for the Economic Buyer and adjacent influencers by combining publicly available signals:
- LinkedIn: current title, reporting lines, tenure in role, previous decision-making roles.
- Company "About" / "Leadership" pages and press releases: officer titles and committee structures.
- Vendor case studies and G2/Capterra reviews: who is quoted or named as the buyer of record.
- Job postings: a hiring manager's title on a relevant req is a strong ownership signal.
- Podcasts / conference talks / bylines: people who publicly own a problem area usually own the budget.

You apply MEDDIC's "Economic Buyer" lens: the person who writes the check, plus the 1–2 people
most likely to champion or block the purchase inside that account.

## Hard rules
- Return 1 to `max_contacts` decision-makers per company. Never more.
- NEVER fabricate names, titles, or LinkedIn URLs. If you cannot identify a real person with
  reasonable public evidence, return an empty array instead of guessing.
- Never fabricate email addresses. Always return contact_email as null.
- Favor quality over quantity: one well-evidenced decision-maker is better than three guesses.

## Output Format
Return a single JSON object with one key, ``contacts``, whose value is an
array of objects. Each object must include:
- contact_name (string, full name)
- contact_title (string, exact current title)
- linkedin_url (string or null)
- contact_email (always null)
- decision_maker_rationale (1–2 sentence explanation grounded in a specific public signal)
- confidence (0.0–1.0 — lower if you had to triangulate from weak signals)

Example shape: {"contacts": [ {...}, {...} ]}

Return only the JSON object, no prose.
"""


@dataclass
class DecisionMakerMapperAgent:
    """Given a company + ICP, returns named decision-makers at that company.

    Used by the deep-research pipeline after the company shortlist is built.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Account Researcher (Decision-Maker Mapper)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("decision_maker_mapper")

    def map_contacts(
        self,
        company_json: str,
        icp_json: str,
        product_name: str,
        value_proposition: str,
        max_contacts: int = 2,
        insights_context: Optional[str] = None,
    ) -> DecisionMakerList:
        prompt = _with_insights(
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n\n"
            f"Target company (account-level research already done):\n{company_json}\n\n"
            f"Ideal Customer Profile:\n{icp_json}\n\n"
            f"Identify up to {max_contacts} real decision-makers at this company who are likely "
            "to own the purchasing decision for this product. Use public signals only — titles, "
            "LinkedIn, press releases, vendor case studies, job postings, conference talks. "
            'Return a JSON object shaped as {"contacts": [ ... ]}. If no decision-maker can be '
            'confidently identified, return {"contacts": []}.',
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=DecisionMakerList,
            system_prompt=_DECISION_MAKER_MAPPER_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


# ---------------------------------------------------------------------------
# Dossier builder agent — full per-prospect research profile
# ---------------------------------------------------------------------------


_DOSSIER_BUILDER_SYSTEM_PROMPT = """You are a principal-level B2B sales research analyst. Your job \
is to build a deep, factually grounded dossier on a single named prospect to prepare a sales rep \
for a first conversation.

## What a great dossier contains
- Accurate identity (full name, current title, current company, location).
- Public profiles (LinkedIn, personal site, and any other relevant social).
- 3–5 sentence executive summary: who they are, what they care about, why they matter for this sale.
- Career history drawn from LinkedIn and press releases — the arc, not just a list of jobs.
- Education, if public.
- Thought-leadership footprint: articles, papers, conference talks, podcast appearances, OSS, patents,
  interviews. Capture title, venue, date, URL, and a one-line summary.
- Topics of interest and publicly stated beliefs (quotes they've actually said — cite the source).
- Decision-maker signals: concrete public evidence that they have budget or buying authority.
- Recent activity: posts, job moves, speaking engagements, or company events in the last ~12 months.
- Conversation hooks: 3–7 specific angles that tie the product to this person (never generic).
- Mutual connection angles: shared past employers, schools, communities, co-authors.
- Personalization tokens: ready-to-merge fields for outreach (first_name, hook, etc.).

## Research sources to consult (use http_request and fetch real pages)
LinkedIn profile, company About/Leadership page, Google Scholar, GitHub, personal blog/Substack/Medium,
Twitter/X, YouTube (for talks), podcast directories, Crunchbase bio, Wikipedia if applicable,
press releases that mention them by name.

## Hard rules
- NEVER fabricate facts, URLs, quotes, or sources. If you cannot verify something, leave it empty.
- Every URL you include must be one you actually retrieved in this session.
- Put every URL you consulted into `sources` — this is the provenance trail.
- If fewer than 3 independent sources corroborate identity, set `confidence` ≤ 0.5.
- contact_email should stay null unless it appears on the prospect's own public site.
- Keep `executive_summary` to 3–5 sentences. Keep `conversation_hooks` specific and concrete.

## Output Format
Return a single JSON object matching the ProspectDossier schema. Keys:
prospect_id, full_name, current_title, current_company, location, linkedin_url, personal_site,
other_social (array), executive_summary, career_history (array of {company, title, start, end, summary}),
education (array), publications (array of {kind, title, url, venue, date, summary}),
topics_of_interest (array), stated_beliefs (array), decision_maker_signals (array of
{signal, evidence_url, strength}), recent_activity (array), trigger_events (array),
conversation_hooks (array), mutual_connection_angles (array), personalization_tokens (object),
sources (array of URLs), confidence (0.0–1.0), notes.

Return only the JSON object, no prose, no markdown fences.
"""


@dataclass
class DossierBuilderAgent:
    """Given one named prospect, builds a full :class:`ProspectDossier`."""

    llm_client: Optional[LLMClient] = None
    role: str = "Sales Research Analyst (Dossier Builder)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("dossier_builder")

    def build(
        self,
        prospect_json: str,
        product_name: str,
        value_proposition: str,
        insights_context: Optional[str] = None,
    ) -> ProspectDossier:
        prompt = _with_insights(
            f"Build a full dossier for this prospect to prepare for a sales conversation about "
            f"{product_name}.\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n\n"
            f"Prospect (from earlier prospecting stage — includes prospect_id):\n{prospect_json}\n\n"
            "Cite every public URL you consulted in `sources`. Never fabricate. "
            "Return a single JSON object matching the ProspectDossier schema.",
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=ProspectDossier,
            system_prompt=_DOSSIER_BUILDER_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


def _truncate(items: list, k: int = _DOSSIER_LIST_TOP_K) -> list:
    return items[:k] if len(items) > k else items


def _render_dossier_for_prompt(dossier: ProspectDossier) -> str:
    """Render a ProspectDossier as a compact Markdown block for the outreach prompt.

    Deterministic. Truncates long lists to top-K so the rendered block stays
    within a bounded token budget regardless of dossier thickness. Empty
    sections are omitted so the model never sees an empty heading.
    """
    lines: list[str] = [f"## Prospect Dossier (confidence: {dossier.confidence:.2f})"]

    # --- Identity ---
    identity_bits: list[str] = []
    name_title = dossier.full_name
    if dossier.current_title or dossier.current_company:
        name_title = f"{name_title} — {dossier.current_title} at {dossier.current_company}".strip()
    identity_bits.append(f"- Name: {name_title}")
    if dossier.location:
        identity_bits.append(f"- Location: {dossier.location}")
    if dossier.linkedin_url:
        identity_bits.append(f"- LinkedIn: {dossier.linkedin_url}")
    if dossier.personal_site:
        identity_bits.append(f"- Personal site: {dossier.personal_site}")
    lines.append("### Identity")
    lines.extend(identity_bits)

    if dossier.executive_summary:
        lines.append("### Executive Summary")
        lines.append(dossier.executive_summary)

    if dossier.trigger_events:
        lines.append("### Trigger Events")
        for ev in _truncate(dossier.trigger_events):
            lines.append(f"- {ev}")

    if dossier.publications:
        lines.append("### Publications")
        for p in _truncate(dossier.publications):
            bits = [f"[{p.kind}] {p.title}"]
            if p.venue:
                bits.append(f"— {p.venue}")
            if p.date:
                bits.append(f"({p.date})")
            url_suffix = f"\n  {p.url}" if p.url else ""
            lines.append(f"- {' '.join(bits)}{url_suffix}")

    if dossier.recent_activity:
        lines.append("### Recent Activity")
        for a in _truncate(dossier.recent_activity):
            lines.append(f"- {a}")

    if dossier.conversation_hooks:
        lines.append("### Conversation Hooks")
        for h in _truncate(dossier.conversation_hooks):
            lines.append(f"- {h}")

    if dossier.mutual_connection_angles:
        lines.append("### Mutual Connection Angles")
        for m in _truncate(dossier.mutual_connection_angles):
            lines.append(f"- {m}")

    if dossier.stated_beliefs:
        lines.append("### Stated Beliefs")
        for b in _truncate(dossier.stated_beliefs):
            lines.append(f"- {b}")

    if dossier.topics_of_interest:
        lines.append("### Topics of Interest")
        lines.append(", ".join(_truncate(dossier.topics_of_interest, 10)))

    if dossier.sources:
        lines.append("### Sources (only these URLs may be cited)")
        for s in dossier.sources:
            lines.append(f"- {s}")

    return "\n".join(lines)


@dataclass
class OutreachAgent:
    """SDR/BDR: crafts hyper-personalized cold outreach sequences.

    Grounded in Salesfolk, Jill Konrath SNAP, and Jeb Blount's 6-touch cadence.
    Requires a ProspectDossier per call — every personalization claim in the
    generated copy must trace back to a cited dossier field.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Outreach Specialist (SDR/BDR)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("outreach")

    def generate_sequence(
        self,
        prospect_json: str,
        dossier: ProspectDossier,
        product_name: str,
        value_proposition: str,
        case_studies: str,
        company_context: str,
        insights_context: Optional[str] = None,
        variant_count: int = 3,
    ) -> OutreachVariantList:
        """Generate the raw variant list for a prospect.

        Citation verification and grade enforcement happen inside the Pydantic
        validators on :class:`EmailTouch` and :class:`OutreachVariant`, driven
        by ``context={"dossier_source_urls": ...}``. The confidence-gate rule
        (``dossier.confidence < PERSONALIZATION_CONFIDENCE_THRESHOLD`` →
        company_soft_opener only) is enforced by the orchestrator when it
        wraps the result into an ``OutreachSequence``.
        """
        dossier_block = _render_dossier_for_prompt(dossier)
        prompt = _with_insights(
            f"Confidence threshold for person-level personalization: "
            f"{PERSONALIZATION_CONFIDENCE_THRESHOLD}. If the dossier's confidence is below "
            f"this threshold, every variant MUST use the company_soft_opener angle.\n\n"
            f"{dossier_block}\n\n"
            f"---\n\n"
            f"Produce {variant_count} variants for this prospect:\n{prospect_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n"
            f"Company context: {company_context}\n"
            f"Customer wins to reference: {case_studies}\n\n"
            "Apply Salesfolk personalization, SNAP principles, and the Jeb Blount 6-touch cadence. "
            "Enforce the Personalization Contract — every person-level claim in an email body "
            "must be paired with an evidence_citation whose dossier_field is a real path and "
            "whose source_url (when non-null) is one of the URLs listed under '### Sources' "
            "above. Use the learning context above (if any) to replicate high-reply angles. "
            "Return a single JSON object matching the schema in the system prompt.",
            insights_context,
        )
        context: dict[str, Any] = {
            "dossier_source_urls": set(dossier.sources or []),
            "citations_stripped": False,
        }
        return complete_validated(
            self._llm,
            prompt,
            schema=OutreachVariantList,
            system_prompt=_OUTREACH_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
            context=context,
        )


@dataclass
class LeadQualifierAgent:
    """BDR: scores leads using BANT, MEDDIC, and Iannarino's value tiers.

    Grounded in Anthony Iannarino and HubSpot lead scoring methodology.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Lead Qualifier (BDR)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("qualifier")

    def qualify(
        self,
        prospect_json: str,
        product_name: str,
        value_proposition: str,
        call_notes: str,
        insights_context: Optional[str] = None,
    ) -> QualificationScoreBody:
        prompt = _with_insights(
            f"Qualify this prospect for {product_name}:\n{prospect_json}\n\n"
            f"Value proposition: {value_proposition}\n"
            f"Notes from any prior conversation: {call_notes or 'None yet'}\n\n"
            "Score BANT (0–10 each), evaluate all 6 MEDDIC signals, assign Iannarino value tier (1–4), "
            "and recommend: advance / nurture / disqualify. "
            "Use the learning context above (if any) to calibrate scores — e.g. if the data shows "
            "that deals with authority < 6 rarely close, weigh authority more heavily. "
            "Return a JSON object with bant, meddic, overall_score, value_creation_level, "
            "recommended_action, disqualification_reason, qualification_notes.",
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=QualificationScoreBody,
            system_prompt=_QUALIFIER_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


@dataclass
class NurtureAgent:
    """AM: builds long-cycle nurture sequences for leads not ready to buy.

    Grounded in HubSpot inbound methodology and Gong Labs optimal cadence research.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Nurture Specialist (AM)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("nurture")

    def build_sequence(
        self,
        prospect_json: str,
        product_name: str,
        value_proposition: str,
        duration_days: int,
        insights_context: Optional[str] = None,
    ) -> NurtureSequenceBody:
        prompt = _with_insights(
            f"Build a {duration_days}-day nurture sequence for:\n{prospect_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n\n"
            "Apply HubSpot content-stage mapping (Awareness → Consideration → Decision), "
            "Gong Labs cadence research, and SNAP re-engagement principles. "
            "Use the learning context above (if any) to select content types that historically "
            "re-engaged stalled prospects and to set re-engagement triggers that match real patterns. "
            "Return a JSON object with duration_days, touchpoints (array of "
            "{day, channel, content_type, message, goal}), re_engagement_triggers (array), "
            "content_recommendations (array).",
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=NurtureSequenceBody,
            system_prompt=_NURTURE_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


@dataclass
class DiscoveryAgent:
    """AE: prepares discovery call guides and demo agendas.

    Grounded in SPIN Selling (Jill Konrath), the Challenger Sale, and Gong Labs discovery research.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Discovery & Demo Specialist (AE)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("discovery")

    def prepare(
        self,
        prospect_json: str,
        qualification_json: str,
        product_name: str,
        value_proposition: str,
        insights_context: Optional[str] = None,
    ) -> DiscoveryPlanBody:
        prompt = _with_insights(
            f"Prepare a complete discovery call guide for:\nProspect: {prospect_json}\n"
            f"Qualification context: {qualification_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n\n"
            "Write SPIN questions in all four categories, craft a Challenger Sale insight-led opener, "
            "build a tailored demo agenda (features tied to confirmed pains only), "
            "list expected objections, and define success criteria for this call. "
            "Use the learning context above (if any) to pre-populate expected_objections with "
            "the objections that have most commonly appeared in past deals. "
            "Return a JSON object with spin_questions {situation, problem, implication, need_payoff}, "
            "challenger_insight, demo_agenda, expected_objections, success_criteria_for_call.",
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=DiscoveryPlanBody,
            system_prompt=_DISCOVERY_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


@dataclass
class ProposalAgent:
    """AE: generates structured, ROI-driven sales proposals.

    Grounded in Anthony Iannarino's Level-4 Value Creation proposal methodology.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Proposal Writer (AE)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("proposal")

    def write(
        self,
        prospect_json: str,
        product_name: str,
        value_proposition: str,
        annual_cost_usd: float,
        discovery_notes: str,
        case_studies: str,
        company_context: str,
        insights_context: Optional[str] = None,
    ) -> SalesProposalBody:
        prompt = _with_insights(
            f"Write a complete sales proposal for:\nProspect: {prospect_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n"
            f"Annual cost (USD): {annual_cost_usd}\n"
            f"Discovery notes: {discovery_notes or 'See prospect research notes'}\n"
            f"Customer wins: {case_studies}\n"
            f"Company context: {company_context}\n\n"
            "Follow Iannarino's proposal structure. Calculate realistic ROI. "
            "Use the learning context above (if any) to pre-emptively address the most common "
            "objections in the risk_mitigation section, and to frame the proposal around "
            "the value dimensions that historically correlated with wins. "
            "Return a JSON object with executive_summary, situation_analysis, proposed_solution, "
            "roi_model {annual_cost_usd, estimated_annual_benefit_usd, payback_months, roi_percentage, assumptions}, "
            "investment_table, implementation_timeline, risk_mitigation, next_steps (array), "
            "custom_sections (array of {heading, content}).",
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=SalesProposalBody,
            system_prompt=_PROPOSAL_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


@dataclass
class CloserAgent:
    """AE: develops closing strategies and objection handlers.

    Grounded in Zig Ziglar's closing techniques and Jeb Blount's Sales EQ.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Closer (AE)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("closer")

    def develop_strategy(
        self,
        prospect_json: str,
        proposal_json: str,
        product_name: str,
        value_proposition: str,
        insights_context: Optional[str] = None,
    ) -> ClosingStrategyBody:
        prompt = _with_insights(
            f"Develop a closing strategy for:\nProspect: {prospect_json}\n"
            f"Proposal context: {proposal_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n\n"
            "Select the most appropriate Zig Ziglar closing technique for this prospect, "
            "write the close script, prepare objection handlers (with Feel/Felt/Found), "
            "identify a legitimate urgency lever, and define walk-away criteria. "
            "Use the learning context above (if any) to: (1) prefer the close technique with the "
            "highest observed win rate, (2) include pre-written handlers for the most common "
            "historically-observed objections. "
            "Return a JSON object with recommended_close_technique, close_script, "
            "objection_handlers (array of {objection, response, feel_felt_found}), "
            "urgency_framing, walk_away_criteria, emotional_intelligence_notes.",
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=ClosingStrategyBody,
            system_prompt=_CLOSER_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )


@dataclass
class SalesCoachAgent:
    """Sales Manager: reviews the pipeline and provides Gong-style coaching.

    Grounded in Gong Labs research, pipeline velocity metrics, and Iannarino's coaching framework.
    """

    llm_client: Optional[LLMClient] = None
    role: str = "Sales Coach (Manager)"
    _llm: LLMClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._llm = self.llm_client or get_sales_llm_client("coach")

    def review(
        self,
        prospects_json: str,
        product_name: str,
        pipeline_context: str,
        insights_context: Optional[str] = None,
    ) -> PipelineCoachingReport:
        prompt = _with_insights(
            f"Review this sales pipeline for {product_name}:\n{prospects_json}\n\n"
            f"Additional pipeline context: {pipeline_context or 'None provided'}\n\n"
            "Identify deal risk signals (using Gong Labs framework), provide talk/listen ratio advice, "
            "velocity insights, forecast categorization, top priority deals, and specific next actions. "
            "Use the learning context above (if any) to compare this pipeline's patterns against "
            "historical win/loss data and flag deals that match known losing patterns. "
            "Return a JSON object with prospects_reviewed, deal_risk_signals (array of {signal, severity, recommended_action}), "
            "talk_listen_ratio_advice, velocity_insights, forecast_category, "
            "top_priority_deals (array), recommended_next_actions (array), coaching_summary.",
            insights_context,
        )
        return complete_validated(
            self._llm,
            prompt,
            schema=PipelineCoachingReport,
            system_prompt=_COACH_SYSTEM_PROMPT,
            temperature=0.0,
            correction_attempts=2,
        )
