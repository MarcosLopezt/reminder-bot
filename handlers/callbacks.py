"""
Inline button callback query handlers.

Handles task creation confirmation, mark-as-done buttons,
pause/delete quick-actions from /tasks, and timezone selection.
"""

import logging
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes, ConversationHandler

import database as db
from database import make_display_name
from scheduler import (
    notify_task_assigned,
    remove_task_jobs,
    schedule_task_jobs,
    send_dm_with_fallback,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _notify_other_assignees(task: dict, done_by_user_id: int, done_by_username: str, bot) -> None:
    """
    DM every assignee (except the completer) that the task is done.

    Uses send_dm_with_fallback so users who haven't started the bot get a
    group-chat prompt instead of a silent failure.
    """
    assignees = await db.get_task_assignees(task["id"])
    group_chat_id = task["chat_id"]
    mention = f"@{done_by_username}" if done_by_username else "Someone"

    for assignee in assignees:
        if assignee["telegram_user_id"] == done_by_user_id:
            continue  # Don't notify the person who just completed it
        await send_dm_with_fallback(
            bot=bot,
            user_id=assignee["telegram_user_id"],
            username=assignee.get("telegram_username"),
            group_chat_id=group_chat_id,
            text=(
                f"✅ <b>{task['description']}</b> has been completed "
                f"by {mention}. No action needed from you!"
            ),
        )


# ---------------------------------------------------------------------------
# Task creation: confirm / cancel
# ---------------------------------------------------------------------------

async def confirm_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handle the 'Confirm' button during task creation.

    Saves the task to the database, schedules its jobs, and ends the conversation.
    """
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get("pending_task")
    if not pending:
        await query.edit_message_text("⚠️ Session expired. Please start over with /new.")
        return ConversationHandler.END

    parsed = pending["parsed"]
    chat_id = pending["chat_id"]
    created_by = pending["created_by"]
    created_by_username = pending["created_by_username"]
    created_by_first_name = pending.get("created_by_first_name")
    timezone = await db.get_chat_timezone(chat_id)

    # Normalise reminder_times before create_task (Gemini returns a list now)
    raw_times_pre = parsed.get("reminder_times") or [parsed.get("reminder_time", "09:00")]
    if isinstance(raw_times_pre, str):
        raw_times_pre = [raw_times_pre]
    first_time = (raw_times_pre[0] if raw_times_pre else None) or "09:00"

    # Create the task record
    task_id = await db.create_task(
        chat_id=chat_id,
        description=parsed["description"],
        frequency=parsed.get("frequency") or "daily",
        reminder_time=first_time,
        created_by=created_by,
        created_by_username=created_by_username,
        reminder_day=parsed.get("reminder_day"),
        cron_expression=parsed.get("cron_expression"),
        timezone=timezone,
        task_type=parsed.get("task_type", "recurring"),
        specific_date=parsed.get("specific_date"),
    )

    # Add the creator as an assignee if include_self
    if parsed.get("include_self", True):
        await db.add_task_assignee(task_id, created_by, created_by_username)

    # Add other mentioned assignees, resolving user_ids where possible
    unknown_assignees = []
    for username in parsed.get("assignees", []):
        known = await db.get_known_user_by_username(username)
        if known:
            await db.add_task_assignee(task_id, known["user_id"], username)
        else:
            # Store with placeholder — will be resolved when they /start the bot
            await db.add_task_assignee(task_id, 0, username)
            unknown_assignees.append(username)

    if unknown_assignees:
        bot_username = context.bot.username or "this bot"
        known_users = await db.get_all_known_users()
        known_list = (
            "\n".join(f"  • @{u['username']}" for u in known_users if u.get("username"))
            or "  (nobody has started the bot yet)"
        )
        missing = "\n".join(
            f"⚠️ I don't have a connection with @{u} yet — they need to open "
            f"@{bot_username} and press Start so I can send them reminders."
            for u in unknown_assignees
        )
        await query.message.reply_text(
            f"{missing}\n\n<b>Users I already know:</b>\n{known_list}",
            parse_mode="HTML",
        )

    # Normalise reminder_times (accept both legacy reminder_time str and new list)
    raw_times = parsed.get("reminder_times") or [parsed.get("reminder_time", "09:00")]
    if isinstance(raw_times, str):
        raw_times = [raw_times]
    reminder_times = [t for t in raw_times if t] or ["09:00"]

    # Persist reminder time slots
    for i, t in enumerate(reminder_times):
        await db.add_reminder_time(task_id, t, sort_order=i)

    # Schedule the reminder jobs
    task = await db.get_task_by_id(task_id)
    await schedule_task_jobs(task, context.bot)

    # Notify assignees (who have already /started the bot) that they're assigned
    await notify_task_assigned(
        task=task,
        created_by=created_by,
        created_by_username=created_by_username,
        created_by_first_name=created_by_first_name,
        bot=context.bot,
        group_chat_id=chat_id,
    )

    context.user_data.pop("pending_task", None)

    await query.edit_message_text(
        f"✅ <b>Task created!</b>\n\n"
        f"📝 {parsed['description']}\n"
        f"I'll remind you according to the schedule. Use /tasks to see all tasks.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cancel_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle the 'Cancel' button during task creation."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_task", None)
    await query.edit_message_text("❌ Task creation cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Mark as done
# ---------------------------------------------------------------------------

async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'Mark as done' inline button on reminder messages."""
    query = update.callback_query
    await query.answer()

    # Format: done:{task_id}:{reminder_time_id}  (reminder_time_id defaults to 0)
    parts = query.data.split(":")
    task_id = int(parts[1])
    reminder_time_id = int(parts[2]) if len(parts) > 2 else 0

    user = update.effective_user
    today = date.today().isoformat()

    task = await db.get_task_by_id(task_id)
    if not task:
        await query.edit_message_text("⚠️ Task not found (it may have been deleted).")
        return

    mention = make_display_name(user.username, user.first_name)

    success = await db.record_completion(
        task_id=task_id,
        user_id=user.id,
        username=user.username,
        date_for=today,
        display_name=mention,
        reminder_time_id=reminder_time_id,
    )

    if success:
        await query.edit_message_text(
            f"✅ <b>{task['description']}</b> marked as done by {mention}!",
            parse_mode=ParseMode.HTML,
        )
        await _notify_other_assignees(task, user.id, user.username, context.bot)
    else:
        completion = await db.get_completion_for_slot(task_id, reminder_time_id, today)
        if not completion:
            completion = await db.get_completion_for_date(task_id, today)
        done_by = (completion or {}).get("completed_by_name") or make_display_name(
            (completion or {}).get("completed_by_username")
        )
        await query.edit_message_text(
            f"ℹ️ <b>{task['description']}</b> was already done today by {done_by}.",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Quick-action buttons from /tasks list
# ---------------------------------------------------------------------------

async def pause_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'Pause' inline button from the /tasks list."""
    query = update.callback_query
    await query.answer()

    _, task_id_str = query.data.split(":", 1)
    task_id = int(task_id_str)
    user = update.effective_user

    task = await db.get_task_by_id(task_id)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return

    if task["created_by"] != user.id:
        await query.answer("Only the creator can pause this task.", show_alert=True)
        return

    remove_task_jobs(task_id)
    await db.set_task_active(task_id, False)
    await query.answer(f"⏸ '{task['description']}' paused.", show_alert=True)


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 'Delete' inline button from the /tasks list."""
    query = update.callback_query
    await query.answer()

    _, task_id_str = query.data.split(":", 1)
    task_id = int(task_id_str)
    user = update.effective_user

    task = await db.get_task_by_id(task_id)
    if not task:
        await query.answer("Task not found.", show_alert=True)
        return

    if task["created_by"] != user.id:
        await query.answer("Only the creator can delete this task.", show_alert=True)
        return

    # Show a confirmation step before deleting
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirm_delete:{task_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete"),
        ]
    ])
    await query.edit_message_reply_markup(reply_markup=keyboard)
    await query.answer()


async def confirm_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle confirmed task deletion from the inline keyboard."""
    query = update.callback_query
    await query.answer()

    _, task_id_str = query.data.split(":", 1)
    task_id = int(task_id_str)

    task = await db.get_task_by_id(task_id)
    desc = task["description"] if task else "Unknown task"

    remove_task_jobs(task_id)
    await db.delete_task(task_id)
    await query.edit_message_text(
        f"🗑 <b>{desc}</b> has been deleted.",
        parse_mode=ParseMode.HTML,
    )


async def cancel_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a pending deletion — restore the original task list view."""
    query = update.callback_query
    await query.answer("Deletion cancelled.")
    # Just remove the confirmation buttons; user can run /tasks again for the full view.
    await query.edit_message_reply_markup(reply_markup=None)


# ---------------------------------------------------------------------------
# Timezone quick-select
# ---------------------------------------------------------------------------

async def timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle timezone selection from the inline keyboard."""
    query = update.callback_query
    await query.answer()

    _, tz_str = query.data.split(":", 1)
    chat_id = update.effective_chat.id
    await db.set_chat_timezone(chat_id, tz_str)
    await query.edit_message_text(
        f"🕐 Timezone set to <b>{tz_str}</b> for this chat. "
        "New tasks will use this timezone.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Dispatcher — single callback handler that routes by prefix
# ---------------------------------------------------------------------------

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Route all callback queries to the appropriate handler based on the data prefix.

    This is registered as the single CallbackQueryHandler in bot.py
    (for callbacks not handled by the ConversationHandler).
    """
    query = update.callback_query
    data = query.data or ""

    if data == "confirm_task":
        await confirm_task_callback(update, context)
    elif data == "cancel_task":
        await cancel_task_callback(update, context)
    elif data.startswith("done:"):
        await done_callback(update, context)
    elif data.startswith("pause:"):
        await pause_callback(update, context)
    elif data.startswith("delete:"):
        await delete_callback(update, context)
    elif data.startswith("confirm_delete:"):
        await confirm_delete_callback(update, context)
    elif data == "cancel_delete":
        await cancel_delete_callback(update, context)
    elif data.startswith("tz:"):
        await timezone_callback(update, context)
    else:
        await query.answer("Unknown action.")
        logger.warning("Unhandled callback data: %s", data)
