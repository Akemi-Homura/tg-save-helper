# Telegram Saved Messages Helper

English | [简体中文](README.md)

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Telethon](https://img.shields.io/badge/Telethon-1.40-2AABEE?logo=telegram&logoColor=white)](https://github.com/LonamiWebs/Telethon)
[![GitHub stars](https://img.shields.io/github/stars/Akemi-Homura/tg-save-helper?style=flat)](https://github.com/Akemi-Homura/tg-save-helper/stargazers)

A self-hosted Telegram user-client helper for forwarding accessible channel, group, and chat messages to your own Saved Messages. Commands are sent from Saved Messages, so no web dashboard or public network port is required.

The project uses Telethon and never bypasses Telegram forwarding restrictions or downloads protected content for re-upload.

## Features

- Saved Messages as the control console; commands from other chats are ignored
- Forward recent messages, message-ID ranges, and individual Telegram links
- Persistently watch chats and channels for new messages
- Watch channel posts together with comments from the linked discussion group
- Preserve Telegram media albums, captions, text formatting, and links
- Persist watches, forwarding logs, and application state in SQLite
- Respect FloodWait and rate-limit batch operations
- Skip protected, inaccessible, deleted, or invalid messages without bypass attempts
- One-command setup and a systemd service example

## Requirements

- Ubuntu 22.04/24.04 or another Linux distribution with Python 3.10+
- Network access to Telegram
- Your own `api_id` and `api_hash` from [my.telegram.org](https://my.telegram.org/apps)
- A regular Telegram user account; this is not a BotFather bot

## Quick Start

```bash
git clone https://github.com/Akemi-Homura/tg-save-helper.git
cd tg-save-helper
./setup.sh
```

The script creates a virtual environment, installs dependencies, securely prompts for API credentials, completes the interactive Telegram login, and installs a systemd service using the current path and user.

Login codes are usually delivered to the official `Telegram` chat inside the mobile app rather than by SMS. After setup, send this command in Saved Messages:

```text
/help
```

> [!CAUTION]
> `.env` and `.session` files are account credentials. Never commit, screenshot, share, or upload them.

## Configuration

For manual setup:

```bash
cp .env.example .env
chmod 600 .env
```

| Variable | Required | Description |
| --- | --- | --- |
| `TG_API_ID` | Yes | Numeric ID from my.telegram.org |
| `TG_API_HASH` | Yes | API Hash from my.telegram.org |
| `TG_SESSION_NAME` | Yes | Telethon session path; defaults to `data/tg_save_helper` |
| `OWNER_ID` | No | Telegram user ID used to verify the logged-in account |
| `TG_DATABASE_PATH` | No | SQLite path; defaults to `data/tg_save_helper.sqlite3` |
| `LOG_LEVEL` | No | Logging level; defaults to `INFO` |

First interactive login:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.main --login-only
```

Run directly:

```bash
.venv/bin/python -m src.main
```

## Commands

| Command | Description |
| --- | --- |
| `/help` | Show help |
| `/last <source> <count>` | Forward recent logical posts, up to 200 |
| `/between <source> <start_id> <end_id>` | Forward an ID range, up to 500 IDs |
| `/link <message_link>` | Forward one public or `t.me/c/...` link |
| `/watch <source>` | Watch and forward new messages |
| `/unwatch <source>` | Remove a standard watch |
| `/watchcomments <source>` | Watch channel posts and linked comments |
| `/unwatchcomments <source>` | Remove a post-and-comments watch |
| `/lastcomments <source> <count>` | Forward recent posts and all existing comments, up to 10 posts |
| `/listwatch` | List persisted watches |
| `/status` | Show login, watch, forwarding, and error state |

Examples:

```text
/last @example_channel 20
/between @example_channel 1000 1100
/link https://t.me/example_channel/123
/link https://t.me/c/123456789/123
/watch @example_channel
/watchcomments @example_channel
/lastcomments @example_channel 3
```

Accepted source formats:

```text
@example_channel
https://t.me/example_channel
-1001234567890
```

For `https://t.me/c/1234567890/456`, the source is usually `-1001234567890`. The logged-in account must already have access to the target chat.

## Native Forwarding and Comments

`/last`, `/watch`, `/lastcomments`, and `/watchcomments` use Telegram's native forwarding API:

- photos and videos sent as one media album remain grouped;
- captions, formatted text, and links remain intact;
- media is never downloaded or re-uploaded;
- protected or forwarding-restricted messages are logged and skipped.

`/watchcomments` discovers the channel's linked discussion group automatically. It forwards each channel post once, skips the automatic mirrored root in the discussion group, and then forwards real comments as they arrive. Join the linked discussion group first to receive comment updates reliably.

`/lastcomments` fetches all existing comments for the selected posts. Popular threads can take a long time and remain subject to batch rate limiting.

## Rate Limits and Errors

- Up to 50 messages are processed per batch.
- Batches sleep for a random 2–5 seconds, with a short delay between items.
- `FloodWaitError` sleeps for the duration required by Telegram.
- A failed message or media group does not terminate the remaining task.
- Completion summaries report successes, failures, skips, and recent errors.

Rate limiting reduces risk but cannot guarantee that Telegram will never limit the account. Do not use this project for spam, harassment, or platform-rule evasion.

## systemd Deployment

`setup.sh` generates a service for the current path automatically. For manual deployment, adjust `tg-save-helper.service` and install it:

```bash
sudo cp tg-save-helper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-save-helper
sudo systemctl status tg-save-helper
```

Follow logs:

```bash
sudo journalctl -u tg-save-helper -f
```

Update:

```bash
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart tg-save-helper
```

## Data and Security

Runtime data is stored under `data/` by default:

- Telethon session: equivalent to an authenticated Telegram device;
- SQLite: `watched_sources`, `forwarding_logs`, and `app_state`;
- `.env`: API credentials.

These paths are covered by `.gitignore`, but always inspect `git status` before publishing changes. If the server is compromised, terminate the corresponding session immediately under Telegram **Settings → Devices**.

Only outgoing commands sent by the logged-in user in Saved Messages are accepted. Never run multiple processes against the same session file.

## Troubleshooting

### `/help` gets no response

```bash
sudo systemctl status tg-save-helper
sudo journalctl -u tg-save-helper -n 100 --no-pager
```

Verify that interactive login has completed and that the systemd working directory, `.env`, and virtual-environment paths are correct.

### No login code arrives

Enter the phone number in international format, such as `+<country-code><phone-number>`. The code is usually delivered to the official Telegram chat in the mobile app. Two-step verification also requires the account password.

### `/watchcomments` does not receive comments

Confirm that the channel has a linked discussion group and join that group with the logged-in account. Then run `/watchcomments <source>` again.

### A private link cannot be resolved

`t.me/c/...` links work only when the account has joined the chat and the Telethon session can resolve its entity. Open the chat in an official Telegram client before retrying.

## Local Check

```bash
python3 -m py_compile src/*.py
```

## Star History

<a href="https://star-history.com/#Akemi-Homura/tg-save-helper&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Akemi-Homura/tg-save-helper&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Akemi-Homura/tg-save-helper&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Akemi-Homura/tg-save-helper&type=Date" />
  </picture>
</a>
