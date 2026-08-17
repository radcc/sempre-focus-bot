#!/usr/bin/env python3
"""Sempre Focus Bot v2 — Telegram → Notion: задачи, напоминания, идеи.

Текст/голос → Groq Whisper (голос) → Sonnet (разбор на несколько пунктов)
→ Notion (карточки задач/напоминаний, блоки идей) → короткая сводка в Telegram.
Плюс: утреннее (7:30) и вечернее (19:00) сообщения, «отмени», «неделя»,
дедуп по message_id, фильтр шума.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic
from dotenv import dotenv_values

# ---------------------------------------------------------------- конфиг

ENV_PATH = Path(__file__).resolve().parent / ".env"
_env = {**dotenv_values(ENV_PATH), **os.environ}

def _required(key: str) -> str:
    value = (_env.get(key) or "").strip()
    if not value:
        print(f"FATAL: не задан {key}", file=sys.stderr)
        sys.exit(1)
    return value

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
STATUS_REMINDER = (_env.get("STATUS_REMINDER") or "Напоминание").strip()
STATE_DIR = Path(_env.get("STATE_DIR") or Path(__file__).resolve().parent / "state")

MSK = ZoneInfo("Europe/Moscow")
NOTION_VERSION = "2025-09-03"
GOALS_CACHE_TTL = 30 * 60          # 30 минут
GOALS_RETRY_PENALTY = 5 * 60       # после неудачного обновления не долбить Notion чаще
VOICE_TOGGLE_THRESHOLD = 200       # символов расшифровки для toggle
TOGGLE_CHUNK = 1800                # лимит символов на блок расшифровки
RETRY_DELAYS = (1, 5, 15)
MAX_ITEMS = 20                     # пунктов из одного сообщения
MORNING_AT = dtime(7, 30)
EVENING_AT = dtime(19, 0)
SLOT_GRACE = {"morning": timedelta(hours=4, minutes=30),   # догоняем до 12:00
              "evening": timedelta(hours=3)}               # догоняем до 22:00
AWAITING_MAIN_TTL = timedelta(hours=12)   # окно ответа на «Что завтра главное?»
AWAITING_DATE_TTL = timedelta(hours=2)    # окно ответа на «На какую дату напомнить?»

TAGS = ["Продажи", "Маркетинг", "Производство", "Финансы", "Личное", "Управление"]
PEOPLE = ["Я", "Сона", "Саша", "Игорь", "Камила", "Настя", "Подрядчик"]
RECOMMENDATIONS = ["🟢 Сам", "🔁 Делегировать", "❌ Не сейчас"]
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

# «отмени» и с хвостом из служебных слов («отмени последнее», «убери запись, пожалуйста»),
# но НЕ голый префикс: «отмени встречу с Марией» — легитимная задача
UNDO_RE = re.compile(
    r"(отмени|отмена|убери|удали|/?undo)(\s+(последн\w*|это|её|его|запись|карточку|пачку|пожалуйста)){0,3}")
WEEK_WORDS = {"неделя", "/неделя"}


class _MaskTokenFormatter(logging.Formatter):
    """Маскирует токен бота в любых логах, включая трейсбеки."""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_MaskTokenFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("sempre-bot")

# ретраи — только наши (retry3); SDK-таймаут ограничивает зависание одной попытки
anthropic_client = AsyncAnthropic(
    api_key=ANTHROPIC_API_KEY,
    timeout=httpx.Timeout(120.0, connect=10.0),
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

# ---------------------------------------------------------------- ретраи

def _short_error(e: Exception) -> str:
    text = str(e) or type(e).__name__
    return text[:120]


def _reason(e: Exception) -> str:
    """Короткая причина для ответа в Telegram, без внутренностей."""
    mapping = {
        "TimeoutException": "таймаут",
        "ConnectTimeout": "таймаут",
        "ReadTimeout": "таймаут",
        "ConnectError": "нет связи",
        "APIConnectionError": "нет связи",
        "APITimeoutError": "таймаут",
    }
    if isinstance(e, httpx.HTTPStatusError):
        return f"ошибка API {e.response.status_code}"
    if isinstance(e, APIStatusError):
        return f"ошибка API {e.status_code}"
    return mapping.get(type(e).__name__, type(e).__name__)


def _retryable_read(e: Exception) -> bool:
    """Идемпотентные вызовы: повторяем сетевые сбои, 429 и 5xx. 4xx — нет."""
    if isinstance(e, (httpx.TransportError, APIConnectionError)):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return code == 429 or code >= 500
    if isinstance(e, APIStatusError):
        return e.status_code == 429 or e.status_code >= 500
    return isinstance(e, RuntimeError)  # обрезанный/пустой ответ Claude


def _retryable_write(e: Exception) -> bool:
    """Неидемпотентная запись в Notion: повторяем только когда запрос
    гарантированно не был принят — ошибки соединения и 429.
    ReadTimeout и 5xx после возможного коммита не повторяем (иначе дубли)."""
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429


async def retry3(name: str, fn, retryable=_retryable_read):
    """3 ретрая после первой попытки, паузы 1/5/15 секунд между вызовами."""
    last = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 — политику решает retryable
            last = e
            if not retryable(e):
                log.warning("%s: не повторяю (%s)", name, _short_error(e))
                raise
            log.warning("%s: попытка %d не удалась: %s", name, attempt + 1, _short_error(e))
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
    raise last

# ---------------------------------------------------------------- состояние на диске

def _atomic_write(path: Path, payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, path)


# дедуп обработанных message_id — рестарт не создаёт дубли из очереди Telegram
_seen_ids: set[int] = set()
_seen_order: list[int] = []
_SEEN_LIMIT = 2000


def _seen_file() -> Path:
    return STATE_DIR / "processed_ids.json"


def load_seen() -> None:
    try:
        data = json.loads(_seen_file().read_text())
        _seen_order.extend(int(x) for x in data.get("ids", []))
        _seen_ids.update(_seen_order)
        log.info("дедуп: загружено %d обработанных id", len(_seen_ids))
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("дедуп: не смог прочитать состояние — начинаю с чистого")


def mark_processed(message_id: int) -> None:
    if message_id in _seen_ids:
        return
    _seen_ids.add(message_id)
    _seen_order.append(message_id)
    while len(_seen_order) > _SEEN_LIMIT:
        _seen_ids.discard(_seen_order.pop(0))
    try:
        _atomic_write(_seen_file(), {"ids": _seen_order})
    except Exception:
        log.exception("дедуп: не смог сохранить состояние")


# отметки «слот расписания за сегодня отработан» — рестарт в 7:31 не теряет утро
def _sched_file() -> Path:
    return STATE_DIR / "schedule.json"


def load_sched() -> dict:
    try:
        return json.loads(_sched_file().read_text())
    except Exception:
        return {}


def save_sched(sent: dict) -> None:
    try:
        _atomic_write(_sched_file(), sent)
    except Exception:
        log.exception("расписание: не смог сохранить состояние")

# ---------------------------------------------------------------- память процесса

# последняя пачка созданного (для «отмени»); после рестарта пусто — так и задумано
_last_batch: dict = {"pages": [], "blocks": [], "titles": []}
_undo_done: dict = {"flag": False}  # True сразу после успешной отмены
# ждём ответ на «Что завтра главное?» (после вечернего сообщения)
_awaiting_main: dict = {"until": None}
# очередь напоминаний, ждущих дату: [{page_id, title}]
_awaiting_dates: dict = {"queue": [], "ts": None}

# ---------------------------------------------------------------- markdown → Notion

def chunk_text(text: str, size: int = TOGGLE_CHUNK) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def md_rich(text: str, prefix_bold: str = "") -> list[dict]:
    """Rich text с поддержкой **жирного**; куски не длиннее лимита Notion."""
    rich: list[dict] = []
    if prefix_bold:
        rich.append({
            "type": "text",
            "text": {"content": prefix_bold},
            "annotations": {"bold": True},
        })
    parts = re.split(r"\*\*(.+?)\*\*", text)
    for i, part in enumerate(parts):
        if not part:
            continue
        for chunk in chunk_text(part):
            item: dict = {"type": "text", "text": {"content": chunk}}
            if i % 2 == 1:
                item["annotations"] = {"bold": True}
            rich.append(item)
    if not rich:
        rich.append({"type": "text", "text": {"content": text or " "}})
    return rich


def paragraph(rich: list[dict]) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich}}


def md_blocks(md: str) -> list[dict]:
    blocks = []
    for line in (md or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line[:2] in ("- ", "• ", "* "):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": md_rich(line[2:])},
            })
        else:
            blocks.append(paragraph(md_rich(line)))
    return blocks


def section_blocks(label: str, md: str) -> list[dict]:
    lines = [l for l in (md or "").splitlines() if l.strip()]
    if not lines:
        return []
    if len(lines) == 1:
        return [paragraph(md_rich(lines[0], prefix_bold=f"{label}: "))]
    return [paragraph(md_rich("", prefix_bold=f"{label}:"))] + md_blocks(md)


def cap_blocks(blocks: list[dict], limit: int) -> list[dict]:
    """Notion принимает максимум 100 блоков за запрос: хвост склеиваем в абзац."""
    if len(blocks) <= limit:
        return blocks

    def block_plain(b: dict) -> str:
        rich = b.get(b["type"], {}).get("rich_text", [])
        return "".join(x.get("text", {}).get("content", "") for x in rich)

    head = blocks[:limit - 1]
    tail_text = "\n".join(filter(None, (block_plain(b) for b in blocks[limit - 1:])))
    head.append(paragraph([{"type": "text", "text": {"content": c}} for c in chunk_text(tail_text)]))
    return head


def toggle_block(title: str, text: str) -> dict:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": title}}],
            "children": [paragraph([{"type": "text", "text": {"content": c}}])
                         for c in chunk_text(text)],
        },
    }

# ---------------------------------------------------------------- Notion: чтение

_goals_cache: dict = {"text": None, "ts": 0.0}


async def _fetch_children_text(block_id: str, depth: int = 0) -> list[str]:
    if depth > 2:
        return []
    lines: list[str] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = await notion.get(f"/blocks/{block_id}/children", params=params)
        r.raise_for_status()
        data = r.json()
        for b in data.get("results", []):
            btype = b.get("type", "")
            payload = b.get(btype, {}) or {}
            rich = payload.get("rich_text", [])
            text = "".join(x.get("plain_text", "") for x in rich).strip()
            if btype == "to_do" and text:
                text = ("[x] " if payload.get("checked") else "[ ] ") + text
            if text:
                marker = "- " if "list_item" in btype or btype == "to_do" else ""
                lines.append("  " * depth + marker + text)
            if b.get("has_children") and btype not in ("child_page", "child_database"):
                lines.extend(await _fetch_children_text(b["id"], depth + 1))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return lines


async def get_goals_text() -> str:
    now = asyncio.get_event_loop().time()
    if _goals_cache["text"] is not None and now - _goals_cache["ts"] < GOALS_CACHE_TTL:
        return _goals_cache["text"]
    try:
        lines = await retry3("notion-goals", lambda: _fetch_children_text(NOTION_GOALS_PAGE_ID))
        _goals_cache["text"] = "\n".join(lines)
        _goals_cache["ts"] = now
    except Exception as e:
        if _goals_cache["text"] is None:
            raise
        # штрафной TTL: во время инцидента Notion не гонять полный retry3 на каждое сообщение
        _goals_cache["ts"] = now - GOALS_CACHE_TTL + GOALS_RETRY_PENALTY
        log.warning("цели: не обновились, использую кэш ещё %d с: %s",
                    GOALS_RETRY_PENALTY, _short_error(e))
    return _goals_cache["text"]


_ideas_anchor: dict = {"id": None}


async def get_ideas_anchor() -> str:
    if _ideas_anchor["id"]:
        return _ideas_anchor["id"]
    r = await notion.get(f"/blocks/{NOTION_IDEAS_PAGE_ID}/children", params={"page_size": 1})
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise RuntimeError("страница «Идеи» пуста — нет блока-инструкции")
    _ideas_anchor["id"] = results[0]["id"]
    return _ideas_anchor["id"]


async def query_pages(filter_: dict) -> list[dict]:
    """Все страницы базы по фильтру (с пагинацией)."""
    results: list[dict] = []
    cursor = None
    while True:
        body: dict = {"filter": filter_, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = await notion.post(f"/data_sources/{NOTION_DATA_SOURCE_ID}/query", json=body)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


def page_title(page: dict) -> str:
    rich = page.get("properties", {}).get("Name", {}).get("title", [])
    return "".join(x.get("plain_text", "") for x in rich) or "Без названия"


async def reminders_for(iso_date: str) -> list[dict]:
    filter_ = {"and": [
        {"property": "Статус", "select": {"equals": STATUS_REMINDER}},
        {"property": "Когда", "date": {"equals": iso_date}},
    ]}
    return await retry3("notion-reminders", lambda: query_pages(filter_))


async def count_created_today() -> int:
    midnight = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    filter_ = {"timestamp": "created_time", "created_time": {"on_or_after": midnight.isoformat()}}
    pages = await retry3("notion-count", lambda: query_pages(filter_))
    return len(pages)

# ---------------------------------------------------------------- Claude: разбор

SAVE_TOOL = {
    "name": "save_entries",
    "description": "Сохранить разобранные пункты сообщения (задачи, напоминания, идеи, шум).",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string",
                                 "enum": ["task", "reminder", "idea", "noise", "set_reminder_date"]},
                        "title": {"type": "string",
                                  "description": "Лаконичный заголовок с глаголом, до 80 символов; для noise — сам фрагмент"},
                        "tag": {"type": "string", "enum": TAGS},
                        "who": {"type": "string", "enum": PEOPLE},
                        "deadline_iso": {"type": ["string", "null"],
                                         "description": "YYYY-MM-DD; для reminder — дата напоминания"},
                        "date_missing": {"type": "boolean",
                                         "description": "reminder: дата не названа во входе"},
                        "goal_score": {"type": "integer", "minimum": 0, "maximum": 3},
                        "ceo_score": {"type": "integer", "minimum": 0, "maximum": 3},
                        "recommendation": {"type": "string", "enum": RECOMMENDATIONS},
                        "summary_md": {"type": "string"},
                        "context_md": {"type": ["string", "null"]},
                        "next_step": {"type": ["string", "null"]},
                        "analysis_md": {"type": "string"},
                    },
                    "required": ["type", "title"],
                },
            },
        },
        "required": ["items"],
    },
}


def upcoming_morning_date() -> date:
    """Дата ближайшего утреннего сообщения: сегодня до 7:30, иначе завтра.
    Ответ «что завтра главное» в 00:30 должен прийти этим же утром, не через день."""
    now = datetime.now(MSK)
    return now.date() if now.time() < MORNING_AT else now.date() + timedelta(days=1)


def build_system_prompt(goals: str, main_hint: bool, date_hint: str | None) -> str:
    now = datetime.now(MSK)
    morning_iso = upcoming_morning_date().isoformat()
    prompt = f"""Ты — тихий секретарь Алексея, основателя контент-агентства Sempre. \
Разбери сообщение на отдельные пункты и сохрани через инструмент save_entries. \
Одно сообщение может содержать МНОГО пунктов вперемешку: задачи, напоминания, идеи, мусор.

Сейчас: {WEEKDAYS_RU[now.weekday()]}, {now.strftime("%d.%m.%Y %H:%M")} (Москва).

=== Страница «🎯 Цели» (цель года, фокусы 90 дней, команда, правила CEO-оценки) ===
{goals}
=== Конец страницы целей ===

Типы пунктов:
- task — конкретное действие. Поля: title, tag, who, deadline_iso (только если срок явно следует из текста), goal_score, ceo_score, recommendation, summary_md, context_md, next_step, analysis_md.
- reminder — «напомни», «не забудь», «напоминалка», «надо будет спросить/проверить у …» и аналоги по смыслу. Поля: title (формулировка действия: «Спросить у Игоря отчёт», не пересказ), who, tag, deadline_iso = дата напоминания. Если дата не названа — deadline_iso = завтра и date_missing = true. Если человек не из списка команды — who = «Я», а имя оставь в title.
- idea — мысль, концепция, «а что если», без конкретного действия. Префикс «идея:» — принудительно идея, но само слово «идея» в title не включай. Поля: title, tag, summary_md.
- noise — явно случайный набор символов или обрывок без смысла. title = сам фрагмент. Если есть хоть какой-то шанс, что это задача — это task, не noise (ложное занесение дешевле потерянной мысли).

Правила для task:
- goal_score (→Цель): двигает ли к фокусам 90 дней. 3 — прямой рычаг, 0 — не двигает.
- ceo_score (CEO): чья это работа по правилам CEO-оценки. 3 — только основатель, 0 — точно не он.
- recommendation: важная задача с ceo_score ≤ 1 → всегда «🔁 Делегировать» (в analysis_md — кому из команды). goal_score = 0 и нет срочности → «❌ Не сейчас». Иначе «🟢 Сам».
- «Передал/поручил {{имя}}», «жду от {{имя}}» → who = имя; названная дата проверки → deadline_iso. Имя исполнителя в title не дублируй, если who = этот человек («Перепроверить гипотезу», не «Насте перепроверить гипотезу»).
- Относительные даты («завтра», «в пятницу», «через неделю») → конкретная дата по Москве, YYYY-MM-DD.
- Короткий вход не раздувай: context_md = null, next_step = null.

Особое правило: сообщение вида «завтра главное — X» → один reminder, title = «Главное сегодня: X», deadline_iso = {morning_iso}.

Стиль всех текстов: минимализм, по-русски, без воды. Бот причёсывает хаотичную диктовку — формулировки чище входа."""
    if main_hint:
        prompt += ("\n\nКонтекст: бот только что спросил «Что завтра главное?». Если сообщение "
                   "похоже на ответ (короткая формулировка главного дела), верни ОДИН пункт "
                   f"type=reminder, title=«Главное сегодня: …», deadline_iso = {morning_iso}.")
    if date_hint:
        prompt += (f"\n\nКонтекст: бот спросил «На какую дату напомнить?» про карточку «{date_hint}». "
                   "Если сообщение начинается с даты или срока без действия («в пятницу», «22 августа»), "
                   "верни для этой части пункт type=set_reminder_date с deadline_iso = эта дата; "
                   "остальные части сообщения разбери как обычно.")
    return prompt


async def classify(input_text: str, main_hint: bool = False, date_hint: str | None = None) -> list[dict]:
    goals = await get_goals_text()

    async def _call():
        resp = await anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8000,
            system=build_system_prompt(goals, main_hint, date_hint),
            messages=[{"role": "user", "content": input_text}],
            tools=[SAVE_TOOL],
            tool_choice={"type": "tool", "name": "save_entries"},
        )
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("ответ Claude обрезан по max_tokens")
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("Claude не вернул tool_use")

    result = await retry3("claude", _call)
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Claude вернул неожиданную структуру")
    return [normalize_item(i) for i in items[:MAX_ITEMS] if isinstance(i, dict)]


def normalize_item(item: dict) -> dict:
    if item.get("type") not in ("task", "reminder", "idea", "noise", "set_reminder_date"):
        item["type"] = "task"
    item["title"] = (item.get("title") or "Без названия").strip()[:80]
    if item.get("tag") not in TAGS:
        item["tag"] = "Личное"
    if item.get("who") not in PEOPLE:
        item["who"] = "Я"
    if item.get("recommendation") not in RECOMMENDATIONS:
        item["recommendation"] = "🟢 Сам"
    deadline = item.get("deadline_iso")
    if deadline and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(deadline)):
        item["deadline_iso"] = None
    for key in ("goal_score", "ceo_score"):
        try:
            item[key] = max(0, min(3, int(item.get(key, 0))))
        except (TypeError, ValueError):
            item[key] = 0
    item["date_missing"] = bool(item.get("date_missing"))
    # дата напоминания обязательна: нет даты (или была кривая) — считаем, что не назвал
    if item["type"] == "reminder" and not item.get("deadline_iso"):
        item["date_missing"] = True
    return item

# ---------------------------------------------------------------- Claude: сжатие недели

WEEK_TOOL = {
    "name": "week_summary",
    "description": "Сжать результаты недели и фокусы 90 дней.",
    "input_schema": {
        "type": "object",
        "properties": {
            "week_results": {"type": "array", "maxItems": 3, "items": {"type": "string"},
                             "description": "3 результата недели, каждый до 6 слов, с цифрами"},
            "focuses": {"type": "array", "maxItems": 5, "items": {"type": "string"},
                        "description": "Фокусы 90 дней, каждый одной короткой строкой"},
        },
        "required": ["week_results", "focuses"],
    },
}

_week_cache: dict = {"data": None, "ts": 0.0}


async def get_week_summary(fresh: bool = False) -> dict:
    now = asyncio.get_event_loop().time()
    if fresh:
        _goals_cache["ts"] = 0.0
        _week_cache["ts"] = 0.0
    if _week_cache["data"] is not None and now - _week_cache["ts"] < GOALS_CACHE_TTL:
        return _week_cache["data"]
    goals = await get_goals_text()

    async def _call():
        resp = await anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1000,
            system=("Из текста страницы целей выдели «Результаты недели» и «Фокусы 90 дней». "
                    "Сожми: результаты — до 6 слов каждый, с цифрами, не дословно; "
                    "фокусы — одной короткой строкой каждый. Без воды."),
            messages=[{"role": "user", "content": goals}],
            tools=[WEEK_TOOL],
            tool_choice={"type": "tool", "name": "week_summary"},
        )
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("ответ Claude обрезан по max_tokens")
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("Claude не вернул tool_use")

    data = await retry3("claude-week", _call)
    _week_cache["data"] = data
    _week_cache["ts"] = asyncio.get_event_loop().time()
    return data

# ---------------------------------------------------------------- Notion: запись

async def create_task_page(item: dict, transcript: str | None) -> str:
    properties = {
        "Name": {"title": [{"text": {"content": item["title"]}}]},
        "Статус": {"select": {"name": STATUS_INBOX}},
        "Тег": {"select": {"name": item["tag"]}},
        "Кто": {"select": {"name": item["who"]}},
        "Рекомендация": {"select": {"name": item["recommendation"]}},
    }
    if item.get("deadline_iso"):
        properties["Когда"] = {"date": {"start": item["deadline_iso"]}}

    children: list[dict] = []
    summary = (item.get("summary_md") or "").strip()
    if summary and summary.lower() != item["title"].lower():
        children.extend(md_blocks(summary))
    if item.get("context_md"):
        children.extend(section_blocks("Контекст", item["context_md"]))
    if item.get("next_step"):
        children.extend(section_blocks("Следующий шаг", item["next_step"]))

    analysis_head = f"→Цель {item['goal_score']}/3 · CEO {item['ceo_score']}/3 · {item['recommendation']}"
    children.append(paragraph(md_rich(analysis_head, prefix_bold="🤖 Анализ: ")))
    children.extend(md_blocks(item.get("analysis_md") or ""))

    children = cap_blocks(children, 99)
    if transcript:
        children.append(toggle_block("Расшифровка", transcript))

    async def _create():
        r = await notion.post("/pages", json={
            "parent": {"type": "data_source_id", "data_source_id": NOTION_DATA_SOURCE_ID},
            "properties": properties,
            "children": children,
        })
        r.raise_for_status()
        return r.json()["id"]

    return await retry3("notion-task", _create, retryable=_retryable_write)


async def create_reminder_page(item: dict, transcript: str | None = None) -> str:
    """Карточка-напоминание: скрытая колонка, минимум полей."""
    when = item.get("deadline_iso") or (datetime.now(MSK).date() + timedelta(days=1)).isoformat()
    properties = {
        "Name": {"title": [{"text": {"content": item["title"]}}]},
        "Статус": {"select": {"name": STATUS_REMINDER}},
        "Тег": {"select": {"name": item["tag"]}},
        "Кто": {"select": {"name": item["who"]}},
        "Когда": {"date": {"start": when}},
    }
    body: dict = {
        "parent": {"type": "data_source_id", "data_source_id": NOTION_DATA_SOURCE_ID},
        "properties": properties,
    }
    if transcript:
        body["children"] = [toggle_block("Расшифровка", transcript)]

    async def _create():
        r = await notion.post("/pages", json=body)
        r.raise_for_status()
        return r.json()["id"]

    return await retry3("notion-reminder", _create, retryable=_retryable_write)


async def append_idea(item: dict, body_text: str, with_toggle: bool,
                      after_block: str | None = None) -> list[str]:
    """Блок идеи после callout (или после указанного блока — для порядка
    нескольких идей из одного сообщения); возвращает id созданных блоков."""
    today = datetime.now(MSK).strftime("%d.%m")
    blocks: list[dict] = [paragraph(md_rich("", prefix_bold=f"{today} — {item['title']}"))]

    if with_toggle:
        summary = (item.get("summary_md") or "").strip() or item["title"]
        blocks.extend(md_blocks(summary))
        blocks = cap_blocks(blocks, 99)
        blocks.append(toggle_block("Расшифровка", body_text))
    else:
        body = (body_text or "").strip()
        if body and body.lower() != item["title"].lower():
            blocks.extend(md_blocks(body))
        blocks = cap_blocks(blocks, 100)

    async def _append():
        for attempt in (0, 1):
            anchor = after_block or await get_ideas_anchor()
            r = await notion.patch(f"/blocks/{NOTION_IDEAS_PAGE_ID}/children",
                                   json={"children": blocks, "after": anchor})
            if r.status_code in (400, 404) and attempt == 0 and not after_block:
                # якорь протух (блок-инструкцию переделали): при 4xx Notion ничего
                # не записал — безопасно перечитать якорь и повторить сразу
                _ideas_anchor["id"] = None
                continue
            r.raise_for_status()
            return [b["id"] for b in r.json().get("results", [])]
        raise RuntimeError("вставка идеи не удалась")  # недостижимо, для линтера

    return await retry3("notion-idea", _append, retryable=_retryable_write)


async def archive_page(page_id: str) -> None:
    async def _archive():
        r = await notion.patch(f"/pages/{page_id}", json={"archived": True})
        r.raise_for_status()

    await retry3("notion-archive", _archive)  # идемпотентно — политика read


async def delete_block(block_id: str) -> None:
    async def _delete():
        r = await notion.delete(f"/blocks/{block_id}")
        if r.status_code == 404:
            return  # уже удалён
        r.raise_for_status()

    await retry3("notion-delblock", _delete)


async def set_page_date(page_id: str, iso_date: str) -> None:
    async def _set():
        r = await notion.patch(f"/pages/{page_id}",
                               json={"properties": {"Когда": {"date": {"start": iso_date}}}})
        r.raise_for_status()

    await retry3("notion-setdate", _set)


async def set_page_status(page_id: str, status: str) -> None:
    async def _set():
        r = await notion.patch(f"/pages/{page_id}",
                               json={"properties": {"Статус": {"select": {"name": status}}}})
        r.raise_for_status()

    await retry3("notion-setstatus", _set)

# ---------------------------------------------------------------- Groq: расшифровка

async def transcribe_voice(bot: Bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, buf)
    audio = buf.getvalue()

    async def _call():
        r = await groq.post(
            "/audio/transcriptions",
            files={"file": ("audio.ogg", audio)},
            data={"model": "whisper-large-v3-turbo", "language": "ru", "response_format": "json"},
        )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()

    return await retry3("groq", _call)

# ---------------------------------------------------------------- сводка для Telegram

def _plural(n: int, forms: tuple[str, str, str]) -> str:
    if n % 100 in (11, 12, 13, 14):
        f = forms[2]
    elif n % 10 == 1:
        f = forms[0]
    elif n % 10 in (2, 3, 4):
        f = forms[1]
    else:
        f = forms[2]
    return f"{n} {f}"


def human_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return iso
    today = datetime.now(MSK).date()
    if d == today:
        return "сегодня"
    if d == today + timedelta(days=1):
        return "завтра"
    return d.strftime("%d.%m")


def build_summary(created: list[dict], updated: list[dict], noise: list[str],
                  truncated: bool = False, ask_title: str | None = None) -> str:
    """created: [{kind, title, deadline_iso, who, date_missing}];
    updated: [{title, deadline_iso}] — напоминания, которым проставили дату."""
    tasks = [c for c in created if c["kind"] == "task"]
    rems = [c for c in created if c["kind"] == "reminder"]
    ideas = [c for c in created if c["kind"] == "idea"]
    lines: list[str] = []

    if created:
        counts = []
        if tasks:
            counts.append(_plural(len(tasks), ("задачу", "задачи", "задач")))
        if rems:
            counts.append(_plural(len(rems), ("напоминание", "напоминания", "напоминаний")))
        if ideas:
            counts.append(_plural(len(ideas), ("идею", "идеи", "идей")))
        lines.append("Занёс " + ", ".join(counts) + ":")
        for t in tasks:
            line = "— "
            if t["who"] != "Я":
                line += f"{t['who']}: "
            line += t["title"]
            if t.get("deadline_iso"):
                line += f" ({human_date(t['deadline_iso'])})"
            lines.append(line)
        for r in rems:
            lines.append(f"⏰ {human_date(r['deadline_iso'])} {r['title']}" if r.get("deadline_iso")
                         else f"⏰ завтра {r['title']}")
        for i in ideas:
            lines.append(f"💡 {i['title']}")

    for u in updated:
        lines.append(f"Ок, напомню {human_date(u['deadline_iso'])}: {u['title']}")
    if noise:
        lines.append("Пропустил как шум: " + ", ".join(f"«{n}»" for n in noise))
    if not lines:
        return "Не понял, что занести. Скажи иначе."
    if truncated:
        lines.append(f"Вошло {MAX_ITEMS} пунктов — потолок одного сообщения. Остальное продиктуй отдельно.")
    if ask_title:
        lines.append(f"На какую дату напомнить «{ask_title}»? Пока поставил завтра.")
    if len(created) > 1:
        lines.append(f"«отмени» уберёт всю пачку ({len(created)}).")
    return "\n".join(lines)

# ---------------------------------------------------------------- обработка сообщений

def forward_source(message: Message) -> str | None:
    origin = message.forward_origin
    if origin is None:
        return None
    if getattr(origin, "sender_user", None):
        user = origin.sender_user
        return " ".join(filter(None, [user.first_name, user.last_name])) or user.username or "?"
    if getattr(origin, "sender_user_name", None):
        return origin.sender_user_name
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    if chat is not None:
        return chat.title or chat.username or "?"
    return "?"


def _normalize_cmd(text: str) -> str:
    return re.sub(r"[.,!?…«»\"']+", "", (text or "").strip().lower()).strip()


def is_undo(norm: str) -> bool:
    return bool(UNDO_RE.fullmatch(norm))


async def do_undo(message: Message) -> None:
    if not _last_batch["pages"] and not _last_batch["blocks"]:
        if _undo_done["flag"]:
            await message.answer("Уже убрал — дальше руками.")
        else:
            await message.answer("Не помню последнюю — удали руками.")
        return
    titles = list(_last_batch["titles"])
    archived = list(_last_batch["pages"])
    try:
        for page_id in _last_batch["pages"]:
            await archive_page(page_id)
        for block_id in _last_batch["blocks"]:
            await delete_block(block_id)
    except Exception as e:  # noqa: BLE001
        log.exception("отмена не удалась")
        await message.answer(f"Не смог убрать ({_reason(e)}). Удали руками.")
        return
    _last_batch.update({"pages": [], "blocks": [], "titles": []})
    _undo_done["flag"] = True
    # вопросы про дату к заархивированным карточкам больше не актуальны
    _awaiting_dates["queue"] = [s for s in _awaiting_dates["queue"] if s["page_id"] not in archived]
    if not _awaiting_dates["queue"]:
        _awaiting_dates["ts"] = None
    mark_processed(message.message_id)
    log.info("отмена: убрано %d объектов", len(titles))
    await message.answer("Убрал: " + ", ".join(titles))


async def do_week(message: Message) -> None:
    try:
        # явный запрос = свежие данные, мимо кэшей (обзор понедельника правит цели)
        week = await get_week_summary(fresh=True)
    except Exception as e:  # noqa: BLE001
        log.exception("неделя: не собралась")
        await message.answer(f"Не смог собрать ({_reason(e)}). Попробуй ещё раз.")
        return
    results = week.get("week_results") or []
    focuses = week.get("focuses") or []
    lines = []
    if results:
        lines.append("Неделя: " + "  ".join(f"{i}) {r}" for i, r in enumerate(results, 1)))
    if focuses:
        lines.append("Фокусы 90:")
        lines.extend(f"— {f}" for f in focuses)
    mark_processed(message.message_id)
    await message.answer("\n".join(lines) or "На странице целей пусто.")


async def process_items(message: Message, text: str, transcript: str | None = None) -> None:
    source = forward_source(message)
    input_text = f"Переслано от {source}:\n{text}" if source else text
    if transcript is not None:
        input_text = f"(голосовое сообщение, расшифровка)\n{input_text}"

    now = datetime.now(MSK)
    main_hint = bool(_awaiting_main["until"] and now < _awaiting_main["until"])
    if _awaiting_dates["queue"] and _awaiting_dates["ts"] and now - _awaiting_dates["ts"] > AWAITING_DATE_TTL:
        _awaiting_dates.update({"queue": [], "ts": None})  # вопрос протух
    date_hint = _awaiting_dates["queue"][0]["title"] if _awaiting_dates["queue"] else None

    created: list[dict] = []
    updated: list[dict] = []
    pages: list[str] = []
    blocks: list[str] = []
    noise: list[str] = []
    items: list[dict] = []
    try:
        items = await classify(input_text, main_hint=main_hint, date_hint=date_hint)
        # окно «что завтра главное» гасим только распознанным ответом (иначе — по TTL)
        if main_hint and any(i["type"] == "reminder" and i["title"].startswith("Главное сегодня")
                             for i in items):
            _awaiting_main["until"] = None
        # ответил не датой — вопрос про дату снят, карточка остаётся на «завтра»
        if date_hint and not any(i["type"] == "set_reminder_date" for i in items):
            _awaiting_dates.update({"queue": [], "ts": None})

        # расшифровка длинного голосового прикладывается ровно один раз —
        # к первой созданной карточке любого типа
        transcript_pending = (transcript if transcript and len(transcript) > VOICE_TOGGLE_THRESHOLD
                              else None)
        single_voice_idea = (transcript is not None and len(items) == 1
                             and items[0]["type"] == "idea")
        last_idea_block: str | None = None
        for item in items:
            kind = item["type"]
            if kind == "noise":
                noise.append(item["title"])
            elif kind == "set_reminder_date":
                # применяем к первой ждущей карточке; вне сценария — молча пропускаем,
                # задачей этот пункт не становится никогда
                if _awaiting_dates["queue"] and item.get("deadline_iso"):
                    slot = _awaiting_dates["queue"].pop(0)
                    await set_page_date(slot["page_id"], item["deadline_iso"])
                    updated.append({"title": slot["title"], "deadline_iso": item["deadline_iso"]})
                    if not _awaiting_dates["queue"]:
                        _awaiting_dates["ts"] = None
            elif kind == "idea":
                with_toggle = single_voice_idea and transcript_pending is not None
                body = transcript if single_voice_idea else (item.get("summary_md") or "")
                ids = await append_idea(item, body or "", with_toggle, after_block=last_idea_block)
                if with_toggle:
                    transcript_pending = None
                blocks.extend(ids)
                if ids:
                    last_idea_block = ids[-1]
                created.append({"kind": "idea", "title": item["title"]})
            elif kind == "reminder":
                page_id = await create_reminder_page(item, transcript_pending)
                transcript_pending = None
                pages.append(page_id)
                when = item.get("deadline_iso") or (now.date() + timedelta(days=1)).isoformat()
                created.append({"kind": "reminder", "title": item["title"],
                                "deadline_iso": when, "date_missing": item["date_missing"]})
                if item["date_missing"]:
                    _awaiting_dates["queue"].append({"page_id": page_id, "title": item["title"]})
                    _awaiting_dates["ts"] = now
            else:  # task (и всё непонятное)
                page_id = await create_task_page(item, transcript_pending)
                transcript_pending = None
                pages.append(page_id)
                created.append({"kind": "task", "title": item["title"],
                                "deadline_iso": item.get("deadline_iso"), "who": item["who"]})
    except Exception as e:  # noqa: BLE001 — сбой не должен терять текст
        log.exception("не сохранил запись")
        if created:
            # «отмени» должно целиться в частично созданную пачку, не в предыдущую
            _last_batch.update({"pages": pages, "blocks": blocks,
                                "titles": [c["title"] for c in created]})
            _undo_done["flag"] = False
        planned = len([i for i in items if i.get("type") in ("task", "reminder", "idea")])
        head = (f"⚠️ Занёс {len(created)} из {planned}, дальше сбой ({_reason(e)})."
                if created else f"⚠️ Не сохранил ({_reason(e)}).")
        await message.answer(head + " Текст ниже — кинь позже:")
        for chunk in chunk_text(text, 4000):
            await message.answer(chunk)
        return

    if created:
        _last_batch.update({"pages": pages, "blocks": blocks,
                            "titles": [c["title"] for c in created]})
        _undo_done["flag"] = False
    mark_processed(message.message_id)
    log.info("сохранено: %d пунктов (%s), дат обновлено: %d, шум: %d",
             len(created), ", ".join(c["kind"] for c in created) or "-", len(updated), len(noise))
    ask_title = _awaiting_dates["queue"][0]["title"] if _awaiting_dates["queue"] else None
    # запись уже в Notion: сбой подтверждения не должен звучать как «не сохранил»
    try:
        await message.answer(build_summary(created, updated, noise,
                                           truncated=len(items) >= MAX_ITEMS,
                                           ask_title=ask_title))
    except Exception:  # noqa: BLE001
        log.exception("сохранено, но подтверждение в Telegram не отправилось")


async def handle_text_like(message: Message, text: str, transcript: str | None = None) -> None:
    norm = _normalize_cmd(text)
    if is_undo(norm):
        await do_undo(message)
        return
    if norm in WEEK_WORDS:
        await do_week(message)
        return
    await process_items(message, text, transcript)

# ---------------------------------------------------------------- расписание 7:30 / 19:00

MONDAY_TEXT = ("Понедельник. Обзор недели, 60–90 мин:\n"
               "блокнот → «Цели» (3 результата) → доска и делегированные → идеи → календарь.\n"
               "Открой Cowork: «прочитай STATUS.md, проведи обзор».")

EVENING_TAIL = ("Вечерний разбор, 10 мин:\n"
                "— Inbox → 0 (Notion, колонка To Do)\n"
                "— выбери главный результат завтра\n"
                "— переименуй завтрашний deep work\n"
                "Что завтра главное? Ответь строкой — напомню утром.")


def slot_state(now: datetime, slot_time: dtime, grace: timedelta, sent_date: str | None) -> str | None:
    """None — рано или уже отправлено; 'send' — пора; 'expire' — слот пропущен."""
    slot_dt = now.replace(hour=slot_time.hour, minute=slot_time.minute, second=0, microsecond=0)
    if now < slot_dt or sent_date == now.date().isoformat():
        return None
    return "send" if now - slot_dt <= grace else "expire"


async def send_morning(bot: Bot) -> None:
    today = datetime.now(MSK)
    pages = await reminders_for(today.date().isoformat())
    titles = [page_title(p) for p in pages]
    if today.weekday() == 0:
        msg = MONDAY_TEXT
        if len(titles) == 1:
            msg += f"\nСегодня: {titles[0]}"
        elif titles:
            msg += "\nСегодня:\n" + "\n".join(f"— {t}" for t in titles)
        await bot.send_message(OWNER_TELEGRAM_ID, msg)
    elif titles:
        await bot.send_message(OWNER_TELEGRAM_ID, "\n".join(titles))
    else:
        log.info("утро: напоминаний нет — молчу")
        return
    # доставленные напоминания → Статус=Архив: в скрытой колонке остаётся только будущее
    for p in pages:
        try:
            await set_page_status(p["id"], "Архив")
        except Exception:  # noqa: BLE001 — не смертельно, карточка просто останется в колонке
            log.exception("утро: не смог перевести «%s» в Архив", page_title(p))


async def send_evening(bot: Bot) -> None:
    if datetime.now(MSK).weekday() >= 5:
        log.info("вечер: выходной — молчу")
        return
    try:
        n = await count_created_today()
        day_line = f"Итог дня: занесено {n}."
    except Exception:  # noqa: BLE001 — вечер важнее счётчика
        log.exception("вечер: подсчёт карточек не удался")
        day_line = "Итог дня: не сосчитал."
    lines = [day_line]
    try:
        week = await get_week_summary()
        results = (week.get("week_results") or [])[:3]
        if results:
            lines.append("Неделя: " + "  ".join(f"{i}) {r}" for i, r in enumerate(results, 1)))
    except Exception:  # noqa: BLE001 — вечер важнее сжатия недели
        log.exception("вечер: сжатие недели не удалось")
    lines.append(EVENING_TAIL)
    await bot.send_message(OWNER_TELEGRAM_ID, "\n".join(lines))
    _awaiting_main["until"] = datetime.now(MSK) + AWAITING_MAIN_TTL


async def scheduler_loop(bot: Bot) -> None:
    """Минутный тик с отметками на диске: рестарт возле 7:30/19:00 не теряет
    и не дублирует отправку; простой дольше грейса — слот пропускается с логом."""
    sent = load_sched()
    fails: dict = {}
    slots = {"morning": (MORNING_AT, send_morning), "evening": (EVENING_AT, send_evening)}
    log.info("расписание: утро %s, вечер %s (Москва), отметки: %s", MORNING_AT, EVENING_AT, sent or "-")
    while True:
        now = datetime.now(MSK)
        today_iso = now.date().isoformat()
        for slot, (slot_time, sender) in slots.items():
            state = slot_state(now, slot_time, SLOT_GRACE[slot], sent.get(slot))
            if state is None:
                continue
            if state == "expire":
                log.warning("расписание: %s за %s пропущено (простой дольше грейса)", slot, today_iso)
                sent[slot] = today_iso
                save_sched(sent)
                continue
            key = (slot, today_iso)
            try:
                await sender(bot)
                sent[slot] = today_iso
                save_sched(sent)
                fails.pop(key, None)
                log.info("расписание: %s отправлено", slot)
            except Exception:  # noqa: BLE001 — расписание не должно умирать
                fails[key] = fails.get(key, 0) + 1
                log.exception("расписание: %s не отправилось (попытка %d)", slot, fails[key])
                if fails[key] >= 3:
                    sent[slot] = today_iso
                    save_sched(sent)
                    log.error("расписание: %s — сдаюсь до завтра", slot)
        await asyncio.sleep(30)

# ---------------------------------------------------------------- Telegram

class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if event.from_user is None or event.from_user.id != OWNER_TELEGRAM_ID:
            uid = event.from_user.id if event.from_user else "?"
            log.info("чужое сообщение от id=%s — молча игнорирую", uid)
            return None
        if event.message_id in _seen_ids:
            log.info("дедуп: сообщение %d уже обработано — пропускаю", event.message_id)
            return None
        return await handler(event, data)


dp = Dispatcher()
dp.message.middleware(OwnerOnlyMiddleware())


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Кидай текст или голос — разберу и разложу: задачи в Inbox, напоминания, идеи.\n"
        "Можно несколько пунктов одним сообщением.\n"
        "«отмени» — убрать последнюю запись или всю последнюю пачку.\n"
        "«неделя» — результаты и фокусы. /ping — жив ли."
    )


@dp.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("жив")


@dp.message(F.voice | F.audio | F.video_note)
async def on_voice(message: Message, bot: Bot) -> None:
    media = message.voice or message.audio or message.video_note
    try:
        transcript = await transcribe_voice(bot, media.file_id)
    except Exception as e:  # noqa: BLE001
        log.exception("расшифровка не удалась")
        await message.answer(f"⚠️ Не сохранил (расшифровка не удалась: {_reason(e)}). Отправь текстом.")
        return
    if not transcript:
        await message.answer("⚠️ Не расслышал — в голосовом нет слов. Отправь ещё раз.")
        return
    await handle_text_like(message, transcript, transcript=transcript)


@dp.message(F.text)
async def on_text(message: Message) -> None:
    await handle_text_like(message, message.text)


@dp.message()
async def on_other(message: Message) -> None:
    await message.answer("Пока понимаю только текст и голос")


async def main() -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    me = await bot.get_me()
    log.info("старт: @%s, модель %s", me.username, ANTHROPIC_MODEL)
    load_seen()
    try:
        anchor = await retry3("notion-anchor", get_ideas_anchor)
        log.info("якорь страницы идей: %s", anchor)
        await get_goals_text()
        log.info("страница целей загружена, кэш 30 мин")
    except Exception:
        log.exception("прогрев кэша не удался — продолжу, подтяну при первом сообщении")
    # ссылку держим, иначе задачу может убрать сборщик мусора
    scheduler_task = asyncio.create_task(scheduler_loop(bot))
    try:
        # только message; handle_as_tasks=False: апдейты строго по одному и offset
        # подтверждается после обработки — рестарт не теряет сообщение (дедуп
        # закрывает обратную сторону — повторную доставку уже обработанного)
        await dp.start_polling(bot, allowed_updates=["message"], handle_as_tasks=False)
    finally:
        scheduler_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
