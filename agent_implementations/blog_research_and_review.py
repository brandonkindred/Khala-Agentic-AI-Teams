"""
Example: run research agent, then blog review agent on the results.

The blog review agent takes the brief + researched sources and produces:
1. Top 10 catchy title choices with probability of success.
2. A detailed blog outline with notes for the first draft.
"""

import logging
logger = logging.getLogger(__name__)

from blog_research_agent.agent import ResearchAgent
from blog_research_agent.agent_cache import AgentCache
from blog_research_agent.models import ResearchBriefInput
from blog_research_agent.llm import OllamaLLMClient
from blog_review_agent import BlogReviewAgent, BlogReviewInput

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

llm_client = OllamaLLMClient(
    model="deepseek-r1",
    timeout=1800.0,
)

# 1. Research
cache = AgentCache(cache_dir=".agent_cache")
research_agent = ResearchAgent(llm_client=llm_client, cache=cache)
brief = ResearchBriefInput(
    brief="LLM observability best practices for large enterprises",
    audience="CTOs and platform teams",
    tone_or_purpose="technical deep-dive",
    max_results=20,
)
research_result = research_agent.run(brief)
logger.info("RESEARCH RESULT DOCUMENT: \n ---------------------------------------- \n %s", research_result)

# 2. Blog review (titles + outline from brief + sources)
review_agent = BlogReviewAgent(llm_client=llm_client)
review_input = BlogReviewInput(
    brief=brief.brief,
    audience=brief.audience,
    tone_or_purpose=brief.tone_or_purpose,
    references=research_result.references,
)
review_result = review_agent.run(review_input)
logger.info("OUTLINE DOCUMENT: \n ---------------------------------------- \n %s", review_result)


# 3. Output
print("\n--- Top 10 title choices (with probability of success) ---")
for i, tc in enumerate(review_result.title_choices, 1):
    print(f"{i}. {tc.title}  [{tc.probability_of_success:.0%}]")
print("\n--- Blog outline ---\n")
print(review_result.outline)
