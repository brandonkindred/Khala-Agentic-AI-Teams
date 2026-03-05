"""Prompts for the Reservation Agent."""

PARSE_RESERVATION_PROMPT = """Parse this reservation request.

Request: {request}

User's preferences:
{preferences}

Current date/time: {current_datetime}

Extract:
- Type of reservation (restaurant, appointment, service)
- Venue/service name (if specified)
- Date and time
- Party size / number of people
- Special requests or requirements
- Location preferences

Respond with JSON:
{{
  "reservation_type": "restaurant" | "appointment" | "service",
  "venue_name": "<name or null>",
  "datetime": "<ISO datetime>",
  "party_size": <number>,
  "special_requests": "<notes>",
  "location": "<preferred location or null>",
  "preferences_to_apply": ["<relevant user preferences>", ...],
  "confidence": 0.0-1.0
}}
"""

RECOMMEND_VENUES_PROMPT = """Recommend venues based on user preferences.

User is looking for: {venue_type}
Location: {location}
Additional criteria: {criteria}

User preferences:
{preferences}

Available options from search:
{search_results}

Rank the options by how well they match the user's preferences.
Consider:
- Cuisine preferences and dietary restrictions
- Price range preferences
- Location convenience
- Ratings and reviews
- Past positive experiences

Respond with JSON:
{{
  "recommendations": [
    {{
      "name": "<venue name>",
      "score": 0.0-1.0,
      "matching_preferences": ["<preference>", ...],
      "reasoning": "<why this is a good match>"
    }}
  ]
}}
"""

CONFIRM_RESERVATION_PROMPT = """Generate a confirmation message for this reservation.

Reservation details:
- Venue: {venue_name}
- Date/Time: {datetime}
- Party size: {party_size}
- Notes: {notes}

Generate a friendly confirmation message to show the user.

Respond with JSON:
{{
  "confirmation_message": "<message>",
  "reminders": ["<reminder>", ...],
  "suggestions": ["<suggestion for the visit>", ...]
}}
"""
