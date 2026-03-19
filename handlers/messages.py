"""
Fully conversational message handler.

Every plain-text message is sent to Gemini for intent classification.
The bot understands natural language for all operations — task creation,
completion, deletion, pausing, editing, and more. Slash commands continue
to work as a fallback.
"""

import logging
from datetime import date, datetime
from typing import Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import database as db
from ai_parser import classify_message, format_parsed_task_for_display
from database import make_display_name
from scheduler import remove_task_jobs, schedule_task_jobs, send_dm_with_fallback

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route every plain-text message through the Gemini intent classifier."""
    text = (update.message.text or "").strip()
    if not text:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    # --- Clarification flow: user was asked "which task?" ---
    pending = context.user_data.get("pending_action")
    if pending:
        resolved = await _try_resolve_clarification(update, context, text, pending)
        if resolved:
            return
        # Didn't look like a number — fall through and classify normally
        context.user_data.pop("pending_action", None)

    # --- Classify intent ---
    tasks = await db.get_tasks_for_chat(chat_id)
    timezone_str = await db.get_chat_timezone(chat_id)
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.UTC
    now = datetime.now(tz)

    result = await classify_message(
        text=text,
        tasks=tasks,
        timezone_str=timezone_str,
        now=now,
        requesting_username=user.username,
    )

    intent = result.get("intent", "unknown")
    logger.info("Intent: %s  user=%s  text=%r", intent, user.username or user.id, text[:80])

    if intent == "create_task":
        await _handle_create_task(update, context, result, user)
    elif intent == "list_tasks":
        await _handle_list_tasks(update, context, chat_id, tasks, timezone_str)
    elif intent == "complete_task":
        await _handle_action_with_task(update, context, result, tasks, user, "complete")
    elif intent == "delete_task":
        await _handle_action_with_task(update, context, result, tasks, user, "delete")
    elif intent == "pause_task":
        await _handle_action_with_task(update, context, result, tasks, user, "pause")
    elif intent == "resume_task":
        all_tasks = await db.get_tasks_for_chat(chat_id, active_only=False)
        await _handle_action_with_task(update, context, result, all_tasks, user, "resume")
    elif intent == "show_history":
        await _handle_action_with_task(update, context, result, tasks, user, "history")
    elif intent == "edit_task":
        await _handle_action_with_task(update, context, result, tasks, user, "edit", extra=result.get("edit_changes"))
    elif intent in ("greeting", "unknown"):
        reply = result.get("reply") or "I didn't quite understand that. Try describing a task or asking what you need help with!"
        await update.message.reply_text(reply)
    else:
        await update.message.reply_text("I didn't understand that. Try asking me to remind you about something!")


# ---------------------------------------------------------------------------
# Clarification resolution
# ---------------------------------------------------------------------------

async def _try_resolve_clarification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    pending: dict,
) -> bool:
    """
    If the user replied with a task number after a 'which task?' prompt,
    execute the pending action. Returns True if handled.
    """
    try:
        task_number = int(text.strip())
    except ValueError:
        return False

    chat_id = update.effective_chat.id
    user = update.effective_user
    active_only = pending.get("action") != "resume"
    tasks = await db.get_tasks_for_chat(chat_id, active_only=active_only)

    if task_number < 1 or task_number > len(tasks):
        await update.message.reply_text(f"I don't have a task #{task_number}. Please try again.")
        context.user_data.pop("pending_action", None)
        return True

    task = tasks[task_number - 1]
    context.user_data.pop("pending_action", None)
    await _execute_task_action(update, context, pending["action"], task, user, pending.get("extra"))
    return True


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------

async def _handle_create_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: dict,
    user,
) -> None:
    parsed = result.get("task")
    if not parsed or parsed.get("confidence", 1.0) < 0.4:
        await update.message.reply_text(
            "I couldn't quite understand the task details. Could you be more specific? "
            "For example: 'Remind me to feed the dog every day at 8am'"
        )
        return

    context.user_data["pending_task"] = {
        "parsed": parsed,
        "chat_id": update.effective_chat.id,
        "created_by": user.id,
        "created_by_username": user.username,
        "created_by_first_name": user.first_name,
    }

    display_text = format_parsed_task_for_display(parsed, requesting_username=user.username)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="confirm_task"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_task"),
        ]
    ])
    await update.message.reply_text(display_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# List tasks
# ---------------------------------------------------------------------------

async def _handle_list_tasks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    tasks: list[dict],
    timezone_str: str,
) -> None:
    if not tasks:
        await update.message.reply_text("You have no active tasks. Tell me what you'd like to be reminded about!")
        return

    today = date.today().isoformat()
    lines = [f"📋 <b>Active tasks</b> — {date.today().strftime('%A, %B %d')}\n"]
    buttons = []

    for idx, task in enumerate(tasks, start=1):
        completion = await db.get_completion_for_date(task["id"], today)
        status_icon = "✅" if completion else "⏰"
        schedule = _format_schedule(task)

        if completion:
            done_by = completion.get("completed_by_name") or make_display_name(completion.get("completed_by_username"))
            status_note = f"done by {done_by}"
        else:
            status_note = "pending"

        lines.append(
            f"{idx}. {status_icon} <b>{task['description']}</b>\n"
            f"   <i>{schedule} — {status_note}</i>"
        )

        row = []
        if not completion:
            row.append(InlineKeyboardButton("✅ Done", callback_data=f"done:{task['id']}"))
        row.append(InlineKeyboardButton("⏸ Pause", callback_data=f"pause:{task['id']}"))
        row.append(InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{task['id']}"))
        buttons.append(row)

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


# ---------------------------------------------------------------------------
# Action dispatcher (complete / delete / pause / resume / history / edit)
# ---------------------------------------------------------------------------

async def _handle_action_with_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: dict,
    tasks: list[dict],
    user,
    action: str,
    extra: Optional[dict] = None,
) -> None:
    """
    Route an action to the right task. Asks for clarification if ambiguous.
    """
    if not tasks:
        await update.message.reply_text("You don't have any tasks right now.")
        return

    task_number = result.get("task_number")

    # Gemini identified a specific task
    if task_number and 1 <= task_number <= len(tasks):
        task = tasks[task_number - 1]
        await _execute_task_action(update, context, action, task, user, extra)
        return

    # Only one task — apply unambiguously
    if len(tasks) == 1:
        await _execute_task_action(update, context, action, tasks[0], user, extra)
        return

    # Ambiguous — ask which task
    context.user_data["pending_action"] = {"action": action, "extra": extra}
    lines = ["Which task do you mean? Reply with the number:\n"]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. {t['description']} ({_format_schedule(t)})")
    await update.message.reply_text("\n".join(lines))


async def _execute_task_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    task: dict,
    user,
    extra: Optional[dict] = None,
) -> None:
    chat_id = update.effective_chat.id
    mention = make_display_name(user.username, user.first_name)

    if action == "complete":
        await _do_complete(update, context, task, user, mention, chat_id)
    elif action == "delete":
        await _do_delete(update, task, user)
    elif action == "pause":
        await _do_pause(update, task, user)
    elif action == "resume":
        await _do_resume(update, context, task, user)
    elif action == "history":
        await _do_history(update, task)
    elif action == "edit":
        await _do_edit(update, context, task, user, extra or {})


# ---------------------------------------------------------------------------
# Individual action handlers
# ---------------------------------------------------------------------------

async def _do_complete(update, context, task, user, mention, chat_id):
    today = date.today().isoformat()
    success = await db.record_completion(
        task_id=task["id"],
        user_id=user.id,
        username=user.username,
        date_for=today,
        display_name=mention,
    )
    if success:
        await update.message.reply_text(
            f"✅ <b>{task['description']}</b> marked as done by {mention}!",
            parse_mode=ParseMode.HTML,
        )
        # Notify other assignees
        assignees = await db.get_task_assignees(task["id"])
        for assignee in assignees:
            if assignee["telegram_user_id"] == user.id:
                continue
            await send_dm_with_fallback(
                bot=context.bot,
                user_id=assignee["telegram_user_id"],
                username=assignee.get("telegram_username"),
                group_chat_id=chat_id,
                text=f"✅ <b>{task['description']}</b> has been completed by {mention}. No action needed from you!",
            )
    else:
        completion = await db.get_completion_for_date(task["id"], today)
        done_by = completion.get("completed_by_name") or make_display_name(completion.get("completed_by_username")) if completion else "someone"
        await update.message.reply_text(
            f"ℹ️ <b>{task['description']}</b> was already marked done by {done_by} today.",
            parse_mode=ParseMode.HTML,
        )


async def _do_delete(update, task, user):
    if task["created_by"] != user.id:
        await update.message.reply_text("❌ Only the task creator can delete it.")
        return
    remove_task_jobs(task["id"])
    await db.delete_task(task["id"])
    await update.message.reply_text(
        f"🗑 <b>{task['description']}</b> has been deleted.",
        parse_mode=ParseMode.HTML,
    )


async def _do_pause(update, task, user):
    if task["created_by"] != user.id:
        await update.message.reply_text("❌ Only the task creator can pause it.")
        return
    remove_task_jobs(task["id"])
    await db.set_task_active(task["id"], False)
    await update.message.reply_text(
        f"⏸ <b>{task['description']}</b> has been paused.",
        parse_mode=ParseMode.HTML,
    )


async def _do_resume(update, context, task, user):
    if task["created_by"] != user.id:
        await update.message.reply_text("❌ Only the task creator can resume it.")
        return
    await db.set_task_active(task["id"], True)
    schedule_task_jobs(task, context.bot)
    await update.message.reply_text(
        f"▶️ <b>{task['description']}</b> has been resumed.",
        parse_mode=ParseMode.HTML,
    )


async def _do_history(update, task):
    history = await db.get_task_history(task["id"], days=7)
    lines = [f"📅 <b>History: {task['description']}</b> (last 7 days)\n"]
    if not history:
        lines.append("No completions recorded in the last 7 days.")
    else:
        for entry in history:
            done_by = entry.get("completed_by_name") or make_display_name(entry.get("completed_by_username"))
            lines.append(f"  ✅ {entry['date_for']} — {done_by}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _do_edit(update, context, task, user, changes: dict):
    if task["created_by"] != user.id:
        await update.message.reply_text("❌ Only the task creator can edit it.")
        return

    new_time = changes.get("new_time")
    new_day = changes.get("new_day")
    new_frequency = changes.get("new_frequency")

    if not any([new_time, new_day, new_frequency]):
        await update.message.reply_text("I understood you want to edit this task but couldn't determine what to change. Could you be more specific?")
        return

    await db.update_task_schedule(task["id"], reminder_time=new_time, reminder_day=new_day, frequency=new_frequency)
    updated_task = await db.get_task_by_id(task["id"])
    remove_task_jobs(task["id"])
    schedule_task_jobs(updated_task, context.bot)

    await update.message.reply_text(
        f"✏️ <b>{task['description']}</b> updated: {_format_schedule(updated_task)}",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Schedule formatting helper
# ---------------------------------------------------------------------------

def _format_schedule(task: dict) -> str:
    t = task.get("reminder_time", "")
    try:
        h, m = t.split(":")
        from datetime import time as dtime
        time_display = dtime(int(h), int(m)).strftime("%I:%M %p").lstrip("0")
    except Exception:
        time_display = t

    if task.get("task_type") == "one_time" and task.get("specific_date"):
        return f"one-time on {task['specific_date']} at {time_display}"

    freq = task.get("frequency", "")
    if freq == "daily":
        return f"daily at {time_display}"
    if freq == "weekly" and task.get("reminder_day"):
        return f"every {task['reminder_day'].capitalize()} at {time_display}"
    if freq == "custom" and task.get("cron_expression"):
        return f"custom ({task['cron_expression']})"
    return f"at {time_display}"
