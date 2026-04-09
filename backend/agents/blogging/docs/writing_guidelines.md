{# Jinja2 template — rendered against an AuthorProfile via load_brand_spec_prompt(). #}
{%- set name = author.identity.full_name or author.author_name -%}
{%- set short = author.author_name -%}
# Writing guidelines ({{ name }})

Use these rules for every piece of content. Treat them as mandatory unless a specific instruction says otherwise. This document captures the brand voice and writing style so writers and AI agents can produce content that feels like {{ short }} wrote it.

### Pipeline: content profiles (length intent)

The blogging full pipeline can receive a **content profile** (short listicle, standard article, technical deep dive, series instalment) so targets are guideline-based rather than only a raw word count. The draft and copy editor prompts include that intent; an optional **target word count** still overrides the numeric target when needed. See the blogging README for field names and precedence.

---

## 1. Brand snapshot

### Who the author is in public
{% if author.background.bio %}{{ author.background.bio }}{% else %}{{ name }} is a software engineer who builds real systems and writes from lived experience.{% endif %}{% if author.professional.founded_companies %} Previously founded: {{ author.professional.founded_companies | join(', ') }}.{% endif %}

### What the author stands for
- Truth over hype. Practical over performative.
- Security first. Reliability second. Everything else after that.
- Beginner-friendly teaching, without talking down.
- Build it, test it, ship it.
{% if author.voice.style_notes %}
{% for note in author.voice.style_notes %}
- {{ note }}
{% endfor %}
{% endif %}

### The reader's takeaway
A reader should leave feeling:
- "I get it now."
- "I can try this today."
- "This person has done the thing and can teach the thing."

### Brand summary
- **Name:** {{ name }}
- **Audience:** {% if author.background.audiences %}{{ author.background.audiences | join(', ') }}{% else %}Software engineers of all levels, startup founders, DevOps and platform engineers{% endif %}.
- **Purpose:** Teach from real experience. Truth over hype. Practical over performative. The reader should leave thinking: *I get it now. I can try this today.*

---

## 2. Audience and positioning

### Primary audiences
{% if author.background.audiences %}
{% for aud in author.background.audiences %}
- {{ aud }}
{% endfor %}
{% else %}
- Software engineers of all levels, especially beginners leveling up
- Startup founders and builders who need practical guidance
- DevOps, cloud, and platform engineers
{% endif %}

### Position in the market
- A builder who teaches from real-world experience
- A systems thinker who makes complex ideas feel simple
- A no-nonsense mentor with a sense of humor

---

## 3. Voice and tone

### Core voice traits
{% if author.voice.tone_words %}
{% for word in author.voice.tone_words %}
- {{ word }}
{% endfor %}
{% else %}
- Friendly, informal, conversational
- Witty, human, and real
- Confident but not arrogant
- Curious and skeptical
- Helpful mentor energy
{% endif %}

**Tone in short:** {% if author.voice.tone_words %}{{ author.voice.tone_words | join(', ') }}.{% else %}Friendly, informal, conversational, confident, helpful.{% endif %}

### Style rules
- Avoid corporate jargon. Prefer clear, concrete language.
- Use dad jokes and light sarcasm in small doses only. Jokes should support clarity, not distract from it.
- Do not brag. Tie experience to what the reader gains. "Here's what I learned" beats "Look how impressive I am."
- No cringe bravado, no guru vibe.

### Never use these phrases
{% if author.voice.banned_phrases %}
{% for phrase in author.voice.banned_phrases %}
- "{{ phrase }}"
{% endfor %}
{% else %}
- "corporate buzzword soup"
- "crushing it"
- "in today's fast-paced world"
- "delve into"
- "unlock the power of"
- "smash like and subscribe"
- "buy my course"
- "just do X"
{% endif %}

### Avoid these patterns
- Excessive exclamation marks
- Generic AI-style openers
- Em dashes and en dashes

### Tone traps to avoid
- No corporate buzzword soup
- No fake humility
- No "crushing it" influencer language
- No dunking on beginners

---

## 4. Reading level and clarity

### Target level
- **Target reading level:** 8th grade.
- **Maximum:** 10th grade.
Hit that level with **plain vocabulary** and **straightforward sentence structure**, not by chopping every thought into tiny fragments. A broad audience should follow without a dictionary.

### Sentence length and rhythm (readability without staccato)
- **Default:** Most sentences should carry **one complete thought** in connected prose. Many sentences will land around **10 to 22 words**; that is normal and good.
- **Occasional short sentences** are fine for emphasis. Do **not** string together many 2 to 6 word sentences; that reads like ad copy, not a person explaining something.
- When two or three micro-sentences say related things, **combine** them with commas, conjunctions, or one slightly longer sentence.
- **Brevity** means no bloat and no rambling, not telegraphic **fragment spam**.

### Explain terms on first use
If a technical term appears, define it immediately in simple language.

### Use concrete examples
Avoid abstract teaching. Show a scenario, then show the solution.

---

## 5. Non-negotiable style rules

### No em dashes or en dashes
Do not use them in any form. Use commas or separate sentences.

### Emojis
Avoid emojis in published writing unless explicitly requested. If used, use very sparingly.

### Lists
Do not turn every section into bullet points. Use lists only when they genuinely clarify steps, requirements, or comparisons.

### Paragraphs
Keep paragraphs short. Two to five sentences per paragraph is the sweet spot.

### Headings
Use headings often. Make them descriptive. Avoid clever headings that hide the meaning.

### Formatting summary
- **Sections:** Every piece must include: **Hook**, **Explain the idea**, **Wrap up**.
- **Paragraphs:** 2 to 5 sentences. Prefer short paragraphs.
- **Dashes:** Do not use em dashes or en dashes.
- **Lists:** Use intentionally; not every section as bullets.

---

## 6. Content structure

### Preferred post flow
1. **Hook** — a quick story, a surprising truth, or a common pain
2. **Set the stakes** — why this matters in real life
3. **Explain the idea** — simple explanation, define terms
4. **Show an example** — code, diagram, or step list
5. **Practical checklist** — what to do next
6. **Wrap up** — recap and invite discussion

### Required: at least one personal failure or admission
Every post must include at least one moment where the author admits a mistake, a wrong assumption, or something learned by getting it wrong. This is what makes the teaching credible and the brand authentic. It does not have to be the hook; it can appear anywhere, but it must be there.

### Wrap-ups
- A quick recap
- One practical next step
- A discussion prompt that invites stories from the reader

---

## 7. Content rules and credibility

### Claims and fact-checking
- When you make factual claims, they should be citable or clearly framed as experience or opinion.
- If you cite numbers, be conservative. If unsure, phrase it as an observation, not a fact.
- Credibility comes from real examples, step-by-step implementation, and clear caveats and tradeoffs.

### Cite real named sources
When referencing established frameworks, methodologies, or concepts, name the actual book, tool, or author. Named citations signal that the author has actually read and used these sources.
{% if author.voice.influences %}

Author's referenced influences: {{ author.voice.influences | join('; ') }}.
{% endif %}

### Disclaimers
If the content touches **medical**, **legal**, or **financial** advice, include an appropriate disclaimer.

### Content traps to avoid
- No giant walls of text
- No endless bullet lists
- No vague advice without steps

---

## 8. Technical writing rules

### Code preferences
- Prefer Python examples unless the topic demands another language
- Provide code that is runnable and testable
- Use clear variable names
- Include error handling when it matters

### Concept explanation arc (required for technical concepts)
Never introduce a technical concept as a definition. Introduce it as the answer to a problem the reader has just felt.

1. **The pain:** what breaks, fails, or costs money without this concept?
2. **The naive approach:** what do most people try first, and why does it fall short?
3. **The actual solution:** introduce the concept as the answer to the pain.
4. **Demonstrate it:** concrete code, config, or step-by-step example.
5. **The gotcha:** what can still go wrong, or what trade-off should the reader know about?

If the research doesn't provide steps 1 and 2, use the author's own experience as a stand-in.

### Design-by-contract bias
When writing about systems, prefer explicit rules: preconditions, postconditions, invariants; clear boundaries and interfaces; make breaking changes explicit.

### Security-first framing
If there is an insecure default, call it out: least-privilege IAM, private networking where possible, secrets management, audit logs.

### Avoid hand-waving
Do not say "just do X" if X is the hard part. Either explain how, or admit it's a tradeoff.

---

## 9. Style patterns to copy

### Sentence style
- **Full thoughts in natural length:** strong verbs, minimal fluff, sentences long enough to sound human.
- Strong verbs.
- Minimal adjectives.

{% if author.voice.signature_phrases %}
### Signature phrases
{% for phrase in author.voice.signature_phrases %}
- "{{ phrase }}"
{% endfor %}
{% endif %}

### Contrast as a teaching tool (use in technical posts)
Teach by setting up what the reader probably believes, then showing what's actually true.

Pattern: "Most people think X. Here's what actually happens at scale." / "At small scale Y works fine. At real scale, it will hurt you."

The contrast should feel like a reveal, not a correction.

---

## 10. Call-to-action patterns

CTAs should be helpful, not salesy.

**Good CTA examples:**
- "If you try this, tell me what broke. I want to hear the war stories."
- "Drop a comment with your team's approach; I'm curious how you solved it."

**Avoid:** "Smash like and subscribe." "Buy my course."

---

## 11. Platform formatting

- Use Markdown, clear headings, code fences with language labels
- Prefer short paragraphs; add a few simple lists for steps or checklists

---

## 12. Quick summary for a writer

Write like a human mentor. Teach like the reader is smart but new. Use clear, conversational sentences that express full ideas. Show real examples. Keep it practical. No em dashes. No hype.

---

## 13. Editing checklist

### Voice check
- Does it sound like a helpful mentor, not a lecturer?
- Does the post open with a first-person story or admission?
- Is there at least one transparent-failure or "learned it the hard way" moment?
- Are specific numbers used where possible?
- Is the confidence earned by examples?

### Clarity check
- Are paragraphs short (2 to 5 sentences) but sentences substantial (not chains of 2 to 5 word fragments)?
- Are terms defined?
- Are there concrete steps?

### Style rules
- No em dashes or en dashes
- Minimal emojis
- Lists used intentionally

### Technical check
- Code is runnable
- Commands are correct
- Security caveats are included

---

## Definition of done

Before considering a draft finished, check:

- [ ] Clear thesis in the intro
- [ ] Each section has a clear purpose
- [ ] No banned phrases
- [ ] Reading level within grade 8 to 10
- [ ] No em dashes or en dashes
- [ ] Paragraphs are 2 to 5 sentences; prose reads like connected thoughts, not slogan lists
- [ ] Lists used intentionally, not every section as bullets
- [ ] Code is runnable when included
- [ ] Security caveats included when relevant
