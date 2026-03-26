# mr.cleaner Telegram Bot

Real-time Telegram moderation bot that:
- Deletes service messages like "X joined" and "X left" in groups/supergroups.
- Uses an OpenAI-compatible model to detect and delete ads/spam.
- Works with Uzbek Latin, Uzbek Cyrillic, and Russian (handled in moderation prompt).

## 1) Requirements

- Python 3.11+
- A Telegram bot token from `@BotFather`
- Bot added to target groups as **admin** with permission to delete messages

## 2) Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` values:

```env
BOT_TOKEN=...
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://your-provider.example/v1
OPENAI_MODEL=gpt-4o-mini
MODERATION_MODE=strict
AUDIT_CHAT_ID=-1001234567890
DEBUG=1
ADMIN_PASSWORD=change_me
WEBHOOK_SECRET=change_me_long_random_string
ADMIN_SESSION_TTL_SECONDS=2592000
STATE_FILE=bot_state.json
DROP_PENDING_UPDATES=1
ADMIN_CACHE_TTL_SECONDS=300
OPENAI_TIMEOUT_SECONDS=20
MAX_MODERATION_CONCURRENCY=8
```

## 3) Run locally

```bash
python bot.py
```

`bot.py` still uses polling for local testing. Production deployment is webhook-based via Modal and `modal_app.py`.

## 4) Telegram setup checklist

In `@BotFather`:
- Disable privacy mode with `/setprivacy` -> `Disable`
  - This allows the bot to read all group messages for AI moderation.

In each group:
- Add bot as admin
- Enable permission: delete messages

For audit logging channel:
- Add bot to your audit channel as admin (or member with send permission)
- Set `AUDIT_CHAT_ID` in `.env` to that channel ID (usually starts with `-100`)

## 5) Behavior notes

- Service updates (`new_chat_members`, `left_chat_member`) are removed immediately.
- Normal text/caption messages are sent to AI classifier.
- Captioned media groups are moderated as a single unit; if the caption is flagged, the whole album is deleted.
- Edited messages are rechecked by default, and that behavior can now be turned on or off from the admin panel.
- Admin/owner messages are exempt from AI deletion.
- If classified as ad/spam, message is deleted.
- Each deleted message is posted to `AUDIT_CHAT_ID` when configured.
- If AI API fails temporarily, the bot skips deletion for that message and continues running.
- Admin membership checks are cached briefly to avoid one Telegram API call per user message.
- AI moderation concurrency is capped with `MAX_MODERATION_CONCURRENCY` to protect cost and stability under bursts.

## 6) Admin control panel

The bot now exposes a private-chat control panel protected by `ADMIN_PASSWORD`.

1. Set `ADMIN_PASSWORD` in `.env`.
2. Start the bot.
3. Open a private chat with the bot.
4. Use `/login` and send the password when prompted, or `/login your-password`.
5. After a successful login, the bot stores your Telegram user ID in `STATE_FILE`.
6. Later, `/panel` opens immediately while that stored session is still valid.

Available controls:
- Toggle service-message cleanup on/off
- Toggle AI moderation on/off
- Toggle edited-message rechecks on/off
- Toggle audit logging on/off
- Use `/feature <service|moderation|edited|audit> <on|off>` for direct control
- Use `/logout` to end the current admin session

Feature toggles and authenticated admin user IDs are persisted in `STATE_FILE` so they survive restarts.
Authenticated control-panel users also receive private activity notifications for deletions and feature changes.

## 7) Notes and limits

- Admin controls are global for the whole bot, not per-group.
- Admin login sessions are stored as Telegram user IDs with expiry in `STATE_FILE`.
- `/logout` removes the stored session immediately.
- Password-based login is intentionally simple. Use a strong secret and rotate it if it leaks.

## 8) Deployment Artifacts

This repo now includes:
- `modal_app.py` for webhook deployment on Modal
- `Dockerfile` for container deployments
- `deploy/systemd/mr-cleaner.service` for a low-cost Linux VPS deployment

Recommended production defaults:
- Keep `DROP_PENDING_UPDATES=1` so restarts do not replay stale moderation work
- Keep `ADMIN_CACHE_TTL_SECONDS` at `300` or similar to reduce Telegram API load
- Keep `OPENAI_TIMEOUT_SECONDS` finite so provider issues fail fast
- Start with `MAX_MODERATION_CONCURRENCY=8` and tune based on traffic and budget

## 9) Modal webhook deployment

This repo is now ready for a webhook-first Modal deployment.

Design choices:
- `BOT_TOKEN`, `OPENAI_API_KEY`, `ADMIN_PASSWORD`, and `WEBHOOK_SECRET` belong in a Modal Secret.
- Mutable bot state does **not** belong in a Secret. Secrets are configuration, not a runtime session store.
- Bot state stays as a JSON file, but on a Modal Volume mounted at `/state/bot_state.json`.
- The webhook app is pinned to `max_containers=1` so the shared JSON file is safe from cross-container write races.

Setup:

```bash
pip install modal
modal setup
```

Create the Modal Secret:

```bash
modal secret create mr-cleaner-env \
  BOT_TOKEN=... \
  OPENAI_API_KEY=... \
  OPENAI_BASE_URL=https://api.openai.com/v1 \
  OPENAI_MODEL=gpt-4o-mini \
  MODERATION_MODE=balanced \
  AUDIT_CHAT_ID=-1001234567890 \
  DEBUG=1 \
  ADMIN_PASSWORD=change_me \
  WEBHOOK_SECRET=change_me_long_random_string \
  ADMIN_SESSION_TTL_SECONDS=2592000 \
  DROP_PENDING_UPDATES=1 \
  ADMIN_CACHE_TTL_SECONDS=300 \
  OPENAI_TIMEOUT_SECONDS=20 \
  MAX_MODERATION_CONCURRENCY=8
```

Do not commit a filled `.env` file to Git. For production, keep real credentials only in the Modal Secret.

Deploy the webhook app:

```bash
modal deploy modal_app.py
```

That creates a public endpoint with label `mr-cleaner-webhook`, so the URL will look like:

```text
https://<your-workspace>--mr-cleaner-webhook.modal.run
```

Register that endpoint with Telegram:

```bash
modal run modal_app.py::configure_webhook \
  --webhook-base-url https://<your-workspace>--mr-cleaner-webhook.modal.run
```

To remove the webhook later:

```bash
modal run modal_app.py::clear_webhook --drop-pending-updates
```

Operational notes:
- Health check: `GET /healthz`
- Telegram webhook endpoint: `POST /telegram/webhook`
- Telegram request validation uses the `X-Telegram-Bot-Api-Secret-Token` header when `WEBHOOK_SECRET` is set
- State is durable across restarts because the JSON file is stored on the Modal Volume
- The single-container limit is intentional. If you need horizontal scale later, replace the JSON file with a shared transactional store
