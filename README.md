# Hackii Telegram Bot

Python Telegram bot with:

- VIP join-request handling
- Optional automatic approval
- Saved welcome message/media sequence
- Configurable final message
- User-message notification to admin with UID
- Admin reply button for replying to the user
- SQLite local database
- GitHub `members.json` backup/restore
- Merge-based member sync so existing GitHub members are not wiped by an empty/new local database

## Project structure

```text
hackii_telegram_bot/
├── hackiirequestbot.py
├── requirements.txt
├── members.json
├── README.md
└── .gitignore
```

## GitHub setup

1. Create a new GitHub repository.
2. Upload all files from this folder.
3. Keep `members.json` in the repository root.
4. `members.json` should start as:

```json
[]
```

Do not put your Telegram bot token or GitHub personal access token inside the Python file.

## Environment variables

Set these in Render:

```text
BOT_TOKEN=YOUR_NEW_TELEGRAM_BOT_TOKEN
ADMIN_ID=YOUR_TELEGRAM_NUMERIC_ID
GITHUB_TOKEN=YOUR_GITHUB_TOKEN
GITHUB_OWNER=YOUR_GITHUB_USERNAME
GITHUB_REPO=YOUR_REPOSITORY_NAME
GITHUB_FILE=members.json
```

## Render

Recommended service type: Background Worker.

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
python hackiirequestbot.py
```

## Important

Generate a new Telegram bot token if an old token was ever exposed.

The GitHub member sync merges the existing `members.json` list with the local database before writing it back. This is intended to prevent an empty/new local database from deleting previously backed-up member IDs.

The bot still requires the GitHub environment variables to be correctly configured for the backup/restore feature to work.
