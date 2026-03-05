"""Prompts for the Calendar Agent."""

PARSE_EVENT_PROMPT = """Parse this text to extract calendar event details.

Text: {text}

Current date/time: {current_datetime}
User's timezone: {timezone}

Extract event details:
- Title/description of the event
- Start date and time (convert relative references like "tomorrow" or "next Tuesday")
- End time or duration
- Location (if mentioned)
- Attendees (if mentioned)
- Any recurring pattern

Respond with JSON:
{{
  "events": [
    {{
      "title": "<event title>",
      "start_time": "<ISO datetime>",
      "end_time": "<ISO datetime or null>",
      "duration_minutes": <number or null>,
      "location": "<location or null>",
      "attendees": ["<names or emails>"],
      "description": "<additional details>",
      "is_recurring": false,
      "recurrence_pattern": "<pattern or null>",
      "confidence": 0.0-1.0
    }}
  ],
  "ambiguities": ["<any unclear details that need confirmation>"]
}}
"""

SCHEDULE_SUGGESTION_PROMPT = """Suggest optimal times for scheduling an event.

Event details:
- Title: {title}
- Duration: {duration_minutes} minutes
- Attendees: {attendees}

User's preferences:
- Preferred date: {preferred_date}
- Preferred time range: {preferred_time_range}
- Constraints: {constraints}

Available time slots on that day:
{available_slots}

User's typical schedule patterns:
{schedule_patterns}

Rank the available slots and suggest the best 3 options.

Respond with JSON:
{{
  "suggestions": [
    {{
      "start_time": "<ISO datetime>",
      "end_time": "<ISO datetime>",
      "score": 0.0-1.0,
      "reason": "<why this is a good time>"
    }}
  ]
}}
"""

CONFLICT_RESOLUTION_PROMPT = """There are conflicts for the requested time slot.

Requested event:
- Title: {title}
- Time: {start_time} to {end_time}

Conflicting events:
{conflicts}

Suggest how to resolve this:
1. Alternative times that work
2. Which event might be more flexible
3. Whether the events could be combined

Respond with JSON:
{{
  "resolution_options": [
    {{
      "option": "<description>",
      "action": "reschedule" | "cancel" | "combine" | "overlap_ok",
      "new_time": "<ISO datetime or null>",
      "affected_events": ["<event ids>"]
    }}
  ],
  "recommendation": "<which option is best>"
}}
"""
