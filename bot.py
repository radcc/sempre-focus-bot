#!/usr/bin/env python3
"""Sempre Focus Bot — Telegram → Notion Inbox (v1).

Текст/голос от Алексея → Groq Whisper (голос) → Claude Haiku (разбор)
→ Notion (карточка задачи или блок в «Идеи») → ответ одной строкой.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from anthropic import AsyncAnthropic
from dotenv import dotenv_values

# ---------------------------------------------------------------- конфиг

ENV_PATH = Path(__file__).resolve().parent / ".env"
_env = {**dotenv_values(ENV_PATH)}

def _required(key: str) -> str:
    value = (_env.get(key) or "").strip()
    if not value:
        print(f"FATAL: в .env не задан {key}", file=sys.stderr)
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

MSK = ZoneInfo("Europe/Moscow")
NOTION_VERSION = "2025-09-03"
GOALS_CACHE_TTL = 30 * 60          # 30 минут
VOICE_TOGGLE_THRESHOLD = 200       # символов расшифровки для toggle
RETRY_DELAYS = (1, 5, 15)

TAGS = ["Продажи", "Маркетинг", "Производство", "Финансы", "Личное", "Управление"]
PEOPLE = ["Я", "Сона", "Саша", "Игорь", "Камила", "Настя", "Подрядчик"]
RECOMMENDATIONS = ["🟢 Сам", "🔁 Делегировать", "❌ Не сейчас"]
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

class _MaskTokenFormatter(logging.Formatter):
    """Маскирует токен бота в любых логах, включая трейсбеки
    (aiogram при ошибке скачивания файла включает URL с токеном)."""

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(TELEGRAM_BOT_TOKEN, "***TOKEN***")


_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(_MaskTokenFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("sempre-bot")

anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
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

async def retry3(name: str, fn, retryable=None):
    """3 ретрая после первой попытки, паузы 1/5/15 секунд между вызовами.

    retryable(e) -> False останавливает повторы сразу (для неидемпотентных записей).
    """
    last = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 — ретраим любую ошибку внешнего API
            last = e
            if retryable is not None and not retryable(e):
                log.warning("%s: не повторяю (%s)", name, _short_error(e))
                raise
            log.warning("%s: попытка %d не удалась: %s", name, attempt + 1, _short_error(e))
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
    raise last


def _write_retryable(e: Exception) -> bool:
    """Запись в Notion повторяем, только если запрос точно не был принят:
    ошибки соединения и 4xx (отказ без записи). ReadTimeout и 5xx после
    возможного коммита не повторяем — иначе дубли карточек."""
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500


def _short_error(e: Exception) -> str:
    text = str(e) or type(e).__name__
    return text[:120]


def _reason(e: Exception) -> str:
    """Короткая причина для ответа в Telegram, без внутренностей."""
    name = type(e).__name__
    mapping = {
        "TimeoutException": "таймаут",
        "ConnectTimeout": "таймаут",
        "ReadTimeout": "таймаут",
        "ConnectError": "нет связи",
    }
    if isinstance(e, httpx.HTTPStatusError):
        return f"ошибка API {e.response.status_code}"
    return mapping.get(name, name)

# ---------------------------------------------------------------- markdown → Notion

def chunk_text(text: str, size: int = 1900) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def md_rich(text: str, prefix_bold: str = "") -> list[dict]:
    """Rich text с поддержкой **жирного**; куски не длиннее 1900 символов."""
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
    """Простой markdown → блоки: строки-списки и абзацы."""
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
    """Секция вида «**Метка:** текст» — однострочная инлайном, многострочная списком."""
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

# ---------------------------------------------------------------- Notion: цели (кэш 30 мин)

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
        log.warning("цели: не обновились, использую кэш: %s", _short_error(e))
    return _goals_cache["text"]

# ---------------------------------------------------------------- Notion: якорь на странице идей

_ideas_anchor: dict = {"id": None}


async def get_ideas_anchor() -> str:
    """Id первого блока страницы «Идеи» (callout-инструкция), кэшируется навсегда."""
    if _ideas_anchor["id"]:
        return _ideas_anchor["id"]
    r = await notion.get(f"/blocks/{NOTION_IDEAS_PAGE_ID}/children", params={"page_size": 1})
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise RuntimeError("страница «Идеи» пуста — нет блока-инструкции")
    _ideas_anchor["id"] = results[0]["id"]
    return _ideas_anchor["id"]

# ---------------------------------------------------------------- Claude: разбор

SAVE_TOOL = {
    "name": "save_entry",
    "description": "Сохранить разобранную запись (задачу или идею).",
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["task", "idea"]},
            "title": {"type": "string", "description": "Короткий заголовок с глаголом, до 80 символов"},
            "tag": {"type": "string", "enum": TAGS},
            "who": {"type": "string", "enum": PEOPLE},
            "deadline_iso": {"type": ["string", "null"], "description": "YYYY-MM-DD или null"},
            "goal_score": {"type": "integer", "minimum": 0, "maximum": 3},
            "ceo_score": {"type": "integer", "minimum": 0, "maximum": 3},
            "recommendation": {"type": "string", "enum": RECOMMENDATIONS},
            "summary_md": {"type": "string", "description": "Суть в 1–2 строки"},
            "context_md": {"type": ["string", "null"]},
            "next_step": {"type": ["string", "null"]},
            "analysis_md": {"type": "string", "description": "2–4 строки: почему такие оценки, кому делегировать"},
            "reply_line": {"type": "string"},
        },
        "required": ["type", "title", "tag", "who", "deadline_iso", "goal_score",
                     "ceo_score", "recommendation", "summary_md", "context_md",
                     "next_step", "analysis_md", "reply_line"],
    },
}


def build_system_prompt(goals: str) -> str:
    now = datetime.now(MSK)
    return f"""Ты — разборщик входящих для Алексея, основателя контент-агентства Sempre. \
Преврати сообщение в структурированную запись через инструмент save_entry.

Сейчас: {WEEKDAYS_RU[now.weekday()]}, {now.strftime("%d.%m.%Y %H:%M")} (Москва).

=== Страница «🎯 Цели» (цель года, фокусы 90 дней, команда, правила CEO-оценки) ===
{goals}
=== Конец страницы целей ===

Правила:
- type="idea": мысли, концепции, «а что если» — без конкретного действия. Префикс «идея:» во входе — принудительно идея. Всё остальное — task.
- title: короткий заголовок с глаголом, до 80 символов.
- goal_score (→Цель): насколько задача двигает к фокусам 90 дней со страницы целей. 3 — прямой рычаг, 0 — не двигает.
- ceo_score (CEO): чья это работа по правилам CEO-оценки со страницы целей. 3 — только основатель, 0 — точно не он.
- recommendation: важная задача с ceo_score ≤ 1 → всегда «🔁 Делегировать» (в analysis_md напиши, кому из команды). goal_score = 0 и нет срочности → «❌ Не сейчас». Иначе — «🟢 Сам».
- «Передал/поручил {{имя}}», «жду от {{имя}}» → who = это имя; названная дата проверки → deadline_iso.
- deadline_iso: только если дата или срок явно следуют из текста. Относительные даты («завтра», «в пятницу», «через неделю») переведи в конкретную дату по Москве, формат YYYY-MM-DD.
- summary_md: суть в 1–2 строки. context_md: структурированный контекст — только если во входе есть детали, иначе null. next_step: конкретное следующее действие или null.
- analysis_md: 2–4 строки — почему такие оценки и кому делегировать.
- Стиль всех текстов: минимализм, по-русски, без воды и сложных слов. Короткий вход («оплатить HR завтра») не раздувай: context_md = null, next_step = null."""


async def classify(input_text: str) -> dict:
    goals = await get_goals_text()

    async def _call():
        resp = await anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4000,
            system=build_system_prompt(goals),
            messages=[{"role": "user", "content": input_text}],
            tools=[SAVE_TOOL],
            tool_choice={"type": "tool", "name": "save_entry"},
        )
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("ответ Claude обрезан по max_tokens")
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError("Claude не вернул tool_use")

    entry = await retry3("claude", _call)
    return normalize_entry(entry)


def normalize_entry(entry: dict) -> dict:
    entry["type"] = entry.get("type") if entry.get("type") in ("task", "idea") else "task"
    entry["title"] = (entry.get("title") or "Без названия").strip()[:80]
    if entry.get("tag") not in TAGS:
        entry["tag"] = "Личное"
    if entry.get("who") not in PEOPLE:
        entry["who"] = "Я"
    if entry.get("recommendation") not in RECOMMENDATIONS:
        entry["recommendation"] = "🟢 Сам"
    deadline = entry.get("deadline_iso")
    if deadline and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(deadline)):
        entry["deadline_iso"] = None
    for key in ("goal_score", "ceo_score"):
        try:
            entry[key] = max(0, min(3, int(entry.get(key, 0))))
        except (TypeError, ValueError):
            entry[key] = 0
    return entry

# ---------------------------------------------------------------- Notion: запись

async def create_task_page(entry: dict, transcript: str | None) -> None:
    properties = {
        "Name": {"title": [{"text": {"content": entry["title"]}}]},
        "Статус": {"select": {"name": STATUS_INBOX}},
        "Тег": {"select": {"name": entry["tag"]}},
        "Кто": {"select": {"name": entry["who"]}},
        "Рекомендация": {"select": {"name": entry["recommendation"]}},
    }
    if entry.get("deadline_iso"):
        properties["Когда"] = {"date": {"start": entry["deadline_iso"]}}

    children: list[dict] = []
    summary = (entry.get("summary_md") or "").strip()
    if summary and summary.lower() != entry["title"].lower():
        children.extend(md_blocks(summary))
    if entry.get("context_md"):
        children.extend(section_blocks("Контекст", entry["context_md"]))
    if entry.get("next_step"):
        children.extend(section_blocks("Следующий шаг", entry["next_step"]))

    analysis_head = f"→Цель {entry['goal_score']}/3 · CEO {entry['ceo_score']}/3 · {entry['recommendation']}"
    children.append(paragraph(md_rich(analysis_head, prefix_bold="🤖 Анализ: ")))
    children.extend(md_blocks(entry.get("analysis_md") or ""))

    children = cap_blocks(children, 99)
    if transcript and len(transcript) > VOICE_TOGGLE_THRESHOLD:
        children.append(toggle_block("Расшифровка", transcript))

    async def _create():
        r = await notion.post("/pages", json={
            "parent": {"type": "data_source_id", "data_source_id": NOTION_DATA_SOURCE_ID},
            "properties": properties,
            "children": children,
        })
        r.raise_for_status()

    await retry3("notion-task", _create, retryable=_write_retryable)


async def append_idea(entry: dict, source_text: str, is_voice: bool) -> None:
    today = datetime.now(MSK).strftime("%d.%m")
    blocks: list[dict] = [paragraph(md_rich("", prefix_bold=f"{today} — {entry['title']}"))]

    if is_voice and len(source_text) > VOICE_TOGGLE_THRESHOLD:
        summary = (entry.get("summary_md") or "").strip() or entry["title"]
        blocks.extend(md_blocks(summary))
        blocks = cap_blocks(blocks, 99)
        blocks.append(toggle_block("Расшифровка", source_text))
    else:
        body = source_text.strip()
        if body and body.lower() != entry["title"].lower():
            blocks.extend(md_blocks(body))
        blocks = cap_blocks(blocks, 100)

    async def _append():
        anchor = await get_ideas_anchor()
        r = await notion.patch(f"/blocks/{NOTION_IDEAS_PAGE_ID}/children",
                               json={"children": blocks, "after": anchor})
        if r.status_code in (400, 404):
            # якорь мог протухнуть (блок-инструкцию переделали) — перечитаем при повторе
            _ideas_anchor["id"] = None
        r.raise_for_status()

    await retry3("notion-idea", _append, retryable=_write_retryable)

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

# ---------------------------------------------------------------- Telegram

def forward_source(message: Message) -> str | None:
    origin = message.forward_origin
    if origin is None:
        return None
    if getattr(origin, "sender_user", None):
        user = origin.sender_user
        name = " ".join(filter(None, [user.first_name, user.last_name])) or user.username or "?"
        return name
    if getattr(origin, "sender_user_name", None):
        return origin.sender_user_name
    chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    if chat is not None:
        return chat.title or chat.username or "?"
    return "?"


def build_reply(entry: dict) -> str:
    if entry["type"] == "idea":
        return f"💡 Идея: {entry['title']}"
    reply = f"✅ Inbox: {entry['title']}"
    if entry.get("deadline_iso"):
        try:
            reply += f" · 📅 {date.fromisoformat(entry['deadline_iso']).strftime('%d.%m')}"
        except ValueError:
            pass
    if entry["who"] != "Я":
        reply += f" · 👤 {entry['who']}"
    return reply


async def process_text(message: Message, text: str, transcript: str | None = None) -> None:
    """Общий путь: текст (или расшифровка) → Claude → Notion → ответ."""
    source = forward_source(message)
    input_text = f"Переслано от {source}:\n{text}" if source else text
    if transcript is not None:
        input_text = f"(голосовое сообщение, расшифровка)\n{input_text}"
    try:
        entry = await classify(input_text)
        if entry["type"] == "idea":
            await append_idea(entry, text, is_voice=transcript is not None)
        else:
            await create_task_page(entry, transcript)
        log.info("сохранено: %s «%s»", entry["type"], entry["title"])
    except Exception as e:  # noqa: BLE001 — любой сбой не должен терять текст
        log.exception("не сохранил запись")
        await message.answer(f"⚠️ Не сохранил ({_reason(e)}). Текст ниже — кинь позже:")
        for chunk in chunk_text(text, 4000):
            await message.answer(chunk)
        return
    # запись уже в Notion: сбой подтверждения не должен звучать как «не сохранил»
    try:
        await message.answer(build_reply(entry))
    except Exception:  # noqa: BLE001
        log.exception("сохранено, но подтверждение в Telegram не отправилось")


class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if event.from_user is None or event.from_user.id != OWNER_TELEGRAM_ID:
            uid = event.from_user.id if event.from_user else "?"
            log.info("чужое сообщение от id=%s — отказ", uid)
            await event.answer("Это личный бот")
            return None
        return await handler(event, data)


dp = Dispatcher()
dp.message.middleware(OwnerOnlyMiddleware())


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Кидай текст или голосовое — разберу и положу в Notion Inbox.\n"
        "«идея: …» — на страницу Идей.\n"
        "/ping — проверить, жив ли я."
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
    await process_text(message, transcript, transcript=transcript)


@dp.message(F.text)
async def on_text(message: Message) -> None:
    await process_text(message, message.text)


@dp.message()
async def on_other(message: Message) -> None:
    await message.answer("Пока понимаю только текст и голос")


async def main() -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    me = await bot.get_me()
    log.info("старт: @%s, модель %s", me.username, ANTHROPIC_MODEL)
    try:
        anchor = await retry3("notion-anchor", get_ideas_anchor)
        log.info("якорь страницы идей: %s", anchor)
        await get_goals_text()
        log.info("страница целей загружена, кэш 30 мин")
    except Exception:
        log.exception("прогрев кэша не удался — продолжу, подтяну при первом сообщении")
    # только message: отредактированные и прочие апдейты игнорируем
    await dp.start_polling(bot, allowed_updates=["message"])


if __name__ == "__main__":
    asyncio.run(main())
