"""AWS Strands AI agent implementations for the Sales Team pod.

Each agent wraps a strands.Agent with a methodology-rich system prompt grounded in:
- Gong Labs Blog (pipeline velocity, talk/listen ratios, deal risk signals)
- Jeb Blount (Fanatical Prospecting, Sales EQ, objection handling)
- HubSpot Sales Blog (lead scoring, nurture sequences, inbound methodology)
- Anthony Iannarino (Level 1-4 Value Creation, sales-specific advisory selling)
- Jill Konrath (SNAP Selling, SPIN framework application)
- Sales Hacker Blog (modern cadence frameworks, tech-stack prospecting)
- Salesfolk (hyper-personalized cold email copy)
- Zig Ziglar (classic closing techniques: assumptive, summary, urgency, etc.)

If the `strands` package is not installed the agents degrade gracefully, returning
structured-text stubs so the API remains functional and tests can run without the SDK.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, List

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Strands SDK import
# ---------------------------------------------------------------------------

try:
    from strands import Agent as StrandsAgent  # type: ignore[import]

    try:
        from strands_tools import current_time, http_request, python_repl  # type: ignore[import]

        _DEFAULT_TOOLS = [http_request, python_repl, current_time]
    except ImportError:
        _DEFAULT_TOOLS = []

    HAS_STRANDS = True
    logger.info("AWS Strands SDK loaded — sales agents will use strands.Agent")
except ImportError:
    HAS_STRANDS = False
    _DEFAULT_TOOLS = []
    logger.warning(
        "strands package not found — sales agents running in stub mode. "
        "Install strands-agents and strands-agents-tools for full LLM capability."
    )


# ---------------------------------------------------------------------------
# Base helper
# ---------------------------------------------------------------------------


def _build_strands_agent(system_prompt: str, tools: list | None = None) -> Any | None:
    """Construct a strands.Agent if the SDK is available, else return None."""
    if not HAS_STRANDS:
        return None
    return StrandsAgent(
        system_prompt=system_prompt,
        tools=tools if tools is not None else _DEFAULT_TOOLS,
    )


def _call_agent(agent: Any | None, prompt: str, stub_response: str) -> str:
    """Call a strands.Agent and return its text output, falling back to the stub."""
    if agent is None:
        return stub_response
    try:
        result = agent(prompt)
        # strands.Agent may return an AgentResult or a str
        if hasattr(result, "message"):
            content = result.message
        else:
            content = str(result)
        return content.strip()
    except Exception as exc:
        logger.error("Strands agent call failed: %s", exc, exc_info=True)
        return stub_response


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
Return a JSON array of prospect objects. Each object must include:
company_name, website, contact_name, contact_title, contact_email (if findable), linkedin_url,
company_size_estimate, industry, icp_match_score (0.0–1.0), research_notes, trigger_events (array).

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
Build a 6-touch sequence:
1. Day 1: Personalized email (pain-first)
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

## Output Format
Return a JSON object with keys:
- email_sequence: array of {day, subject_line, body, personalization_tokens, call_to_action}
- call_script: full call script as a string
- linkedin_message: connection request copy
- sequence_rationale: brief explanation of angle chosen
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

    role: str = "Prospector (SDR)"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_PROSPECTOR_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def prospect(self, icp_json: str, product_name: str, value_proposition: str,
                 max_prospects: int, company_context: str) -> str:
        prompt = (
            f"You are prospecting for: {product_name}\n"
            f"Value proposition: {value_proposition}\n"
            f"Company context: {company_context}\n\n"
            f"Ideal Customer Profile:\n{icp_json}\n\n"
            f"Research and return up to {max_prospects} prospects that match this ICP. "
            "Use web search to find real companies, recent trigger events, and likely contacts. "
            "Return a JSON array of prospect objects."
        )
        stub = json.dumps([
            {
                "company_name": "Acme Corp",
                "website": "https://acme.example.com",
                "contact_name": "Jane Smith",
                "contact_title": "VP of Sales",
                "contact_email": None,
                "linkedin_url": "https://linkedin.com/in/jane-smith-example",
                "company_size_estimate": "200–500",
                "industry": "SaaS",
                "icp_match_score": 0.85,
                "research_notes": "Recently raised Series B; hiring 10 AEs; uses Salesforce.",
                "trigger_events": ["Series B funding announced", "Headcount growing 40% YoY"],
            }
        ])
        return _call_agent(self._agent, prompt, stub)


@dataclass
class OutreachAgent:
    """SDR/BDR: crafts hyper-personalized cold outreach sequences.

    Grounded in Salesfolk, Jill Konrath SNAP, and Jeb Blount's 6-touch cadence.
    """

    role: str = "Outreach Specialist (SDR/BDR)"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_OUTREACH_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def generate_sequence(self, prospect_json: str, product_name: str,
                          value_proposition: str, case_studies: str, company_context: str) -> str:
        prompt = (
            f"Create a complete 6-touch outreach sequence for this prospect:\n{prospect_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n"
            f"Company context: {company_context}\n"
            f"Customer wins to reference: {case_studies}\n\n"
            "Apply Salesfolk personalization, SNAP principles, and the Jeb Blount 6-touch cadence. "
            "Return a JSON object with email_sequence, call_script, linkedin_message, sequence_rationale."
        )
        stub = json.dumps({
            "email_sequence": [
                {
                    "day": 1,
                    "subject_line": "{{company_name}} + [Product] — quick thought",
                    "body": (
                        "Hi {{contact_name}},\n\nSaw {{trigger_event}} — congrats on the momentum.\n\n"
                        "We help [titles] at companies like yours [core outcome] without [key friction].\n\n"
                        "Worth a 15-min call this week?"
                    ),
                    "personalization_tokens": ["{{company_name}}", "{{contact_name}}", "{{trigger_event}}"],
                    "call_to_action": "15-minute call this week",
                },
            ],
            "call_script": (
                "Hi {{contact_name}}, this is [SDR] from [Company]. "
                "I know I'm calling out of the blue — do you have 27 seconds? "
                "[Pause] We help [titles] solve [pain]. Is that on your radar?"
            ),
            "linkedin_message": (
                "Hi {{contact_name}}, noticed {{trigger_event}} — impressive growth. "
                "I work with similar [titles] on [outcome]. Would love to connect."
            ),
            "sequence_rationale": "Trigger-event hook chosen to maximize relevance and open rates.",
        })
        return _call_agent(self._agent, prompt, stub)


@dataclass
class LeadQualifierAgent:
    """BDR: scores leads using BANT, MEDDIC, and Iannarino's value tiers.

    Grounded in Anthony Iannarino and HubSpot lead scoring methodology.
    """

    role: str = "Lead Qualifier (BDR)"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_QUALIFIER_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def qualify(self, prospect_json: str, product_name: str,
                value_proposition: str, call_notes: str) -> str:
        prompt = (
            f"Qualify this prospect for {product_name}:\n{prospect_json}\n\n"
            f"Value proposition: {value_proposition}\n"
            f"Notes from any prior conversation: {call_notes or 'None yet'}\n\n"
            "Score BANT (0–10 each), evaluate all 6 MEDDIC signals, assign Iannarino value tier (1–4), "
            "and recommend: advance / nurture / disqualify. "
            "Return a JSON object with bant, meddic, overall_score, value_creation_level, "
            "recommended_action, disqualification_reason, qualification_notes."
        )
        stub = json.dumps({
            "bant": {"budget": 7, "authority": 6, "need": 9, "timeline": 6},
            "meddic": {
                "metrics_identified": True,
                "economic_buyer_known": False,
                "decision_criteria_understood": True,
                "decision_process_mapped": False,
                "identify_pain": True,
                "champion_found": True,
            },
            "overall_score": 0.72,
            "value_creation_level": 3,
            "recommended_action": "Advance to Discovery — schedule 30-min discovery call",
            "disqualification_reason": None,
            "qualification_notes": (
                "Strong need and champion present. EB not yet identified — must multi-thread "
                "before proposal stage. Budget likely available but not confirmed."
            ),
        })
        return _call_agent(self._agent, prompt, stub)


@dataclass
class NurtureAgent:
    """AM: builds long-cycle nurture sequences for leads not ready to buy.

    Grounded in HubSpot inbound methodology and Gong Labs optimal cadence research.
    """

    role: str = "Nurture Specialist (AM)"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_NURTURE_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def build_sequence(self, prospect_json: str, product_name: str,
                       value_proposition: str, duration_days: int) -> str:
        prompt = (
            f"Build a {duration_days}-day nurture sequence for:\n{prospect_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n\n"
            "Apply HubSpot content-stage mapping (Awareness → Consideration → Decision), "
            "Gong Labs cadence research, and SNAP re-engagement principles. "
            "Return a JSON object with duration_days, touchpoints (array), "
            "re_engagement_triggers (array), content_recommendations (array)."
        )
        stub = json.dumps({
            "duration_days": duration_days,
            "touchpoints": [
                {
                    "day": 1,
                    "channel": "email",
                    "content_type": "educational article",
                    "message": "Sharing a benchmark report on [pain area] that peers in your space found useful.",
                    "goal": "Establish thought leadership and keep top of mind",
                },
                {
                    "day": 14,
                    "channel": "linkedin",
                    "content_type": "case study snippet",
                    "message": "Quick win: [Customer] reduced [metric] by 40% in 60 days with [Product].",
                    "goal": "Introduce social proof at consideration stage",
                },
                {
                    "day": 30,
                    "channel": "email",
                    "content_type": "ROI calculator",
                    "message": "I built a quick calculator showing the cost of [pain] for a company your size.",
                    "goal": "Move prospect from consideration to decision stage",
                },
                {
                    "day": 60,
                    "channel": "phone",
                    "content_type": "check-in call",
                    "message": "Following up to see if [trigger event or industry change] has shifted priorities.",
                    "goal": "Re-qualify and determine readiness to advance",
                },
            ],
            "re_engagement_triggers": [
                "New funding round announced",
                "Leadership change in buying committee",
                "End-of-quarter budget release",
                "Competitor product incident",
            ],
            "content_recommendations": [
                "Industry benchmark report: [Pain area] in 2026",
                "Customer case study: How [Similar Company] solved [Pain]",
                "Blog post: 5 signs your [current solution] is costing you more than you think",
                "ROI calculator: Cost of [problem] for [company size] teams",
            ],
        })
        return _call_agent(self._agent, prompt, stub)


@dataclass
class DiscoveryAgent:
    """AE: prepares discovery call guides and demo agendas.

    Grounded in SPIN Selling (Jill Konrath), the Challenger Sale, and Gong Labs discovery research.
    """

    role: str = "Discovery & Demo Specialist (AE)"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_DISCOVERY_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def prepare(self, prospect_json: str, qualification_json: str,
                product_name: str, value_proposition: str) -> str:
        prompt = (
            f"Prepare a complete discovery call guide for:\nProspect: {prospect_json}\n"
            f"Qualification context: {qualification_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n\n"
            "Write SPIN questions in all four categories, craft a Challenger Sale insight-led opener, "
            "build a tailored demo agenda (features tied to confirmed pains only), "
            "list expected objections, and define success criteria for this call. "
            "Return a JSON object with spin_questions {situation, problem, implication, need_payoff}, "
            "challenger_insight, demo_agenda, expected_objections, success_criteria_for_call."
        )
        stub = json.dumps({
            "spin_questions": {
                "situation": [
                    "Walk me through how your team currently handles [process].",
                    "How many people are involved in [process], and what tools do they use?",
                ],
                "problem": [
                    "What's the biggest frustration your team has with [current approach]?",
                    "Where do deals most commonly fall through in your current process?",
                ],
                "implication": [
                    "What happens to your [key metric] when [pain] occurs?",
                    "If this isn't resolved by Q3, what's the downstream impact on your team's targets?",
                ],
                "need_payoff": [
                    "If you could eliminate [pain] entirely, what would that free your team to focus on?",
                    "What would a 20% improvement in [metric] mean for your business this year?",
                ],
            },
            "challenger_insight": (
                "Most [titles] we talk to assume [common belief]. "
                "What our data across 200+ customers shows is that [counterintuitive truth] — "
                "which means the real leverage point is [reframe]."
            ),
            "demo_agenda": [
                "Set agenda & confirm success criteria (2 min)",
                "Challenger insight: reframe the problem (2 min)",
                "Validate key pains from discovery (5 min)",
                "Show [Feature A] — ties to confirmed pain #1 (5 min)",
                "Show [Feature B] — ties to confirmed pain #2 (5 min)",
                "Objection checkpoint — invite concerns (5 min)",
                "Propose next steps (3 min)",
            ],
            "expected_objections": [
                "We already have [competitor] — why switch?",
                "This isn't in the budget right now.",
                "I need to loop in [other stakeholder] before we can move forward.",
            ],
            "success_criteria_for_call": (
                "Confirmed 2 quantified pain points, identified the Economic Buyer, "
                "and booked a follow-up with the full buying committee within 5 business days."
            ),
        })
        return _call_agent(self._agent, prompt, stub)


@dataclass
class ProposalAgent:
    """AE: generates structured, ROI-driven sales proposals.

    Grounded in Anthony Iannarino's Level-4 Value Creation proposal methodology.
    """

    role: str = "Proposal Writer (AE)"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_PROPOSAL_SYSTEM_PROMPT, [*_DEFAULT_TOOLS])

    def write(self, prospect_json: str, product_name: str, value_proposition: str,
              annual_cost_usd: float, discovery_notes: str,
              case_studies: str, company_context: str) -> str:
        prompt = (
            f"Write a complete sales proposal for:\nProspect: {prospect_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n"
            f"Annual cost (USD): {annual_cost_usd}\n"
            f"Discovery notes: {discovery_notes or 'See prospect research notes'}\n"
            f"Customer wins: {case_studies}\n"
            f"Company context: {company_context}\n\n"
            "Follow Iannarino's proposal structure. Calculate realistic ROI. "
            "Return a JSON object with executive_summary, situation_analysis, proposed_solution, "
            "roi_model {annual_cost_usd, estimated_annual_benefit_usd, payback_months, roi_percentage, assumptions}, "
            "investment_table, implementation_timeline, risk_mitigation, next_steps (array), "
            "custom_sections (array of {heading, content})."
        )
        benefit = annual_cost_usd * 2.8
        stub = json.dumps({
            "executive_summary": (
                f"This proposal outlines how {product_name} will help {{company_name}} achieve "
                "[strategic outcome] by [specific date], delivering measurable ROI within [N] months."
            ),
            "situation_analysis": (
                "Based on our discovery conversations, {{company_name}} is facing [confirmed pain #1] "
                "and [confirmed pain #2], costing an estimated $[X] per year in [metric]."
            ),
            "proposed_solution": (
                f"You will have a fully operational {product_name} environment within [N] weeks, "
                "enabling [outcome #1] and [outcome #2] without [key friction]."
            ),
            "roi_model": {
                "annual_cost_usd": annual_cost_usd,
                "estimated_annual_benefit_usd": round(benefit, 2),
                "payback_months": round(12 / ((benefit - annual_cost_usd) / annual_cost_usd), 1),
                "roi_percentage": round(((benefit - annual_cost_usd) / annual_cost_usd) * 100, 1),
                "assumptions": [
                    "10% productivity gain across [N] team members",
                    "20% reduction in [metric] based on comparable customer data",
                    "Conservative 80% adoption rate in first 90 days",
                ],
            },
            "investment_table": (
                f"Annual subscription: ${annual_cost_usd:,.0f}\n"
                "Implementation & onboarding: Included\n"
                "Dedicated customer success: Included\n"
                f"Total Year 1: ${annual_cost_usd:,.0f}"
            ),
            "implementation_timeline": (
                "Week 1–2: Technical setup and data migration\n"
                "Week 3: Admin training and workflow configuration\n"
                "Week 4: Team onboarding and go-live\n"
                "Day 90: Business review and optimization session"
            ),
            "risk_mitigation": (
                "1. Change management: Dedicated CSM for 90-day onboarding.\n"
                "2. Data security: SOC2 Type II certified; your data never leaves [region].\n"
                "3. ROI risk: 30-day money-back guarantee if [specific outcome] not achieved."
            ),
            "next_steps": [
                "Review this proposal with your team by [date]",
                "Schedule a 30-min Q&A call with our technical team",
                f"Sign and return by [date] to secure [implementation slot]",
            ],
            "custom_sections": [],
        })
        return _call_agent(self._agent, prompt, stub)


@dataclass
class CloserAgent:
    """AE: develops closing strategies and objection handlers.

    Grounded in Zig Ziglar's closing techniques and Jeb Blount's Sales EQ.
    """

    role: str = "Closer (AE)"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_CLOSER_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def develop_strategy(self, prospect_json: str, proposal_json: str,
                         product_name: str, value_proposition: str) -> str:
        prompt = (
            f"Develop a closing strategy for:\nProspect: {prospect_json}\n"
            f"Proposal context: {proposal_json}\n\n"
            f"Product: {product_name}\n"
            f"Value proposition: {value_proposition}\n\n"
            "Select the most appropriate Zig Ziglar closing technique for this prospect, "
            "write the close script, prepare objection handlers (with Feel/Felt/Found), "
            "identify a legitimate urgency lever, and define walk-away criteria. "
            "Return a JSON object with recommended_close_technique, close_script, "
            "objection_handlers (array of {objection, response, feel_felt_found}), "
            "urgency_framing, walk_away_criteria, emotional_intelligence_notes."
        )
        stub = json.dumps({
            "recommended_close_technique": "summary",
            "close_script": (
                "So we've agreed that [pain #1] is costing you [metric], "
                "and [pain #2] is blocking [outcome]. "
                f"{product_name} solves both, and you'll see ROI within [N] months. "
                "Shall we get the paperwork started so you can hit [Q goal]?"
            ),
            "objection_handlers": [
                {
                    "objection": "The price is too high.",
                    "response": (
                        "I understand — and I want to make sure this makes sense for you financially. "
                        "The ROI model shows a [N]-month payback. "
                        "Is the concern the absolute cost, or the timing of the spend?"
                    ),
                    "feel_felt_found": (
                        "I understand how you feel — many of our customers felt the same way. "
                        "What they found was that after 90 days the ROI more than justified the investment."
                    ),
                },
                {
                    "objection": "We need to think about it.",
                    "response": (
                        "Of course — what specifically would you like to think through? "
                        "I want to make sure you have everything you need to make a confident decision."
                    ),
                    "feel_felt_found": None,
                },
            ],
            "urgency_framing": (
                "Implementation slots are currently booking [N] weeks out. "
                "To hit your [Q] deadline, we'd need to sign by [date]. "
                "I can hold your slot until [date + 3 days] — no pressure, but I wanted you to know."
            ),
            "walk_away_criteria": (
                "If budget is genuinely unavailable for 6+ months OR the prospect repeatedly "
                "avoids scheduling next steps after 3 follow-up attempts, "
                "politely disengage and flag for nurture re-entry in 90 days."
            ),
            "emotional_intelligence_notes": (
                "This buyer appears analytical — lead with data before emotion. "
                "Validate their thoroughness: 'It makes sense that you want to be thorough — "
                "this is a significant decision.' Mirror their deliberate pace; "
                "rushing this buyer will lose the deal."
            ),
        })
        return _call_agent(self._agent, prompt, stub)


@dataclass
class SalesCoachAgent:
    """Sales Manager: reviews the pipeline and provides Gong-style coaching.

    Grounded in Gong Labs research, pipeline velocity metrics, and Iannarino's coaching framework.
    """

    role: str = "Sales Coach (Manager)"
    _agent: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = _build_strands_agent(_COACH_SYSTEM_PROMPT, _DEFAULT_TOOLS)

    def review(self, prospects_json: str, product_name: str, pipeline_context: str) -> str:
        prompt = (
            f"Review this sales pipeline for {product_name}:\n{prospects_json}\n\n"
            f"Additional pipeline context: {pipeline_context or 'None provided'}\n\n"
            "Identify deal risk signals (using Gong Labs framework), provide talk/listen ratio advice, "
            "velocity insights, forecast categorization, top priority deals, and specific next actions. "
            "Return a JSON object with prospects_reviewed, deal_risk_signals (array of {signal, severity, recommended_action}), "
            "talk_listen_ratio_advice, velocity_insights, forecast_category, "
            "top_priority_deals (array), recommended_next_actions (array), coaching_summary."
        )
        stub = json.dumps({
            "prospects_reviewed": 1,
            "deal_risk_signals": [
                {
                    "signal": "Single-threaded — only one contact engaged",
                    "severity": "high",
                    "recommended_action": (
                        "Request an intro to the Economic Buyer within the next call. "
                        "Use: 'Who else on your team would need to be involved in a decision like this?'"
                    ),
                },
                {
                    "signal": "No confirmed next step on calendar",
                    "severity": "medium",
                    "recommended_action": (
                        "Do not end any call without a specific next-step booked. "
                        "Use calendar link in outreach footer."
                    ),
                },
            ],
            "talk_listen_ratio_advice": (
                "On discovery calls, aim for 43% talk / 57% listen. "
                "Ask SPIN questions in clusters of 2, then pause and let silence work for you."
            ),
            "velocity_insights": (
                "Average stage duration in this pipeline appears longer than benchmark (14 days in qualification). "
                "Recommend qualifying or disqualifying within 7 days by applying hard BANT questions in call #2."
            ),
            "forecast_category": "pipeline",
            "top_priority_deals": ["Acme Corp"],
            "recommended_next_actions": [
                "Multi-thread Acme Corp — request intro to VP Finance by EOW",
                "Send Acme Corp ROI model from proposal before next call",
                "Set a 5-day follow-up reminder for any prospect with no activity",
            ],
            "coaching_summary": (
                "Pipeline is early-stage and needs multi-threading. "
                "Primary risk is single-threaded deals with no Economic Buyer identified. "
                "Focus this week on advancing qualification conversations and securing EB meetings."
            ),
        })
        return _call_agent(self._agent, prompt, stub)
