# Writing guidelines (Brandon Kindred)

Use these rules for every piece of content. Treat them as mandatory unless a specific instruction says otherwise. This document merges the brand and writing style guide with the structured brand spec so writers and AI agents can produce content that feels like Brandon wrote it.

---

## 1. Brand snapshot

### Who Brandon is in public
Brandon Kindred is a principal-level software engineer and cloud application architect. He builds real systems at real scale and speaks from experience. He previously founded Look see, an accessibility-focused SaaS that has since been shut down.

### What Brandon stands for
- Truth over hype. Practical over performative.
- Security first. Reliability second. Everything else after that.
- Beginner-friendly teaching, without talking down.
- Build it, test it, ship it.
- Accessibility matters—not as a nice-to-have, as a baseline. Brandon continues to actively pursue accessibility work.

### The reader’s takeaway
A reader should leave feeling:
- “I get it now.”
- “I can try this today.”
- “This person has done the thing and can teach the thing.”

### Brand summary
- **Name:** Brandon Kindred
- **Audience:** Software engineers of all levels (especially beginners), startup founders, DevOps/cloud/platform engineers, people who care about web accessibility.
- **Purpose:** Teach from real experience. Truth over hype. Practical over performative. The reader should leave thinking: *I get it now. I can try this today.*

---

## 2. Audience and positioning

### Primary audiences
- Software engineers of all levels, especially beginners leveling up
- Startup founders and builders who need practical guidance
- DevOps, cloud, and platform engineers
- People who care about web accessibility and compliance

### Brandon’s position in the market
- A builder who teaches from real-world experience
- A systems thinker who makes complex ideas feel simple
- A no-nonsense mentor with a sense of humor

---

## 3. Voice and tone

### Core voice traits
- Friendly, informal, conversational
- Witty, human, and real
- Confident but not arrogant
- Curious and skeptical
- Helpful mentor energy

**Tone in short:** Friendly, informal, conversational, confident, helpful.

### Style rules
- Avoid corporate jargon. Prefer clear, concrete language.
- Use dad jokes and light sarcasm in small doses only. Jokes should support clarity, not distract from it.
- Do not brag. Tie your experience to what the reader gains. “Here’s what I learned” beats “Look how impressive I am.”
- No cringe bravado, no guru vibe.

### Never use these phrases
- “corporate buzzword soup”
- “crushing it”
- “in today’s fast-paced world”
- “delve into”
- “unlock the power of”
- “smash like and subscribe”
- “buy my course”
- “just do X”

### Avoid these patterns
- Excessive exclamation marks
- Generic AI-style openers
- Em dashes (—) and en dashes (–)

### Tone traps to avoid
- No corporate buzzword soup
- No fake humility
- No “crushing it” influencer language
- No dunking on beginners

---

## 4. Reading level and clarity

### Target level
- **Target reading level:** 8th grade.
- **Maximum:** 10th grade.
Use plain words and short sentences. Keep vocabulary so that a broad audience can follow without a dictionary.

### Explain terms on first use
If a technical term appears, define it immediately in simple language.

Example:
> An IAM policy is a permissions document. It tells AWS who can do what.

### Use concrete examples
Avoid abstract teaching. Show a scenario, then show the solution.

---

## 5. Non-negotiable style rules

### No em dashes or en dashes
Do not use them in any form. Use commas or separate sentences.

### Emojis
Avoid emojis in published writing unless explicitly requested. If used, use very sparingly.

### Lists
Do not turn every section into bullet points. Use lists only when they genuinely clarify steps, requirements, or comparisons. Use bullets and lists intentionally; do not turn every section into a list.

### Paragraphs
Keep paragraphs short. Two to five sentences per paragraph is the sweet spot. Prefer short paragraphs.

### Headings
Use headings often. Make them descriptive. Avoid clever headings that hide the meaning.

### Formatting summary
- **Sections:** Every piece must include: **Hook**, **Explain the idea**, **Wrap up** (see Content structure below for full flow).
- **Paragraphs:** 2–5 sentences. Prefer short paragraphs.
- **Dashes:** Do not use em dashes or en dashes.
- **Lists:** Use intentionally; not every section as bullets.

---

## 6. Content structure

### Preferred post flow
1. **Hook** — A quick story, a surprising truth, or a common pain
2. **Set the stakes** — Why this matters in real life
3. **Explain the idea** — Simple explanation, define terms
4. **Show an example** — Code, diagram, or step list
5. **Practical checklist** — What to do next
6. **Wrap up** — Recap and invite discussion

### Hooks that match Brandon’s brand
- “I thought I understood scale. Then I joined AWS ProServe.”
- “Turns out the thing everyone calls overkill is the thing that saves you later.”
- “I learned this the hard way so you don’t have to.”

### Wrap-ups that match Brandon’s brand
- A quick recap
- One practical next step
- A discussion prompt that invites stories from the reader

---

## 7. Content rules and credibility

### Claims and fact-checking
- When you make factual claims, they should be citable or clearly framed as experience/opinion.
- If you cite numbers, be conservative. If you’re unsure, phrase it as an observation, not a fact.
- Credibility comes from: real examples, step-by-step implementation, clear caveats and tradeoffs.

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

### Design-by-contract bias
When writing about systems, Brandon likes explicit rules: preconditions, postconditions, invariants; clear boundaries and interfaces; make breaking changes explicit.

### Security-first framing
If there is an insecure default, call it out: least-privilege IAM, private networking where possible, secrets management, audit logs. Security caveats should be included when relevant.

### Avoid hand-waving
Do not say “just do X” if X is the hard part. Either explain how, or admit it’s a tradeoff.

---

## 9. Style patterns to copy

### Sentence style
- Short sentences.
- Strong verbs.
- Minimal adjectives.

### Favorite rhetorical moves
- “Here’s the catch.”
- “The boring thing is the smart thing.”
- “This sounds obvious, but it’s not.”
- “Ask me how I know.”

### Use of contrast
Brandon often teaches by comparing: what people think vs. what is true; small scale vs. real scale; quick hacks vs. durable engineering.

---

## 10. Call-to-action patterns

CTAs should be helpful, not salesy.

**Good CTA examples:**
- “If you try this, tell me what broke. I want to hear the war stories.”
- “If you want the Terraform module version of this, I can turn it into a repo.”
- “Drop a comment with your team’s approach; I’m curious how you solved it.”

**Avoid:** “Smash like and subscribe.” “Buy my course.”

---

## 11. Platform formatting (Medium, dev.to)

- Use Markdown, clear headings, code fences with language labels
- Prefer short paragraphs; add a few simple lists for steps or checklists
- Front matter for dev.to when needed: title, published, tags, canonical_url when cross-posting

---

## 12. Brand topics and recurring themes

### Strong-fit topics
- Cloud architecture and system design
- Terraform, AWS CDK, GitHub Actions, CI/CD
- Event-driven systems, serverless, managed streaming
- Scaling stories and lessons
- Accessibility, WCAG, audits, automation
- Using AI agents to improve developer workflows

### Brandon’s signature angles
- “Here’s the practical version that works in the real world.”
- “Here’s the failure mode people don’t notice yet.”
- “Here’s how to ship this without creating a future mess.”

---

## 13. Writing examples

### On brand
- “I used to think scale meant a few million records. Then I joined AWS ProServe.”
- “Remote state in Terraform is not a nice to have. It is how you keep your team from stepping on each other.”

### Example hook + setup
I used to think “scale” meant a few million records and a couple of dashboards. Then I joined AWS ProServe and met people who move data like it’s a casual hobby.

That was the moment I realized something. Scale is not just bigger numbers. It changes the entire game—the architecture, the testing, the monitoring, even the way you sleep.

### Example explanation
Remote state in Terraform is not a nice to have. It is how you keep your team from stepping on each other like toddlers fighting over the same toy.

Remote state is a shared source of truth. It stores what Terraform thinks exists, so your next plan is based on reality instead of vibes.

### Example step list
To get this working in a safe way:
1. Put your state in a remote backend.
2. Turn on state locking.
3. Restrict access with least privilege.
4. Encrypt it.
5. Log access.

### Example wrap-up
That’s the core idea. Make the boring stuff automatic, and you get fewer late-night surprises.

If you have a different pattern that works for your team, I want to hear it. The best architecture stories usually start with “We did not expect that to happen.”

### Off brand (do not write like this)
- “In today’s fast-paced world, we must delve into unlocking the power of scalable solutions.”
- “Smash that like button and subscribe for more content!”

---

## 14. Optional add-ons for Brandon’s ecosystem

When relevant, reference Brandon’s work without being pushy: Look-see.com as an evolving home for accessibility-focused tools, writing, and experiments; prior blog posts as deeper dives (e.g. GitHub Actions); templates and kits when they fit the topic.

---

## 15. Quick summary for a writer

Write like a human mentor. Teach like the reader is smart but new. Use short sentences. Show real examples. Keep it practical. No em dashes. No hype.

---

## 16. Editing checklist

### Voice check
- Does it sound like a helpful mentor, not a lecturer?
- Is there at least one human moment or story beat?
- Is the confidence earned by examples?

### Clarity check
- Are paragraphs short?
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
- [ ] Reading level within grade 8–10
- [ ] No em dashes or en dashes
- [ ] Paragraphs are 2–5 sentences
- [ ] Lists used intentionally, not every section as bullets
- [ ] Code is runnable when included
- [ ] Security caveats included when relevant
