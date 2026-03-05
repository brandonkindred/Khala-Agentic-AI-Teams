"""Prompts for the Document Generator Agent."""

PROCESS_DOC_PROMPT = """Create a process document for the following task.

Task/Process: {topic}

Context: {context}

User's relevant information:
{user_info}

Create a clear, step-by-step guide that:
1. Explains the purpose
2. Lists prerequisites or requirements
3. Provides numbered steps with clear instructions
4. Includes tips or best practices
5. Notes potential issues and solutions

Format as {format}.

Respond with JSON:
{{
  "title": "<document title>",
  "content": "<full document content in {format} format>"
}}
"""

CHECKLIST_PROMPT = """Create a comprehensive checklist for the following task.

Task: {task}

Context: {context}

{time_estimate_instruction}

Create a thorough checklist that:
1. Breaks down the task into actionable items
2. Groups related items together
3. Orders items logically
4. Includes any preparation steps
5. Notes dependencies between items

Respond with JSON:
{{
  "title": "<checklist title>",
  "items": [
    {{
      "item": "<checklist item>",
      "category": "<category/group>",
      "priority": "required" | "recommended" | "optional",
      "time_estimate": "<time or null>"
    }}
  ],
  "total_time_estimate": "<total time or null>"
}}
"""

TEMPLATE_PROMPT = """Create a template for the following purpose.

Template type: {template_type}
Purpose: {purpose}
Required fields: {fields}

Create a professional template that:
1. Has clear section headers
2. Includes placeholders for variable content (use {{PLACEHOLDER_NAME}} format)
3. Provides example text or guidance
4. Is easy to fill in

Respond with JSON:
{{
  "title": "<template title>",
  "content": "<template content with {{PLACEHOLDERS}}>",
  "placeholders": ["<PLACEHOLDER_NAME>", ...]
}}
"""

SOP_PROMPT = """Create a Standard Operating Procedure (SOP) document.

Process Name: {process_name}
Description: {description}

Initial steps provided:
{steps}

{safety_section}
{troubleshooting_section}

Create a formal SOP document with:
1. Purpose and scope
2. Responsibilities
3. Required materials/tools
4. Detailed procedure steps
5. Quality checks
{additional_sections}

Format as markdown.

Respond with JSON:
{{
  "title": "<SOP title>",
  "content": "<full SOP document in markdown>"
}}
"""

MEETING_AGENDA_PROMPT = """Create a meeting agenda.

Meeting purpose: {purpose}
Duration: {duration}
Attendees: {attendees}
Topics to cover: {topics}

Create a professional meeting agenda that:
1. Has a clear objective
2. Allocates time for each topic
3. Identifies discussion leaders
4. Includes any preparation needed
5. Reserves time for Q&A and action items

Respond with JSON:
{{
  "title": "<meeting title>",
  "content": "<agenda in markdown format>",
  "time_allocations": [
    {{"topic": "<topic>", "minutes": <number>}}
  ]
}}
"""
