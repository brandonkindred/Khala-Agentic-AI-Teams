"""Prompts for the Task Agent."""

PARSE_TASKS_PROMPT = """Parse this text to extract task or grocery list items.

Text: {text}

Context:
- Current lists the user has: {existing_lists}
- Current date: {current_date}

Extract:
1. Which list to add items to (or suggest a new list name)
2. Individual items with:
   - Description
   - Quantity (if mentioned)
   - Priority (low/medium/high/urgent) - default to medium
   - Due date (if mentioned)
   - Tags/categories

Respond with JSON:
{{
  "list_name": "<list name or 'default'>",
  "items": [
    {{
      "description": "<item description>",
      "quantity": "<quantity or null>",
      "priority": "low" | "medium" | "high" | "urgent",
      "due_date": "<ISO date or null>",
      "tags": ["<tag>", ...]
    }}
  ]
}}
"""

CATEGORIZE_ITEMS_PROMPT = """Categorize these grocery/shopping items.

Items:
{items}

Assign each item to one of these categories:
- produce (fruits, vegetables)
- dairy (milk, cheese, yogurt)
- meat (beef, chicken, fish)
- bakery (bread, pastries)
- frozen (frozen foods)
- pantry (canned goods, dry goods)
- beverages (drinks)
- snacks (chips, cookies)
- household (cleaning, paper goods)
- personal_care (hygiene, health)
- other

Respond with JSON:
{{
  "categorized_items": [
    {{
      "description": "<item>",
      "category": "<category>",
      "aisle_hint": "<typical aisle or section>"
    }}
  ]
}}
"""

SUGGEST_ITEMS_PROMPT = """Based on the user's profile and recent purchases, suggest items they might need.

User preferences:
{preferences}

Recent lists:
{recent_lists}

Current list:
{current_list}

Suggest items they might have forgotten or regularly need.

Respond with JSON:
{{
  "suggestions": [
    {{
      "description": "<item>",
      "reason": "<why they might need it>",
      "confidence": 0.0-1.0
    }}
  ]
}}
"""
