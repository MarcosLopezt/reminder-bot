"""
Telegram Reminder Bot — Entry Point.

Sets up the bot, registers handlers, initializes the database and scheduler,
then starts polling for updates.
"""

import logging
import os
import sys

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

import database as db
import scheduler as sched
from handlers.callbacks import (
    callback_router,
    cancel_task_callback,
    confirm_task_callback,
)
from handlers.commands import (
    AWAITING_CONFIRMATION,
    AWAITING_DESCRIPTION,
    cancel_command,
    delete_command,
    done_command,
    history_command,
    new_command,
    pause_command,
    receive_description,
    resume_command,
    start_command,
    tasks_command,
    timezone_command,
)
from handlers.messages import handle_message

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
# Suppress noisy third-party loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.DEBUG)  # show all job fires

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Post-init hook — runs after the Application is built but before polling
# ---------------------------------------------------------------------------

async def _on_startup(application: Application) -> None:
    """Initialize DB, start the scheduler, and load persisted tasks."""
    logger.info("Running startup tasks…")
    await db.init_db()
    await sched.setup_scheduler(application.job_queue)
    await sched.load_all_tasks(application.bot)
    logger.info("Bot is ready.")


async def _on_shutdown(application: Application) -> None:
    """Gracefully shut down the scheduler."""
    if sched.job_queue and sched.job_queue.scheduler.running:
        sched.job_queue.scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down.")


# ---------------------------------------------------------------------------
# Build the Application
# ---------------------------------------------------------------------------

def build_application(token: str) -> Application:
    """Construct and configure the Telegram Application."""

    app = (
        Application.builder()
        .token(token)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # ------------------------------------------------------------------
    # ConversationHandler for /new and natural-language task creation
    # ------------------------------------------------------------------
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_command),
        ],
        states={
            AWAITING_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_description),
            ],
            AWAITING_CONFIRMATION: [
                CallbackQueryHandler(confirm_task_callback, pattern="^confirm_task$"),
                CallbackQueryHandler(cancel_task_callback, pattern="^cancel_task$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
        ],
        # Scope conversations to individual users so multiple users can create
        # tasks simultaneously in the same group chat.
        per_user=True,
        per_chat=True,
    )
    app.add_handler(conversation)

    # ------------------------------------------------------------------
    # Standard command handlers
    # ------------------------------------------------------------------
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("tasks", tasks_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("pause", pause_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("delete", delete_command))
    app.add_handler(CommandHandler("timezone", timezone_command))

    # ------------------------------------------------------------------
    # Inline callback router (covers all non-conversation callbacks)
    # ------------------------------------------------------------------
    app.add_handler(CallbackQueryHandler(callback_router))

    # ------------------------------------------------------------------
    # Natural-language message handler (lowest priority)
    # ------------------------------------------------------------------
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    # ------------------------------------------------------------------
    # Global error handler
    # ------------------------------------------------------------------
    async def error_handler(update: object, context) -> None:
        logger.error("Unhandled exception:", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "❌ An unexpected error occurred. Please try again later."
                )
            except Exception:
                pass

    app.add_error_handler(error_handler)

    return app


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.critical("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        logger.critical("GEMINI_API_KEY is not set. Exiting.")
        sys.exit(1)

    logger.info("Starting Telegram Reminder Bot…")
    app = build_application(token)

    # Run the bot until Ctrl-C is pressed
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
