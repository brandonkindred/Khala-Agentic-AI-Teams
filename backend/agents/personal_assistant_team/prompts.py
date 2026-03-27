"""Shared prompt templates for Personal Assistant team."""

INTENT_CLASSIFICATION_PROMPT = """You are an expert intent classifier for a personal assistant.

Analyze the user's message and classify it into one or more of these categories:
- email: Reading, writing, or managing emails
- calendar: Scheduling, events, appointments
- tasks: Todo lists, grocery lists, reminders
- deals: Finding deals, discounts, price comparisons
- reservations: Restaurant bookings, appointments, service reservations
- documentation: Creating process docs, templates, checklists
- profile: Updating user preferences, goals, personal information
- general: General questions or conversations

If you cannot classify with confidence > 0.5, set primary_intent to "general" and explain the uncertainty in a "notes" field.
If the input is empty or unintelligible, return confidence 0.0 with primary_intent "general" and a helpful "notes" field.

Respond with JSON:
{
  "primary_intent": "<category>",
  "secondary_intents": ["<category>", ...],
  "entities": {
    "dates": [...],
    "times": [...],
    "locations": [...],
    "people": [...],
    "items": [...]
  },
  "confidence": 0.0-1.0,
  "notes": "<optional: explain ambiguity or low confidence>"
}

User message: {message}
"""

PROFILE_EXTRACTION_PROMPT = """You are an expert at analyzing text to extract information about a user's preferences, likes, dislikes, goals, and personal details.

Extract any relevant information that could be added to a user profile. Look for:
- Food preferences (likes, dislikes, dietary restrictions)
- Clothing preferences (styles, sizes, colors)
- Favorite things (flowers, colors, brands)
- Goals (short-term, long-term, dreams)
- Lifestyle information (hobbies, routines, habits)
- Professional information (job, skills, goals)
- Relationship mentions (important people, birthdays)
- Financial preferences (budget, spending habits)
- Health information (allergies, fitness goals)
- Travel preferences (destinations, airlines, hotels)
- Shopping preferences (stores, sizes, styles)

If the text contains no extractable profile information, return an empty "extracted_info" list with a brief "reasoning" explaining why.

Respond with JSON:
{
  "extracted_info": [
    {
      "category": "<profile_category>",
      "field": "<specific_field>",
      "value": "<extracted_value>",
      "confidence": 0.0-1.0
    }
  ],
  "reasoning": "<brief explanation>"
}

Text to analyze:
{text}
"""

EVENT_EXTRACTION_PROMPT = """You are an expert at analyzing text (such as an email or message) to extract calendar events.

Look for any mentions of:
- Meetings, appointments, or scheduled events
- Dates and times
- Locations
- Attendees
- Deadlines or due dates

If no events are found, return an empty "events" list. For partial information (e.g. a date without a time), include what you can and set missing fields to null.

Respond with JSON:
{
  "events": [
    {
      "title": "<event title>",
      "start_time": "<ISO datetime or null>",
      "end_time": "<ISO datetime or null>",
      "location": "<location or null>",
      "attendees": ["<name or email>", ...],
      "description": "<brief description>",
      "confidence": 0.0-1.0
    }
  ]
}

Text to analyze:
{text}
"""

EMAIL_DRAFT_PROMPT = """You are an expert assistant helping compose an email on behalf of a user.

User profile summary:
{profile_summary}

User's writing style notes:
{writing_style}

Request: {request}

Compose an email that:
1. Matches the user's communication style
2. Is appropriate for the context
3. Is clear and professional (or casual, based on context)

Respond with JSON:
{
  "subject": "<email subject>",
  "body": "<email body>",
  "tone": "<formal/casual/friendly/professional>",
  "suggested_recipients": ["<email>", ...]
}
"""

DEAL_RELEVANCE_PROMPT = """You are an expert at scoring how relevant a deal is to a user based on their profile.

User preferences:
{preferences}

Deal information:
{deal}

Score this deal's relevance to the user from 0.0 to 1.0.
Consider:
- Does it match their preferred brands?
- Does it align with their interests/hobbies?
- Is it in a category they care about?
- Does the discount meet their threshold?

Respond with JSON:
{
  "relevance_score": 0.0-1.0,
  "matching_preferences": ["<preference that matches>", ...],
  "reasoning": "<brief explanation>"
}
"""

TASK_PARSING_PROMPT = """You are an expert at parsing a user's request to add items to a task list or grocery list.

Request: {request}

Extract the items to add, including:
- Item description
- Quantity (if mentioned)
- Priority (if mentioned)
- Due date (if mentioned)
- Which list to add to (if mentioned)

Respond with JSON:
{
  "list_name": "<list name or 'default'>",
  "items": [
    {
      "description": "<item>",
      "quantity": "<quantity or null>",
      "priority": "<low/medium/high/urgent>",
      "due_date": "<date or null>"
    }
  ]
}
"""

RESERVATION_PROMPT = """You are an expert assistant helping make a reservation based on the user's request.

User profile:
{profile_summary}

Request: {request}

Extract reservation details:
- Type (restaurant, appointment, service)
- Venue/service name (if specified)
- Date and time
- Party size
- Special requests or notes
- Preferences to consider (from profile)

Respond with JSON:
{
  "reservation_type": "<restaurant/appointment/service>",
  "venue_name": "<name or null>",
  "datetime": "<ISO datetime>",
  "party_size": <number>,
  "special_requests": "<notes>",
  "preferences_to_apply": ["<relevant preferences>", ...]
}
"""
