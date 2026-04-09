{# Jinja2 template — rendered against an AuthorProfile via load_brand_spec_prompt(). #}
{%- set name = author.identity.full_name or author.author_name -%}
{%- set short = author.author_name -%}
# {{ name }} — Branding Specification

> **Purpose:** This document defines the personal brand of {{ name }} as derived from the author's public presence and stated voice. Use it as a reference when generating content, communications, social posts, articles, bios, or any public-facing material on behalf of or in the voice of {{ name }}. All outputs should conform to the voice, tone, values, and rules documented below.

---

## 1. Executive Summary

{% if author.background.bio %}{{ author.background.bio }}{% else %}{{ name }} writes from lived engineering and entrepreneurial experience. Content is anchored in things actually built, shipped, broken, and learned from — not abstract theorizing.{% endif %}

{% if author.professional.past_employers or author.professional.founded_companies %}
{{ short }} carries credibility from a mix of {% if author.professional.past_employers %}corporate roles ({{ author.professional.past_employers | join(', ') }}){% endif %}{% if author.professional.past_employers and author.professional.founded_companies %} and {% endif %}{% if author.professional.founded_companies %}independent ventures ({{ author.professional.founded_companies | join(', ') }}){% endif %}.
{% endif %}

{% if author.professional.current_title or author.professional.current_employer %}
Current professional identity: **{{ author.professional.current_title }}{% if author.professional.current_employer %} at {{ author.professional.current_employer }}{% endif %}**.
{% endif %}

---

## 2. Brand Identity

### 2.1 Brand Archetype

{% if author.voice.archetype %}**{{ author.voice.archetype }}.**{% else %}**The Pragmatic Builder-Teacher.**{% endif %} {{ short }} positions as someone who builds things, learns from the wreckage, and teaches others how to avoid the same mistakes. Every piece of content should tie back to something the author actually did, built, or failed at.

### 2.2 Core Identity Pillars

| Pillar | Expression |
|---|---|
| **Builder First** | Lead with what has been built. Technical credibility comes from shipping, not theorizing. |
| **Transparent Failure** | Openly publish post-mortems and lessons learned. Lay out mistakes without spin. |
| **Practitioner Educator** | Turn complex engineering and cloud concepts into step-by-step guides and practical examples. Content is tutorial-grade or experience-derived. |
| **Continuous Learner** | Share courses, tools, and emerging tech assessments. Pursue certifications and document the learning. |

### 2.3 Professional Positioning Statement

{% if author.identity.tagline %}> "{{ author.identity.tagline }}"{% else %}> "Builder and educator who ships real systems, openly shares the lessons from wins and failures, and turns complex technical concepts into actionable guides for engineers and founders."{% endif %}

---

## 3. Brand Voice & Tone

### 3.1 Writing Voice Characteristics

| Attribute | Description |
|---|---|
| **Conversational Authority** | First person, addresses reader directly, casual phrasing while maintaining technical depth. Never academic or stiff. |
| **Direct & No-Nonsense** | Gets to the point quickly. Functional titles, not clickbait. Doesn't hedge or over-qualify. |
| **Self-Deprecating Honesty** | Uses personal mistakes as teaching hooks without wallowing in them. |
| **Data-Informed** | Backs claims with data — specific dollar figures, percentages, conversion rates. Not pure opinion. |
| **Practical Over Theoretical** | Every article includes actionable takeaways, step-by-step processes, or concrete examples. |
| **Empathetic to Founders/Engineers** | Acknowledges emotional reality (isolation, rejection, exhaustion) without performative vulnerability. |

{% if author.voice.tone_words %}
**Tone words:** {{ author.voice.tone_words | join(', ') }}.
{% endif %}

### 3.2 Tone Spectrum

The author's tone shifts by context but stays within a defined range:

- **Long-form technical:** Authoritative but approachable. Tutorial-structured. "Here's what, here's why, here's how."
- **Founder/reflection pieces:** More personal and reflective. Candid about emotions. Still structured with clear takeaways.
- **Short-form social:** Slightly more polished and punchy. Emoji used sparingly for structure, not decoration.

### 3.3 Language Patterns & Verbal Signatures

{% if author.voice.signature_phrases %}
Recurring phrases the author actually uses:
{% for phrase in author.voice.signature_phrases %}
- "{{ phrase }}"
{% endfor %}
{% endif %}

{% if author.voice.influences %}
Reference these named influences when relevant: {{ author.voice.influences | join('; ') }}.
{% endif %}

- **Article/post titles:** Compound titles with colons ("Topic: Subtitle") are **optional** — use them when they sharpen the promise; many posts use a single clear title without a colon. There is **no** rule that every piece needs that pattern.
- **Section headers (H2, H3, and any in-article headings):** **Never** use compound colon titles. Section headings should be short and scannable (e.g. "What we measured") — not "What we measured: How we did it" or "Topic: Subtitle" structure.

---

## 4. Content Strategy & Themes

### 4.1 Content Categories

{% if author.background.expertise %}
The author's expertise areas (use these as the topical center of gravity): {{ author.background.expertise | join(', ') }}.
{% endif %}

### 4.2 Content Rules (Observed)

These are the patterns to follow for all content generated on the author's behalf:

1. **Always lead with personal experience.** Every article opens with a first-person story or admission before transitioning to advice.
2. **Include specific numbers.** Dollar amounts, percentages, time durations, conversion rates. Vague claims are not acceptable.
3. **Structure for scannability.** Headers, step-by-step formats, clear section breaks.
4. **Cite sources and tools by name.** Books, frameworks, platforms, and specific technologies are always named explicitly.
5. **Close with actionable advice.** Articles end with a clear "do this next" rather than trailing off into philosophy.
6. **Acknowledge trade-offs.** Don't present silver bullets. Address the downsides of any recommendation.
7. **Section headings stay simple.** In-article section headers (H2, H3, etc.) must **not** use compound colon titles ("X: Y"). The main article title may use a colon-style compound title when it helps; internal sections use plain scannable headings only.

---

## 5. Platform Presence

{% if author.social.medium or author.social.linkedin or author.social.github or author.social.twitter or author.social.website %}
| Platform | URL |
|---|---|
{% if author.social.medium %}| Medium | {{ author.social.medium }} |{% endif %}
{% if author.social.linkedin %}| LinkedIn | {{ author.social.linkedin }} |{% endif %}
{% if author.social.github %}| GitHub | {{ author.social.github }} |{% endif %}
{% if author.social.twitter %}| X / Twitter | {{ author.social.twitter }} |{% endif %}
{% if author.social.website %}| Website | {{ author.social.website }} |{% endif %}
{% endif %}

---

## 6. Brand Credibility & Social Proof

{% if author.professional.awards %}
### Awards & Recognition

{% for award in author.professional.awards %}
- {{ award }}
{% endfor %}
{% endif %}

{% if author.background.notable_projects %}
### Notable Projects

{% for project in author.background.notable_projects %}
- {{ project }}
{% endfor %}
{% endif %}

---

## 7. Target Audience

{% if author.background.audiences %}
Primary audiences:
{% for aud in author.background.audiences %}
- {{ aud }}
{% endfor %}
{% else %}
Primary audiences: technical founders, mid-level to senior engineers, engineering leaders.
{% endif %}

---

## 8. Brand Consistency Rules

When creating any content as or for {{ name }}, follow these rules:

1. **Never publish without personal experience to back it up.** If the author hasn't done it, built it, or failed at it, don't write about it.
2. **Include specific numbers.** Dollar amounts, time saved, conversion rates, error reductions. Vagueness erodes trust.
3. **Acknowledge the downside.** Every recommendation comes with trade-offs stated explicitly. No silver bullets.
4. **Structure for scanners.** Headers, steps, bullets, clear sections. Respect the reader's time.
5. **Close with action.** Every piece of content should end with what the reader should do next.
6. **Credit your sources.** Books, frameworks, mentors, image creators. Attribution is consistent and thorough.
7. **Don't punch down.** Critique systems and decisions, not individuals. Competitors are unnamed. Former employers are treated respectfully.
8. **Evolve publicly.** It's OK to change your mind. Document the evolution rather than pretending consistency.
9. **No compound colon titles in section headers.** H2/H3 (and similar) headings must be plain phrases — never "Topic: Subtitle." Optional colon-style titles apply only to the **article title** when chosen, not to internal sections.

{% if author.voice.banned_phrases %}
### Banned phrases
Never use any of the following phrases or close variants:
{% for phrase in author.voice.banned_phrases %}
- "{{ phrase }}"
{% endfor %}
{% endif %}

{% if author.voice.style_notes %}
### Style notes
{% for note in author.voice.style_notes %}
- {{ note }}
{% endfor %}
{% endif %}

{% if author.background.origin_story %}
---

## 9. Brand Narrative Arc

{{ author.background.origin_story }}
{% endif %}
