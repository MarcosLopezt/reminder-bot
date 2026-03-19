"""
Database layer for the Telegram Reminder Bot using aiosqlite.

Provides async CRUD operations for tasks, assignees, completions, and settings.
"""

import logging
import os
from datetime import date
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

DATABASE_PATH = "reminders.db"


def make_display_name(
    username: Optional[str],
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> str:
    """
    Return the best available display name for a user.

    Priority: @username → first_name [last_name] → 'someone'
    """
    if username:
        return f"@{username}"
    parts = [p for p in (first_name, last_name) if p]
    if parts:
        return " ".join(parts)
    return "someone"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    frequency TEXT NOT NULL CHECK(frequency IN ('daily', 'weekly', 'custom')),
    cron_expression TEXT,
    reminder_time TEXT NOT NULL,
    reminder_day TEXT,
    created_by INTEGER NOT NULL,
    created_by_username TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_assignees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    telegram_username TEXT,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, telegram_user_id)
);

CREATE TABLE IF NOT EXISTS task_completions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    completed_by_user_id INTEGER NOT NULL,
    completed_by_username TEXT,
    completed_by_name TEXT,
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    date_for TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
    UNIQUE(task_id, date_for)
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS known_users (
    user_id   INTEGER PRIMARY KEY,
    username  TEXT,
    first_name TEXT,
    last_seen TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


async def init_db() -> None:
    """Initialize the database with the schema, then apply any pending migrations."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

    # Migrations for columns added after initial deployment.
    # SQLite does not support 'ADD COLUMN IF NOT EXISTS', so we catch the error.
    migrations = [
        "ALTER TABLE task_completions ADD COLUMN completed_by_name TEXT",
        "ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'recurring'",
        "ALTER TABLE tasks ADD COLUMN specific_date TEXT",
    ]
    async with aiosqlite.connect(DATABASE_PATH) as db:
        for sql in migrations:
            try:
                await db.execute(sql)
            except aiosqlite.OperationalError:
                pass  # Column already exists
        await db.commit()

    # Migrate rows that still carry the old hard-coded 'UTC' default to the
    # configured DEFAULT_TIMEZONE. This only runs when DEFAULT_TIMEZONE != 'UTC'
    # so it is a no-op for deployments that intentionally want UTC.
    default_tz = os.getenv("DEFAULT_TIMEZONE", "UTC")
    if default_tz != "UTC":
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE chat_settings SET timezone = ? WHERE timezone = 'UTC'",
                (default_tz,),
            )
            await db.execute(
                "UPDATE tasks SET timezone = ? WHERE timezone = 'UTC'",
                (default_tz,),
            )
            await db.commit()
        logger.info("Migrated UTC timezone rows to %s.", default_tz)

    logger.info("Database initialized successfully.")


async def create_task(
    chat_id: int,
    description: str,
    frequency: str,
    reminder_time: str,
    created_by: int,
    created_by_username: Optional[str] = None,
    reminder_day: Optional[str] = None,
    cron_expression: Optional[str] = None,
    timezone: str = "UTC",
    task_type: str = "recurring",
    specific_date: Optional[str] = None,
) -> int:
    """Create a new task and return its ID."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO tasks (
                chat_id, description, frequency, reminder_time,
                created_by, created_by_username, reminder_day,
                cron_expression, timezone, task_type, specific_date
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id, description, frequency, reminder_time,
                created_by, created_by_username, reminder_day,
                cron_expression, timezone, task_type, specific_date,
            ),
        )
        await db.commit()
        task_id = cursor.lastrowid
        logger.info("Created task %d: %s", task_id, description)
        return task_id


async def add_task_assignee(
    task_id: int,
    user_id: int,
    username: Optional[str] = None,
) -> None:
    """Add an assignee to a task (silently ignores duplicates)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO task_assignees (task_id, telegram_user_id, telegram_username)
            VALUES (?, ?, ?)
            """,
            (task_id, user_id, username),
        )
        await db.commit()


async def get_task_assignees(task_id: int) -> list[dict]:
    """Return all assignees for a given task."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM task_assignees WHERE task_id = ?",
            (task_id,),
        )
        return [dict(r) for r in rows]


async def get_tasks_for_chat(chat_id: int, active_only: bool = True) -> list[dict]:
    """Return all tasks for a chat, optionally filtering to active only."""
    where = "WHERE t.chat_id = ?" + (" AND t.is_active = 1" if active_only else "")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            f"""
            SELECT t.*,
                   GROUP_CONCAT(ta.telegram_username, ', ') AS assignee_usernames,
                   GROUP_CONCAT(ta.telegram_user_id, ',')   AS assignee_ids
            FROM tasks t
            LEFT JOIN task_assignees ta ON t.id = ta.task_id
            {where}
            GROUP BY t.id
            ORDER BY t.id
            """,
            (chat_id,),
        )
        return [dict(r) for r in rows]


async def get_task_by_id(task_id: int) -> Optional[dict]:
    """Return a single task by its ID (with aggregated assignee info)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """
            SELECT t.*,
                   GROUP_CONCAT(ta.telegram_username, ', ') AS assignee_usernames,
                   GROUP_CONCAT(ta.telegram_user_id, ',')   AS assignee_ids
            FROM tasks t
            LEFT JOIN task_assignees ta ON t.id = ta.task_id
            WHERE t.id = ?
            GROUP BY t.id
            """,
            (task_id,),
        )
        return dict(rows[0]) if rows else None


async def get_all_active_tasks() -> list[dict]:
    """Return every active task across all chats (used by scheduler on startup)."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """
            SELECT t.*,
                   GROUP_CONCAT(ta.telegram_username, ', ') AS assignee_usernames,
                   GROUP_CONCAT(ta.telegram_user_id, ',')   AS assignee_ids
            FROM tasks t
            LEFT JOIN task_assignees ta ON t.id = ta.task_id
            WHERE t.is_active = 1
            GROUP BY t.id
            """,
        )
        return [dict(r) for r in rows]


async def record_completion(
    task_id: int,
    user_id: int,
    username: Optional[str],
    date_for: Optional[str] = None,
    display_name: Optional[str] = None,
) -> bool:
    """
    Record a task completion.

    Returns True if recorded successfully, False if already completed for that date.
    date_for defaults to today (ISO format).
    display_name is the human-readable name to show in notifications (e.g. '@alice' or 'Alice').
    """
    if date_for is None:
        date_for = date.today().isoformat()

    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            await db.execute(
                """
                INSERT INTO task_completions
                    (task_id, completed_by_user_id, completed_by_username,
                     completed_by_name, date_for)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, user_id, username, display_name, date_for),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_completion_for_date(
    task_id: int,
    date_for: Optional[str] = None,
) -> Optional[dict]:
    """Return the completion record for a task on a given date, or None."""
    if date_for is None:
        date_for = date.today().isoformat()

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM task_completions WHERE task_id = ? AND date_for = ?",
            (task_id, date_for),
        )
        return dict(rows[0]) if rows else None


async def get_task_history(task_id: int, days: int = 7) -> list[dict]:
    """Return completion history for a task over the last N days."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """
            SELECT * FROM task_completions
            WHERE task_id = ? AND date_for >= date('now', ?)
            ORDER BY date_for DESC
            """,
            (task_id, f"-{days} days"),
        )
        return [dict(r) for r in rows]


async def set_task_active(task_id: int, is_active: bool) -> bool:
    """Pause or resume a task. Returns True if the task was found."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        result = await db.execute(
            "UPDATE tasks SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, task_id),
        )
        await db.commit()
        return result.rowcount > 0


async def delete_task(task_id: int) -> bool:
    """Permanently delete a task and its related data. Returns True if found."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        result = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
        return result.rowcount > 0


async def get_chat_timezone(chat_id: int) -> str:
    """Return the configured timezone for a chat.

    Falls back to the DEFAULT_TIMEZONE env var (default: 'UTC') when the chat
    has no explicit timezone setting stored yet.
    """
    default_tz = os.getenv("DEFAULT_TIMEZONE", "UTC")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        rows = await db.execute_fetchall(
            "SELECT timezone FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        )
        return rows[0][0] if rows else default_tz


async def set_chat_timezone(chat_id: int, timezone: str) -> None:
    """Persist the timezone setting for a chat."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO chat_settings (chat_id, timezone) VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET timezone = ?, updated_at = datetime('now')
            """,
            (chat_id, timezone, timezone),
        )
        await db.commit()


async def upsert_known_user(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str] = None,
) -> None:
    """
    Insert or update a user in known_users.

    Called every time a user sends /start so we always have a fresh record.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT INTO known_users (user_id, username, first_name, last_seen)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                last_seen  = excluded.last_seen
            """,
            (user_id, username, first_name),
        )
        await db.commit()


async def get_known_user_by_id(user_id: int) -> Optional[dict]:
    """Return a known user by their Telegram user ID, or None."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM known_users WHERE user_id = ?",
            (user_id,),
        )
        return dict(rows[0]) if rows else None


async def get_all_known_users() -> list[dict]:
    """Return all users who have started the bot."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM known_users ORDER BY username",
        )
        return [dict(r) for r in rows]


async def get_known_user_by_username(username: str) -> Optional[dict]:
    """
    Look up a known user by their Telegram username (case-insensitive).

    Returns the row dict or None if the user has never started the bot.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM known_users WHERE lower(username) = lower(?)",
            (username,),
        )
        return dict(rows[0]) if rows else None


async def update_assignee_user_ids(username: str, user_id: int) -> int:
    """
    Back-fill real user_ids for task_assignees stored with a placeholder of 0.

    Called when a user does /start — links any tasks that mention their
    @username to their actual Telegram user_id so the bot can DM them.
    Returns the number of rows updated.
    """
    async with aiosqlite.connect(DATABASE_PATH) as db:
        result = await db.execute(
            """
            UPDATE task_assignees
            SET telegram_user_id = ?
            WHERE telegram_user_id = 0
              AND lower(telegram_username) = lower(?)
            """,
            (user_id, username),
        )
        await db.commit()
        return result.rowcount


async def update_task_schedule(
    task_id: int,
    reminder_time: Optional[str] = None,
    reminder_day: Optional[str] = None,
    frequency: Optional[str] = None,
) -> bool:
    """Update a task's schedule fields. Returns True if the task was found."""
    sets, params = [], []
    if reminder_time is not None:
        sets.append("reminder_time = ?")
        params.append(reminder_time)
    if reminder_day is not None:
        sets.append("reminder_day = ?")
        params.append(reminder_day)
    if frequency is not None:
        sets.append("frequency = ?")
        params.append(frequency)
    if not sets:
        return True
    params.append(task_id)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        result = await db.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await db.commit()
        return result.rowcount > 0


async def get_daily_summary_data(chat_id: int, date_for: Optional[str] = None) -> list[dict]:
    """Return active tasks with their completion status for a given date."""
    if date_for is None:
        date_for = date.today().isoformat()

    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            """
            SELECT t.id, t.description, t.frequency, t.reminder_time,
                   tc.completed_by_username, tc.completed_at
            FROM tasks t
            LEFT JOIN task_completions tc
                ON t.id = tc.task_id AND tc.date_for = ?
            WHERE t.chat_id = ? AND t.is_active = 1
            ORDER BY t.id
            """,
            (date_for, chat_id),
        )
        return [dict(r) for r in rows]
