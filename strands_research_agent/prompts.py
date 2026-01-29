BRIEF_PARSING_PROMPT = """You are a research planning assistant.

You will be given a short content brief, optional audience, and optional tone/purpose.
Extract the following as concise bullet points:
- core topics
- desired angle (e.g. comparison, how-to, trend analysis, risks)
- explicit constraints (industry, region, technology stack)

Return JSON with keys: core_topics (list of strings), angle (string), constraints (list of strings).
Keep responses short and information-dense."""


QUERY_GENERATION_PROMPT = """You are an expert research strategist.

Based on the following normalized brief information, generate a diverse set of focused web search queries.

Information:
- core_topics: {core_topics}
- angle: {angle}
- constraints: {constraints}
- audience: {audience}
- tone_or_purpose: {tone_or_purpose}

Produce between 3 and 8 search queries that together cover:
- high-level overviews and definitions
- implementation/how-to aspects (if applicable)
- statistics/market data if relevant
- risks/limitations/criticisms if relevant
- queries tailored to the specified audience where appropriate

Return JSON with a list under the key "queries", where each item has:
- "query_text": string
- "intent": short label like "overview", "how-to", "stats", "risks", "case-studies", etc."""


DOC_RELEVANCE_SCORING_PROMPT = """You are an assistant that scores relevance and tags for a web document.

You are given:
- a research brief
- a candidate document (title + extracted text)

Your task:
1. Decide how relevant this document is to the brief (0 to 1, with 1 being extremely relevant).
2. Briefly classify the type: e.g. "guides", "academic", "news", "tooling", "docs", "blog", "report".
3. Provide up to 3 short tags capturing the document's focus (e.g. "best-practices", "case-studies", "benchmarks").

Return JSON with keys:
- relevance_score: float between 0 and 1
- type: string
- tags: list of strings
Keep the analysis brief; do not include the full document text in the response."""


DOC_SUMMARIZATION_PROMPT = """You are a helpful research summarizer.

You will receive:
- a research brief (with audience and purpose if provided)
- one web document (title + extracted text)

Your task:
- Write a concise 2–4 sentence summary focused on what is most relevant to the brief.
- Extract 3–8 bullet key points that would be useful for someone writing content based on this document.
- Flag if the content appears highly opinionated or promotional.

Return JSON with keys:
- summary: string
- key_points: list of strings
- is_promotional: boolean
Do not include the full original text in the response."""


FINAL_SYNTHESIS_PROMPT = """You are a senior research analyst.

You will receive:
- the original research brief
- a list of selected references with summaries and key points

Your tasks:
1. Identify the main themes and consensus across the references.
2. Note any disagreements, trade-offs, or controversies.
3. Surface any obvious gaps where more research could help.
4. Suggest a high-level outline for a content piece that uses these references.

Return a compact, paragraph-style analysis followed by a short suggested outline as bullet points."""

