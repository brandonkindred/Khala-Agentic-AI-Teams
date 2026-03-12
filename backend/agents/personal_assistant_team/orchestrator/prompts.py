"""Prompts for the Personal Assistant Orchestrator."""

INTENT_CLASSIFICATION_PROMPT = """You are an expert intent classifier for a personal assistant.

Analyze the user's message and classify it into one or more of these categories:
- email: Reading, writing, or managing emails
- calendar: Scheduling, events, appointments, availability
- tasks: Todo lists, grocery lists, reminders, task management
- deals: Finding deals, discounts, price comparisons, shopping
- reservations: Restaurant bookings, appointments, service reservations
- documentation: Creating process docs, templates, checklists, SOPs
- profile: Updating user preferences, goals, personal information
- general: General questions, conversations, or unclear requests

Also extract relevant entities:
- dates/times mentioned
- locations
- people/names
- items/products
- amounts/quantities

Respond with JSON:
{{
  "primary_intent": "<category>",
  "secondary_intents": ["<category>", ...],
  "entities": {{
    "dates": ["<date>", ...],
    "times": ["<time>", ...],
    "locations": ["<location>", ...],
    "people": ["<name>", ...],
    "items": ["<item>", ...],
    "amounts": ["<amount>", ...]
  }},
  "confidence": 0.0-1.0
}}

User message: {message}
"""

RESPONSE_GENERATION_PROMPT = """Generate a helpful response for the user.

User's message: {message}

Intent detected: {intent}

Actions taken:
{actions}

Results:
{results}

User's profile summary:
{profile_summary}

Generate a natural, helpful response that:
1. Acknowledges what the user asked
2. Summarizes what was done
3. Presents any relevant results
4. Suggests follow-up actions if appropriate

Respond with JSON:
{{
  "message": "<response to user>",
  "follow_up_suggestions": ["<suggestion>", ...]
}}
"""

CONVERSATION_CONTEXT_PROMPT = """You are an expert personal assistant.

Current conversation context:
{context}

User's profile summary:
{profile_summary}

User says: {message}

Respond helpfully while considering:
1. The user's preferences and history
2. Previous conversation context
3. What actions might be needed

If you need to take actions, specify them clearly.
If this is just a conversation, respond naturally.
"""
