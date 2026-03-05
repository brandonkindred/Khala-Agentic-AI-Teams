"""Prompts for the Deal Finder Agent."""

DEAL_RELEVANCE_PROMPT = """Score how relevant this deal is to the user based on their profile.

User preferences:
{preferences}

User's wishlist items:
{wishlist}

Deal information:
- Title: {title}
- Description: {description}
- Store: {store}
- Original price: {original_price}
- Sale price: {sale_price}
- Discount: {discount_percent}%

Consider:
1. Does it match their preferred brands?
2. Does it align with their interests, hobbies, or needs?
3. Is it in a category they care about?
4. Does the discount meet their alert threshold ({discount_threshold}%)?
5. Does it match any wishlist items?

Respond with JSON:
{{
  "relevance_score": 0.0-1.0,
  "matching_preferences": ["<preference that matches>", ...],
  "matching_wishlist_items": ["<item description>", ...],
  "reasoning": "<brief explanation>"
}}
"""

GENERATE_SEARCH_QUERIES_PROMPT = """Generate search queries to find deals matching the user's preferences.

User preferences:
{preferences}

User's wishlist items:
{wishlist}

User's recent interests:
{recent_interests}

Generate 3-5 search queries that would find relevant deals for this user.
Focus on:
- Wishlist items
- Frequently purchased categories
- Upcoming events (birthdays, holidays)
- Hobbies and interests

Respond with JSON:
{{
  "queries": [
    {{
      "query": "<search query>",
      "category": "<category>",
      "priority": "high" | "medium" | "low"
    }}
  ]
}}
"""

EXTRACT_DEAL_INFO_PROMPT = """Extract deal information from this web page content.

Content:
{content}

Extract any deals, discounts, or sales mentioned:
- Product name
- Original price
- Sale price
- Discount percentage
- Store name
- Expiration date (if mentioned)
- Product category

Respond with JSON:
{{
  "deals": [
    {{
      "title": "<product name>",
      "description": "<brief description>",
      "original_price": <number or null>,
      "sale_price": <number or null>,
      "discount_percent": <number or null>,
      "store": "<store name>",
      "category": "<category>",
      "expires_at": "<date or null>"
    }}
  ]
}}
"""
