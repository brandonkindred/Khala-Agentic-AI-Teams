"""Prompts for the Email Agent."""

EMAIL_SUMMARY_PROMPT = """Analyze this email and provide a summary.

Email:
From: {sender}
Subject: {subject}
Date: {date}

Body:
{body}

Provide:
1. A brief summary (1-2 sentences)
2. Key points (bullet points)
3. Any events mentioned (with dates/times if available)
4. Action items or requests
5. Overall sentiment (positive, negative, neutral, urgent)

Respond with JSON:
{{
  "summary": "<brief summary>",
  "key_points": ["<point1>", "<point2>", ...],
  "extracted_events": [
    {{
      "title": "<event>",
      "datetime": "<ISO datetime or null>",
      "location": "<location or null>"
    }}
  ],
  "action_items": ["<action1>", "<action2>", ...],
  "sentiment": "<positive/negative/neutral/urgent>"
}}
"""

EMAIL_DRAFT_PROMPT = """You are an expert at drafting an email on behalf of a user.

User profile:
{profile_summary}

User's typical writing style: {writing_style}

Intent/Request: {intent}

Additional context: {context}

{reply_context}

Compose an email that:
1. Matches the user's communication style
2. Is appropriate for the relationship/context
3. Achieves the intended purpose
4. Is clear and well-structured

Respond with JSON:
{{
  "subject": "<email subject>",
  "body": "<email body>",
  "tone": "<formal/casual/friendly/professional>",
  "suggested_recipients": ["<email or name>", ...]
}}
"""

EVENT_EXTRACTION_PROMPT = """Analyze this email to extract any calendar events, appointments, or scheduled activities.

Email:
From: {sender}
Subject: {subject}
Date: {date}

Body:
{body}

Look for:
- Meetings or appointments with dates/times
- Deadlines or due dates
- Events, conferences, or gatherings
- Scheduled calls or video meetings
- Travel arrangements

Respond with JSON:
{{
  "events": [
    {{
      "title": "<event title>",
      "start_time": "<ISO datetime or null if unclear>",
      "end_time": "<ISO datetime or null>",
      "location": "<location or null>",
      "attendees": ["<names or emails>"],
      "description": "<brief description>",
      "confidence": 0.0-1.0,
      "is_recurring": false,
      "recurrence_pattern": "<pattern or null>"
    }}
  ]
}}

If no events are found, return {{"events": []}}
"""

SMART_REPLY_PROMPT = """Generate quick reply options for this email.

Email:
From: {sender}
Subject: {subject}

Body:
{body}

Generate 3 appropriate quick reply options of varying tones:
1. A positive/accepting response
2. A neutral/informational response
3. A declining/postponing response (if appropriate)

Respond with JSON:
{{
  "replies": [
    {{
      "label": "<short label>",
      "body": "<reply text>",
      "tone": "<tone>"
    }}
  ]
}}
"""
