"""Telegram gateway for the existing Tieru runtime.

Telegram is transport only: every authorized turn is handled by ``Tieru.respond``
with the same memory, tools, MCP bridge, Trust policy, Replay, and observers used
by the other gateways. The allowlist is fail-closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from tieru.app import Tieru
from tieru.config import Settings, load_settings, parse_telegram_allowed_users
from tieru.db import connect
from tieru.gateway.cli import _observer

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LENGTH = 4000
TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TELEGRAM_TEXT_FILE_LIMIT = 200_000
UNAUTHORIZED_MESSAGE = "⛔ Bạn không có quyền sử dụng Tieru Agent này."
ERROR_MESSAGE = "⚠️ Tieru gặp lỗi khi xử lý yêu cầu.\n\nVui lòng thử lại."
IMAGE_UNSUPPORTED = "⚠️ Phiên bản Tieru hiện tại chưa hỗ trợ phân tích ảnh."
FILE_UNSUPPORTED = "⚠️ Tieru hiện tại chưa có tool phù hợp để đọc loại file này."

START_TEXT = """🤖 Tieru Agent

Xin chào! Tôi là Tieru.

Bạn có thể gửi tin nhắn trực tiếp để trò chuyện với agent.

Commands:
/new - Tạo cuộc hội thoại mới
/help - Xem hướng dẫn
/status - Kiểm tra trạng thái agent
/tools - Xem tools hiện có
/memory - Xem thông tin memory
/id - Xem Telegram User ID"""

HELP_TEXT = """🤖 Tieru Help

Bạn có thể:

• Chat trực tiếp với Tieru
• Yêu cầu Tieru sử dụng tools
• Gửi ảnh
• Gửi file
• Tạo conversation mới bằng /new

Commands:

/start
/new
/status
/tools
/memory
/id
/help"""

_TEXT_SUFFIXES = {
    ".txt", ".md", ".json", ".csv", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb",
    ".php", ".sh", ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".xml", ".html", ".css",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}


def _allowed_ids(extra_legacy: str = "") -> set[str]:
    """Return the merged legacy and multi-user Telegram allowlist."""
    legacy = os.getenv("TELEGRAM_ALLOWED_USER", "")
    if extra_legacy.strip():
        legacy = f"{legacy},{extra_legacy}"
    return parse_telegram_allowed_users(
        legacy,
        os.getenv("TELEGRAM_ALLOWED_USERS", ""),
    )


def sender_allowed(allowed_ids: set[str] | frozenset[str], sender_id: str | int) -> bool:
    """Fail-closed sender policy shared by polling and deterministic tests."""
    return bool(allowed_ids) and str(sender_id) in allowed_ids


def get_session_id(user_id: str | int) -> str:
    """Stable initial conversation ID for a Telegram user."""
    return f"telegram:{user_id}"


def new_session_id(user_id: str | int) -> str:
    """Create a fresh conversation tag without deleting durable memory."""
    return f"{get_session_id(user_id)}:{uuid.uuid4().hex}"


def split_message(text: str, max_length: int = TELEGRAM_MESSAGE_LENGTH) -> list[str]:
    """Split without losing text, preferring paragraph and line boundaries.

    If a fenced code block begins near the end of a chunk, it is moved intact to
    the next chunk when possible. A single code block longer than the limit must
    still be split; Markdown send fallback then protects the Telegram handler.
    """
    if max_length < 1:
        raise ValueError("max_length must be positive")
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_length:
        window = remaining[:max_length]
        floor = max_length // 3
        cut = -1
        for separator in ("\n\n", "\n", " "):
            found = window.rfind(separator, floor)
            if found >= floor:
                cut = found + len(separator)
                break
        if cut < 1:
            cut = max_length

        if window[:cut].count("```") % 2:
            opening = window.rfind("```", 0, cut)
            if opening >= floor:
                cut = opening
        if cut < 1:
            cut = max_length
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def posture(allowed_ids: set[str] | None = None) -> str:
    """Describe access posture without exposing identities or credentials."""
    ids = _allowed_ids() if allowed_ids is None else allowed_ids
    if ids:
        return f"  reachable by: {len(ids)} allowlisted user(s)"
    return (
        "  LOCKED: Telegram allowlist is empty; all inbound messages are denied.\n"
        "          Set TELEGRAM_ALLOWED_USER or TELEGRAM_ALLOWED_USERS first."
    )


@dataclass
class _AgentRecord:
    agent: Any
    lock: asyncio.Lock


class TelegramAgentError(RuntimeError):
    """A runtime/configuration failure that must not terminate Telegram polling."""


def _call_agent_safely(callable_: Callable, *args, **kwargs):
    """Convert CLI-style SystemExit failures at the gateway boundary."""
    try:
        return callable_(*args, **kwargs)
    except SystemExit as exc:
        raise TelegramAgentError(str(exc) or "Tieru initialization failed") from None


class TelegramAgentManager:
    """Own one mutable Tieru session per user and serialize that user's turns."""

    def __init__(
        self,
        settings: Settings | None = None,
        agent_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self._agent_factory = agent_factory or self._default_agent_factory
        self._records: dict[str, _AgentRecord] = {}
        self._creation_locks: dict[str, asyncio.Lock] = {}
        self._pending_sessions: dict[str, str] = {}
        self._closed = False

    def _default_agent_factory(self, session_id: str) -> Tieru:
        # respond() runs through asyncio.to_thread. This connection is explicitly
        # cross-thread and each instance is protected by its per-user lock.
        conn = connect(self.settings.home, check_same_thread=False)
        try:
            agent = Tieru(settings=self.settings, conn=conn)
            latest = conn.execute(
                """SELECT session_id FROM chat_log
                   WHERE source = 'telegram'
                     AND (session_id = ? OR session_id LIKE ?)
                   ORDER BY id DESC LIMIT 1""",
                (session_id, f"{session_id}:%"),
            ).fetchone()
            agent.session.switch(latest["session_id"] if latest is not None else session_id)
            return agent
        except BaseException:
            conn.close()
            raise

    async def get_agent(self, user_id: str | int) -> Any:
        key = str(user_id)
        if self._closed:
            raise RuntimeError("Telegram agent manager is closed")
        record = self._records.get(key)
        if record is not None:
            return record.agent
        creation_lock = self._creation_locks.setdefault(key, asyncio.Lock())
        async with creation_lock:
            record = self._records.get(key)
            if record is None:
                session_id = self._pending_sessions.get(key, get_session_id(key))
                agent = await asyncio.to_thread(
                    _call_agent_safely, self._agent_factory, session_id
                )
                record = _AgentRecord(agent=agent, lock=asyncio.Lock())
                self._records[key] = record
                self._pending_sessions.pop(key, None)
            return record.agent

    async def respond(self, user_id: str | int, message: str, observer=None, stream=True):
        key = str(user_id)
        await self.get_agent(key)
        record = self._records[key]
        async with record.lock:
            return await asyncio.to_thread(
                _call_agent_safely,
                record.agent.respond,
                message,
                observer=observer,
                source="telegram",
                stream=stream,
            )

    async def reset(self, user_id: str | int, session_id: str | None = None) -> str:
        key = str(user_id)
        fresh_id = session_id or new_session_id(key)
        creation_lock = self._creation_locks.setdefault(key, asyncio.Lock())
        async with creation_lock:
            record = self._records.get(key)
            if record is None:
                # /new is a session operation. It must work even when the model
                # is temporarily misconfigured or unavailable.
                self._pending_sessions[key] = fresh_id
                return fresh_id
            async with record.lock:
                record.agent.session.start_new(fresh_id)
        return fresh_id

    async def info(self, user_id: str | int) -> dict[str, Any]:
        key = str(user_id)
        await self.get_agent(key)
        record = self._records[key]
        async with record.lock:
            agent = record.agent
            try:
                provider = agent.model_router.provider("main")
            except Exception:
                provider = "configured"
            schemas = list(agent.tools.schemas()) if getattr(agent, "tools", None) else []
            return {
                "session_id": agent.session.session_id,
                "provider": provider or "configured",
                "long_term_memory": getattr(agent, "memory", None) is not None,
                "session_memory": getattr(agent, "session", None) is not None,
                "tools": schemas,
                "home": Path(agent.settings.home),
            }

    async def close(self) -> None:
        self._closed = True
        records = list(self._records.values())
        for record in records:
            async with record.lock:
                await asyncio.to_thread(self._close_record, record)
        self._records.clear()
        self._pending_sessions.clear()

    @staticmethod
    def _close_record(record: _AgentRecord) -> None:
        with contextlib.suppress(Exception):
            record.agent.close()
        conn = getattr(record.agent, "conn", None)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def _consume_future(future) -> None:
    with contextlib.suppress(BaseException):
        future.result()


class _TelegramObserver:
    """Bridge safe stream/status events from Tieru's worker thread to Telegram."""

    def __init__(self, loop: asyncio.AbstractEventLoop, status_message: Any) -> None:
        self.loop = loop
        self.status_message = status_message
        self.text = ""
        self.last_update = 0.0
        self.closed = False

    def __call__(self, kind: str, event: dict) -> None:
        _observer(kind, event)
        if self.closed or self.status_message is None:
            return
        preview = ""
        if kind == "text":
            self.text += str(event.get("delta", ""))
            now = time.monotonic()
            if now - self.last_update < 1.0:
                return
            self.last_update = now
            preview = self.text[-TELEGRAM_MESSAGE_LENGTH:] or "🤔 Tieru đang xử lý..."
        elif kind == "tool_started":
            preview = "🛠 Tieru đang chạy tool..."
        if preview:
            future = asyncio.run_coroutine_threadsafe(
                self.status_message.edit_text(preview), self.loop
            )
            future.add_done_callback(_consume_future)

    def close(self) -> None:
        self.closed = True


async def _typing_loop(bot: Any, chat_id: int | str) -> None:
    from telegram.constants import ChatAction

    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Telegram typing indicator stopped", exc_info=True)
            return


def _reply_context(message: Any) -> str:
    original = getattr(message, "reply_to_message", None)
    if original is None:
        return ""
    text = getattr(original, "text", None) or getattr(original, "caption", None)
    if not text and getattr(original, "document", None):
        text = f"[Document: {getattr(original.document, 'file_name', 'file')}]"
    if not text and getattr(original, "photo", None):
        text = "[Photo]"
    if not text:
        return ""
    return str(text)[:4000]


def _with_reply_context(message: Any, new_content: str) -> str:
    original = _reply_context(message)
    if not original:
        return new_content
    return f"User is replying to:\n\n{original}\n\nNew message:\n\n{new_content}"


async def _send_chunk(message: Any, text: str, markdown: bool) -> None:
    if not markdown:
        await message.reply_text(text)
        return
    from telegram.constants import ParseMode
    from telegram.error import BadRequest

    try:
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        logger.info("Telegram Markdown parse failed; falling back to plain text")
        await message.reply_text(text)


async def send_tieru_response(message: Any, text: str, *, markdown: bool = True) -> None:
    for chunk in split_message(text or "(no reply)"):
        await _send_chunk(message, chunk, markdown)


async def _require_allowed(update: Any, allowed_ids: set[str] | frozenset[str]) -> bool:
    user = getattr(update, "effective_user", None)
    message = getattr(update, "effective_message", None)
    if user is not None and sender_allowed(allowed_ids, user.id):
        return True
    if message is not None:
        await message.reply_text(UNAUTHORIZED_MESSAGE)
    return False


async def authorized_agent_response(
    manager: TelegramAgentManager,
    allowed_ids: set[str] | frozenset[str],
    user_id: str | int,
    message: str,
    observer=None,
):
    """Testable authorization boundary immediately before Tieru execution."""
    if not sender_allowed(allowed_ids, user_id):
        return None
    return await manager.respond(user_id, message, observer=observer, stream=True)


async def _run_agent_request(
    update: Any,
    context: Any,
    manager: TelegramAgentManager,
    allowed_ids: set[str] | frozenset[str],
    prompt: str,
) -> None:
    if not await _require_allowed(update, allowed_ids):
        return
    message = update.effective_message
    user_id = update.effective_user.id
    logger.info("telegram message user_id=%s message_length=%d", user_id, len(prompt))
    typing_task = asyncio.create_task(_typing_loop(context.bot, update.effective_chat.id))
    status_message = None
    observer = None
    started = time.perf_counter()
    try:
        status_message = await message.reply_text("🤔 Tieru đang xử lý...")
        observer = _TelegramObserver(asyncio.get_running_loop(), status_message)
        result = await authorized_agent_response(
            manager, allowed_ids, user_id, prompt, observer=observer
        )
        info = await manager.info(user_id)
        logger.info(
            "tieru response session=%s duration=%.3fs",
            info["session_id"],
            time.perf_counter() - started,
        )
        if observer is not None:
            observer.close()
        if status_message is not None:
            with contextlib.suppress(Exception):
                await status_message.delete()
        await send_tieru_response(message, result.reply if result is not None else "")
    except Exception:
        logger.exception("tieru telegram handler user_id=%s", user_id)
        if observer is not None:
            observer.close()
        if status_message is not None:
            with contextlib.suppress(Exception):
                await status_message.delete()
        await message.reply_text(ERROR_MESSAGE)
    finally:
        typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing_task


async def start_command(update: Any, context: Any, *, manager, allowed_ids) -> None:
    del context, manager
    if await _require_allowed(update, allowed_ids):
        await send_tieru_response(update.effective_message, START_TEXT, markdown=False)


async def help_command(update: Any, context: Any, *, manager, allowed_ids) -> None:
    del context, manager
    if await _require_allowed(update, allowed_ids):
        await send_tieru_response(update.effective_message, HELP_TEXT, markdown=False)


async def id_command(update: Any, context: Any, *, manager, allowed_ids) -> None:
    del context, manager
    if await _require_allowed(update, allowed_ids):
        await update.effective_message.reply_text(
            f"Your Telegram User ID: {update.effective_user.id}"
        )


async def new_command(update: Any, context: Any, *, manager, allowed_ids) -> None:
    del context
    if not await _require_allowed(update, allowed_ids):
        return
    try:
        session_id = await manager.reset(update.effective_user.id)
        logger.info("telegram new session=%s", session_id)
        await update.effective_message.reply_text("✅ Đã tạo cuộc hội thoại mới.")
    except Exception:
        logger.exception("telegram new-session handler user_id=%s", update.effective_user.id)
        await update.effective_message.reply_text(ERROR_MESSAGE)


async def status_command(update: Any, context: Any, *, manager, allowed_ids) -> None:
    del context
    if not await _require_allowed(update, allowed_ids):
        return
    try:
        info = await manager.info(update.effective_user.id)
        text = (
            "🟢 Tieru Agent Online\n\n"
            f"Session:\n{info['session_id']}\n\n"
            f"LLM:\n{info['provider']}\n\n"
            f"Memory:\n{'enabled' if info['long_term_memory'] else 'disabled'}\n\n"
            "Telegram:\nconnected"
        )
        await send_tieru_response(update.effective_message, text, markdown=False)
    except Exception:
        logger.exception("telegram status handler user_id=%s", update.effective_user.id)
        await update.effective_message.reply_text(ERROR_MESSAGE)


async def tools_command(update: Any, context: Any, *, manager, allowed_ids) -> None:
    del context
    if not await _require_allowed(update, allowed_ids):
        return
    try:
        info = await manager.info(update.effective_user.id)
        names = sorted({str(tool.get("name", "")).strip() for tool in info["tools"]} - {""})
        body = "\n".join(f"• {name}" for name in names) or "• (none)"
        await send_tieru_response(
            update.effective_message, f"🛠 Tieru Tools\n\n{body}", markdown=False
        )
    except Exception:
        logger.exception("telegram tools handler user_id=%s", update.effective_user.id)
        await update.effective_message.reply_text(ERROR_MESSAGE)


async def memory_command(update: Any, context: Any, *, manager, allowed_ids) -> None:
    del context
    if not await _require_allowed(update, allowed_ids):
        return
    try:
        info = await manager.info(update.effective_user.id)
        text = (
            "🧠 Tieru Memory\n\n"
            "Long-term memory: "
            f"{'enabled' if info['long_term_memory'] else 'disabled'}\n"
            "Session memory: "
            f"{'enabled' if info['session_memory'] else 'disabled'}\n"
            f"Session: {info['session_id']}"
        )
        await send_tieru_response(update.effective_message, text, markdown=False)
    except Exception:
        logger.exception("telegram memory handler user_id=%s", update.effective_user.id)
        await update.effective_message.reply_text(ERROR_MESSAGE)


async def handle_text(update: Any, context: Any, *, manager, allowed_ids) -> None:
    message = update.effective_message
    prompt = _with_reply_context(message, message.text or "")
    await _run_agent_request(update, context, manager, allowed_ids, prompt)


def _tool_supports(schemas: list[dict[str, Any]], terms: tuple[str, ...]) -> bool:
    for schema in schemas:
        haystack = f"{schema.get('name', '')} {schema.get('description', '')}".lower()
        if any(term in haystack for term in terms):
            return True
    return False


async def _download(
    context: Any, file_id: str, destination: Path, declared_size: int | None
) -> None:
    if declared_size is not None and declared_size > TELEGRAM_DOWNLOAD_LIMIT:
        raise ValueError("Telegram file exceeds the download limit")
    telegram_file = await context.bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=str(destination))
    if destination.stat().st_size > TELEGRAM_DOWNLOAD_LIMIT:
        raise ValueError("Telegram file exceeds the download limit")


def _safe_filename(filename: str | None, fallback: str) -> str:
    name = Path(filename or fallback).name.strip().replace("\x00", "")
    return (name or fallback)[:200]


def _read_text_file(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    decoded = raw.decode("utf-8-sig")
    truncated = len(decoded) > TELEGRAM_TEXT_FILE_LIMIT
    return decoded[:TELEGRAM_TEXT_FILE_LIMIT], truncated


def _file_prompt(
    filename: str,
    local_path: Path,
    caption: str,
    reply_context: str,
    content: str | None = None,
    truncated: bool = False,
) -> str:
    prompt = (
        "The user sent a Telegram file.\n\n"
        f"Filename:\n{filename}\n\n"
        f"Local path:\n{local_path}\n\n"
        f"User request:\n{caption or 'Please inspect this file.'}"
    )
    if content is not None:
        label = "File content (truncated by the gateway):" if truncated else "File content:"
        prompt += f"\n\n{label}\n{content}"
    if reply_context:
        prompt = f"User is replying to:\n\n{reply_context}\n\n{prompt}"
    return prompt


async def handle_photo(update: Any, context: Any, *, manager, allowed_ids) -> None:
    if not await _require_allowed(update, allowed_ids):
        return
    message = update.effective_message
    user_id = update.effective_user.id
    try:
        info = await manager.info(user_id)
        if not _tool_supports(info["tools"], ("vision", "image analysis", "read image")):
            await message.reply_text(IMAGE_UNSUPPORTED)
            return
        root = info["home"] / "tmp" / "telegram"
        root.mkdir(parents=True, exist_ok=True)
        photo = message.photo[-1]
        with tempfile.TemporaryDirectory(prefix=f"{user_id}-", dir=root) as directory:
            path = Path(directory) / "photo.jpg"
            await _download(context, photo.file_id, path, getattr(photo, "file_size", None))
            prompt = _file_prompt(
                "photo.jpg", path.resolve(), message.caption or "Analyze this image.",
                _reply_context(message),
            )
            await _run_agent_request(update, context, manager, allowed_ids, prompt)
    except ValueError:
        logger.warning("telegram photo rejected user_id=%s", user_id)
        await message.reply_text("⚠️ File quá lớn để xử lý an toàn.")
    except Exception:
        logger.exception("telegram photo handler user_id=%s", user_id)
        await message.reply_text(ERROR_MESSAGE)


async def handle_document(update: Any, context: Any, *, manager, allowed_ids) -> None:
    if not await _require_allowed(update, allowed_ids):
        return
    message = update.effective_message
    user_id = update.effective_user.id
    document = message.document
    filename = _safe_filename(document.file_name, "document")
    suffix = Path(filename).suffix.lower()
    mime_type = (document.mime_type or "").lower()
    is_text = suffix in _TEXT_SUFFIXES or mime_type.startswith("text/")
    try:
        info = await manager.info(user_id)
        supports_images = _tool_supports(
            info["tools"], ("vision", "image analysis", "read image")
        )
        supports_files = _tool_supports(
            info["tools"], ("read file", "filesystem", "document", "pdf")
        )
        if suffix in _IMAGE_SUFFIXES or mime_type.startswith("image/"):
            if not supports_images:
                await message.reply_text(IMAGE_UNSUPPORTED)
                return
        elif not is_text and not supports_files:
            await message.reply_text(FILE_UNSUPPORTED)
            return

        root = info["home"] / "tmp" / "telegram"
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{user_id}-", dir=root) as directory:
            storage_suffix = suffix if len(suffix) <= 10 and suffix[1:].isalnum() else ".bin"
            path = Path(directory) / f"upload{storage_suffix}"
            await _download(
                context, document.file_id, path, getattr(document, "file_size", None)
            )
            content = None
            truncated = False
            if is_text:
                try:
                    content, truncated = await asyncio.to_thread(_read_text_file, path)
                except UnicodeDecodeError:
                    await message.reply_text(FILE_UNSUPPORTED)
                    return
            prompt = _file_prompt(
                filename, path.resolve(), message.caption or "", _reply_context(message),
                content=content, truncated=truncated,
            )
            await _run_agent_request(update, context, manager, allowed_ids, prompt)
    except ValueError:
        logger.warning("telegram document rejected user_id=%s filename=%s", user_id, filename)
        await message.reply_text("⚠️ File quá lớn để xử lý an toàn.")
    except Exception:
        logger.exception("telegram document handler user_id=%s filename=%s", user_id, filename)
        await message.reply_text(ERROR_MESSAGE)


async def _set_commands(application: Any) -> None:
    from telegram import BotCommand

    await application.bot.set_my_commands([
        BotCommand("start", "Khởi động Tieru"),
        BotCommand("new", "Tạo cuộc hội thoại mới"),
        BotCommand("help", "Hướng dẫn"),
        BotCommand("status", "Trạng thái agent"),
        BotCommand("tools", "Danh sách tools"),
        BotCommand("memory", "Trạng thái memory"),
        BotCommand("id", "Telegram User ID"),
    ])


async def _telegram_error(update: object, context: Any) -> None:
    del update
    logger.error(
        "Unhandled Telegram update error: %s",
        type(context.error).__name__,
        exc_info=(type(context.error), context.error, context.error.__traceback__),
    )


def _build_app(
    token: str,
    allowed: str = "",
    *,
    agent_manager: TelegramAgentManager | None = None,
):
    """Build the polling application used by standalone and dashboard modes."""
    from telegram.ext import Application, CommandHandler, MessageHandler, filters

    settings = agent_manager.settings if agent_manager is not None else load_settings()
    manager = agent_manager or TelegramAgentManager(settings)
    allowed_ids = frozenset(set(settings.telegram_allowed_users) | _allowed_ids(allowed))

    async def shutdown(_application: Any) -> None:
        await manager.close()

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(16)
        .post_init(_set_commands)
        .post_shutdown(shutdown)
        .build()
    )
    kwargs = {"manager": manager, "allowed_ids": allowed_ids}
    app.add_handler(CommandHandler("start", partial(start_command, **kwargs)))
    app.add_handler(CommandHandler("new", partial(new_command, **kwargs)))
    app.add_handler(CommandHandler("help", partial(help_command, **kwargs)))
    app.add_handler(CommandHandler("status", partial(status_command, **kwargs)))
    app.add_handler(CommandHandler("tools", partial(tools_command, **kwargs)))
    app.add_handler(CommandHandler("memory", partial(memory_command, **kwargs)))
    app.add_handler(CommandHandler("id", partial(id_command, **kwargs)))
    app.add_handler(MessageHandler(filters.PHOTO, partial(handle_photo, **kwargs)))
    app.add_handler(MessageHandler(filters.Document.ALL, partial(handle_document, **kwargs)))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, partial(handle_text, **kwargs))
    )
    app.add_error_handler(_telegram_error)
    app.bot_data["tieru_agent_manager"] = manager
    app.bot_data["telegram_allowed_ids"] = allowed_ids
    return app


def _require_telegram_extra() -> None:
    try:
        import telegram  # noqa: F401
    except ImportError:
        raise SystemExit("Telegram extra not installed: pip install -e '.[telegram]'") from None


def main() -> None:
    _require_telegram_extra()
    settings = load_settings()
    token = settings.telegram_token
    allowed_ids = set(settings.telegram_allowed_users) | _allowed_ids()
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env (message @BotFather to create a bot).")
    if not allowed_ids:
        raise SystemExit(
            "Telegram gateway locked: set TELEGRAM_ALLOWED_USER or "
            "TELEGRAM_ALLOWED_USERS to numeric user IDs."
        )
    app = _build_app(token)
    print("Tieru is listening on Telegram — message your bot. Ctrl-C to stop.")
    print(posture(allowed_ids))
    app.run_polling()


def start_in_background() -> bool:
    """Start a fail-isolated Telegram poller for the dashboard process."""
    settings = load_settings()
    token = settings.telegram_token
    allowed_ids = set(settings.telegram_allowed_users) | _allowed_ids()
    if not token:
        return False
    if not allowed_ids:
        print("(telegram) gateway locked: Telegram allowlist is empty; not starting")
        return False
    try:
        _require_telegram_extra()
    except SystemExit:
        print("(telegram) extra is not installed — pip install -e '.[telegram]'")
        return False

    import threading

    print("(telegram) starting:")
    print(posture(allowed_ids))
    warned = {"conflict": False}

    def on_poll_error(exc: Exception) -> None:
        from telegram.error import Conflict

        if isinstance(exc, Conflict) and not warned["conflict"]:
            warned["conflict"] = True
            print(
                "(telegram) another instance is already running this bot — "
                "stop the other `tieru telegram` process to use dashboard polling."
            )

    def run() -> None:
        logging.getLogger("telegram").setLevel(logging.CRITICAL)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = None
        try:
            app = _build_app(token)
            loop.run_until_complete(app.initialize())
            if app.post_init is not None:
                loop.run_until_complete(app.post_init(app))
            loop.run_until_complete(app.start())
            loop.run_until_complete(app.updater.start_polling(error_callback=on_poll_error))
            loop.run_forever()
        except Exception as exc:
            print(f"(telegram) background poller stopped: {type(exc).__name__}")
        finally:
            if app is not None:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(app.updater.stop())
                with contextlib.suppress(Exception):
                    loop.run_until_complete(app.stop())
                if app.post_shutdown is not None:
                    with contextlib.suppress(Exception):
                        loop.run_until_complete(app.post_shutdown(app))
                with contextlib.suppress(Exception):
                    loop.run_until_complete(app.shutdown())
            loop.close()

    threading.Thread(target=run, daemon=True, name="telegram-poll").start()
    return True


if __name__ == "__main__":
    main()
