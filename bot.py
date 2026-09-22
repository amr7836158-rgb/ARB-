"""
================================================================================
 main.py  —  Central Telegram BOT Manager & Reaction Scheduler   (v2, single file)
================================================================================

 WHAT THIS IS
 ------------
 A central control bot ("Main Manager Bot") that lets an admin register MANY real
 Telegram bots by their Bot Tokens (created in @BotFather), AUTO-DISCOVERS the
 channels those bots have been added to (no "add channel" form), builds a per-post
 queue, and lets each bot react to a channel post ONE AFTER THE OTHER on a
 configurable schedule — with retry+backoff, rate limiting, dedup and logging.

 WHAT THIS IS *NOT*
 ------------------
 No user accounts, no phone numbers, no user sessions, no MTProto, no fake
 accounts, no spoofing. Every executor is a real Telegram bot via Bot Token.
 Only the official Telegram Bot API is used.

 SECURITY / TOKEN POLICY
 -----------------------
 Tokens are read ONLY from environment variables. NEVER hardcode a token in this
 file or in .env.example. If a token was ever pasted into a chat, ROTATE it in
 @BotFather (/revoke) — a leaked token can be used by anyone.

 CAPABILITY NOTE (verified against official docs)
 ------------------------------------------------
  * Bots CAN change the reaction on a message: setMessageReaction(chat_id,
    message_id, reaction, is_big).
    https://core.telegram.org/bots/api#setmessagereaction
    https://docs.aiogram.dev/en/latest/api/methods/set_message_reaction.html
  * Documented constraints we DO NOT work around:
      - "Service messages of some types can't be reacted to."
      - A non-premium bot can set UP TO ONE reaction per message.
      - Bots can NEVER use paid reactions.
      - Reading channel posts requires the bot to be an ADMIN in the channel and
        the "channel_post" update type enabled.
  * The usable reactions for a chat are read dynamically from
    getChat().available_reactions — never hardcoded.

 CHANNEL DISCOVERY (no manual "add channel")
 -------------------------------------------
 There is NO Bot API method that lists "all chats a bot is in". Discovery is
 therefore PASSIVE and driven by real updates:
     - my_chat_member  : fires when the bot is added/promoted in a channel
     - channel_post    : fires for new posts in a channel the bot administers
 On each such update the manager inspects the chat (getChat), stores/updates it,
 and checks EVERY enabled worker bot's membership/permission (getChatMember).
 So: add the bots to the channel manually in Telegram, then the channel appears
 here automatically.

 REAL RISK STATEMENT (honest, no "zero risk")
 --------------------------------------------
 Using the official Bot API per the rules does NOT get a bot banned by itself.
 The real ban/throttle triggers are: IGNORING 429 "Too Many Requests" / flooding,
 sending bulk UNSOLICITED messages, or spamming. This system mitigates them with:
 a per-bot rate limiter, a global concurrency cap, and retry with exponential
 backoff that honours Telegram's `retry_after`. It performs reactions only, never
 mass messaging. There is still no absolute guarantee — enforcement is ultimately
 Telegram's decision — but the design avoids the known triggers.

 QUICK START
 -----------
   1) python -m venv .venv && source .venv/bin/activate
   2) pip install -r requirements.txt
   3) cp .env.example .env  → fill MANAGER_BOT_TOKEN + ADMIN_USER_IDS
   4) python main.py
   5) In Telegram: /start, /addbot (send each worker Bot Token),
      then add manager + worker bots to a channel as admin → /channels auto-fills.
      /bindbot, /setreaction, /setdelay, /status, /queue, /logs.

 RENDER
 ------
   Build:   pip install -r requirements.txt
   Start:   python main.py          (single long-polling worker; run ONE instance)
   Env:     MANAGER_BOT_TOKEN, ADMIN_USER_IDS, DATABASE_URL, DEFAULT_DELAY_SECONDS,
            MAX_RETRIES, RETRY_DELAY_SECONDS, WORK_START, WORK_END, LOG_LEVEL
   NOTE: Render's disk is EPHEMERAL — SQLite is wiped on redeploy/restart unless a
   persistent disk is mounted. For durable state mount a Disk and set
   DATABASE_URL=sqlite+aiosqlite:////var/data/manager.db (or use PostgreSQL).
   Run exactly ONE instance (long polling); two instances cause HTTP 409 conflicts.
================================================================================
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone, timedelta
from typing import AsyncIterator, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv optional
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# =============================================================================
# 1. CONFIGURATION  (env only — never hardcode secrets here)
# =============================================================================

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


MANAGER_BOT_TOKEN: str = _env("MANAGER_BOT_TOKEN", "8866918707:AAGIlR5QfSOQF1UieKmEht2iW0b1nGzwyd4")
DATABASE_URL: str = _env("DATABASE_URL", "sqlite+aiosqlite:///manager.db")
DEFAULT_DELAY_SECONDS: int = int(_env("DEFAULT_DELAY_SECONDS", "600"))
MAX_RETRIES: int = int(_env("MAX_RETRIES", "3"))
RETRY_DELAY_SECONDS: int = int(_env("RETRY_DELAY_SECONDS", "30"))
WORK_START: str = _env("WORK_START", "00:00")
WORK_END: str = _env("WORK_END", "23:59")
LOG_LEVEL: str = _env("LOG_LEVEL", "INFO").upper()

# Rate limiting (avoid Telegram flood / 429). Tunable via env.
API_MIN_INTERVAL: float = float(_env("API_MIN_INTERVAL", "1.1"))   # per bot, seconds
API_MAX_CONCURRENT: int = int(_env("API_MAX_CONCURRENT", "6"))     # global cap
SCHEDULER_TICK: float = float(_env("SCHEDULER_TICK", "1.0"))

ADMIN_USER_IDS: set[int] = {
    int(x) for x in _env("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x
}


class TaskStatus:
    PENDING = "PENDING"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class BotStatus:
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class PostStatus:
    NEW = "NEW"
    QUEUED = "QUEUED"
    DONE = "DONE"


REACTION_NOT_AVAILABLE = "REACTION_NOT_AVAILABLE"

# =============================================================================
# 2. LOGGING
# =============================================================================

logger = logging.getLogger("botmanager")


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def utcnow() -> datetime:
    """Naive UTC now.

    Stored as naive UTC so that values written to and read back from SQLite are
    always timezone-consistent (avoids aware/naive comparison bugs), while still
    being the correct UTC instant for PostgreSQL migrations later.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# =============================================================================
# 3. DATABASE MODELS  (SQLAlchemy 2.x ; SQLite now, PostgreSQL-ready)
# =============================================================================

class Base(DeclarativeBase):
    pass


class BotModel(Base):
    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_bot_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    username: Mapped[str] = mapped_column(String(255), default="")
    token: Mapped[str] = mapped_column(Text)
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), default=BotStatus.ACTIVE)

    last_activity: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    success_tasks: Mapped[int] = mapped_column(Integer, default=0)
    failed_tasks: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChannelModel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    channel_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    channel_name: Mapped[str] = mapped_column(String(255), default="")
    channel_type: Mapped[str] = mapped_column(String(16), default="PRIVATE")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    selected_reaction: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    fallback_reaction: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=DEFAULT_DELAY_SECONDS)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChannelBotModel(Base):
    """Which worker bots have access to which channel (discovered, not typed)."""
    __tablename__ = "channel_bots"
    __table_args__ = (UniqueConstraint("channel_id", "bot_id", name="uq_channel_bot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"))
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    permission: Mapped[str] = mapped_column(String(32), default="unknown")  # admin/member/none


class PostModel(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("channel_id", "telegram_message_id", name="uq_post_channel_message"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default=PostStatus.NEW)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskModel(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("channel_id", "message_id", "bot_id", "action", name="uq_task_dedup"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)

    action: Mapped[str] = mapped_column(String(64), default="reaction")
    reaction: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.PENDING, index=True)

    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True, nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[str] = mapped_column(String(16), default="NORMAL")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReactionModel(Base):
    __tablename__ = "reactions"
    __table_args__ = (UniqueConstraint("chat_id", "emoji", name="uq_reaction_chat_emoji"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    emoji: Mapped[str] = mapped_column(String(32))
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SettingModel(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class LogModel(Base):
    __tablename__ = "logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    event: Mapped[str] = mapped_column(String(64), default="")
    bot_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    post_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ---- engine / session -------------------------------------------------------
engine = create_async_engine(DATABASE_URL, echo=False, future=True)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
    """Enable WAL + busy_timeout so concurrent async writes don't lock out."""
    if DATABASE_URL.startswith("sqlite"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionFactory() as session:
        defaults = {
            "default_delay_seconds": str(DEFAULT_DELAY_SECONDS),
            "max_retries": str(MAX_RETRIES),
            "retry_delay_seconds": str(RETRY_DELAY_SECONDS),
            "work_start": WORK_START,
            "work_end": WORK_END,
            "queue_paused": "false",
        }
        for k, v in defaults.items():
            if not await session.get(SettingModel, k):
                session.add(SettingModel(key=k, value=v))
        await session.commit()


async def db_log(session: AsyncSession, level: str, event: str, message: str,
                 bot_id: Optional[int] = None, post_id: Optional[int] = None,
                 duration_ms: Optional[int] = None) -> None:
    session.add(LogModel(level=level, event=event, message=message,
                         bot_id=bot_id, post_id=post_id, duration_ms=duration_ms))
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — logging must never crash the loop
        logger.error("db_log failed: %s", exc)
    logger.log(getattr(logging, level.upper(), logging.INFO), "[%s] %s", event, message)


async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(SettingModel, key)
    return row.value if row else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(SettingModel, key)
    if row:
        row.value = value
    else:
        session.add(SettingModel(key=key, value=value))
    await session.commit()


# =============================================================================
# 4. RATE LIMITER  (per-bot serialization + global concurrency cap)
# =============================================================================

class RateLimiter:
    """
    Prevents Telegram flood: each bot's API calls are serialized with a minimum
    interval, and a global semaphore caps total in-flight calls. Non-blocking
    (uses asyncio.sleep, never time.sleep).
    """

    def __init__(self, min_interval: float, max_concurrent: int) -> None:
        self.min_interval = min_interval
        self._global = asyncio.Semaphore(max_concurrent)
        self._bot_locks: dict[int, asyncio.Lock] = {}
        self._last_call: dict[int, float] = {}

    def _lock_for(self, bot_id: int) -> asyncio.Lock:
        lock = self._bot_locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._bot_locks[bot_id] = lock
        return lock

    @asynccontextmanager
    async def slot(self, bot_id: int) -> AsyncIterator[None]:
        async with self._global:
            async with self._lock_for(bot_id):
                last = self._last_call.get(bot_id, 0.0)
                wait = self.min_interval - (_time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    yield
                finally:
                    self._last_call[bot_id] = _time.monotonic()


rate_limiter = RateLimiter(API_MIN_INTERVAL, API_MAX_CONCURRENT)


# =============================================================================
# 5. BOT REGISTRY
# =============================================================================

class BotRegistry:
    def __init__(self) -> None:
        self.manager: Optional[Bot] = None
        self.manager_dp: Optional[Dispatcher] = None
        self.workers: dict[int, Bot] = {}
        self.poll_tasks: dict[int, asyncio.Task] = {}

    def make_bot(self, token: str) -> Bot:
        return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def start_worker(self, token: str, tg_bot_id: int) -> Bot:
        if tg_bot_id in self.workers:
            return self.workers[tg_bot_id]
        bot = self.make_bot(token)
        dp = build_worker_dispatcher()
        self.workers[tg_bot_id] = bot
        self.poll_tasks[tg_bot_id] = asyncio.create_task(
            _run_polling(dp, bot, name=f"worker:{tg_bot_id}")
        )
        logger.info("Started polling for worker bot %s", tg_bot_id)
        return bot

    async def stop_worker(self, tg_bot_id: int) -> None:
        task = self.poll_tasks.pop(tg_bot_id, None)
        if task:
            task.cancel()
        bot = self.workers.pop(tg_bot_id, None)
        if bot:
            try:
                await bot.session.close()
            except Exception:  # noqa: BLE001
                logger.debug("session close failed for %s", tg_bot_id)

    async def shutdown(self) -> None:
        for tg_id in list(self.workers.keys()):
            await self.stop_worker(tg_id)
        if self.manager:
            try:
                await self.manager.session.close()
            except Exception:  # noqa: BLE001
                pass


registry = BotRegistry()


async def _run_polling(dp: Dispatcher, bot: Bot, name: str) -> None:
    """One bot's long-polling. A crash here NEVER kills the whole app."""
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "channel_post",
                             "my_chat_member", "message_reaction"],
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Polling crashed for %s: %s", name, exc)


# =============================================================================
# 6. TELEGRAM HELPERS
# =============================================================================

async def validate_token(token: str) -> Optional[dict]:
    if ":" not in token or len(token) < 20:
        return None
    bot = registry.make_bot(token)
    try:
        me = await bot.get_me()
        return {"id": me.id, "username": me.username or "", "name": me.full_name or ""}
    except TelegramAPIError as exc:
        logger.warning("Token validation failed: %s", exc)
        return None
    finally:
        await bot.session.close()


async def bot_permission_in_chat(bot: Bot, channel_id: int) -> str:
    """Return 'admin' | 'member' | 'none' | 'error' for this bot in the chat."""
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=bot.id)
        if member.status in ("administrator", "creator"):
            return "admin"
        if member.status in ("member", "restricted", "left"):
            return "member" if member.status != "left" else "none"
        return "none"
    except TelegramForbiddenError:
        return "none"
    except TelegramAPIError as exc:
        logger.warning("permission check failed chat=%s: %s", channel_id, exc)
        return "error"


async def fetch_available_reactions(bot: Bot, channel_id: int,
                                    session: AsyncSession) -> Optional[list[str]]:
    """
    Read the reactions Telegram ALLOWS in this chat (dynamic, never hardcoded).
    None = Telegram reported no restricted set (default set applies).
    """
    try:
        chat = await bot.get_chat(chat_id=channel_id)
    except TelegramAPIError as exc:
        await db_log(session, "WARNING", "GET_CHAT_FAILED", f"chat={channel_id}: {exc}")
        return None

    avail = getattr(chat, "available_reactions", None) or []
    emojis: list[str] = [r.emoji for r in avail if getattr(r, "type", None) == "emoji"]

    for emoji in emojis:
        found = (await session.execute(
            select(ReactionModel).where(
                ReactionModel.chat_id == channel_id, ReactionModel.emoji == emoji)
        )).scalar_one_or_none()
        if found:
            found.available = True
            found.checked_at = utcnow()
        else:
            session.add(ReactionModel(chat_id=channel_id, emoji=emoji, available=True))
    await session.commit()
    return emojis


def pick_reaction(channel: ChannelModel, available: Optional[list[str]]) -> Optional[str]:
    """Never hardcoded: configured emoji if allowed, else fallback, else None."""
    desired, fallback = channel.selected_reaction, channel.fallback_reaction
    if available is not None:
        if desired and desired in available:
            return desired
        if fallback and fallback in available:
            return fallback
        return None
    # Telegram reported no restricted set → only require a non-empty configured emoji
    return desired or fallback or None


# =============================================================================
# 7. CORE SERVICE
# =============================================================================

class Service:
    # ---- channel discovery (passive; no manual "add channel") -------------
    async def discover_channel(self, reporter: Bot, chat_id: int) -> None:
        """Called on my_chat_member / channel_post. Upserts channel + permissions."""
        try:
            chat = await reporter.get_chat(chat_id)
        except TelegramAPIError as exc:
            logger.warning("discover get_chat failed %s: %s", chat_id, exc)
            return
        if chat.type not in ("channel",):
            return  # only channels
        ctype = "PUBLIC" if chat.username else "PRIVATE"

        async with SessionFactory() as session:
            ch = (await session.execute(
                select(ChannelModel).where(ChannelModel.channel_id == chat.id)
            )).scalar_one_or_none()
            if ch:
                ch.channel_username = chat.username
                ch.channel_name = chat.title or ch.channel_name
                ch.channel_type = ctype
                ch.updated_at = utcnow()
            else:
                ch = ChannelModel(
                    channel_id=chat.id, channel_username=chat.username,
                    channel_name=chat.title or "", channel_type=ctype,
                    delay_seconds=int(await get_setting(session, "default_delay_seconds",
                                                        str(DEFAULT_DELAY_SECONDS))))
                session.add(ch)
            await session.commit()
            await session.refresh(ch)
            await db_log(session, "INFO", "CHANNEL_DISCOVERED",
                         f"{chat.title} ({chat.id}) type={ctype}")

        await self.refresh_channel_permissions(chat.id)

    async def refresh_channel_permissions(self, channel_id: int) -> None:
        """Check EVERY enabled worker bot's access to a channel and store it."""
        async with SessionFactory() as session:
            ch = (await session.execute(
                select(ChannelModel).where(ChannelModel.channel_id == channel_id)
            )).scalar_one_or_none()
            if not ch:
                return
            bots = (await session.execute(
                select(BotModel).where(
                    BotModel.enabled.is_(True), BotModel.is_manager.is_(False))
            )).scalars().all()
            for b in bots:
                bot = registry.workers.get(b.tg_bot_id)
                if bot is None:
                    continue
                perm = await bot_permission_in_chat(bot, channel_id)
                link = (await session.execute(
                    select(ChannelBotModel).where(
                        ChannelBotModel.channel_id == ch.id,
                        ChannelBotModel.bot_id == b.id)
                )).scalar_one_or_none()
                if link:
                    link.permission = perm
                    link.enabled = perm in ("admin", "member")
                else:
                    session.add(ChannelBotModel(
                        channel_id=ch.id, bot_id=b.id,
                        permission=perm, enabled=perm in ("admin", "member")))
            await session.commit()

    # ---- post discovery ---------------------------------------------------
    async def register_post(self, channel_tg_id: int, message_id: int) -> None:
        async with SessionFactory() as session:
            channel = (await session.execute(
                select(ChannelModel).where(ChannelModel.channel_id == channel_tg_id)
            )).scalar_one_or_none()
            if not channel or not channel.enabled:
                return
            exists = (await session.execute(
                select(PostModel).where(
                    PostModel.channel_id == channel.id,
                    PostModel.telegram_message_id == message_id)
            )).scalar_one_or_none()
            if exists:
                return
            post = PostModel(channel_id=channel.id, telegram_message_id=message_id,
                             status=PostStatus.QUEUED)
            session.add(post)
            await session.commit()
            await session.refresh(post)
            await db_log(session, "INFO", "POST_DISCOVERED",
                         f"channel={channel.channel_id} message={message_id}", post_id=post.id)
            await self._enqueue_tasks(session, channel, post)

    async def _enqueue_tasks(self, session: AsyncSession, channel: ChannelModel,
                             post: PostModel) -> None:
        bots = (await session.execute(
            select(BotModel)
            .join(ChannelBotModel, ChannelBotModel.bot_id == BotModel.id)
            .where(
                ChannelBotModel.channel_id == channel.id,
                ChannelBotModel.enabled.is_(True),
                BotModel.enabled.is_(True),
                BotModel.is_manager.is_(False),
            )
            .order_by(BotModel.id)
        )).scalars().all()

        if not bots:
            await db_log(session, "WARNING", "NO_WORKERS",
                         f"channel={channel.channel_id} has no bound workers", post_id=post.id)
            return

        base = utcnow()
        delay = max(1, channel.delay_seconds)
        for index, bot in enumerate(bots):
            exists = (await session.execute(
                select(TaskModel).where(
                    TaskModel.channel_id == channel.channel_id,
                    TaskModel.message_id == post.telegram_message_id,
                    TaskModel.bot_id == bot.id,
                    TaskModel.action == "reaction")
            )).scalar_one_or_none()
            if exists:
                continue
            session.add(TaskModel(
                post_id=post.id, channel_id=channel.channel_id,
                message_id=post.telegram_message_id, bot_id=bot.id,
                action="reaction", reaction=channel.selected_reaction,
                status=TaskStatus.SCHEDULED,
                scheduled_at=base + timedelta(seconds=index * delay)))
        await session.commit()
        await db_log(session, "INFO", "TASKS_ENQUEUED",
                     f"post={post.id} workers={len(bots)} delay={delay}s", post_id=post.id)

    # ---- scheduler --------------------------------------------------------
    @staticmethod
    def _in_working_hours(ws: str, we: str) -> bool:
        try:
            now = utcnow().time()
            sh, sm = (int(x) for x in ws.split(":"))
            eh, em = (int(x) for x in we.split(":"))
            start, end = time(sh, sm), time(eh, em)
        except Exception:  # noqa: BLE001
            return True
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end

    async def scheduler_loop(self) -> None:
        logger.info("Scheduler started (tick=%ss)", SCHEDULER_TICK)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("Scheduler tick error: %s", exc)
            await asyncio.sleep(SCHEDULER_TICK)

    async def _tick(self) -> None:
        async with SessionFactory() as session:
            if (await get_setting(session, "queue_paused", "false")) == "true":
                return
            ws = await get_setting(session, "work_start", WORK_START)
            we = await get_setting(session, "work_end", WORK_END)
            if not self._in_working_hours(ws, we):
                return
            now = utcnow()
            task = (await session.execute(
                select(TaskModel).where(
                    TaskModel.status.in_([TaskStatus.PENDING, TaskStatus.SCHEDULED]),
                    TaskModel.scheduled_at.is_not(None),
                    TaskModel.scheduled_at <= now)
                .order_by(TaskModel.scheduled_at).limit(1)
            )).scalar_one_or_none()
            if not task:
                return
            task.status = TaskStatus.RUNNING
            await session.commit()
            task_id = task.id
        await self.execute_task(task_id)

    # ---- execution --------------------------------------------------------
    async def execute_task(self, task_id: int) -> None:
        async with SessionFactory() as session:
            task = await session.get(TaskModel, task_id)
            if not task:
                return
            bot_row = await session.get(BotModel, task.bot_id)
            channel = (await session.execute(
                select(ChannelModel).where(ChannelModel.channel_id == task.channel_id)
            )).scalar_one_or_none()
            if not bot_row or not channel:
                task.status = TaskStatus.FAILED
                task.error_message = "bot or channel missing"
                await session.commit()
                return
            max_retries = int(await get_setting(session, "max_retries", str(MAX_RETRIES)))
            retry_delay = int(await get_setting(session, "retry_delay_seconds",
                                                str(RETRY_DELAY_SECONDS)))
            bot = registry.workers.get(bot_row.tg_bot_id)
            if bot is None:
                bot = registry.make_bot(bot_row.token)
            started = utcnow()
            try:
                perm = await bot_permission_in_chat(bot, channel.channel_id)
                if perm not in ("admin", "member"):
                    raise RuntimeError(f"no access to channel (permission={perm})")

                available = await fetch_available_reactions(bot, channel.channel_id, session)
                emoji = pick_reaction(channel, available)
                if emoji is None:
                    task.status = TaskStatus.SKIPPED
                    task.error_message = REACTION_NOT_AVAILABLE
                    task.executed_at = utcnow()
                    await session.commit()
                    await db_log(session, "WARNING", REACTION_NOT_AVAILABLE,
                                 f"post={task.post_id} bot={bot_row.username}: "
                                 f"configured reaction not allowed", bot_id=bot_row.id,
                                 post_id=task.post_id)
                    return

                async with rate_limiter.slot(bot_row.tg_bot_id):
                    await bot.set_message_reaction(
                        chat_id=channel.channel_id, message_id=task.message_id,
                        reaction=[ReactionTypeEmoji(emoji=emoji)], is_big=False)

                task.status = TaskStatus.SUCCESS
                task.reaction = emoji
                task.executed_at = utcnow()
                task.error_message = None
                bot_row.success_tasks += 1
                bot_row.total_tasks += 1
                bot_row.last_activity = utcnow()
                bot_row.status = BotStatus.ACTIVE
                duration = int((utcnow() - started).total_seconds() * 1000)
                await session.commit()
                await db_log(session, "INFO", "REACTION_SUCCESS",
                             f"channel={channel.channel_id} post={task.message_id} "
                             f"bot={bot_row.username} reaction={emoji}",
                             bot_id=bot_row.id, post_id=task.post_id, duration_ms=duration)
                await self._maybe_finish_post(session, task.post_id)

            except TelegramRetryAfter as exc:
                # honour Telegram's explicit retry_after
                task.attempts += 1
                task.error_message = f"flood: retry_after={exc.retry_after}"
                if task.attempts <= max_retries:
                    task.status = TaskStatus.SCHEDULED
                    task.scheduled_at = utcnow() + timedelta(seconds=int(exc.retry_after) + 1)
                    await db_log(session, "WARNING", "TASK_FLOOD_WAIT",
                                 f"task={task.id} retry_after={exc.retry_after}",
                                 bot_id=bot_row.id, post_id=task.post_id)
                else:
                    await self._finalize_failure(session, task, bot_row)
                await session.commit()
            except (TelegramBadRequest, TelegramForbiddenError,
                    TelegramAPIError, RuntimeError) as exc:
                await self._handle_failure(session, task, bot_row, str(exc),
                                           max_retries, retry_delay)
            except Exception as exc:  # noqa: BLE001
                await self._handle_failure(session, task, bot_row, repr(exc),
                                           max_retries, retry_delay)
            finally:
                if registry.workers.get(bot_row.tg_bot_id) is None:
                    await bot.session.close()

    async def _handle_failure(self, session: AsyncSession, task: TaskModel,
                              bot_row: BotModel, error: str,
                              max_retries: int, retry_delay: int) -> None:
        task.attempts += 1
        task.error_message = error[:2000]
        if task.attempts <= max_retries:
            # exponential backoff: retry_delay * 2^(attempts-1)
            backoff = retry_delay * (2 ** (task.attempts - 1))
            task.status = TaskStatus.SCHEDULED
            task.scheduled_at = utcnow() + timedelta(seconds=backoff)
            await db_log(session, "WARNING", "TASK_RETRY",
                         f"task={task.id} attempt={task.attempts} backoff={backoff}s: {error}",
                         bot_id=bot_row.id, post_id=task.post_id)
        else:
            await self._finalize_failure(session, task, bot_row)
        await session.commit()

    async def _finalize_failure(self, session: AsyncSession, task: TaskModel,
                                bot_row: BotModel) -> None:
        task.status = TaskStatus.FAILED
        task.executed_at = utcnow()
        bot_row.failed_tasks += 1
        bot_row.total_tasks += 1
        bot_row.status = BotStatus.ERROR
        await db_log(session, "ERROR", "TASK_FAILED",
                     f"task={task.id} bot={bot_row.username}: {task.error_message}",
                     bot_id=bot_row.id, post_id=task.post_id)
        await self._maybe_finish_post(session, task.post_id)

    async def _maybe_finish_post(self, session: AsyncSession, post_id: int) -> None:
        pending = (await session.execute(
            select(func.count(TaskModel.id)).where(
                TaskModel.post_id == post_id,
                TaskModel.status.in_([TaskStatus.PENDING, TaskStatus.SCHEDULED,
                                      TaskStatus.RUNNING]))
        )).scalar_one()
        if pending == 0:
            post = await session.get(PostModel, post_id)
            if post:
                post.status = PostStatus.DONE
                await session.commit()

    # ---- lifecycle --------------------------------------------------------
    async def load_workers_from_db(self) -> None:
        async with SessionFactory() as session:
            bots = (await session.execute(
                select(BotModel).where(
                    BotModel.enabled.is_(True), BotModel.is_manager.is_(False))
            )).scalars().all()
        for b in bots:
            try:
                await registry.start_worker(b.token, b.tg_bot_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Could not start worker %s: %s", b.tg_bot_id, exc)

    async def rescan_all_channels(self) -> int:
        async with SessionFactory() as session:
            chans = (await session.execute(select(ChannelModel))).scalars().all()
        for c in chans:
            await self.refresh_channel_permissions(c.channel_id)
        return len(chans)

    async def stats(self) -> dict:
        async with SessionFactory() as session:
            today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            total_bots = (await session.execute(
                select(func.count(BotModel.id)).where(BotModel.is_manager.is_(False)))).scalar_one()
            active = (await session.execute(
                select(func.count(BotModel.id)).where(
                    BotModel.is_manager.is_(False), BotModel.enabled.is_(True)))).scalar_one()
            channels = (await session.execute(select(func.count(ChannelModel.id)))).scalar_one()
            pending = (await session.execute(
                select(func.count(TaskModel.id)).where(
                    TaskModel.status.in_([TaskStatus.PENDING, TaskStatus.SCHEDULED])))).scalar_one()
            running = (await session.execute(
                select(func.count(TaskModel.id)).where(TaskModel.status == TaskStatus.RUNNING))).scalar_one()
            success_today = (await session.execute(
                select(func.count(TaskModel.id)).where(
                    TaskModel.status == TaskStatus.SUCCESS, TaskModel.executed_at >= today))).scalar_one()
            failed_today = (await session.execute(
                select(func.count(TaskModel.id)).where(
                    TaskModel.status == TaskStatus.FAILED, TaskModel.executed_at >= today))).scalar_one()
            nxt = (await session.execute(
                select(TaskModel).where(
                    TaskModel.status.in_([TaskStatus.PENDING, TaskStatus.SCHEDULED]),
                    TaskModel.scheduled_at.is_not(None))
                .order_by(TaskModel.scheduled_at).limit(1))).scalar_one_or_none()
            return {"total_bots": total_bots, "active": active, "disabled": total_bots - active,
                    "channels": channels, "pending": pending, "running": running,
                    "success_today": success_today, "failed_today": failed_today,
                    "next_task_at": nxt.scheduled_at if nxt else None}


service = Service()

# =============================================================================
# 8. AUTHORIZATION
# =============================================================================

def is_admin(user_id: Optional[int]) -> bool:
    if not ADMIN_USER_IDS:
        return True  # dev mode only — set ADMIN_USER_IDS in production
    return user_id in ADMIN_USER_IDS


def admin_only(func):
    async def wrapper(message: Message, *args, **kwargs):
        if not is_admin(message.from_user.id if message.from_user else None):
            await message.answer("⛔ غير مصرّح.")
            return
        return await func(message, *args, **kwargs)
    return wrapper


# =============================================================================
# 9. FSM + KEYBOARDS
# =============================================================================

class AdminFSM(StatesGroup):
    add_bot_token = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 البوتات", callback_data="menu:bots"),
         InlineKeyboardButton(text="📢 القنوات", callback_data="menu:channels")],
        [InlineKeyboardButton(text="🧵 Queue", callback_data="menu:queue"),
         InlineKeyboardButton(text="📊 الإحصائيات", callback_data="menu:stats")],
        [InlineKeyboardButton(text="⚙️ الإعدادات", callback_data="menu:settings"),
         InlineKeyboardButton(text="📜 السجلات", callback_data="menu:logs")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ رجوع", callback_data="menu:home")]])


# =============================================================================
# 10. MANAGER DISPATCHER + HANDLERS
# =============================================================================

def build_manager_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    @admin_only
    async def cmd_start(message: Message) -> None:
        await message.answer(
            "<b>Main Manager Bot</b>\n"
            "إدارة وجدولة تفاعلات بوتات Telegram (Bot API فقط).\n\n"
            "/addbot /removebot /bots /botinfo /enablebot /disablebot\n"
            "/channels /scan (اكتشاف القنوات) /channelinfo\n"
            "/setreaction /setfallback /setdelay\n"
            "/status /queue /settings /pause /resume /logs",
            reply_markup=main_menu())

    @dp.message(Command("help"))
    @admin_only
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "١) أضف البوتات بـ /addbot (Bot Token من BotFather).\n"
            "٢) أضِف البوت الرئيسي والبوتات الأخرى إلى القناة كـ Administrator من Telegram.\n"
            "٣) ستظهر القناة تلقائيًا؛ استخدم /scan لإعادة الفحص.\n"
            "٤) /setreaction ثم /setdelay.",
            reply_markup=main_menu())

    @dp.message(Command("status"))
    @admin_only
    async def cmd_status(message: Message) -> None:
        s = await service.stats()
        nxt = s["next_task_at"].strftime("%H:%M") if s["next_task_at"] else "—"
        await message.answer(
            "<b>📊 حالة النظام</b>\n"
            f"البوتات: {s['total_bots']} (نشط {s['active']} / معطّل {s['disabled']})\n"
            f"القنوات: {s['channels']}\n"
            f"مهام معلّقة: {s['pending']} | قيد التنفيذ: {s['running']}\n"
            f"نجحت اليوم: {s['success_today']} | فشلت اليوم: {s['failed_today']}\n"
            f"المهمة القادمة: {nxt}",
            reply_markup=main_menu())

    # -- bots ----------------------------------------------------------------
    @dp.message(Command("addbot"))
    @admin_only
    async def cmd_addbot(message: Message, state: FSMContext) -> None:
        await state.set_state(AdminFSM.add_bot_token)
        await message.answer("أرسل Bot Token للبوت. /cancel للإلغاء.")

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("تم الإلغاء.", reply_markup=main_menu())

    @dp.message(AdminFSM.add_bot_token, F.text)
    async def on_addbot_token(message: Message, state: FSMContext) -> None:
        if not is_admin(message.from_user.id if message.from_user else None):
            return
        token = (message.text or "").strip()
        info = await validate_token(token)
        if not info:
            await message.answer("❌ Token غير صالح. أعد المحاولة أو /cancel.")
            return
        async with SessionFactory() as session:
            exists = (await session.execute(
                select(BotModel).where(BotModel.tg_bot_id == info["id"]))).scalar_one_or_none()
            if exists:
                exists.token = token
                exists.enabled = True
                exists.status = BotStatus.ACTIVE
                await session.commit()
                await message.answer(f"ℹ️ البوت @{info['username']} محدّث ومفعّل.")
            else:
                session.add(BotModel(
                    tg_bot_id=info["id"], name=info["name"], username=info["username"],
                    token=token, enabled=True, status=BotStatus.ACTIVE, is_manager=False))
                await session.commit()
                await db_log(session, "INFO", "BOT_ADDED", f"@{info['username']} id={info['id']}")
        try:
            await registry.start_worker(token, info["id"])
        except Exception as exc:  # noqa: BLE001
            logger.error("start_worker failed: %s", exc)
        await state.clear()
        await message.answer(
            f"✅ تمت الإضافة\nName: {info['name']}\nUsername: @{info['username']}\n"
            f"Bot ID: {info['id']}\nStatus: Active", reply_markup=main_menu())

    @dp.message(Command("bots"))
    @admin_only
    async def cmd_bots(message: Message) -> None:
        async with SessionFactory() as session:
            rows = (await session.execute(
                select(BotModel).where(BotModel.is_manager.is_(False)).order_by(BotModel.id)
            )).scalars().all()
        if not rows:
            await message.answer("لا توجد بوتات. استخدم /addbot.")
            return
        lines = ["<b>🤖 البوتات</b>"]
        for b in rows:
            lines.append(f"#{b.id} @{b.username} — {b.status} — مهام: {b.total_tasks}")
        await message.answer("\n".join(lines), reply_markup=main_menu())

    @dp.message(Command("botinfo"))
    @admin_only
    async def cmd_botinfo(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("الاستخدام: /botinfo <bot_id>")
            return
        async with SessionFactory() as session:
            b = await session.get(BotModel, int(parts[1]))
            if not b:
                await message.answer("غير موجود.")
                return
            chans = (await session.execute(
                select(ChannelModel)
                .join(ChannelBotModel, ChannelBotModel.channel_id == ChannelModel.id)
                .where(ChannelBotModel.bot_id == b.id))).scalars().all()
            ch = ", ".join(c.channel_name for c in chans) if chans else "—"
        await message.answer(
            f"<b>Bot #{b.id}</b>\nName: {b.name}\nUsername: @{b.username}\n"
            f"Bot ID: {b.tg_bot_id}\nStatus: {b.status}\nChannel: {ch}\n"
            f"Last Activity: {b.last_activity or '—'}\n"
            f"Total: {b.total_tasks} | Success: {b.success_tasks} | Failed: {b.failed_tasks}",
            reply_markup=main_menu())

    @dp.message(Command("enablebot"))
    @admin_only
    async def cmd_enablebot(message: Message) -> None:
        await _toggle_bot(message, True)

    @dp.message(Command("disablebot"))
    @admin_only
    async def cmd_disablebot(message: Message) -> None:
        await _toggle_bot(message, False)

    async def _toggle_bot(message: Message, enabled: bool) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("الاستخدام: /enablebot|/disablebot <bot_id>")
            return
        async with SessionFactory() as session:
            b = await session.get(BotModel, int(parts[1]))
            if not b:
                await message.answer("غير موجود.")
                return
            b.enabled = enabled
            b.status = BotStatus.ACTIVE if enabled else BotStatus.DISABLED
            await session.commit()
            token, tg = b.token, b.tg_bot_id
        if enabled:
            await registry.start_worker(token, tg)
        else:
            await registry.stop_worker(tg)
        await message.answer("✅ تم.", reply_markup=main_menu())

    @dp.message(Command("removebot"))
    @admin_only
    async def cmd_removebot(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("الاستخدام: /removebot <bot_id>")
            return
        async with SessionFactory() as session:
            b = await session.get(BotModel, int(parts[1]))
            if not b:
                await message.answer("غير موجود.")
                return
            tg = b.tg_bot_id
            await session.delete(b)
            await session.commit()
        await registry.stop_worker(tg)
        await message.answer("🗑️ تم الحذف.", reply_markup=main_menu())

    # -- channels (discovered, not typed) ------------------------------------
    @dp.message(Command("channels"))
    @admin_only
    async def cmd_channels(message: Message) -> None:
        async with SessionFactory() as session:
            rows = (await session.execute(
                select(ChannelModel).order_by(ChannelModel.id))).scalars().all()
        if not rows:
            await message.answer(
                "لا قنوات مكتشفة بعد.\n"
                "أضف البوت الرئيسي والبوتات إلى القناة كـ Administrator من Telegram "
                "ثم انشر منشورًا أو استخدم /scan.")
            return
        lines = ["<b>📢 القنوات المكتشفة</b>"]
        for c in rows:
            lines.append(f"#{c.id} {c.channel_name} ({c.channel_id}) — {c.channel_type} — "
                         f"reaction={c.selected_reaction or '—'} — delay={c.delay_seconds}s")
        await message.answer("\n".join(lines), reply_markup=main_menu())

    @dp.message(Command("scan"))
    @admin_only
    async def cmd_scan(message: Message) -> None:
        n = await service.rescan_all_channels()
        await message.answer(f"🔍 أُعيد فحص {n} قناة وتحديث صلاحيات البوتات.",
                             reply_markup=main_menu())

    @dp.message(Command("channelinfo"))
    @admin_only
    async def cmd_channelinfo(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("الاستخدام: /channelinfo <channel_id>")
            return
        async with SessionFactory() as session:
            c = await session.get(ChannelModel, int(parts[1]))
            if not c:
                await message.answer("غير موجود.")
                return
            links = (await session.execute(
                select(ChannelBotModel).where(ChannelBotModel.channel_id == c.id))).scalars().all()
            botmap = {}
            for l in links:
                b = await session.get(BotModel, l.bot_id)
                botmap[b.username] = l.permission
        txt = "\n".join(f"@{u}: {p}" for u, p in botmap.items()) or "—"
        await message.answer(
            f"<b>Channel #{c.id}</b>\nName: {c.channel_name}\nID: {c.channel_id}\n"
            f"Type: {c.channel_type}\nReaction: {c.selected_reaction or '—'}\n"
            f"Delay: {c.delay_seconds}s\nBots:\n{txt}", reply_markup=main_menu())

    @dp.message(Command("removechannel"))
    @admin_only
    async def cmd_removechannel(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.answer("الاستخدام: /removechannel <channel_id>")
            return
        async with SessionFactory() as session:
            c = await session.get(ChannelModel, int(parts[1]))
            if not c:
                await message.answer("غير موجود.")
                return
            await session.delete(c)
            await session.commit()
        await message.answer("🗑️ تم حذف القناة.", reply_markup=main_menu())

    @dp.message(Command("bindbot"))
    @admin_only
    async def cmd_bindbot(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await message.answer("الاستخدام: /bindbot <channel_id> <bot_id>")
            return
        async with SessionFactory() as session:
            c = await session.get(ChannelModel, int(parts[1]))
            b = await session.get(BotModel, int(parts[2]))
            if not c or not b:
                await message.answer("قناة أو بوت غير موجود.")
                return
            link = (await session.execute(
                select(ChannelBotModel).where(
                    ChannelBotModel.channel_id == c.id,
                    ChannelBotModel.bot_id == b.id))).scalar_one_or_none()
            if link:
                link.enabled = True
            else:
                session.add(ChannelBotModel(channel_id=c.id, bot_id=b.id, enabled=True))
            await session.commit()
        await message.answer("🔗 تم الربط.", reply_markup=main_menu())

    @dp.message(Command("unbindbot"))
    @admin_only
    async def cmd_unbindbot(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await message.answer("الاستخدام: /unbindbot <channel_id> <bot_id>")
            return
        async with SessionFactory() as session:
            link = (await session.execute(
                select(ChannelBotModel).where(
                    ChannelBotModel.channel_id == int(parts[1]),
                    ChannelBotModel.bot_id == int(parts[2])))).scalar_one_or_none()
            if link:
                link.enabled = False
                await session.commit()
        await message.answer("فُصل الربط.", reply_markup=main_menu())

    # -- settings ------------------------------------------------------------
    @dp.message(Command("setreaction"))
    @admin_only
    async def cmd_setreaction(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 3 or not parts[1].isdigit():
            await message.answer("الاستخدام: /setreaction <channel_id> <emoji>")
            return
        ch_id, emoji = int(parts[1]), parts[2]
        async with SessionFactory() as session:
            c = await session.get(ChannelModel, ch_id)
            if not c:
                await message.answer("غير موجود.")
                return
            available = await fetch_available_reactions(registry.manager, c.channel_id, session)
            if available is not None and emoji not in available:
                await message.answer(
                    f"⚠️ {emoji} غير متاح في القناة.\nالمتاح: {' '.join(available) or '—'}")
                return
            c.selected_reaction = emoji
            await session.commit()
        await message.answer(f"✅ Reaction = {emoji}", reply_markup=main_menu())

    @dp.message(Command("setfallback"))
    @admin_only
    async def cmd_setfallback(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 3 or not parts[1].isdigit():
            await message.answer("الاستخدام: /setfallback <channel_id> <emoji>")
            return
        async with SessionFactory() as session:
            c = await session.get(ChannelModel, int(parts[1]))
            if not c:
                await message.answer("غير موجود.")
                return
            c.fallback_reaction = parts[2]
            await session.commit()
        await message.answer("✅ تم.", reply_markup=main_menu())

    @dp.message(Command("setdelay"))
    @admin_only
    async def cmd_setdelay(message: Message) -> None:
        parts = (message.text or "").split()
        if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await message.answer("الاستخدام: /setdelay <channel_id> <seconds> (60/300/600/900/1800)")
            return
        async with SessionFactory() as session:
            c = await session.get(ChannelModel, int(parts[1]))
            if not c:
                await message.answer("غير موجود.")
                return
            c.delay_seconds = int(parts[2])
            await session.commit()
        await message.answer("⏱️ تم.", reply_markup=main_menu())

    @dp.message(Command("settings"))
    @admin_only
    async def cmd_settings(message: Message) -> None:
        async with SessionFactory() as session:
            keys = ["default_delay_seconds", "max_retries", "retry_delay_seconds",
                    "work_start", "work_end", "queue_paused"]
            vals = {k: await get_setting(session, k) for k in keys}
        await message.answer(
            "<b>⚙️ الإعدادات</b>\n"
            + "\n".join(f"{k}: {v}" for k, v in vals.items())
            + "\n\nالتعديل: /setopt <key> <value>",
            reply_markup=main_menu())

    @dp.message(Command("setopt"))
    @admin_only
    async def cmd_setopt(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("الاستخدام: /setopt <key> <value>")
            return
        async with SessionFactory() as session:
            await set_setting(session, parts[1], parts[2])
        await message.answer("✅ تم.", reply_markup=main_menu())

    @dp.message(Command("pause"))
    @admin_only
    async def cmd_pause(message: Message) -> None:
        async with SessionFactory() as session:
            await set_setting(session, "queue_paused", "true")
        await message.answer("⏸️ تم إيقاف Queue.", reply_markup=main_menu())

    @dp.message(Command("resume"))
    @admin_only
    async def cmd_resume(message: Message) -> None:
        async with SessionFactory() as session:
            await set_setting(session, "queue_paused", "false")
        await message.answer("▶️ تم الاستئناف.", reply_markup=main_menu())

    @dp.message(Command("queue"))
    @admin_only
    async def cmd_queue(message: Message) -> None:
        async with SessionFactory() as session:
            rows = (await session.execute(
                select(TaskModel).order_by(TaskModel.scheduled_at).limit(20))).scalars().all()
        if not rows:
            await message.answer("Queue فارغ.", reply_markup=main_menu())
            return
        lines = ["<b>🧵 Queue</b>"]
        for t in rows:
            when = t.scheduled_at.strftime("%H:%M") if t.scheduled_at else "—"
            lines.append(f"Post #{t.message_id} • bot={t.bot_id} • {t.status} • {when}")
        await message.answer("\n".join(lines), reply_markup=main_menu())

    @dp.message(Command("logs"))
    @admin_only
    async def cmd_logs(message: Message) -> None:
        async with SessionFactory() as session:
            rows = (await session.execute(
                select(LogModel).order_by(LogModel.id.desc()).limit(20))).scalars().all()
        if not rows:
            await message.answer("لا سجلات.", reply_markup=main_menu())
            return
        lines = ["<b>📜 آخر السجلات</b>"]
        for r in rows:
            lines.append(f"[{r.created_at.strftime('%H:%M:%S')}] {r.level} {r.event}: {r.message[:80]}")
        await message.answer("\n".join(lines), reply_markup=main_menu())

    # -- callbacks -----------------------------------------------------------
    @dp.callback_query(F.data.startswith("menu:"))
    async def on_menu(cq: CallbackQuery, state: FSMContext) -> None:
        if not is_admin(cq.from_user.id if cq.from_user else None):
            await cq.answer("غير مصرّح", show_alert=True)
            return
        target = cq.data.split(":", 1)[1]
        await cq.answer()
        if target == "home":
            await cq.message.edit_text("القائمة الرئيسية:", reply_markup=main_menu())
        elif target == "bots":
            await cq.message.edit_text("استخدم /addbot /bots /botinfo", reply_markup=back_menu())
        elif target == "channels":
            await cq.message.edit_text("استخدم /channels /scan /channelinfo", reply_markup=back_menu())
        elif target == "queue":
            await cq.message.edit_text("استخدم /queue", reply_markup=back_menu())
        elif target == "stats":
            s = await service.stats()
            await cq.message.edit_text(
                f"Bots: {s['total_bots']} | Active: {s['active']} | Disabled: {s['disabled']}\n"
                f"Channels: {s['channels']}\nQueue: {s['pending']} | Running: {s['running']}\n"
                f"Success Today: {s['success_today']} | Failed Today: {s['failed_today']}",
                reply_markup=back_menu())
        elif target == "settings":
            await cq.message.edit_text("استخدم /settings /pause /resume", reply_markup=back_menu())
        elif target == "logs":
            await cq.message.edit_text("استخدم /logs", reply_markup=back_menu())

    # -- discovery handlers --------------------------------------------------
    @dp.my_chat_member()
    async def on_my_chat_member(update: ChatMemberUpdated) -> None:
        try:
            if update.chat.type == "channel":
                await service.discover_channel(registry.manager, update.chat.id)
        except Exception as exc:  # noqa: BLE001
            logger.error("my_chat_member handler error: %s", exc)

    @dp.channel_post()
    async def on_channel_post(message: Message) -> None:
        try:
            if message.chat and message.chat.type == "channel":
                await service.discover_channel(registry.manager, message.chat.id)
                await service.register_post(message.chat.id, message.message_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("channel_post handler error: %s", exc)

    return dp


# =============================================================================
# 11. WORKER DISPATCHER  (also discovers channels + posts; dedup in DB)
# =============================================================================

def build_worker_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    @dp.my_chat_member()
    async def on_worker_my_chat_member(update: ChatMemberUpdated) -> None:
        try:
            if update.chat.type == "channel" and registry.manager is not None:
                await service.discover_channel(registry.manager, update.chat.id)
        except Exception as exc:  # noqa: BLE001
            logger.error("worker my_chat_member error: %s", exc)

    @dp.channel_post()
    async def on_worker_channel_post(message: Message) -> None:
        try:
            if message.chat and message.chat.type == "channel":
                await service.register_post(message.chat.id, message.message_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("worker channel_post error: %s", exc)

    return dp


# =============================================================================
# 12. MAIN
# =============================================================================

async def main() -> None:
    setup_logging()

    if not MANAGER_BOT_TOKEN:
        logger.critical("MANAGER_BOT_TOKEN is not set. Fill .env then re-run.")
        return
    if not ADMIN_USER_IDS:
        logger.warning("ADMIN_USER_IDS empty — management is UNRESTRICTED (dev only).")

    await init_db()

    registry.manager = registry.make_bot(MANAGER_BOT_TOKEN)
    registry.manager_dp = build_manager_dispatcher()

    me = await registry.manager.get_me()
    logger.info("Manager bot started: @%s (id=%s)", me.username, me.id)

    async with SessionFactory() as session:
        row = (await session.execute(
            select(BotModel).where(BotModel.tg_bot_id == me.id))).scalar_one_or_none()
        if not row:
            session.add(BotModel(tg_bot_id=me.id, name=me.full_name or "Manager",
                                 username=me.username or "", token=MANAGER_BOT_TOKEN,
                                 is_manager=True, enabled=True, status=BotStatus.ACTIVE))
        else:
            row.token = MANAGER_BOT_TOKEN
            row.is_manager = True
        await session.commit()

    await service.load_workers_from_db()
    scheduler_task = asyncio.create_task(service.scheduler_loop())

    try:
        await registry.manager_dp.start_polling(
            registry.manager,
            allowed_updates=["message", "callback_query", "channel_post",
                             "my_chat_member", "message_reaction"])
    finally:
        scheduler_task.cancel()
        await registry.shutdown()
        await engine.dispose()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
