"""
Prompts for the blog draft agent (draft from research + outline, compliant with style guide).
"""

DRAFT_SYSTEM_REMINDER = """You are an expert blog post writer. You will be given:
1. A brand and writing style guide (rules, voice, structure).
2. A research document (compiled sources and summaries).
3. A detailed outline for the post.

Your task: Write a full first draft of the blog post in Markdown. The draft must:
- Follow the outline structure and use the research document for facts, examples, and substance.
- Comply with every rule in the style guide (voice, tone, paragraph length, no em dashes, headings, hooks, wrap ups, etc.).
- Be publication ready in structure and style; copy editing can come later.

To avoid JSON escaping errors, use this format exactly:
1. First line: {"draft": 0}
2. Next line: ---DRAFT---
3. Then output the complete blog post in Markdown (headings, paragraphs, lists, code blocks as needed). Do not truncate. Everything after ---DRAFT--- is the draft."""

REVISE_DRAFT_PROMPT = """You are an expert blog writer revising a draft based on copy editor feedback.

You will be given:
1. A brand and writing style guide.
2. The current draft (Markdown).
3. Copy editor feedback from an expert editor: a list of issues with locations, detailed descriptions of what is wrong and why, and concrete suggested fixes. Each item is written so you can understand exactly what to change and why.

Your task: Revise the draft to address the feedback. Apply every must_fix and should_fix item. Consider the consider items where they improve the piece. Use the detailed issue descriptions and suggestions to make precise changes. Preserve the draft's structure, facts, and substance. Do not remove content unless the feedback explicitly asks for it. Output the complete revised draft.

To avoid JSON escaping errors, use this format exactly:
1. First line: {"draft": 0}
2. Next line: ---DRAFT---
3. Then output the complete revised blog post in Markdown. Do not truncate. Everything after ---DRAFT--- is the draft."""

MINIMAL_STYLE_REMINDER = """Style rules to follow: Write like a human mentor. Use short sentences and plain words (8th grade level). No em dashes or en dashes; use commas or separate sentences. Short paragraphs (2–4 sentences). Use headings often; make them descriptive. No corporate buzzwords, no hype. Define technical terms on first use. Prefer concrete examples. Hook at the start; recap and one practical next step at the end. Avoid emojis. Lists only when they clarify steps or comparisons."""

ALLOWED_CLAIMS_INSTRUCTION = """
ALLOWED FACTUAL CLAIMS (you MUST use only these for facts; tag each with [CLAIM:id]):
When you use a factual claim from this list, place [CLAIM:<id>] immediately after it in the draft.
Example: "Studies show that 80% of teams adopt CI/CD within two years [CLAIM:1]."
Do NOT introduce new factual claims not in this list. Opinions and recommendations need not be tagged.
---
{claims_text}
---
"""

# Per-document extraction: output JSON with "notes" and "citations" for use when drafting from references.
EXTRACT_NOTES_PROMPT = """You are extracting notes and citations from a single source for a blog post.

You will be given:
1. The blog post outline (so you know what is relevant).
2. Optional audience and tone.
3. One source document (title, URL, and text).

Your task: From this single source only, extract facts, quotes, and citations that are relevant to writing the blog post. Do not invent information. Do not include information from other sources.

Output valid JSON only, with exactly these keys:
- "notes": A string containing concise bullet-point or paragraph notes of relevant facts and ideas from the source. Preserve key statistics, quotes, and attributions.
- "citations": A list of objects, each with "fact_or_quote" (string) and "source_ref" (string, e.g. "Title (url)").

If the source has nothing relevant to the outline, set "notes" to a short line saying so and "citations" to [].

Example format:
{"notes": "...", "citations": [{"fact_or_quote": "...", "source_ref": "Article Title (https://example.com)"}]}
"""
