# Feature Spec — Dossier-Driven Outreach

**Status:** Proposed
**Author:** Sales team — principal review
**Slug:** `dossier_driven_outreach`
**Related:** commit `24b3e6d` (deep-research prospecting — top-100 prospects + per-prospect dossiers)

---

## 1. Problem

Commit `24b3e6d` shipped a high-quality research capability: `DossierBuilderAgent`
produces a rich `ProspectDossier` per prospect (executive summary, publications,
trigger events, conversation hooks, mutual connections, stated beliefs, sources,
confidence). Dossiers are persisted in Postgres via
[`DossierStore`](../dossier_store.py).

**None of that research reaches the outreach copy.**

The outreach loop at [`orchestrator.py:523`](../orchestrator.py) calls:

```python
self.outreach.generate_sequence(
    p.model_dump_json(indent=2),   # bare Prospect, not dossier
    product, vp, cases, ctx,       # same strings for every prospect
    insights_ctx,                  # learning block
)
```

The `OutreachAgent.generate_sequence` signature at
[`agents.py`](../agents.py) has no `dossier` parameter. The
`OUTREACH_SYSTEM_PROMPT` at [`prompts/outreach.py`](../prompts/outreach.py) tells the model to
"lead with *their* world" while giving it nothing of their world. The model
either invents personalization (bad) or falls back to generic value-prop prose
(also bad).

`ProspectDossier.confidence` is computed and persisted but never read
downstream — low-confidence research is silently treated as authoritative.

## 2. Goal

Make the dossier a required input to `OutreachAgent` and produce cold outreach
that is:

1. **Grounded** — every personalization claim in the opener maps back to a
   specific dossier field with a source URL carried through for human review.
2. **Gated by confidence** — dossiers with `confidence < 0.6` drop to a
   company-level opener instead of a person-level one; no fabricated intimacy.
3. **Multi-variant** — each prospect produces three angle variants so
   `LearningEngine` has signal to attribute outcomes to angle type, not just
   to subject-line text.
4. **Auditable** — every `OutreachVariant` carries a `personalization_grade`
   and every `EmailTouch` carries `evidence_citations[]` so a human reviewing
   100 emails can trust or reject each one in seconds.

### Non-goals

- Pain-mapping layer — tracked separately (Gap #2). This spec consumes only
  what is already in the dossier.
- Brand voice enforcement — tracked separately (Gap #3).
- Changing the 6-touch cadence structure.
- Changing Discovery / Proposal / Qualifier agents — follow-ups once this
  pattern is proven on Outreach.
- Persisting `OutreachSequence` (remains transient).

## 3. Design

### 3.1 Contract change: `OutreachAgent.generate_sequence`

```python
# backend/agents/sales_team/agents.py
def generate_sequence(
    self,
    prospect_json: str,
    dossier: ProspectDossier,                         # required
    product_name: str,
    value_proposition: str,
    case_studies: str,
    company_context: str,
    insights_context: Optional[str] = None,
    variant_count: int = 3,
) -> str:
```

`dossier` is required. Callers without a dossier must build one (via
`DossierBuilderAgent`) or the prospect is skipped by the orchestrator with a
`sales.outreach.dossier_missing` log line.

### 3.2 Model changes — `models.py`

Add new types and replace `OutreachSequence`. No compatibility shims.

```python
PersonalizationGrade = Literal["high", "medium", "low", "fallback"]
OutreachAngle = Literal[
    "trigger_event",
    "thought_leadership",
    "mutual_connection",
    "peer_proof",
    "company_soft_opener",   # used when dossier.confidence < threshold
]


class EvidenceCitation(BaseModel):
    """Dossier-rooted evidence backing a personalization claim."""
    claim: str                     # "I saw your talk on distributed tracing at QCon"
    dossier_field: str             # "publications[2]"
    source_url: Optional[str]      # URL from the dossier; None for summary-level claims
    strength: Literal["weak", "medium", "strong"] = "medium"


class EmailTouch(BaseModel):
    day: int
    subject_line: str
    body: str
    personalization_tokens: List[str] = Field(default_factory=list)
    call_to_action: str = ""
    evidence_citations: List[EvidenceCitation] = Field(default_factory=list)


class OutreachVariant(BaseModel):
    """One angle choice for a prospect's outreach sequence."""
    angle: OutreachAngle
    email_sequence: List[EmailTouch]
    call_script: str = ""
    linkedin_message: str = ""
    rationale: str = ""
    personalization_grade: PersonalizationGrade = "medium"


class OutreachSequence(BaseModel):
    prospect: Prospect
    dossier_id: str
    dossier_confidence: float = Field(..., ge=0.0, le=1.0)
    variants: List[OutreachVariant]
```

There are no top-level `email_sequence` / `call_script` / `linkedin_message` /
`sequence_rationale` fields. Consumers read `sequence.variants[i]` directly.

### 3.3 Prompt rewrite — `prompts/outreach.py::SYSTEM_PROMPT`

Three additions on top of the existing Salesfolk / SNAP / Jeb Blount framework.

**A. Personalization Contract (hard rules):**

```
## Personalization Contract (hard rules — violations invalidate the variant)
1. The Day-1 opener sentence MUST cite at least ONE specific item from:
     - dossier.publications[]      (name the title + venue)
     - dossier.recent_activity[]   (name the event + rough date)
     - dossier.trigger_events[]    (name the trigger + implication)
     - dossier.mutual_connection_angles[] (name the shared entity)
2. You may NOT cite a detail that is not in the dossier. Do not infer from
   prospect.company name alone. If the dossier is empty or its
   `confidence` < 0.6, the ONLY allowed angle is company_soft_opener.
3. For every cited detail, emit an entry in `evidence_citations` identifying
   the dossier field path and (where present) the source_url from
   dossier.sources.
4. If you cannot meet rules 1–3 for an angle, output the company_soft_opener
   template and set `personalization_grade = "fallback"` — do NOT fake intimacy.
```

**B. Angle Selection:**

```
## Angle Selection
- trigger_event      — recent funding, reorg, leadership change, product launch
- thought_leadership — the prospect has published / spoken on a topic the
                       product touches
- mutual_connection  — shared employer / school / open-source project
- peer_proof         — a named customer the prospect will recognize (use
                       case_studies)
- company_soft_opener — company-level trigger only; used when
                        dossier.confidence < 0.6
```

**C. Variants:**

```
## Variants
Produce N variants (N given by caller). Each variant MUST use a DIFFERENT
angle — never repeat an angle across variants. Rank by expected reply rate
in each variant's rationale.
```

**Output schema:**

```json
{
  "variants": [
    {
      "angle": "trigger_event",
      "email_sequence": [
        {
          "day": 1, "subject_line": "...", "body": "...",
          "personalization_tokens": [...], "call_to_action": "...",
          "evidence_citations": [
            {"claim": "...", "dossier_field": "trigger_events[0]",
             "source_url": "https://...", "strength": "strong"}
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
```

### 3.4 Dossier rendering helper

`_render_dossier_for_prompt(dossier: ProspectDossier) -> str` in `agents.py`
produces a compact Markdown block (target ≤ 1,200 tokens). Truncates long
lists to top-5. Single deterministic point where dossier shape meets prompt
format — easy to unit-test.

### 3.5 Orchestrator wiring

In `SalesPodOrchestrator.run()` at [`orchestrator.py:519`](../orchestrator.py):

1. Before the outreach loop, call `DossierStore.get_dossiers_by_prospect_ids`
   (new batch method) for all current prospect ids.
2. For each prospect:
   - If no dossier found → log `sales.outreach.dossier_missing` and skip.
   - Otherwise pass `dossier=dossier_map[p.id]` and
     `variant_count=self._variant_count` (default 3) into
     `generate_sequence`.
3. `_outreach_from_json` parses `variants[]`, applies the confidence gate
   (§3.6) and the citation verifier (§3.6), and builds `OutreachSequence`.

In `deep_research_only()` at [`orchestrator.py:819`](../orchestrator.py)
dossiers are already in hand — pass them directly, no DB round-trip.

`outreach_only(...)` gains a required `dossier_map: dict[str, ProspectDossier]`
parameter (keyed by `prospect.id`). Prospects missing from the map are
skipped with the same log line.

### 3.6 Confidence gate + citation verifier (post-parse)

Applied in `_outreach_from_json` before the `OutreachSequence` is returned:

1. **Citation verifier.** For every `evidence_citation.source_url`:
   if the URL is non-null and not in `dossier.sources`, drop the citation,
   log `sales.outreach.citation_unverified`, and downgrade the variant's
   `personalization_grade` to `"low"`.
2. **Confidence gate.** If `dossier.confidence < _PERSONALIZATION_CONFIDENCE_THRESHOLD`
   (constant, 0.6) and a variant's angle is not `company_soft_opener`,
   replace it: emit a single `company_soft_opener` variant with
   `personalization_grade = "fallback"` and log
   `sales.outreach.confidence_override`.
3. A variant whose Day-1 email has zero `evidence_citations` and whose
   angle is not `company_soft_opener` is also forced to `"fallback"` —
   this catches models that ignore the soft prompt rule.

### 3.7 Learning engine feedback

Add a nullable `angle` column to `sales_outcomes`. `OutcomeStore.record_outcome`
accepts an `angle: OutreachAngle | None` kwarg. `LearningEngine.analyze`
gains per-angle reply/win statistics. The formatted insights block surfaces
angle-mix guidance to the outreach agent on the next run.

## 4. API surface

No new endpoints. `GET/POST /api/sales/pipeline` response's
`outreach_sequences[]` payload uses the new `OutreachSequence` shape.

## 5. Data model changes

1. **`sales_outcomes` table** — add nullable `angle TEXT` column via a new
   migration in `postgres/`.
2. **`sales_dossiers` table** — no schema change. New query helper
   `DossierStore.get_dossiers_by_prospect_ids`.
3. **No new tables.** `OutreachSequence` remains transient.

## 6. Test plan

Unit tests live in [`tests/`](../tests). Additions:

1. **`test_dossier_rendering.py`** — `_render_dossier_for_prompt` produces
   stable output; truncates long lists; omits empty sections; preserves URLs.
2. **`test_outreach_contract.py`** (uses fixture LLM responses):
   - High-confidence dossier → all variants have `personalization_grade`
     in {"high", "medium"} and at least one `evidence_citation` on Day 1.
   - `confidence = 0.4` → variant list collapses to a single
     `company_soft_opener` with grade `"fallback"`.
   - Model output missing `evidence_citations` → variant forced to
     `"fallback"`.
   - Model cites a URL not in `dossier.sources` → citation stripped,
     variant downgraded to `"low"`, warning logged.
3. **`test_orchestrator_outreach_wiring.py`:**
   - Prospects with dossiers → `generate_sequence` called with `dossier=`.
   - Prospects without dossiers → skipped, warning logged, not in
     `outreach_sequences[]`.
   - Batch `get_dossiers_by_prospect_ids` is called once, not N times.

Coverage target: ≥ 85 % lines for the new helper + wiring.

## 7. Observability

Structured log lines (all under `sales.outreach.*`):

| Event | Fields |
|---|---|
| `sales.outreach.generated` | prospect_id, dossier_id, variants_count, angles[], grades[], confidence |
| `sales.outreach.confidence_override` | prospect_id, dossier_id, confidence, original_angle |
| `sales.outreach.citation_unverified` | prospect_id, dossier_id, url_claimed |
| `sales.outreach.dossier_missing` | prospect_id |

## 8. Rollout

Single coherent change set:

- `models.py` — new types, new `OutreachSequence` shape.
- `agents.py` — prompt rewrite, required `dossier` parameter, renderer,
  threshold constant.
- `dossier_store.py` — batch fetch helper.
- `orchestrator.py` — parser, confidence gate, citation verifier, dossier
  lookup in `run()` and `outreach_only`.
- `postgres/` — migration for nullable `angle` column on `sales_outcomes`.
- `outcome_store.py` + `learning_engine.py` — record and surface angle.
- `__init__.py` — export new types.
- UI (`sales-team.model.ts`, `sales-pipeline-results.component.html`) —
  consume `variants[]`.
- Tests updated in the same change set.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Prompt grows too large for thick dossiers. | `_render_dossier_for_prompt` truncates lists to top-5, budgets ≤ 1,200 tokens. |
| Model fabricates a `source_url` not in `dossier.sources`. | Post-parse citation verifier strips the citation, downgrades grade, logs warning. |
| Confidence threshold (0.6) is wrong. | Single constant in `agents.py`; revisit once ≥ 200 outcomes have recorded angles. |
| Variants inflate LLM cost 3×. | Cost of the deep-research path is already dominated by dossier web-fetches; marginal outreach cost is small. `variant_count` is a parameter if the need arises to dial it down. |
| `run()` path has prospects without dossiers today. | They are skipped with a clear log line; the deep-research path builds dossiers first and is the supported entry point for dossier-driven outreach. |

## 10. Success criteria

Measured after 2 weeks of production traffic:

- ≥ 70 % of outreach generated for `confidence ≥ 0.6` dossiers lands
  `personalization_grade ∈ {"high", "medium"}`.
- ≥ 95 % of `evidence_citations` URLs are present in `dossier.sources`.
- `sales.outreach.citation_unverified` < 2 % of generated emails.
- Per-angle reply-rate data is queryable via `LearningEngine.analyze`.

## 11. Follow-ups

- **Gap #2 — `PainMapperAgent`.** Adds a `pain_hypothesis` angle to the
  enum; extends the Personalization Contract to cite inferred pain.
- **Gap #3 — `BrandProductProfile`.** Adds a brand block to the same prompt
  and a `BrandVoiceQA` post-processor.
- **Discovery + Proposal.** Apply the §3.3 pattern to both agents' prompts
  and signatures once this proves out.

## 12. Open questions

1. Should `OutreachSequence` be persisted? Flagged for PM; out of scope here.
2. Should citations to dossier-internal claims (e.g., `executive_summary`)
   require a `source_url`, or is `None` acceptable? Current spec allows
   `None`; revisit if it becomes a hallucination loophole.
3. `variant_count` cap: the parameter accepts any value, but the prompt
   restricts to the five enum angles — so effective max is 5.
