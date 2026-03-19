# Telegram Reminder Bot

A Telegram bot that helps you manage **shared recurring tasks** in group chats or private conversations. Just describe what you need in plain language — the bot uses AI to understand the schedule, assignees, and details automatically.

## What It Does

- **Natural language reminders** — Send messages like _"Remind me and @roommate to take out the trash every Monday at 7pm"_ and the bot handles the rest.
- **Recurring & one-time tasks** — Supports daily, weekly, custom cron schedules, or one-off reminders.
- **Automatic follow-ups** — If nobody marks a task as done, a follow-up reminder fires 2 hours later.
- **Completion notifications** — When someone completes a task, all other assignees are notified.
- **Daily summary** — End-of-day recap of what got done and what's still pending.

## Use Cases

- 🏠 **Shared chores** — Roommates splitting household tasks (dishes, trash, laundry).
- 👨‍👩‍👧 **Family responsibilities** — Coordinating who feeds the pets, picks up the kids, etc.
- 💼 **Small team follow-ups** — Daily standups, weekly report reminders, recurring check-ins.
- 🧑 **Personal reminders** — Medication, exercise, habit tracking in a private chat.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and setup |
| `/new [description]` | Create a task using natural language |
| `/tasks` | List active tasks and today's status |
| `/done <number>` | Mark a task as done |
| `/history <number>` | View completion history |
| `/pause / /resume / /delete` | Manage task lifecycle |
| `/timezone [tz]` | Set the chat's timezone |

## License

MIT
