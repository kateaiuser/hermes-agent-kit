"""Hermes Telegram bridge — телефонный доступ к твоему ассистенту на Hermes.

Каждое сообщение из Telegram уходит в Claude Code, запущенный в доме агента
(`claude -p` с cwd = дом). Поэтому бот — это НЕ отдельная болталка, а тот же
самый Hermes: личность из SOUL.md, навыки из skills/, память и workspace-файлы.
Тот же агент, что и в терминале, просто из телефона.

Доступ к модели бот не настраивает сам — он наследует окружение, в котором
запущен. Подойдёт ЛЮБОЙ из вариантов (см. INSTALL):
  - подписка Claude Pro/Max — войти один раз через `claude` (или `claude setup-token`
    на сервере, токен в CLAUDE_CODE_OAUTH_TOKEN);
  - API-ключ — ANTHROPIC_API_KEY (в .env или в окружении).
"""
import os
import json
import asyncio
import logging
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("hermes-telegram")

BOT_DIR = Path(__file__).resolve().parent
# Дом агента = папка на уровень выше (где лежат SOUL.md, skills/, workspace/).
HERMES_HOME = Path(os.getenv("HERMES_HOME", BOT_DIR.parent)).resolve()
SOUL = HERMES_HOME / "SOUL.md"
SESSIONS_FILE = BOT_DIR / "sessions.json"
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
MODEL = os.getenv("MODEL", "").strip()
TIMEOUT = int(os.getenv("TIMEOUT", "180"))

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
# Кому отвечать. Пусто = всем (НЕ рекомендуется: чужие тратят твой лимит/баланс).
_allowed = os.getenv("ALLOWED_USER_IDS", "").replace(" ", "")
ALLOWED_USER_IDS = {int(x) for x in _allowed.split(",") if x} if _allowed else set()


def load_sessions() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_sessions(data: dict) -> None:
    try:
        SESSIONS_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        log.exception("не смог сохранить sessions.json")


sessions = load_sessions()  # chat_id -> claude session_id (нить разговора)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(HERMES_HOME), capture_output=True, text=True, timeout=TIMEOUT
    )


def ask_claude(message: str, chat_id: str) -> tuple[str, str | None]:
    """Зовёт Claude Code в доме агента. Возвращает (текст ответа, новый session_id)."""
    base = [CLAUDE_BIN, "-p", message, "--output-format", "json",
            "--permission-mode", "acceptEdits"]
    if SOUL.exists():
        base += ["--append-system-prompt", SOUL.read_text(encoding="utf-8")]
    if MODEL:
        base += ["--model", MODEL]

    sid = sessions.get(chat_id)
    cmd = base + (["--resume", sid] if sid else [])

    try:
        proc = _run(cmd)
        # Битая/устаревшая сессия — пробуем заново без --resume.
        if proc.returncode != 0 and sid:
            log.info("resume не сработал, начинаю новую сессию для chat %s", chat_id)
            proc = _run(base)
    except subprocess.TimeoutExpired:
        return "Я слишком долго думала и прервалась. Попробуй ещё раз или короче.", None
    except FileNotFoundError:
        return ("Не нахожу команду claude на этой машине. Установлен ли Claude Code "
                "там, где запущен бот?"), None

    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out)
        text = (data.get("result") or "").strip()
        return (text or "Я тебя услышала, но ответ вышел пустой. Скажи иначе?"), data.get("session_id")
    except json.JSONDecodeError:
        if out:
            return out, None  # версия CLI вернула не-JSON — отдаём как есть
        return (proc.stderr or "Что-то пошло не так на стороне ассистента.").strip(), None


async def _keep_typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        uid = update.effective_user.id if update.effective_user else "?"
        log.warning("сообщение от чужого user_id=%s — игнор", uid)
        return

    chat_id = str(update.effective_chat.id)
    stop = asyncio.Event()
    typing = asyncio.create_task(_keep_typing(context, update.effective_chat.id, stop))
    try:
        reply, new_sid = await asyncio.to_thread(ask_claude, update.message.text, chat_id)
    finally:
        stop.set()
        await typing

    if new_sid:
        sessions[chat_id] = new_sid
        save_sessions(sessions)

    for i in range(0, len(reply), 4000):  # лимит Telegram ~4096
        await update.message.reply_text(reply[i:i + 4000])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Привет! Я на связи. Напиши мне что угодно — если мы ещё не знакомы, "
        "я сразу спрошу, как меня называть."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reset — забыть нить разговора (файлы памяти при этом остаются)."""
    if not is_allowed(update):
        return
    sessions.pop(str(update.effective_chat.id), None)
    save_sessions(sessions)
    await update.message.reply_text(
        "Начала новую сессию. Контекст последнего разговора забыт, память на месте."
    )


def main() -> None:
    if not SOUL.exists():
        log.warning("SOUL.md не найден в %s — проверь HERMES_HOME", HERMES_HOME)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Hermes Telegram bridge запущен. Дом агента: %s", HERMES_HOME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
