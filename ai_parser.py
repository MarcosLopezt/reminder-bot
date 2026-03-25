"""
Google Gemini API integration for the Telegram Reminder Bot.

Two entry points:
  classify_message()  — single call that classifies intent AND extracts params.
  format_parsed_task_for_display() — formats a parsed task for confirmation.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

import pytz
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_client: Optional[genai.Client] = None


def get_client() -> genai.Client:
    """Return a cached google-genai Client."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


# ---------------------------------------------------------------------------
# Intent classification + parameter extraction (single Gemini call)
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
You are the AI brain of a Telegram reminder/task-management bot.
Analyze the user's message and return ONE JSON object — no markdown, no extra text.

{tasks_block}
Current date/time: {now_str}  |  Timezone: {timezone}
{username_block}

JSON schema to return:
{{
  "intent": "<one of: create_task | list_tasks | complete_task | delete_task | pause_task | resume_task | show_history | edit_task | greeting | unknown>",
  "language": "<ISO-639-1 code of the user's message, e.g. 'es', 'en', 'pt'>",
  "reply": "<For greeting/unknown: a warm, concise response IN THE USER'S LANGUAGE. For other intents: null>",
  "task_number": <For intents that target an existing task: 1-based index from the task list above, or null if unclear>,
  "task": {{
    "description": "<short imperative phrase, e.g. 'Feed the dog'>",
    "assignees": ["<usernames without @, lowercase>"],
    "include_self": true,
    "frequency": "<daily | weekly | custom>",
    "reminder_times": ["<HH:MM 24-h — list ALL times mentioned, e.g. ['08:00', '22:00']>"],
    "reminder_day": "<monday-sunday or null>",
    "cron_expression": "<5-part cron or null>",
    "task_type": "<recurring | one_time>",
    "specific_date": "<YYYY-MM-DD resolved from relative phrases like 'tomorrow'/'next Friday', or null for recurring>",
    "confidence": 0.0
  }},
  "edit_changes": {{
    "new_time": "<HH:MM or null>",
    "new_day": "<monday-sunday or null>",
    "new_frequency": "<daily | weekly | custom | null>"
  }}
}}

Rules:
- "task" field: fill ONLY for create_task intent, otherwise null.
- "edit_changes" field: fill ONLY for edit_task intent, otherwise null.
- "task_number": fill for complete_task/delete_task/pause_task/resume_task/show_history/edit_task.
  Match the user's words against the task list and return the index of the best match, or null if unclear.
- One-time vs recurring:
    * "tomorrow", "on Friday", "next Monday", "on March 25", "tonight", "this afternoon" → task_type="one_time", resolve specific_date to YYYY-MM-DD.
    * "every day", "daily", "every week", "every Monday", "weekdays" → task_type="recurring", specific_date=null.
- Resolve relative dates using the current date/time provided above (e.g. "tomorrow" = current date + 1 day).
- reminder_times: list of ALL times mentioned. Convert natural language ("8am"→"08:00", "9pm"→"21:00", "noon"→"12:00"). Default ["09:00"] if no time given. Always a list, even for one time.
- greeting intent: hi, hello, thanks, how are you, etc.
- unknown: anything that doesn't fit other categories.
- reply for unknown: acknowledge you didn't understand and briefly list what the bot can do — in the user's language.
- language: detect from the user's message (not the task list).
"""


async def classify_message(
    text: str,
    tasks: list[dict],
    timezone_str: str,
    now: datetime,
    requesting_username: Optional[str] = None,
) -> dict:
    """
    Classify the user's intent and extract all parameters in a single Gemini call.

    Args:
        text:               The user's raw message.
        tasks:              Active tasks for this chat (for context / fuzzy matching).
        timezone_str:       IANA timezone string (e.g. 'America/Argentina/Cordoba').
        now:                Current datetime (tz-aware, already in the user's timezone).
        requesting_username: Telegram username of the sender (without @).

    Returns:
        A dict with keys: intent, language, reply, task_number, task, edit_changes.
        Falls back to {"intent": "unknown", "reply": "..."} on any error.
    """
    # Build tasks context block
    if tasks:
        task_lines = "\n".join(
            f"{i}. {t['description']} ({_short_schedule(t)})"
            for i, t in enumerate(tasks, 1)
        )
        tasks_block = f"Active tasks in this chat:\n{task_lines}\n"
    else:
        tasks_block = "Active tasks in this chat: (none)\n"

    username_block = (
        f"The requesting user's Telegram username is @{requesting_username}."
        if requesting_username
        else ""
    )

    now_str = now.strftime("%A, %B %d %Y, %H:%M")

    prompt = _CLASSIFY_PROMPT.format(
        tasks_block=tasks_block,
        now_str=now_str,
        timezone=timezone_str,
        username_block=username_block,
    ) + f"\n\nUser message: {text}"

    client = get_client()
    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )
        raw = (response.text or "").strip()
        result = json.loads(raw)
        logger.info("classify_message intent=%s lang=%s", result.get("intent"), result.get("language"))
        return result
    except json.JSONDecodeError as exc:
        logger.error("classify_message JSON parse error: %s", exc)
    except Exception as exc:
        logger.error("classify_message Gemini error: %s", exc)

    return {"intent": "unknown", "language": "en", "reply": "Sorry, I had trouble understanding that. Try again!"}


def _short_schedule(task: dict) -> str:
    """One-line schedule description for inclusion in the Gemini context prompt."""
    from database import fmt_time

    # Support both legacy reminder_time (str) and new reminder_times (list)
    raw_times = task.get("reminder_times") or [task.get("reminder_time", "09:00")]
    if isinstance(raw_times, str):
        raw_times = [raw_times]
    times_str = " and ".join(fmt_time(t) for t in raw_times if t)

    if task.get("task_type") == "one_time" and task.get("specific_date"):
        return f"one-time on {task['specific_date']} at {times_str}"
    freq = task.get("frequency", "")
    if freq == "daily":
        return f"daily at {times_str}"
    if freq == "weekly" and task.get("reminder_day"):
        return f"every {task['reminder_day'].capitalize()} at {times_str}"
    if freq == "custom" and task.get("cron_expression"):
        return f"custom ({task['cron_expression']})"
    return f"at {times_str}"


# ---------------------------------------------------------------------------
# Legacy: kept for the /new command confirmation flow
# ---------------------------------------------------------------------------

async def parse_task_description(
    text: str,
    requesting_username: Optional[str] = None,
) -> Optional[dict]:
    """
    Parse a natural-language task description using Gemini.
    Returns a dict with parsed task fields, or None if parsing failed.
    Used by the /new slash command flow.
    """
    timezone_str = os.getenv("DEFAULT_TIMEZONE", "UTC")
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.UTC
    now = datetime.now(tz)

    result = await classify_message(
        text=text,
        tasks=[],
        timezone_str=timezone_str,
        now=now,
        requesting_username=requesting_username,
    )
    return result.get("task")  # None if not a create_task intent


def format_parsed_task_for_display(parsed: dict, requesting_username: Optional[str] = None) -> str:
    """
    Format parsed task data into a human-readable confirmation message.
    Used by /new command and natural-language task creation.
    """
    from database import fmt_time

    desc = parsed.get("description", "Unknown task")
    frequency = parsed.get("frequency", "daily")
    reminder_day = parsed.get("reminder_day")
    assignees = parsed.get("assignees", [])
    include_self = parsed.get("include_self", True)
    task_type = parsed.get("task_type", "recurring")
    specific_date = parsed.get("specific_date")

    # Normalize reminder_times — accept both old (str) and new (list) formats
    raw_times = parsed.get("reminder_times") or [parsed.get("reminder_time", "09:00")]
    if isinstance(raw_times, str):
        raw_times = [raw_times]
    reminder_times = [t for t in raw_times if t]
    times_display = " and ".join(fmt_time(t) for t in reminder_times)

    assignee_parts = []
    if include_self:
        name = f"@{requesting_username}" if requesting_username else "You"
        assignee_parts.append(name)
    assignee_parts.extend(f"@{a}" for a in assignees)
    assignee_str = ", ".join(assignee_parts) if assignee_parts else "You"

    if task_type == "one_time" and specific_date:
        schedule_str = f"One-time on {specific_date} at {times_display}"
    elif frequency == "daily":
        schedule_str = f"Every day at {times_display}"
    elif frequency == "weekly" and reminder_day:
        schedule_str = f"Every {reminder_day.capitalize()} at {times_display}"
    elif frequency == "custom":
        cron = parsed.get("cron_expression", "")
        schedule_str = f"Custom schedule ({cron})"
    else:
        schedule_str = f"At {times_display}"

    lines = [
        "📋 <b>Here's what I understood:</b>\n",
        f"📝 <b>Task:</b> {desc}",
        f"👥 <b>Assigned to:</b> {assignee_str}",
        f"🔄 <b>Schedule:</b> {schedule_str}",
        "",
        "Does this look right?",
    ]
    return "\n".join(lines)
