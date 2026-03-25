"""
Telegram bot command handlers.

Implements /start, /new, /tasks, /done, /history, /pause, /resume,
/delete, and /timezone commands.
"""

import logging
from datetime import date
from typing import Optional

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

import database as db
from ai_parser import format_parsed_task_for_display, parse_task_description
from database import fmt_time, make_display_name
from scheduler import remove_task_jobs, schedule_task_jobs, send_dm_with_fallback

logger = logging.getLogger(__name__)

# ConversationHandler states
AWAITING_DESCRIPTION = 0
AWAITING_CONFIRMATION = 1

WELCOME_TEXT = (
    "👋 <b>Welcome to the Shared Task Reminder Bot!</b>\n\n"
    "I help groups stay on top of recurring tasks by sending reminders and "
    "tracking who completed what.\n\n"
    "<b>Commands:</b>\n"
    "• /new — Create a new recurring reminder\n"
    "• /tasks — List all active tasks\n"
    "• /done &lt;number&gt; — Mark a task as done for today\n"
    "• /history &lt;number&gt; — View completion history\n"
    "• /pause &lt;number&gt; — Pause a task\n"
    "• /resume &lt;number&gt; — Resume a paused task\n"
    "• /delete &lt;number&gt; — Delete a task\n"
    "• /timezone — Set your timezone\n\n"
    "<b>Pro tip:</b> You can also describe tasks in plain English!\n"
    "Try: <i>\"Remind me and @roommate to take out the trash every Monday at 7pm\"</i>"
)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — send the welcome message and register the user for DMs."""
    user = update.effective_user

    # Persist the user so the bot can DM them later, and resolve any task
    # assignments that were created before they started the bot.
    await db.upsert_known_user(user.id, user.username, user.first_name)
    if user.username:
        updated = await db.update_assignee_user_ids(user.username, user.id)
        if updated:
            logger.info(
                "Linked %d pending assignment(s) to user %d (@%s).",
                updated, user.id, user.username,
            )

    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /new — conversation entry point
# ---------------------------------------------------------------------------

async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handle /new — begin the task-creation conversation.

    If arguments are provided (e.g. /new Feed the dog every day at 8am),
    jump straight to parsing. Otherwise, ask for a description.
    """
    args_text = " ".join(context.args) if context.args else ""
    if args_text:
        return await _parse_and_confirm(update, context, args_text)

    await update.message.reply_text(
        "📝 <b>New task</b>\n\nDescribe the task in plain English. For example:\n"
        "<i>\"Remind me and @sister to water the plants every day at 7pm\"</i>",
        parse_mode=ParseMode.HTML,
    )
    return AWAITING_DESCRIPTION


async def receive_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the task description message sent after /new."""
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Please send a task description.")
        return AWAITING_DESCRIPTION
    return await _parse_and_confirm(update, context, text)


async def _parse_and_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> int:
    """Parse the description with Claude and show a confirmation message."""
    user = update.effective_user
    username = user.username

    status_msg = await update.message.reply_text("🤔 Parsing your task…")

    parsed = await parse_task_description(text, requesting_username=username)

    if not parsed:
        await status_msg.edit_text(
            "❌ Sorry, I couldn't understand that task description. "
            "Please try again with more detail.\n\n"
            "Example: <i>Feed the dog every day at 8am</i>",
            parse_mode=ParseMode.HTML,
        )
        return AWAITING_DESCRIPTION

    # Store parsed data for the confirmation step
    context.user_data["pending_task"] = {
        "parsed": parsed,
        "raw_text": text,
        "chat_id": update.effective_chat.id,
        "created_by": user.id,
        "created_by_username": username,
        "created_by_first_name": user.first_name,
    }

    display_text = format_parsed_task_for_display(parsed, requesting_username=username)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data="confirm_task"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_task"),
        ]
    ])

    await status_msg.edit_text(
        display_text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return AWAITING_CONFIRMATION


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel — abort an in-progress conversation."""
    context.user_data.pop("pending_task", None)
    await update.message.reply_text("❌ Task creation cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------

async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all active tasks for this chat with their current completion status."""
    chat_id = update.effective_chat.id
    today = date.today().isoformat()
    tasks = await db.get_tasks_for_chat(chat_id)

    if not tasks:
        await update.message.reply_text(
            "No active tasks yet. Use /new to create one!"
        )
        return

    lines = [f"📋 <b>Active tasks</b> — {date.today().strftime('%A, %B %d')}\n"]
    buttons = []

    for idx, task in enumerate(tasks, start=1):
        task_id = task["id"]
        reminder_times = await db.get_reminder_times(task_id)
        completions = await db.get_completions_for_date(task_id, today)
        completed_ids = {c["reminder_time_id"] for c in completions}
        freq = _format_frequency(task, reminder_times)

        if reminder_times and len(reminder_times) > 1:
            all_done = len(completions) >= len(reminder_times)
            status_icon = "✅" if all_done else "⏰"
            slot_parts = [
                f"{'✅' if rt['id'] in completed_ids else '⏳'} {fmt_time(rt['reminder_time'])}"
                for rt in reminder_times
            ]
            lines.append(
                f"{idx}. {status_icon} <b>{task['description']}</b>\n"
                f"   <i>{freq}</i>\n"
                f"   {' · '.join(slot_parts)}"
            )
            row = []
            for rt in reminder_times:
                if rt["id"] not in completed_ids:
                    row.append(InlineKeyboardButton(
                        f"✅ Done ({fmt_time(rt['reminder_time'])})",
                        callback_data=f"done:{task_id}:{rt['id']}",
                    ))
                    break
        else:
            completion = completions[0] if completions else None
            status_icon = "✅" if completion else "⏰"
            if completion:
                done_by = completion.get("completed_by_name") or make_display_name(
                    completion.get("completed_by_username")
                )
                status_note = f"done by {done_by}"
            else:
                status_note = "pending"
            lines.append(
                f"{idx}. {status_icon} <b>{task['description']}</b>\n"
                f"   <i>{freq} — {status_note}</i>"
            )
            row = []
            if not completion:
                rtime_id = reminder_times[0]["id"] if reminder_times else 0
                row.append(InlineKeyboardButton("✅ Done", callback_data=f"done:{task_id}:{rtime_id}"))

        if task["is_active"]:
            row.append(InlineKeyboardButton("⏸ Pause", callback_data=f"pause:{task_id}"))
        row.append(InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{task_id}"))
        buttons.append(row)

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


def _format_frequency(task: dict, reminder_times: Optional[list] = None) -> str:
    """Return a human-readable schedule string for a task."""
    freq = task["frequency"]

    if reminder_times:
        times_str = " and ".join(fmt_time(rt["reminder_time"]) for rt in reminder_times)
    else:
        times_str = fmt_time(task.get("reminder_time", ""))

    if freq == "daily":
        return f"Daily at {times_str}"
    elif freq == "weekly" and task.get("reminder_day"):
        return f"Every {task['reminder_day'].capitalize()} at {times_str}"
    elif freq == "custom" and task.get("cron_expression"):
        return f"Custom ({task['cron_expression']}) at {times_str}"
    return f"At {times_str}"


# ---------------------------------------------------------------------------
# /done <number>
# ---------------------------------------------------------------------------

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark a task as done by its list number."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Usage: /done <task number>\nUse /tasks to see the numbered list."
        )
        return

    try:
        task_number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a valid task number.")
        return

    tasks = await db.get_tasks_for_chat(chat_id)
    if task_number < 1 or task_number > len(tasks):
        await update.message.reply_text(
            f"Task #{task_number} not found. Use /tasks to see available tasks."
        )
        return

    task = tasks[task_number - 1]
    await _mark_task_done(update, context, task, user)


async def _mark_task_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    task: dict,
    user,
) -> None:
    """Core logic for recording a task completion and notifying assignees."""
    import pytz
    from datetime import datetime as _dt

    chat_id = update.effective_chat.id
    today = date.today().isoformat()
    mention = make_display_name(user.username, user.first_name)

    # Determine which reminder slot to mark (most recent past uncompleted)
    timezone_str = await db.get_chat_timezone(chat_id)
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.UTC
    now_time_str = _dt.now(tz).strftime("%H:%M")
    rtime_id = await db.get_active_slot(task["id"], now_time_str, today) or 0

    success = await db.record_completion(
        task_id=task["id"],
        user_id=user.id,
        username=user.username,
        date_for=today,
        display_name=mention,
        reminder_time_id=rtime_id,
    )

    if not success:
        completion = await db.get_completion_for_date(task["id"], today)
        done_by = (completion or {}).get("completed_by_name") or make_display_name(
            (completion or {}).get("completed_by_username")
        )
        await update.message.reply_text(
            f"ℹ️ <b>{task['description']}</b> was already marked done by {done_by} today.",
            parse_mode=ParseMode.HTML,
        )
        return
    await update.message.reply_text(
        f"✅ <b>{task['description']}</b> marked as done by {mention}!",
        parse_mode=ParseMode.HTML,
    )

    # Notify other assignees via DM with group-chat fallback
    assignees = await db.get_task_assignees(task["id"])
    for assignee in assignees:
        if assignee["telegram_user_id"] == user.id:
            continue
        await send_dm_with_fallback(
            bot=context.bot,
            user_id=assignee["telegram_user_id"],
            username=assignee.get("telegram_username"),
            group_chat_id=task["chat_id"],
            text=(
                f"✅ <b>{task['description']}</b> has been completed by {mention}. "
                "No action needed from you!"
            ),
        )


# ---------------------------------------------------------------------------
# /history <number>
# ---------------------------------------------------------------------------

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the completion history for a task over the last 7 days."""
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("Usage: /history <task number>")
        return

    try:
        task_number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a valid task number.")
        return

    tasks = await db.get_tasks_for_chat(chat_id)
    if task_number < 1 or task_number > len(tasks):
        await update.message.reply_text(
            f"Task #{task_number} not found. Use /tasks to see available tasks."
        )
        return

    task = tasks[task_number - 1]
    history = await db.get_task_history(task["id"], days=7)

    lines = [f"📅 <b>History: {task['description']}</b> (last 7 days)\n"]

    if not history:
        lines.append("No completions recorded in the last 7 days.")
    else:
        for entry in history:
            done_by = entry.get("completed_by_name") or make_display_name(
                entry.get("completed_by_username")
            )
            lines.append(f"  ✅ {entry['date_for']} — {done_by}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /pause and /resume <number>
# ---------------------------------------------------------------------------

async def _toggle_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    active: bool,
    bot,
) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        action = "resume" if active else "pause"
        await update.message.reply_text(f"Usage: /{action} <task number>")
        return

    try:
        task_number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a valid task number.")
        return

    tasks = await db.get_tasks_for_chat(chat_id, active_only=False)
    if task_number < 1 or task_number > len(tasks):
        await update.message.reply_text("Task not found.")
        return

    task = tasks[task_number - 1]

    # Only the creator can pause/resume
    if task["created_by"] != user.id:
        await update.message.reply_text("❌ Only the task creator can pause or resume it.")
        return

    updated = await db.set_task_active(task["id"], active)
    if not updated:
        await update.message.reply_text("Task not found.")
        return

    if active:
        await schedule_task_jobs(task, bot)
        await update.message.reply_text(
            f"▶️ <b>{task['description']}</b> has been resumed.",
            parse_mode=ParseMode.HTML,
        )
    else:
        remove_task_jobs(task["id"])
        await update.message.reply_text(
            f"⏸ <b>{task['description']}</b> has been paused.",
            parse_mode=ParseMode.HTML,
        )


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pause <number>."""
    await _toggle_task(update, context, active=False, bot=context.bot)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /resume <number>."""
    await _toggle_task(update, context, active=True, bot=context.bot)


# ---------------------------------------------------------------------------
# /delete <number>
# ---------------------------------------------------------------------------

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /delete <number> — permanently remove a task (creator only)."""
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /delete <task number>")
        return

    try:
        task_number = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Please provide a valid task number.")
        return

    tasks = await db.get_tasks_for_chat(chat_id, active_only=False)
    if task_number < 1 or task_number > len(tasks):
        await update.message.reply_text("Task not found.")
        return

    task = tasks[task_number - 1]

    if task["created_by"] != user.id:
        await update.message.reply_text("❌ Only the task creator can delete it.")
        return

    remove_task_jobs(task["id"])
    await db.delete_task(task["id"])
    await update.message.reply_text(
        f"🗑 <b>{task['description']}</b> has been deleted.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# /timezone
# ---------------------------------------------------------------------------

COMMON_TIMEZONES = [
    ("🌎 Córdoba / Buenos Aires", "America/Argentina/Cordoba"),
    ("🌎 New York", "America/New_York"),
    ("🌎 Chicago", "America/Chicago"),
    ("🌎 Denver", "America/Denver"),
    ("🌎 Los Angeles", "America/Los_Angeles"),
    ("🌍 London", "Europe/London"),
    ("🌍 Paris", "Europe/Paris"),
    ("🌍 Berlin", "Europe/Berlin"),
    ("🌏 Tokyo", "Asia/Tokyo"),
    ("🌏 Singapore", "Asia/Singapore"),
    ("🌏 Sydney", "Australia/Sydney"),
    ("🕐 UTC", "UTC"),
]


async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /timezone — let users pick or set a timezone for this chat."""
    if context.args:
        tz_str = context.args[0]
        try:
            pytz.timezone(tz_str)
        except pytz.UnknownTimeZoneError:
            await update.message.reply_text(
                f"❌ Unknown timezone: <code>{tz_str}</code>\n"
                "Use a tz database name like <code>America/New_York</code> or "
                "<code>Europe/London</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        await db.set_chat_timezone(update.effective_chat.id, tz_str)
        await update.message.reply_text(
            f"🕐 Timezone set to <b>{tz_str}</b> for this chat.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Show quick-pick keyboard
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"tz:{tz}")]
        for label, tz in COMMON_TIMEZONES
    ]
    current = await db.get_chat_timezone(update.effective_chat.id)
    await update.message.reply_text(
        f"🕐 Current timezone: <b>{current}</b>\n\n"
        "Choose a timezone or send <code>/timezone America/New_York</code> "
        "for a custom one:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
