"""
Reminder dispatch logic using PTB's built-in JobQueue.

PTB's JobQueue is correctly wired to PTB's own asyncio event loop, which
eliminates the event-loop binding issues that arise when using a standalone
APScheduler instance alongside python-telegram-bot 21.x.
"""

import logging
from datetime import date, datetime, time as dtime
from typing import Optional

import pytz
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import CallbackContext, JobQueue

import database as db
from database import make_display_name

logger = logging.getLogger(__name__)

# Set after setup_scheduler() is called from bot.py's post_init hook.
job_queue: Optional[JobQueue] = None

# Day-name → APScheduler day_of_week integer (Mon=0 … Sun=6)
_DAY_MAP = {
    "monday": "mon", "tuesday": "tue", "wednesday": "wed",
    "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
}


# ---------------------------------------------------------------------------
# APScheduler error / miss listener (attached to the PTB scheduler)
# ---------------------------------------------------------------------------

def _job_listener(event) -> None:
    """Log job errors and missed executions so they are never silently lost."""
    if hasattr(event, "exception") and event.exception:
        logger.error(
            "Job '%s' raised an exception: %s",
            event.job_id, event.exception,
            exc_info=event.traceback,
        )
    else:
        logger.warning("Job '%s' missed its scheduled execution.", event.job_id)


# ---------------------------------------------------------------------------
# DM helper with group-chat fallback
# ---------------------------------------------------------------------------

async def _post_start_prompt(bot: Bot, username_or_id: str, group_chat_id: int) -> None:
    """Post a one-time notice in the group asking a user to start the bot."""
    bot_username = bot.username or "this bot"
    mention = (
        f"@{username_or_id}"
        if not username_or_id.startswith("@") and not username_or_id.lstrip("-").isdigit()
        else username_or_id
    )
    try:
        await bot.send_message(
            chat_id=group_chat_id,
            text=(
                f"ℹ️ {mention} needs to start the bot to receive direct reminders — "
                f"they should message @{bot_username} and press Start."
            ),
            parse_mode="HTML",
        )
    except TelegramError as exc:
        logger.error(
            "Failed to post start-prompt for %s in chat %d: %s",
            mention, group_chat_id, exc,
        )


async def send_dm_with_fallback(
    bot: Bot,
    user_id: int,
    username: Optional[str],
    group_chat_id: int,
    text: str,
    reply_markup=None,
) -> None:
    """
    Send a private DM, falling back gracefully if the user hasn't started the bot.

    - user_id=0: tries to resolve from known_users first.
    - Forbidden / BadRequest: posts a start-prompt to the group instead.
    - user_id == group_chat_id: skipped (private-chat task, would be a duplicate).
    """
    if user_id == 0 and username:
        known = await db.get_known_user_by_username(username)
        if known:
            user_id = known["user_id"]

    if user_id == 0:
        await _post_start_prompt(bot, username or "An assignee", group_chat_id)
        return

    if user_id == group_chat_id:
        return

    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        logger.debug("DM sent to user %d (%s).", user_id, username)
    except (Forbidden, BadRequest) as exc:
        logger.info("Cannot DM user %d (%s): %s", user_id, username, exc)
        await _post_start_prompt(bot, username or str(user_id), group_chat_id)
    except TelegramError as exc:
        logger.error("Failed to DM user %d: %s", user_id, exc)


# ---------------------------------------------------------------------------
# Assignee notification (sent once when a task is created)
# ---------------------------------------------------------------------------

def _format_assignment_schedule(task: dict) -> str:
    """Short human-readable schedule string for assignment DMs."""
    freq = task.get("frequency", "")
    t = task.get("reminder_time", "")
    try:
        h, m = t.split(":")
        display_time = dtime(int(h), int(m)).strftime("%I:%M %p").lstrip("0")
    except Exception:
        display_time = t

    if freq == "daily":
        return f"daily at {display_time}"
    if freq == "weekly" and task.get("reminder_day"):
        return f"every {task['reminder_day'].capitalize()} at {display_time}"
    if freq == "custom" and task.get("cron_expression"):
        return f"custom ({task['cron_expression']})"
    return f"at {display_time}"


async def notify_task_assigned(
    task: dict,
    created_by: int,
    created_by_username: Optional[str],
    created_by_first_name: Optional[str],
    bot: Bot,
    group_chat_id: int,
) -> None:
    """
    DM each non-creator assignee (who has /started the bot) about the new task.
    Assignees with user_id=0 are skipped — the confirm flow already warned the creator.
    """
    creator = make_display_name(created_by_username, created_by_first_name)
    schedule_str = _format_assignment_schedule(task)
    assignees = await db.get_task_assignees(task["id"])

    for assignee in assignees:
        uid = assignee["telegram_user_id"]
        if uid == created_by or uid == 0:
            continue
        await send_dm_with_fallback(
            bot=bot,
            user_id=uid,
            username=assignee.get("telegram_username"),
            group_chat_id=group_chat_id,
            text=(
                f"📋 <b>New task assigned</b> by {creator}:\n"
                f"<b>{task['description']}</b> — <i>{schedule_str}</i>\n\n"
                "Use /tasks to see all your tasks."
            ),
        )


# ---------------------------------------------------------------------------
# Job scheduling helpers
# ---------------------------------------------------------------------------

def _parse_time(reminder_time: str) -> tuple[int, int]:
    h, m = reminder_time.split(":")
    return int(h), int(m)


def _remove_jobs(name: str) -> None:
    """Cancel all PTB jobs with the given name."""
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()
        logger.debug("Scheduled removal of job '%s'.", name)


def schedule_task_jobs(task: dict, bot: Bot = None) -> None:
    """
    Register PTB JobQueue jobs for a single task.

    One-time tasks use run_once(); recurring tasks use CronTrigger via run_custom().
    Both run on PTB's own asyncio event loop — no event-loop bridging required.
    """
    if job_queue is None:
        logger.error("schedule_task_jobs called before setup_scheduler! Task %d skipped.", task["id"])
        return

    task_id = task["id"]
    chat_id = task["chat_id"]
    timezone_str = task.get("timezone", "UTC")
    task_type = task.get("task_type", "recurring")

    try:
        tz = pytz.timezone(timezone_str)
    except pytz.UnknownTimeZoneError:
        logger.warning("Unknown timezone '%s' for task %d, falling back to UTC.", timezone_str, task_id)
        tz = pytz.UTC

    hour, minute = _parse_time(task["reminder_time"])
    job_data = {"task_id": task_id, "chat_id": chat_id}
    job_id_primary = f"reminder_{task_id}"
    job_id_followup = f"followup_{task_id}"

    # Remove any stale jobs first
    _remove_jobs(job_id_primary)
    _remove_jobs(job_id_followup)

    # --- One-time task: fire once at specific_date + reminder_time ---
    if task_type == "one_time":
        specific_date = task.get("specific_date")
        if not specific_date:
            logger.error("One-time task %d missing specific_date; skipping.", task_id)
            return
        try:
            naive_dt = datetime.strptime(f"{specific_date} {hour:02d}:{minute:02d}", "%Y-%m-%d %H:%M")
            aware_dt = tz.localize(naive_dt)
        except Exception as exc:
            logger.error("Cannot parse one-time datetime for task %d: %s", task_id, exc)
            return

        now_utc = datetime.now(pytz.UTC)
        if aware_dt <= now_utc:
            logger.info("One-time task %d at %s has already passed; skipping.", task_id, aware_dt)
            return

        job = job_queue.run_once(
            _one_time_reminder_callback,
            when=aware_dt,
            data=job_data,
            name=job_id_primary,
        )
        logger.info("Scheduled one-time reminder '%s' — fires at: %s  (tz=%s)", job_id_primary, aware_dt, timezone_str)
        return

    # --- Recurring task ---
    frequency = task["frequency"]
    reminder_day = task.get("reminder_day")

    if frequency == "daily":
        primary_trigger = CronTrigger(hour=hour, minute=minute, timezone=tz)
    elif frequency == "weekly" and reminder_day:
        day_str = _DAY_MAP.get(reminder_day.lower(), reminder_day[:3].lower())
        primary_trigger = CronTrigger(day_of_week=day_str, hour=hour, minute=minute, timezone=tz)
    elif frequency == "custom" and task.get("cron_expression"):
        parts = task["cron_expression"].split()
        if len(parts) != 5:
            logger.error("Invalid cron expression for task %d: %s", task_id, task["cron_expression"])
            return
        primary_trigger = CronTrigger(
            minute=parts[0], hour=parts[1],
            day=parts[2], month=parts[3], day_of_week=parts[4],
            timezone=tz,
        )
    else:
        logger.error("Cannot schedule task %d: unrecognised frequency '%s'.", task_id, frequency)
        return

    primary_job = job_queue.run_custom(
        _reminder_callback,
        job_kwargs={"trigger": primary_trigger, "id": job_id_primary, "replace_existing": True},
        data=job_data,
        name=job_id_primary,
    )
    logger.info(
        "Scheduled primary reminder '%s' — next run: %s  (tz=%s)",
        job_id_primary,
        getattr(primary_job.job, 'next_run_time', 'N/A'),
        timezone_str,
    )

    # --- Follow-up trigger (2 h after primary, skip for custom) ---
    followup_trigger = None
    followup_hour = (hour + 2) % 24
    if frequency == "daily":
        followup_trigger = CronTrigger(hour=followup_hour, minute=minute, timezone=tz)
    elif frequency == "weekly" and reminder_day:
        followup_trigger = CronTrigger(
            day_of_week=day_str, hour=followup_hour, minute=minute, timezone=tz
        )

    if followup_trigger:
        followup_job = job_queue.run_custom(
            _followup_callback,
            job_kwargs={"trigger": followup_trigger, "id": job_id_followup, "replace_existing": True},
            data=job_data,
            name=job_id_followup,
        )
        logger.info(
            "Scheduled follow-up reminder '%s' — next run: %s",
            job_id_followup,
            getattr(followup_job.job, 'next_run_time', 'N/A'),
        )

    # --- Daily summary (one per chat_id) ---
    summary_job_id = f"summary_{chat_id}"
    if not job_queue.get_jobs_by_name(summary_job_id):
        summary_trigger = CronTrigger(hour=23, minute=30, timezone=tz)
        summary_job = job_queue.run_custom(
            _daily_summary_callback,
            job_kwargs={"trigger": summary_trigger, "id": summary_job_id, "replace_existing": True},
            data={"chat_id": chat_id},
            name=summary_job_id,
        )
        logger.info(
            "Scheduled daily summary '%s' — next run: %s",
            summary_job_id,
            getattr(summary_job.job, 'next_run_time', 'N/A'),
        )


def remove_task_jobs(task_id: int) -> None:
    """Cancel all PTB jobs associated with a task."""
    for prefix in ("reminder_", "followup_"):
        _remove_jobs(f"{prefix}{task_id}")


async def load_all_tasks(bot: Bot) -> None:
    """
    Reload every active task from the database into the job queue.
    Called once at startup and prints a summary of all scheduled jobs.
    """
    tasks = await db.get_all_active_tasks()
    logger.info("Loading %d active task(s) into job queue.", len(tasks))
    for task in tasks:
        try:
            schedule_task_jobs(task, bot)
        except Exception:
            logger.exception("Failed to schedule task %d.", task["id"])

    # Print all jobs so the operator can verify next-run times at a glance
    _print_scheduled_jobs()


def _print_scheduled_jobs() -> None:
    """Log a table of every scheduled job and its next run time."""
    if job_queue is None:
        return
    all_jobs = job_queue.jobs()
    if not all_jobs:
        logger.info("No jobs currently scheduled.")
        return
    logger.info("=== Scheduled jobs (%d) ===", len(all_jobs))
    for j in sorted(all_jobs, key=lambda x: (getattr(x.job, 'next_run_time', None) or 0)):
        logger.info("  %-35s  next: %s", j.name, getattr(j.job, 'next_run_time', 'N/A'))
    logger.info("=== End of job list ===")


# ---------------------------------------------------------------------------
# PTB job callbacks — receive context from the job queue
# ---------------------------------------------------------------------------

async def _reminder_callback(context: CallbackContext) -> None:
    data = context.job.data
    task_id, chat_id = data["task_id"], data["chat_id"]
    logger.info(
        ">>> REMINDER JOB FIRED  task_id=%d  chat_id=%d  date=%s",
        task_id, chat_id, date.today().isoformat(),
    )
    await _send_reminder(task_id, chat_id, context.bot)


async def _followup_callback(context: CallbackContext) -> None:
    data = context.job.data
    task_id, chat_id = data["task_id"], data["chat_id"]
    logger.info(
        ">>> FOLLOW-UP JOB FIRED  task_id=%d  chat_id=%d",
        task_id, chat_id,
    )
    await _send_followup(task_id, chat_id, context.bot)


async def _daily_summary_callback(context: CallbackContext) -> None:
    chat_id = context.job.data["chat_id"]
    logger.info(">>> DAILY SUMMARY JOB FIRED  chat_id=%d", chat_id)
    await send_daily_summary(chat_id, context.bot)


async def _one_time_reminder_callback(context: CallbackContext) -> None:
    data = context.job.data
    task_id, chat_id = data["task_id"], data["chat_id"]
    logger.info(">>> ONE-TIME REMINDER JOB FIRED  task_id=%d  chat_id=%d", task_id, chat_id)
    await _send_reminder(task_id, chat_id, context.bot)
    # Auto-deactivate after firing
    await db.set_task_active(task_id, False)
    logger.info("One-time task %d auto-deactivated.", task_id)


# ---------------------------------------------------------------------------
# Reminder senders (actual logic, separate from PTB callback wrappers)
# ---------------------------------------------------------------------------

def _make_done_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Mark as done", callback_data=f"done:{task_id}")]]
    )


def _completion_display_name(completion: dict) -> str:
    """Return the best display name stored in a completion record."""
    name = completion.get("completed_by_name")
    if name:
        return name
    username = completion.get("completed_by_username")
    return f"@{username}" if username else "someone"


async def _send_reminder(task_id: int, chat_id: int, bot: Bot) -> None:
    task = await db.get_task_by_id(task_id)
    if not task or not task["is_active"]:
        logger.info("Task %d not found or inactive; skipping.", task_id)
        return

    today = date.today().isoformat()
    completion = await db.get_completion_for_date(task_id, today)

    try:
        if completion:
            done_by = _completion_display_name(completion)
            logger.info("Task %d already done today by %s; sending status message.", task_id, done_by)
            await bot.send_message(
                chat_id=chat_id,
                text=f"✅ <b>{task['description']}</b> — already done today by {done_by}! Nothing to worry about.",
                parse_mode="HTML",
            )
        else:
            logger.info("Task %d not done yet; sending reminder to chat %d.", task_id, chat_id)
            await bot.send_message(
                chat_id=chat_id,
                text=f"🔔 <b>Reminder:</b> {task['description']} — nobody has done it yet today!",
                parse_mode="HTML",
                reply_markup=_make_done_keyboard(task_id),
            )
            assignees = await db.get_task_assignees(task_id)
            logger.info("DMing %d assignee(s) for task %d.", len(assignees), task_id)
            for assignee in assignees:
                await send_dm_with_fallback(
                    bot=bot,
                    user_id=assignee["telegram_user_id"],
                    username=assignee.get("telegram_username"),
                    group_chat_id=chat_id,
                    text=(
                        f"🔔 <b>Reminder:</b> {task['description']} — "
                        "it's your turn to take care of this!"
                    ),
                    reply_markup=_make_done_keyboard(task_id),
                )
    except TelegramError as exc:
        logger.error("Failed to send reminder for task %d: %s", task_id, exc)


async def _send_followup(task_id: int, chat_id: int, bot: Bot) -> None:
    task = await db.get_task_by_id(task_id)
    if not task or not task["is_active"]:
        return

    today = date.today().isoformat()
    if await db.get_completion_for_date(task_id, today):
        logger.info("Task %d already done; skipping follow-up.", task_id)
        return

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ <b>Still pending:</b> {task['description']} — "
                "it hasn't been marked done yet! Please take care of it."
            ),
            parse_mode="HTML",
            reply_markup=_make_done_keyboard(task_id),
        )
        assignees = await db.get_task_assignees(task_id)
        for assignee in assignees:
            await send_dm_with_fallback(
                bot=bot,
                user_id=assignee["telegram_user_id"],
                username=assignee.get("telegram_username"),
                group_chat_id=chat_id,
                text=(
                    f"⚠️ <b>Still pending:</b> {task['description']} — "
                    "this still hasn't been done! Please take care of it."
                ),
                reply_markup=_make_done_keyboard(task_id),
            )
    except TelegramError as exc:
        logger.error("Failed to send follow-up for task %d: %s", task_id, exc)


async def send_daily_summary(chat_id: int, bot: Bot) -> None:
    tasks = await db.get_daily_summary_data(chat_id)
    if not tasks:
        return

    done = [t for t in tasks if t["completed_by_username"]]
    pending = [t for t in tasks if not t["completed_by_username"]]

    lines = [f"📋 <b>Daily summary</b> — {date.today().strftime('%A, %B %d')}\n"]
    lines.append(f"✅ Completed: {len(done)}/{len(tasks)}\n")

    for t in done:
        by = make_display_name(t.get("completed_by_username"))
        lines.append(f"  ✅ {t['description']} — done by {by}")

    if pending:
        lines.append("")
        lines.append("⏰ <b>Still pending:</b>")
        for t in pending:
            lines.append(f"  • {t['description']}")

    try:
        await bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
    except TelegramError as exc:
        logger.error("Failed to send daily summary to chat %d: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

async def setup_scheduler(jq: JobQueue) -> None:
    """
    Store the PTB JobQueue reference and attach an error listener.

    Must be called from post_init so the job queue is running.
    """
    global job_queue
    job_queue = jq
    # The PTB JobQueue wraps APScheduler; we can attach listeners to it directly.
    jq.scheduler.add_listener(_job_listener, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    logger.info("PTB JobQueue ready (APScheduler id=%d).", id(jq.scheduler))
