# Telegram Shared Task Reminder Bot

A production-ready Telegram bot for managing **shared recurring reminders** in groups or private chats. Users describe tasks in plain English; Google Gemini (via the Gemini API free tier) extracts the schedule, assignees, and description. When a task is completed, all assignees are notified. If nobody completes it, escalating reminders fire automatically.

---

## Features

- **Natural language task creation** — "Remind me and @roommate to take out the trash every Monday at 7pm"
- **Gemini-powered parsing** — Extracts task description, assignees, frequency, and time (free tier)
- **Recurring schedules** — Daily, weekly, or custom cron expressions
- **Completion tracking** — Mark tasks done via inline button or plain text ("done", "I fed the dog")
- **Completion notifications** — Notifies all other assignees when one person finishes
- **Escalating reminders** — A follow-up fires 2 hours after the initial reminder if still pending
- **Daily summary** — End-of-day recap of completed vs. pending tasks
- **Timezone support** — Per-chat timezone configuration
- **Pause/resume/delete** — Full task lifecycle management
- **Persistent storage** — SQLite database survives restarts; scheduler rebuilds on startup
- **Group & private chat support** — Works in both contexts

---

## Project Structure

```
telegram-reminder-bot/
├── bot.py              # Entry point — Application setup, handler registration
├── database.py         # Async SQLite layer (aiosqlite)
├── scheduler.py        # APScheduler jobs — reminders, follow-ups, daily summary
├── ai_parser.py        # Gemini API integration for NLP parsing
├── handlers/
│   ├── __init__.py
│   ├── commands.py     # /start /new /tasks /done /history /pause /resume /delete /timezone
│   ├── callbacks.py    # Inline button handlers
│   └── messages.py     # Natural-language message detection
├── requirements.txt
├── .env.example
└── README.md
```

---

## Quick Start

### 1. Create a Telegram Bot

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)**
2. Send `/newbot` and follow the prompts to choose a name and username
3. Copy the **bot token** (looks like `123456:ABC-DEF1234...`)
4. **Recommended:** Send `/setprivacy` → select your bot → **Disable** (so it can read group messages)

### 2. Get a Gemini API Key (free)

1. Go to **[aistudio.google.com](https://aistudio.google.com)**
2. Click **Get API key** → **Create API key**
3. Copy the key (starts with `AIza...`)

### 3. Install & Configure

```bash
# Clone or download the project
cd telegram-reminder-bot

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate       # Linux/macOS
# .venv\Scripts\activate        # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env and fill in your tokens:
#   TELEGRAM_BOT_TOKEN=...
#   GEMINI_API_KEY=...
```

### 4. Run

```bash
python bot.py
```

The bot will:
- Initialize the SQLite database (`reminders.db`)
- Restore all previously scheduled tasks
- Start polling for Telegram updates

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and usage guide |
| `/new [description]` | Create a new recurring task (natural language) |
| `/tasks` | List all active tasks with status for today |
| `/done <number>` | Mark task #N as done for today |
| `/history <number>` | View 7-day completion history for task #N |
| `/pause <number>` | Pause a recurring task (creator only) |
| `/resume <number>` | Resume a paused task (creator only) |
| `/delete <number>` | Permanently delete a task (creator only) |
| `/timezone [tz]` | Set the chat's timezone (e.g. `America/New_York`) |
| `/cancel` | Cancel an in-progress task creation |

---

## Usage Examples

### Creating a task

```
You: /new Remind me and @roommate to water the plants every day at 8am

Bot: 📋 Here's what I understood:
     📝 Task: Water the plants
     👥 Assigned to: @you, @roommate
     🔄 Schedule: Every day at 8:00 AM

     Does this look right?
     [✅ Confirm]  [❌ Cancel]
```

### Completing a task

Three ways to mark a task done:
- Click the **✅ Mark as done** button on a reminder message
- Use `/done 1` (by task number from `/tasks`)
- Send "done" or "I watered the plants" in the chat

### What assignees see

When you mark a task complete, all other assignees get a DM:
```
✅ Water the plants has been completed by @you. No action needed!
```

### Reminder behavior

| Time | Event |
|------|-------|
| Scheduled time | Primary reminder fires |
| +2 hours (if still pending) | Escalating follow-up |
| 11:30 PM | Daily summary for the chat |

---

## Deploying on a VPS or Raspberry Pi

### systemd service (Linux)

Create `/etc/systemd/system/reminder-bot.service`:

```ini
[Unit]
Description=Telegram Reminder Bot
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/telegram-reminder-bot
ExecStart=/home/pi/telegram-reminder-bot/.venv/bin/python bot.py
Restart=always
RestartSec=10
EnvironmentFile=/home/pi/telegram-reminder-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable reminder-bot
sudo systemctl start reminder-bot
sudo journalctl -u reminder-bot -f   # follow logs
```

### Docker (optional)

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t reminder-bot .
docker run -d --env-file .env --name reminder-bot reminder-bot
```

---

## Database Schema

```sql
-- Recurring task definitions
tasks (id, chat_id, description, frequency, cron_expression,
       reminder_time, reminder_day, created_by, created_by_username,
       timezone, is_active, created_at)

-- Who is assigned to each task
task_assignees (id, task_id, telegram_user_id, telegram_username)

-- One record per task per day it was completed
task_completions (id, task_id, completed_by_user_id,
                  completed_by_username, completed_at, date_for)

-- Per-chat timezone settings
chat_settings (chat_id, timezone, updated_at)
```

---

## Configuration Notes

- **Group chats:** Add the bot to a group and give it admin rights so it can read all messages (required for natural-language detection). Disable privacy mode via BotFather for message access.
- **Private DMs to assignees:** The bot can only DM users who have previously sent `/start` to it directly. Encourage all group members to start the bot privately.
- **Timezone:** Set once per chat with `/timezone`. All tasks created in that chat will use it. Default is UTC.
- **Custom cron schedules:** Use `/new` with expressions like "every weekday at 9am" — Claude converts them to cron format automatically.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Bot doesn't respond in groups | Disable privacy mode via BotFather |
| Reminders not firing | Check `bot.log`; verify timezone is correct |
| "TELEGRAM_BOT_TOKEN not set" | Make sure `.env` is in the working directory |
| Gemini parsing fails | Check `GEMINI_API_KEY`; verify free-tier quota at aistudio.google.com |
| Database errors | Delete `reminders.db` to start fresh (loses all data) |

---

## License

MIT — free to use and modify.
