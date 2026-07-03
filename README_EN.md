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
- Detect whitelisted resource-bot links, start the bot, paginate, and forward returned media
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
| `BOT_TOKEN` | No | Control bot token from BotFather; enables command entry in the bot chat |
| `BOT_OWNER_ID` | No | Telegram user ID allowed to use the control bot; defaults to the logged-in user ID |
| `TG_DATABASE_PATH` | No | SQLite path; defaults to `data/tg_save_helper.sqlite3` |
| `TG_SAVED_MEDIA_PATH` | No | Saved-media download directory; defaults to `data/saved_media` |
| `LOG_LEVEL` | No | Logging level; defaults to `INFO` |
| `TG_PANEL_ENABLED` | No | Enable the local management panel; disabled by default |
| `TG_PANEL_HOST` | No | Panel bind address; defaults to `127.0.0.1` |
| `TG_PANEL_PORT` | No | Panel bind port; defaults to `8790` |
| `TG_PANEL_BASE_PATH` | No | Panel path prefix; defaults to `/tghelper` |
| `TG_PANEL_USERNAME` / `TG_PANEL_PASSWORD` | No | Panel Basic Auth credentials; required when the panel is enabled |
| `TG_PANEL_PASSWORD_FILE` | No | Read the panel password from a file when `TG_PANEL_PASSWORD` is not set |

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
| `/stop` | Stop currently running manual commands |
| `/last <source> <count\|all\|unread> [force]` | Forward recent, all, or unread logical posts |
| `/unread <source> [count\|all] [force]` | Forward unread messages; omitting the count is the same as `all` |
| `/between <source> <start_id> <end_id> [force]` | Forward an ID range, up to 500 IDs |
| `/link <message_link> [force]` | Forward one public or `t.me/c/...` link |
| `/watch <source> [count\|all\|unread\|from <message_link>] [force]` | Watch and forward new messages; optionally backfill |
| `/unwatch <source>` | Remove a standard watch |
| `/watchcomments <source> [count\|all\|unread\|from <message_link>] [force]` | Watch channel posts and linked comments; optionally backfill |
| `/unwatchcomments <source>` | Remove a post-and-comments watch |
| `/watchresource <source> [count\|all\|unread\|from <message_link>] [force]` | Watch new posts for resource bot links; optionally backfill |
| `/unwatchresource <source>` | Remove a resource watch |
| `/lastcomments <source> <count\|all\|unread> [force]` | Forward recent, all, or unread posts and their comments |
| `/unreadcomments <source> [count\|all] [force]` | Forward unread channel posts and unread linked comments; omitting the count is the same as `all` |
| `/resourcebot add\|remove\|list [username]` | Manage the resource bot whitelist |
| `/resourcelink <bot_deep_link> [force]` | Trigger one resource bot deep link; `force` re-runs an already processed link |
| `/resource <source> <count\|all\|unread\|from <message_link>\|one from <message_link>> [force]` | Scan resource bot links; `from` starts at the specified original post, and `one` handles only that post |
| `/mixed <source> <count\|all> [force]` | Automatically choose resource / lastcomments / last forwarding per post |
| `/listwatch` | List persisted watches |
| `/status` | Show login, watch, forwarding, and error state |
| `/stats [day\|month\|year]` | Show forwarding and sync stats for today, this month, or this year |
| `/syncsaved <count\|all> [source\|unknown]` | Copy Saved Messages media inside Telegram without downloading; `all` scans everything |
| `/syncsaved-download <count\|all> [source\|unknown]` | Download and re-upload Saved Messages media; `all` scans everything |

When the control bot is enabled, Telegram command hints cannot contain hyphens. Use `/syncsaved_download <count|all>` in the bot chat; the program maps it to `/syncsaved-download`.

Except for sync commands, forwarding commands skip messages that already have a successful `source + message_id` forwarding log. Append `force` to forward them again.

Examples:

```text
/last @example_channel 20
/between @example_channel 1000 1100
/link https://t.me/example_channel/123
/link https://t.me/c/123456789/123
/watch @example_channel
/watchcomments @example_channel
/watchresource @example_channel
/lastcomments @example_channel 3
/resourcebot add seliu
/resourcelink https://t.me/seliu?start=j_2bfc3620
/resourcelink https://t.me/seliu?start=j_2bfc3620 force
/resource @example_channel 10
/resource @example_channel all
/resource @example_channel all from https://t.me/example_channel/4734
/resource @example_channel one from https://t.me/example_channel/4734 force
/syncsaved 500
/syncsaved all
/syncsaved all @example_channel
/syncsaved all unknown
/syncsaved-download 100
```

## CLI debugging

You can run control commands directly from the server shell. Replies are printed to stdout; forwarding commands still perform real Telegram actions:

```bash
.venv/bin/python -m src.cli /status
.venv/bin/python -m src.cli --parse-only /last -3337589510 all
.venv/bin/python -m src.cli /last -3337589510 3
```

By default, the CLI copies the Telegram session to `/tmp` before connecting, so it does not fight the systemd service for the session SQLite lock. Pass `--live-session` to use the configured session directly. Run without a command for an interactive prompt:

```bash
.venv/bin/python -m src.cli
```

## Management panel

With `TG_PANEL_ENABLED=1`, the helper starts a local management panel at:

```text
http://127.0.0.1:8790/tghelper/
```

The panel provides:

- dashboard: active tasks, watch count, recent 24-hour success/failure/skip summary, latest error;
- manual tasks: view active and pending tasks, start, pause, stop, and restart;
- watch tasks: view `/watch`, `/watchcomments`, `/watchresource`, and `/watchcode`, then pause, resume, or stop them;
- console: submit existing Telegram commands for background execution.

The panel should only bind to `127.0.0.1`. For public HTTPS access, put nginx in front of a clear path such as:

```text
https://quals.site/tghelper/
```

Panel actions control the Telegram account, so Basic Auth is required. Do not expose the internal port directly.

## Saved media migration

`/syncsaved <count>` copies the most recent `count` media items using Telegram's existing media references; text commands and replies do not consume the limit. `/syncsaved all` scans all Saved Messages media. A numeric limit is automatically extended when it cuts through an album. `/syncsaved-download <count|all>` retains the download-and-upload path and stores files under `TG_SAVED_MEDIA_PATH`. Both commands reuse an existing same-named broadcast channel created by the account, or create a private one when none exists. Media whose original channel cannot be identified is synced to the fallback channel `收藏媒体_未知来源`. Successful message IDs and channel mappings are shared in SQLite, so rerunning either mode does not upload completed items again.

Both sync commands accept an optional source filter: `/syncsaved all @example_channel` only syncs one source, while `/syncsaved all unknown` only syncs fallback unknown-source media.

## Resource bot links

Resource bot automation only handles whitelisted bots. A fixed whitelist can be configured in `.env`:

```env
TG_RESOURCE_BOTS=seliu
MAX_RESOURCE_BOT_PAGES=100
MAX_RESOURCE_BOT_WAIT_SECONDS=120
MAX_RESOURCE_BOT_MESSAGES=2000
```

Runtime whitelist entries are managed with `/resourcebot add|remove|list` and do not require a restart. The program extracts `https://t.me/<bot>?start=<payload>` links from text, hidden links, and URL buttons, starts the bot, collects media replies, follows `下一页`/`next`, numbered buttons, and text-only pagination prompts, then forwards collected media to Saved Messages.

Each resource link stores its processing context in SQLite: original source post, payload, outgoing `/start` message ID, bot response range, and collected/forwarded counts. If a resource bot returns page 1/N but pagination later fails, these fields allow tracing the bot chat back to the original post and payload.

## Development docs

Forwarding consistency, resource-bot context fields, and database maintenance notes live in [DEVELOPMENT.md](DEVELOPMENT.md).

After media is synced to the per-source private channels, the program creates or reuses a private `收藏媒体汇总` summary channel and forwards the newly synced messages from the per-source channels into it. Messages in the summary channel therefore keep Telegram's "forwarded from" header pointing at the corresponding per-source private channel.

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
- SQLite: watches, forwarding logs, resource-bot processing context, and runtime state;
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
