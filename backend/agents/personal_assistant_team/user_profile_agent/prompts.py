"""Prompts for the User Profile Agent."""

PROFILE_EXTRACTION_PROMPT = """You are analyzing text to extract information about a user's preferences, likes, dislikes, goals, and personal details.

Extract any relevant information that could be added to a user profile. Look for:
- Food preferences (likes, dislikes, dietary restrictions, favorite cuisines)
- Clothing preferences (styles, sizes, colors, sock preferences)
- Favorite things (flowers, colors, brands, music genres)
- Goals (short-term, long-term, dreams, aspirations)
- Lifestyle information (hobbies, routines, habits, exercise)
- Professional information (job, skills, career goals)
- Relationship mentions (important people, birthdays, anniversaries)
- Financial preferences (budget style, spending priorities)
- Health information (allergies, fitness goals, dietary needs)
- Travel preferences (destinations, airlines, hotels, travel style)
- Shopping preferences (stores, sizes, styles)

For each piece of information, provide:
- category: One of [identity, preferences, goals, lifestyle, professional, relationships, financial, health, travel, shopping]
- field: The specific field within that category
- value: The extracted value (string, list, or object as appropriate)
- confidence: How confident you are (0.0-1.0)

Respond with JSON:
{{
  "extracted_info": [
    {{
      "category": "<category>",
      "field": "<field_name>",
      "value": "<value or list>",
      "confidence": 0.0-1.0
    }}
  ],
  "reasoning": "<brief explanation of what was found>"
}}

Text to analyze:
{text}
"""

PROFILE_QUERY_PROMPT = """Based on the user's profile, answer the following question.

User Profile:
{profile}

Question: {query}

Provide a helpful, natural response based on what you know about the user.
If the profile doesn't contain relevant information, say so politely.
"""

PREFERENCE_VALIDATION_PROMPT = """You are validating extracted preferences before adding them to a user profile.

Extracted preference:
- Category: {category}
- Field: {field}
- Value: {value}
- Confidence: {confidence}
- Source: {source_text}

Current profile data for this category:
{current_data}

Determine:
1. Is this a valid preference that should be added?
2. Does it conflict with existing data?
3. Should it replace, merge with, or be added alongside existing data?

Respond with JSON:
{{
  "should_add": true/false,
  "action": "add" | "merge" | "replace" | "skip",
  "reason": "<explanation>",
  "conflicts": ["<any conflicting existing values>"]
}}
"""
