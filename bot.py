import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI, BadRequestError
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


load_dotenv()


FEATURE_LABELS = {
    "service_messages": "Service Cleanup",
    "ai_moderation": "AI Moderation",
    "edited_messages": "Edited Message Checks",
    "audit_logging": "Audit Logging",
}

FEATURE_ALIASES = {
    "service": "service_messages",
    "service_cleanup": "service_messages",
    "service_messages": "service_messages",
    "moderation": "ai_moderation",
    "ai": "ai_moderation",
    "ai_moderation": "ai_moderation",
    "edited": "edited_messages",
    "edit": "edited_messages",
    "updated": "edited_messages",
    "updates": "edited_messages",
    "edited_messages": "edited_messages",
    "audit": "audit_logging",
    "audit_logging": "audit_logging",
}

MEDIA_GROUP_SETTLE_SECONDS = 1.2


@dataclass(frozen=True)
class Settings:
    bot_token: str
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    moderation_mode: str
    audit_chat_id: Optional[int]
    debug: bool
    admin_password: Optional[str]
    admin_session_ttl_seconds: int
    state_file: Path
    webhook_secret: Optional[str]
    drop_pending_updates: bool
    admin_cache_ttl_seconds: int
    openai_timeout_seconds: float
    max_moderation_concurrency: int

    @staticmethod
    def from_env() -> "Settings":
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1"
        openai_model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-4o-mini"
        moderation_mode = os.getenv("MODERATION_MODE", "strict").strip().lower()
        if moderation_mode not in {"strict", "balanced"}:
            moderation_mode = "strict"
        audit_chat_id_raw = os.getenv("AUDIT_CHAT_ID", "").strip()
        if audit_chat_id_raw:
            try:
                audit_chat_id = int(audit_chat_id_raw)
            except ValueError as exc:
                raise RuntimeError("AUDIT_CHAT_ID must be an integer chat ID") from exc
        else:
            audit_chat_id = None
        debug = os.getenv("DEBUG", "1").strip() == "1"
        admin_password = os.getenv("ADMIN_PASSWORD", "").strip() or None
        admin_session_ttl_seconds = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", "2592000").strip() or "2592000")
        state_file = Path(os.getenv("STATE_FILE", "bot_state.json").strip() or "bot_state.json").expanduser()
        webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip() or None
        drop_pending_updates = os.getenv("DROP_PENDING_UPDATES", "1").strip() == "1"
        admin_cache_ttl_seconds = int(os.getenv("ADMIN_CACHE_TTL_SECONDS", "300").strip() or "300")
        openai_timeout_seconds = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "20").strip() or "20")
        max_moderation_concurrency = int(os.getenv("MAX_MODERATION_CONCURRENCY", "8").strip() or "8")

        missing = []
        if not bot_token:
            missing.append("BOT_TOKEN")
        if not openai_api_key:
            missing.append("OPENAI_API_KEY")

        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
        if admin_session_ttl_seconds <= 0:
            raise RuntimeError("ADMIN_SESSION_TTL_SECONDS must be > 0")
        if admin_cache_ttl_seconds < 0:
            raise RuntimeError("ADMIN_CACHE_TTL_SECONDS must be >= 0")
        if openai_timeout_seconds <= 0:
            raise RuntimeError("OPENAI_TIMEOUT_SECONDS must be > 0")
        if max_moderation_concurrency <= 0:
            raise RuntimeError("MAX_MODERATION_CONCURRENCY must be > 0")

        return Settings(
            bot_token=bot_token,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            openai_model=openai_model,
            moderation_mode=moderation_mode,
            audit_chat_id=audit_chat_id,
            debug=debug,
            admin_password=admin_password,
            admin_session_ttl_seconds=admin_session_ttl_seconds,
            state_file=state_file,
            webhook_secret=webhook_secret,
            drop_pending_updates=drop_pending_updates,
            admin_cache_ttl_seconds=admin_cache_ttl_seconds,
            openai_timeout_seconds=openai_timeout_seconds,
            max_moderation_concurrency=max_moderation_concurrency,
        )


@dataclass
class FeatureFlags:
    service_messages: bool = True
    ai_moderation: bool = True
    edited_messages: bool = True
    audit_logging: bool = True


@dataclass
class AdminSession:
    user_id: int
    display_name: str
    username: Optional[str]
    expires_at: float


class BotStateStore:
    def __init__(
        self,
        path: Path,
        *,
        after_save: Optional[Callable[[], None]] = None,
    ) -> None:
        self.path = path
        self._after_save = after_save
        self._lock = threading.RLock()
        self.flags, self.admin_sessions = self._load()

    def _load(self) -> tuple[FeatureFlags, dict[int, AdminSession]]:
        if not self.path.exists():
            return FeatureFlags(), {}

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            features = raw.get("features", {})
            sessions_raw = raw.get("admin_sessions", {})
            admin_sessions: dict[int, AdminSession] = {}
            for user_id_raw, session in sessions_raw.items():
                try:
                    user_id = int(user_id_raw)
                    expires_at = float(session["expires_at"])
                    display_name = str(session.get("display_name") or str(user_id))
                    username = session.get("username")
                    if username is not None:
                        username = str(username)
                except (KeyError, TypeError, ValueError):
                    continue

                admin_sessions[user_id] = AdminSession(
                    user_id=user_id,
                    display_name=display_name,
                    username=username,
                    expires_at=expires_at,
                )

            return FeatureFlags(
                service_messages=bool(features.get("service_messages", True)),
                ai_moderation=bool(features.get("ai_moderation", True)),
                edited_messages=bool(features.get("edited_messages", True)),
                audit_logging=bool(features.get("audit_logging", True)),
            ), admin_sessions
        except Exception as exc:  # noqa: BLE001 - state file should not crash the bot
            logging.warning("Could not load feature state from %s: %s", self.path, exc)
            return FeatureFlags(), {}

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "features": asdict(self.flags),
            "admin_sessions": {
                str(user_id): {
                    "display_name": session.display_name,
                    "username": session.username,
                    "expires_at": session.expires_at,
                }
                for user_id, session in sorted(self.admin_sessions.items())
            },
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if self._after_save is not None:
            self._after_save()

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def snapshot(self) -> dict[str, bool]:
        with self._lock:
            return asdict(self.flags)

    def is_enabled(self, feature_name: str) -> bool:
        if feature_name not in FEATURE_LABELS:
            raise KeyError(f"Unknown feature: {feature_name}")
        with self._lock:
            return bool(getattr(self.flags, feature_name))

    def set_feature(self, feature_name: str, enabled: bool) -> bool:
        if feature_name not in FEATURE_LABELS:
            raise KeyError(f"Unknown feature: {feature_name}")

        with self._lock:
            setattr(self.flags, feature_name, enabled)
            self._save_locked()
            return enabled

    def toggle(self, feature_name: str) -> bool:
        if feature_name not in FEATURE_LABELS:
            raise KeyError(f"Unknown feature: {feature_name}")
        with self._lock:
            enabled = not bool(getattr(self.flags, feature_name))
            setattr(self.flags, feature_name, enabled)
            self._save_locked()
            return enabled

    def purge_expired_sessions(self, *, now: Optional[float] = None) -> bool:
        with self._lock:
            now = time.time() if now is None else now
            expired_ids = [
                user_id
                for user_id, session in self.admin_sessions.items()
                if session.expires_at <= now
            ]
            if not expired_ids:
                return False

            for user_id in expired_ids:
                self.admin_sessions.pop(user_id, None)
            self._save_locked()
            return True

    def has_valid_admin_session(self, user_id: int, *, now: Optional[float] = None) -> bool:
        with self._lock:
            self.purge_expired_sessions(now=now)
            return user_id in self.admin_sessions

    def create_admin_session(
        self,
        *,
        user_id: int,
        display_name: str,
        username: Optional[str],
        ttl_seconds: int,
        now: Optional[float] = None,
    ) -> AdminSession:
        with self._lock:
            now = time.time() if now is None else now
            session = AdminSession(
                user_id=user_id,
                display_name=display_name,
                username=username,
                expires_at=now + ttl_seconds,
            )
            self.admin_sessions[user_id] = session
            self._save_locked()
            return session

    def delete_admin_session(self, user_id: int) -> bool:
        with self._lock:
            removed = self.admin_sessions.pop(user_id, None)
            if removed is None:
                return False
            self._save_locked()
            return True

    def list_active_admin_user_ids(self, *, now: Optional[float] = None) -> list[int]:
        with self._lock:
            self.purge_expired_sessions(now=now)
            return sorted(self.admin_sessions)


class AdModerator:
    def __init__(self, model: str, client: AsyncOpenAI, mode: str = "strict", debug: bool = True) -> None:
        self.model = model
        self.client = client
        self.mode = mode
        self.debug = debug

    async def is_advertisement(self, text: str) -> tuple[bool, str]:
        """Return (is_ad, reason)."""
        trimmed = text.strip()
        if not trimmed:
            return False, "empty"

        mode_hint = (
            "Be strict. If uncertain, prefer classifying as ad."
            if self.mode == "strict"
            else "Be balanced. If uncertain, prefer classifying as non-ad."
        )
        system_prompt = (
            "You are a Telegram moderator. "
            "Detect if a message is ad/spam/promo content. "
            "Support Uzbek Latin, Uzbek Cyrillic, and Russian. "
            "Treat as ads: product/service promotion, sales offers, referral links, "
            "channel/group invites, gambling/casino, crypto shilling, suspicious job offers, "
            "contact me for business, external lead generation, repeated copy-paste marketing. "
            "Do not mark normal conversation/questions/opinions as ads. "
            f"{mode_hint} "
            "Return JSON only: {\"is_ad\": boolean, \"reason\": string}."
        )

        user_prompt = f"Classify this Telegram message:\n\n{trimmed}"

        request_kwargs = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }

        try:
            response = await self.client.chat.completions.create(
                response_format={"type": "json_object"},
                **request_kwargs,
            )
        except BadRequestError as exc:
            if self.debug:
                logging.warning(
                    "Provider rejected response_format; retrying without it | model=%s error=%s",
                    self.model,
                    exc,
                )
            response = await self.client.chat.completions.create(**request_kwargs)

        content = response.choices[0].message.content or "{}"
        parsed = extract_json_object(content)

        is_ad = bool(parsed.get("is_ad", False))
        reason = str(parsed.get("reason", "no reason"))

        return is_ad, reason


def extract_json_object(content: str) -> dict:
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not match:
        raise json.JSONDecodeError("Could not find JSON object", content, 0)

    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("JSON payload is not an object", content, 0)
    return parsed


def configure_logging(debug: bool) -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO if debug else logging.WARNING,
    )
    # Avoid leaking secrets through verbose HTTP client logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_message_text(update: Update) -> Optional[str]:
    message = update.effective_message
    if not message:
        return None

    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return None


def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == ChatType.PRIVATE)


def get_state_store(context: ContextTypes.DEFAULT_TYPE) -> BotStateStore:
    return context.bot_data["state_store"]


def get_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def get_pending_logins(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    pending = context.bot_data.setdefault("pending_login_ids", set())
    return pending


def get_media_groups(context: ContextTypes.DEFAULT_TYPE) -> dict[tuple[int, str], dict]:
    media_groups = context.bot_data.setdefault("media_groups", {})
    return media_groups


def get_media_group_index(context: ContextTypes.DEFAULT_TYPE) -> dict[tuple[int, str], dict]:
    media_group_index = context.bot_data.setdefault("media_group_index", {})
    return media_group_index


def get_admin_cache(context: ContextTypes.DEFAULT_TYPE) -> dict[tuple[int, int], tuple[bool, float]]:
    admin_cache = context.bot_data.setdefault("admin_cache", {})
    return admin_cache


def get_moderation_semaphore(context: ContextTypes.DEFAULT_TYPE) -> asyncio.Semaphore:
    return context.bot_data["moderation_semaphore"]


def is_authenticated_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    return get_state_store(context).has_valid_admin_session(user.id)


def normalize_feature_name(raw_name: str) -> Optional[str]:
    return FEATURE_ALIASES.get(raw_name.strip().lower())


def get_actor_display_name(message) -> str:
    actor = message.from_user
    if actor:
        return f"{actor.full_name} (@{actor.username})" if actor.username else actor.full_name
    if message.sender_chat:
        return f"{message.sender_chat.title} [sender_chat]"
    return "unknown"


def refresh_authenticated_admin_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    settings = get_settings(context)
    get_state_store(context).create_admin_session(
        user_id=user.id,
        display_name=user.full_name,
        username=user.username,
        ttl_seconds=settings.admin_session_ttl_seconds,
    )


async def classify_text(
    context: ContextTypes.DEFAULT_TYPE,
    moderator: "AdModerator",
    text: str,
) -> tuple[bool, str]:
    async with get_moderation_semaphore(context):
        return await moderator.is_advertisement(text)


def build_admin_panel_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    state_store = get_state_store(context)
    settings = get_settings(context)
    snapshot = state_store.snapshot()
    audit_target = str(settings.audit_chat_id) if settings.audit_chat_id else "not configured"

    return (
        "mr.cleaner control panel\n\n"
        "Global features:\n"
        f"- {FEATURE_LABELS['service_messages']}: {'ON' if snapshot['service_messages'] else 'OFF'}\n"
        f"- {FEATURE_LABELS['ai_moderation']}: {'ON' if snapshot['ai_moderation'] else 'OFF'}\n"
        f"- {FEATURE_LABELS['edited_messages']}: {'ON' if snapshot['edited_messages'] else 'OFF'}\n"
        f"- {FEATURE_LABELS['audit_logging']}: {'ON' if snapshot['audit_logging'] else 'OFF'} "
        f"(AUDIT_CHAT_ID: {audit_target})\n\n"
        "Use the buttons below or /feature <service|moderation|edited|audit> <on|off>."
    )


def build_admin_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    state_store = get_state_store(context)
    snapshot = state_store.snapshot()
    rows = [
        [
            InlineKeyboardButton(
                f"{FEATURE_LABELS['service_messages']}: {'ON' if snapshot['service_messages'] else 'OFF'}",
                callback_data="toggle:service_messages",
            )
        ],
        [
            InlineKeyboardButton(
                f"{FEATURE_LABELS['ai_moderation']}: {'ON' if snapshot['ai_moderation'] else 'OFF'}",
                callback_data="toggle:ai_moderation",
            )
        ],
        [
            InlineKeyboardButton(
                f"{FEATURE_LABELS['edited_messages']}: {'ON' if snapshot['edited_messages'] else 'OFF'}",
                callback_data="toggle:edited_messages",
            )
        ],
        [
            InlineKeyboardButton(
                f"{FEATURE_LABELS['audit_logging']}: {'ON' if snapshot['audit_logging'] else 'OFF'}",
                callback_data="toggle:audit_logging",
            )
        ],
        [
            InlineKeyboardButton("Refresh", callback_data="panel:refresh"),
            InlineKeyboardButton("Logout", callback_data="auth:logout"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message:
        return

    await message.reply_text(build_admin_panel_text(context), reply_markup=build_admin_keyboard(context))


async def edit_admin_panel(query, context: ContextTypes.DEFAULT_TYPE, prefix: Optional[str] = None) -> None:
    panel_text = build_admin_panel_text(context)
    if prefix:
        panel_text = f"{prefix}\n\n{panel_text}"

    try:
        await query.edit_message_text(panel_text, reply_markup=build_admin_keyboard(context))
    except TelegramError:
        if query.message:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=panel_text,
                reply_markup=build_admin_keyboard(context),
            )


async def notify_logged_in_admins(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    skip_user_ids: Optional[set[int]] = None,
) -> None:
    state_store = get_state_store(context)
    sessions = set(state_store.list_active_admin_user_ids())
    if skip_user_ids:
        sessions.difference_update(skip_user_ids)
    if not sessions:
        return

    send_tasks = [
        context.bot.send_message(chat_id=user_id, text=text)
        for user_id in sessions
    ]
    results = await asyncio.gather(*send_tasks, return_exceptions=True)

    for user_id, result in zip(sessions, results):
        if isinstance(result, TelegramError):
            logging.warning("Failed to notify admin session user_id=%s: %s", user_id, result)
            state_store.delete_admin_session(user_id)


async def delete_private_message_for_privacy(update: Update) -> None:
    message = update.effective_message
    if not message or not is_private_chat(update):
        return

    try:
        await message.delete()
    except TelegramError:
        pass


async def authenticate_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, password: str) -> None:
    settings = get_settings(context)
    state_store = get_state_store(context)
    user = update.effective_user
    chat = update.effective_chat
    pending = get_pending_logins(context)

    if not user or not chat:
        return

    pending.discard(user.id)

    if not settings.admin_password:
        await context.bot.send_message(
            chat_id=chat.id,
            text="Admin control is disabled. Set ADMIN_PASSWORD in .env and restart the bot.",
        )
        return

    await delete_private_message_for_privacy(update)

    if hmac.compare_digest(password, settings.admin_password):
        state_store.create_admin_session(
            user_id=user.id,
            display_name=user.full_name,
            username=user.username,
            ttl_seconds=settings.admin_session_ttl_seconds,
        )
        logging.info("Admin session started | user_id=%s username=%s", user.id, user.username or "n/a")
        await context.bot.send_message(
            chat_id=chat.id,
            text="Admin login successful.\n\n" + build_admin_panel_text(context),
            reply_markup=build_admin_keyboard(context),
        )
        return

    logging.warning("Failed admin login attempt | user_id=%s username=%s", user.id, user.username or "n/a")
    await context.bot.send_message(chat_id=chat.id, text="Incorrect password. Use /login to try again.")


async def require_admin_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    if not is_authenticated_admin(update, context):
        if message:
            await message.reply_text("Login required. Open a private chat with the bot and use /login.")
        return False
    refresh_authenticated_admin_session(update, context)
    return True


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return

    settings = get_settings(context)
    message = update.effective_message
    if not message:
        return

    if is_authenticated_admin(update, context):
        refresh_authenticated_admin_session(update, context)
        await send_admin_panel(update, context)
        return

    if settings.admin_password:
        await message.reply_text(
            "Use /login to authenticate, then /panel to manage global bot features."
        )
        return

    await message.reply_text(
        "This bot is running, but admin control is disabled until ADMIN_PASSWORD is set in .env."
    )


async def handle_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return

    settings = get_settings(context)
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    if not settings.admin_password:
        await message.reply_text("Set ADMIN_PASSWORD in .env to enable admin login.")
        return

    if is_authenticated_admin(update, context):
        refresh_authenticated_admin_session(update, context)
        await send_admin_panel(update, context)
        return

    password = " ".join(context.args).strip() if context.args else ""
    if password:
        await authenticate_admin(update, context, password)
        return

    get_pending_logins(context).add(user.id)
    await message.reply_text(
        "Send the admin password in this private chat. After login, use /panel to manage features."
    )


async def handle_private_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return

    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return

    if user.id not in get_pending_logins(context):
        return

    await authenticate_admin(update, context, message.text.strip())


async def handle_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return

    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return

    get_pending_logins(context).discard(user.id)
    get_state_store(context).delete_admin_session(user.id)
    await message.reply_text("Admin session closed. Use /login to sign in again.")


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return

    if not await require_admin_session(update, context):
        return

    await send_admin_panel(update, context)


async def handle_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return

    if not await require_admin_session(update, context):
        return

    await send_admin_panel(update, context)


async def handle_feature_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return

    message = update.effective_message
    user = update.effective_user
    if not message:
        return

    if not await require_admin_session(update, context):
        return

    if len(context.args) != 2:
        await message.reply_text("Usage: /feature <service|moderation|edited|audit> <on|off>")
        return

    feature_name = normalize_feature_name(context.args[0])
    desired_state = context.args[1].strip().lower()

    if feature_name is None:
        await message.reply_text("Unknown feature. Use one of: service, moderation, edited, audit.")
        return

    if desired_state not in {"on", "off"}:
        await message.reply_text("State must be either on or off.")
        return

    state_store = get_state_store(context)
    enabled = state_store.set_feature(feature_name, desired_state == "on")
    if user:
        logging.info(
            "Feature updated via command | user_id=%s feature=%s enabled=%s",
            user.id,
            feature_name,
            enabled,
        )
    await notify_logged_in_admins(
        context,
        (
            "[mr.cleaner] Feature updated\n"
            f"User: {user.full_name if user else 'unknown'}\n"
            f"Feature: {FEATURE_LABELS[feature_name]}\n"
            f"State: {'ON' if enabled else 'OFF'}"
        ),
    )
    await send_admin_panel(update, context)


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query:
        return

    if not is_authenticated_admin(update, context):
        await query.answer("Login required. Use /login in private chat.", show_alert=True)
        return
    refresh_authenticated_admin_session(update, context)

    data = query.data or ""
    if data == "panel:refresh":
        await query.answer("Panel refreshed.")
        await edit_admin_panel(query, context)
        return

    if data == "auth:logout":
        if user:
            get_state_store(context).delete_admin_session(user.id)
            get_pending_logins(context).discard(user.id)
        await query.answer("Logged out.")
        await query.edit_message_text("Admin session closed. Use /login to sign in again.")
        return

    if not data.startswith("toggle:"):
        await query.answer("Unknown action.", show_alert=True)
        return

    feature_name = normalize_feature_name(data.split(":", maxsplit=1)[1])
    if feature_name is None:
        await query.answer("Unknown feature.", show_alert=True)
        return

    state_store = get_state_store(context)
    enabled = state_store.toggle(feature_name)
    if user:
        logging.info(
            "Feature toggled via panel | user_id=%s feature=%s enabled=%s",
            user.id,
            feature_name,
            enabled,
        )
    await notify_logged_in_admins(
        context,
        (
            "[mr.cleaner] Feature updated\n"
            f"User: {user.full_name if user else 'unknown'}\n"
            f"Feature: {FEATURE_LABELS[feature_name]}\n"
            f"State: {'ON' if enabled else 'OFF'}"
        ),
    )
    await query.answer(f"{FEATURE_LABELS[feature_name]} {'enabled' if enabled else 'disabled'}.")
    await edit_admin_panel(query, context)


async def delete_message_if_possible(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str) -> None:
    message = update.effective_message
    if not message:
        return

    try:
        await message.delete()
        if context.bot_data.get("debug", True):
            logging.info(
                "Deleted message | chat_id=%s message_id=%s reason=%s",
                message.chat_id,
                message.message_id,
                reason,
            )
        await send_audit_log(update, context, reason)
        await notify_logged_in_admins(
            context,
            (
                "[mr.cleaner] Deleted message\n"
                f"Reason: {reason}\n"
                f"Chat: {message.chat.title or message.chat_id} ({message.chat_id})\n"
                f"User: {get_actor_display_name(message)}\n"
                f"Message ID: {message.message_id}\n"
                f"Text: {(message.text or message.caption or '<non-text>')[:500]}"
            ),
        )
    except TelegramError as exc:
        logging.warning(
            "Could not delete message | chat_id=%s message_id=%s reason=%s error=%s",
            message.chat_id,
            message.message_id,
            reason,
            exc,
        )
        await notify_logged_in_admins(
            context,
            (
                "[mr.cleaner] Delete failed\n"
                f"Reason: {reason}\n"
                f"Chat: {message.chat.title or message.chat_id} ({message.chat_id})\n"
                f"User: {get_actor_display_name(message)}\n"
                f"Message ID: {message.message_id}\n"
                f"Error: {exc}"
            ),
        )


async def delete_media_group_if_possible(context: ContextTypes.DEFAULT_TYPE, media_group_state: dict, reason: str) -> None:
    deleted_count = 0
    for message_id in sorted(media_group_state["message_ids"]):
        try:
            await context.bot.delete_message(chat_id=media_group_state["chat_id"], message_id=message_id)
            deleted_count += 1
        except TelegramError as exc:
            logging.warning(
                "Could not delete media-group message | chat_id=%s message_id=%s reason=%s error=%s",
                media_group_state["chat_id"],
                message_id,
                reason,
                exc,
            )

    if not deleted_count:
        return

    if context.bot_data.get("debug", True):
        logging.info(
            "Deleted media group | chat_id=%s media_group_id=%s deleted=%s reason=%s",
            media_group_state["chat_id"],
            media_group_state["media_group_id"],
            deleted_count,
            reason,
        )

    await send_media_group_audit_log(context, media_group_state, reason, deleted_count)
    await notify_logged_in_admins(
        context,
        (
            "[mr.cleaner] Deleted media group\n"
            f"Reason: {reason}\n"
            f"Chat: {media_group_state['chat_title'] or media_group_state['chat_id']} ({media_group_state['chat_id']})\n"
            f"User: {media_group_state['actor_display']}\n"
            f"Media Group ID: {media_group_state['media_group_id']}\n"
            f"Messages: {deleted_count}\n"
            f"Text: {(media_group_state['text'] or '<media-group>')[:500]}"
        ),
    )


async def send_audit_log(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str) -> None:
    msg = update.effective_message
    audit_chat_id = context.bot_data.get("audit_chat_id")
    state_store = get_state_store(context)
    if not msg or not audit_chat_id or not state_store.is_enabled("audit_logging"):
        return

    actor_display = get_actor_display_name(msg)

    message_text = msg.text or msg.caption or "<non-text>"
    if len(message_text) > 500:
        message_text = message_text[:500] + "..."

    audit_text = (
        f"[mr.cleaner] Deleted message\n"
        f"Reason: {reason}\n"
        f"Chat: {msg.chat.title or msg.chat_id} ({msg.chat_id})\n"
        f"User: {actor_display}\n"
        f"Message ID: {msg.message_id}\n"
        f"Text: {message_text}"
    )

    try:
        await context.bot.send_message(chat_id=audit_chat_id, text=audit_text)
    except TelegramError as exc:
        logging.warning("Failed to send audit log to %s: %s", audit_chat_id, exc)


async def send_media_group_audit_log(
    context: ContextTypes.DEFAULT_TYPE,
    media_group_state: dict,
    reason: str,
    deleted_count: int,
) -> None:
    audit_chat_id = context.bot_data.get("audit_chat_id")
    state_store = get_state_store(context)
    if not audit_chat_id or not state_store.is_enabled("audit_logging"):
        return

    message_text = media_group_state["text"] or "<media-group>"
    if len(message_text) > 500:
        message_text = message_text[:500] + "..."

    audit_text = (
        f"[mr.cleaner] Deleted media group\n"
        f"Reason: {reason}\n"
        f"Chat: {media_group_state['chat_title'] or media_group_state['chat_id']} ({media_group_state['chat_id']})\n"
        f"User: {media_group_state['actor_display']}\n"
        f"Media Group ID: {media_group_state['media_group_id']}\n"
        f"Messages: {deleted_count}\n"
        f"Text: {message_text}"
    )

    try:
        await context.bot.send_message(chat_id=audit_chat_id, text=audit_text)
    except TelegramError as exc:
        logging.warning("Failed to send media-group audit log to %s: %s", audit_chat_id, exc)


async def is_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    if not msg:
        return False
    if msg.sender_chat and msg.sender_chat.id == msg.chat_id:
        return True
    if not msg.from_user:
        return False

    settings = get_settings(context)
    cache_key = (msg.chat_id, msg.from_user.id)
    cached_entry = get_admin_cache(context).get(cache_key)
    now = time.monotonic()
    if cached_entry and cached_entry[1] > now:
        return cached_entry[0]

    try:
        member = await context.bot.get_chat_member(msg.chat_id, msg.from_user.id)
        is_admin = member.status in {"administrator", "creator"}
        if settings.admin_cache_ttl_seconds > 0:
            get_admin_cache(context)[cache_key] = (is_admin, now + settings.admin_cache_ttl_seconds)
        return is_admin
    except TelegramError as exc:
        logging.warning("Could not check admin status for user_id=%s: %s", msg.from_user.id, exc)
        return False


async def handle_service_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete system join/left events in groups/supergroups."""
    msg = update.effective_message
    state_store = get_state_store(context)
    if not msg:
        return
    if msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not state_store.is_enabled("service_messages"):
        return

    await delete_message_if_possible(update, context, "service_message")


async def handle_regular_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    state_store = get_state_store(context)
    if not msg:
        return
    if msg.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if not state_store.is_enabled("ai_moderation"):
        return
    if update.edited_message and not state_store.is_enabled("edited_messages"):
        return

    # Skip service messages and commands.
    if msg.new_chat_members or msg.left_chat_member:
        return
    if msg.text and msg.text.startswith("/"):
        return
    if await is_admin_or_owner(update, context):
        if context.bot_data.get("debug", True):
            logging.info(
                "Skipped admin/owner message | chat_id=%s message_id=%s",
                msg.chat_id,
                msg.message_id,
            )
        return

    if msg.media_group_id:
        await enqueue_media_group_moderation(update, context)
        return

    text = build_message_text(update)
    if not text:
        return

    moderator: AdModerator = context.bot_data["moderator"]

    try:
        is_ad, ai_reason = await classify_text(context, moderator, text)
    except Exception as exc:  # noqa: BLE001 - keep bot resilient
        logging.exception("AI moderation failed: %s", exc)
        return

    if is_ad:
        await delete_message_if_possible(update, context, f"ai_ad:{ai_reason}")
    elif context.bot_data.get("debug", True):
        logging.info(
            "Allowed message | chat_id=%s message_id=%s",
            msg.chat_id,
            msg.message_id,
        )


async def enqueue_media_group_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.media_group_id:
        return

    media_groups = get_media_groups(context)
    media_group_index = get_media_group_index(context)
    key = (msg.chat_id, msg.media_group_id)
    text = build_message_text(update)
    indexed_state = media_group_index.setdefault(
        key,
        {
            "message_ids": set(),
            "chat_title": msg.chat.title,
            "actor_display": get_actor_display_name(msg),
            "text": "",
        },
    )
    indexed_state["message_ids"].add(msg.message_id)
    indexed_state["chat_title"] = msg.chat.title
    indexed_state["actor_display"] = get_actor_display_name(msg)
    if text:
        indexed_state["text"] = text

    state = media_groups.get(key)
    if state is None:
        state = {
            "chat_id": msg.chat_id,
            "chat_title": msg.chat.title,
            "media_group_id": msg.media_group_id,
            "message_ids": set(indexed_state["message_ids"]),
            "actor_display": indexed_state["actor_display"],
            "text": indexed_state["text"],
        }
        media_groups[key] = state

    state["message_ids"].add(msg.message_id)
    state["message_ids"].update(indexed_state["message_ids"])
    state["chat_title"] = msg.chat.title
    state["actor_display"] = indexed_state["actor_display"]
    if text:
        state["text"] = text
    existing_task = state.get("task")
    if existing_task and not existing_task.done():
        existing_task.cancel()
    state["task"] = asyncio.create_task(process_media_group(context, key))


async def process_media_group(context: ContextTypes.DEFAULT_TYPE, key: tuple[int, str]) -> None:
    try:
        await asyncio.sleep(MEDIA_GROUP_SETTLE_SECONDS)
    except asyncio.CancelledError:
        return

    media_groups = get_media_groups(context)
    state = media_groups.pop(key, None)
    if not state or not state["text"]:
        return

    moderator: AdModerator = context.bot_data["moderator"]

    try:
        is_ad, ai_reason = await classify_text(context, moderator, state["text"])
    except Exception as exc:  # noqa: BLE001 - keep bot resilient
        logging.exception("AI moderation failed for media group %s: %s", state["media_group_id"], exc)
        return

    if is_ad:
        await delete_media_group_if_possible(context, state, f"ai_ad:{ai_reason}")
    elif context.bot_data.get("debug", True):
        logging.info(
            "Allowed media group | chat_id=%s media_group_id=%s messages=%s",
            state["chat_id"],
            state["media_group_id"],
            len(state["message_ids"]),
        )


async def on_startup(application: Application) -> None:
    me = await application.bot.get_me()
    settings: Settings = application.bot_data["settings"]
    state_store: BotStateStore = application.bot_data["state_store"]
    logging.info(
        "Bot started as @%s | model=%s drop_pending_updates=%s moderation_concurrency=%s",
        me.username,
        settings.openai_model,
        settings.drop_pending_updates,
        settings.max_moderation_concurrency,
    )
    state_store.purge_expired_sessions()
    logging.info("Feature state | %s", state_store.snapshot())
    logging.info("Active admin sessions | %s", state_store.list_active_admin_user_ids())


def create_app(
    settings: Settings,
    *,
    state_store: Optional[BotStateStore] = None,
    with_updater: bool = True,
) -> Application:
    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=settings.openai_timeout_seconds,
        max_retries=2,
    )
    moderator = AdModerator(
        model=settings.openai_model,
        client=client,
        mode=settings.moderation_mode,
        debug=settings.debug,
    )

    builder = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .rate_limiter(AIORateLimiter())
        .post_init(on_startup)
    )
    if not with_updater:
        builder = builder.updater(None)
    app = builder.build()

    app.bot_data["moderator"] = moderator
    app.bot_data["debug"] = settings.debug
    app.bot_data["audit_chat_id"] = settings.audit_chat_id
    app.bot_data["settings"] = settings
    app.bot_data["state_store"] = state_store or BotStateStore(settings.state_file)
    app.bot_data["pending_login_ids"] = set()
    app.bot_data["media_groups"] = {}
    app.bot_data["media_group_index"] = {}
    app.bot_data["admin_cache"] = {}
    app.bot_data["moderation_semaphore"] = asyncio.Semaphore(settings.max_moderation_concurrency)

    service_filter = filters.ChatType.GROUPS & (
        filters.StatusUpdate.NEW_CHAT_MEMBERS
        | filters.StatusUpdate.LEFT_CHAT_MEMBER
    )

    regular_filter = (
        filters.ChatType.GROUPS
        & filters.UpdateType.MESSAGES
        & (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO)
    )

    private_command_filter = filters.ChatType.PRIVATE
    private_text_filter = filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND

    app.add_handler(CommandHandler("start", handle_start, filters=private_command_filter), group=0)
    app.add_handler(CommandHandler("login", handle_login, filters=private_command_filter), group=0)
    app.add_handler(CommandHandler("logout", handle_logout, filters=private_command_filter), group=0)
    app.add_handler(CommandHandler("status", handle_status, filters=private_command_filter), group=0)
    app.add_handler(CommandHandler("panel", handle_panel, filters=private_command_filter), group=0)
    app.add_handler(CommandHandler("feature", handle_feature_command, filters=private_command_filter), group=0)
    app.add_handler(CallbackQueryHandler(handle_admin_callback), group=0)
    app.add_handler(MessageHandler(private_text_filter, handle_private_password), group=0)
    app.add_handler(MessageHandler(service_filter, handle_service_messages), group=0)
    app.add_handler(MessageHandler(regular_filter, handle_regular_messages), group=1)

    return app


async def initialize_application(application: Application) -> None:
    await application.initialize()
    if application.post_init:
        await application.post_init(application)
    await application.start()


async def shutdown_application(application: Application) -> None:
    if application.running:
        await application.stop()
        if application.post_stop:
            await application.post_stop(application)
    await application.shutdown()
    moderator: Optional[AdModerator] = application.bot_data.get("moderator")
    if moderator is not None:
        await moderator.client.close()
    if application.post_shutdown:
        await application.post_shutdown(application)


async def process_raw_update(application: Application, payload: dict) -> bool:
    update = Update.de_json(payload, application.bot)
    if update is None:
        return False
    await application.process_update(update)
    return True


def build_webhook_url(base_url: str, path: str) -> str:
    normalized_base = base_url.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{normalized_base}{normalized_path}"


def serialize_webhook_info(info) -> dict[str, object]:
    return {
        "url": info.url,
        "has_custom_certificate": info.has_custom_certificate,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date.isoformat() if info.last_error_date else None,
        "last_error_message": info.last_error_message,
        "max_connections": info.max_connections,
        "ip_address": info.ip_address,
    }


async def configure_bot_webhook(
    settings: Settings,
    *,
    webhook_url: str,
    secret_token: Optional[str] = None,
) -> dict[str, object]:
    bot = Bot(token=settings.bot_token)
    await bot.initialize()
    try:
        await bot.set_webhook(
            url=webhook_url,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=settings.drop_pending_updates,
            secret_token=secret_token,
        )
        return serialize_webhook_info(await bot.get_webhook_info())
    finally:
        await bot.shutdown()


async def clear_bot_webhook(
    settings: Settings,
    *,
    drop_pending_updates: Optional[bool] = None,
) -> dict[str, object]:
    bot = Bot(token=settings.bot_token)
    await bot.initialize()
    try:
        await bot.delete_webhook(drop_pending_updates=drop_pending_updates)
        return serialize_webhook_info(await bot.get_webhook_info())
    finally:
        await bot.shutdown()


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.debug)
    app = create_app(settings)

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=settings.drop_pending_updates,
    )


if __name__ == "__main__":
    main()
