"""Конфиг: окружение, константы, логирование, клиенты API."""

from __future__ import annotations

import logging
import os
import sys
from datetime import time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from anthropic import AsyncAnthropic
from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parent / ".env"
_env = {**dotenv_values(ENV_PATH), **os.environ}


def _required(key: str) -> str:
    value = (_env.get(key) or "").strip()
    if not value:
        print(f"FATAL: не задан {key}", file=sys.stderr)
        sys.exit(1)
    return value


def _optional(key: str, default: str) -> str:
    return (_env.get(key) or "").strip() or default


TELEGRAM_BOT_TOKEN = _required("TELEGRAM_BOT_TOKEN")
OWNER_TELEGRAM_ID = int(_required("OWNER_TELEGRAM_ID"))
ANTHROPIC_API_KEY = _required("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _required("ANTHROPIC_MODEL")
GROQ_API_KEY = _required("GROQ_API_KEY")
NOTION_TOKEN = _required("NOTION_TOKEN")
NOTION_DATA_SOURCE_ID = _required("NOTION_DATA_SOURCE_ID")
NOTION_GOALS_PAGE_ID = _required("NOTION_GOALS_PAGE_ID")
NOTION_IDEAS_PAGE_ID = _required("NOTION_IDEAS_PAGE_ID")
STATUS_INBOX = _required("STATUS_INBOX")
# новые переменные v3 — с дефолтами, .env на сервере можно не трогать
ANTHROPIC_MODEL_DEEP = _optional("ANTHROPIC_MODEL_DEEP", "claude-fable-5")
ANTHROPIC_MODEL_DEEP_FALLBACK = _optional("ANTHROPIC_MODEL_DEEP_FALLBACK", "claude-opus-5")
STATUS_REMINDER = _optional("STATUS_REMINDER", "Напоминание")
STATUS_DONE = _optional("STATUS_DONE", "Done")
STATUS_ARCHIVE = _optional("STATUS_ARCHIVE", "Архив")
NOTION_MEMORY_PAGE_ID = _optional("NOTION_MEMORY_PAGE_ID", "3c6e17dcf5cb81e5bad2fb8c83790327")
NOTION_BUGS_CARD_ID = _optional("NOTION_BUGS_CARD_ID", "3bfe17dcf5cb812f8733d407c0f16b23")
STATE_DIR = Path(_env.get("STATE_DIR") or Path(__file__).resolve().parent / "state")

MSK = ZoneInfo("Europe/Moscow")
NOTION_VERSION = "2025-09-03"
CACHE_TTL = 10 * 60                # цели/память/команда — не дольше 10 минут
CACHE_RETRY_PENALTY = 5 * 60       # после неудачного обновления не долбить Notion
VOICE_TOGGLE_THRESHOLD = 200       # символов расшифровки для toggle
TOGGLE_CHUNK = 1800                # лимит символов на блок расшифровки
RETRY_DELAYS = (1, 5, 15)
MAX_ITEMS = 20                     # пунктов из одного сообщения
TITLE_LIMIT = 120                  # мягкий лимит названия карточки
AGENT_MAX_TURNS = 8                # итераций tool-use цикла агента
AGENT_MAX_MUTATIONS = 10           # изменённых карточек за один запрос

MORNING_AT = dtime(7, 30)
EVENING_AT = dtime(19, 0)
REFLECTION_AT = dtime(17, 0)       # пятница
SLOT_GRACE = {"morning": timedelta(hours=4, minutes=30),
              "evening": timedelta(hours=3),
              "reflection": timedelta(hours=2),
              "compact": timedelta(hours=6)}
AWAITING_DATE_TTL = timedelta(hours=2)
AWAITING_REFLECTION_TTL = timedelta(hours=48)
TIMED_REMINDER_LATE = timedelta(minutes=30)   # позже — пометка «было на …»

TAGS = ["Продажи", "Маркетинг", "Производство", "Финансы", "Личное", "Управление"]
RECOMMENDATIONS = ["🟢 Сам", "🔁 Делегировать", "❌ Не сейчас"]
STATUSES = [STATUS_INBOX, "Важно Срочно", "Важно Не срочно", "Не важно Срочно",
            "Не важно Не срочно", STATUS_DONE, STATUS_ARCHIVE, STATUS_REMINDER]
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

BUTTONS = [["Неделя", "Цели 90"], ["Поручения", "Посты"]]


class _MaskTokenFormatter(logging.Formatter):
    """Маскирует токен бота в любых логах, включая трейсбеки."""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_MaskTokenFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("sempre-bot")

# ретраи — только наши; SDK-таймаут ограничивает зависание одной попытки
anthropic_client = AsyncAnthropic(
    api_key=ANTHROPIC_API_KEY,
    timeout=httpx.Timeout(180.0, connect=10.0),
    max_retries=0,
)
notion = httpx.AsyncClient(
    base_url="https://api.notion.com/v1",
    headers={
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    },
    timeout=30,
)
groq = httpx.AsyncClient(
    base_url="https://api.groq.com/openai/v1",
    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
    timeout=120,
)
