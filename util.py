"""Утилиты: ретраи, ошибки, текст/markdown, числа, даты."""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from anthropic import APIConnectionError, APIStatusError

from config import MSK, RETRY_DELAYS, STATE_DIR, TITLE_LIMIT, TOGGLE_CHUNK, log

# ---------------------------------------------------------------- ошибки и ретраи

def short_error(e: Exception) -> str:
    text = str(e) or type(e).__name__
    return text[:120]


class Truncated(RuntimeError):
    """Ответ модели обрезан по max_tokens — повторять бессмысленно."""


def reason(e: Exception) -> str:
    """Короткая причина для ответа в Telegram, без внутренностей."""
    mapping = {
        "TimeoutException": "таймаут",
        "ConnectTimeout": "таймаут",
        "ReadTimeout": "таймаут",
        "ConnectError": "нет связи",
        "APIConnectionError": "нет связи",
        "APITimeoutError": "таймаут",
    }
    if isinstance(e, Truncated):
        return "ответ не уместился"
    if isinstance(e, httpx.HTTPStatusError):
        return f"ошибка API {e.response.status_code}"
    if isinstance(e, APIStatusError):
        return f"ошибка API {e.status_code}"
    if isinstance(e, RuntimeError) and str(e):
        return str(e)[:60]
    return mapping.get(type(e).__name__, "внутренняя ошибка")


def retryable_read(e: Exception) -> bool:
    """Идемпотентные вызовы: повторяем сетевые сбои, 429 и 5xx. 4xx — нет.
    Обрезку по max_tokens и таймаут SDK не повторяем: детерминированы/долги."""
    if isinstance(e, Truncated):
        return False
    if type(e).__name__ == "APITimeoutError":
        return False
    if isinstance(e, (httpx.TransportError, APIConnectionError)):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return code == 429 or code >= 500
    if isinstance(e, APIStatusError):
        return e.status_code == 429 or e.status_code >= 500
    return isinstance(e, RuntimeError)  # пустой tool_use и т.п.


def retryable_write(e: Exception) -> bool:
    """Неидемпотентная запись в Notion: повторяем только когда запрос
    гарантированно не был принят — ошибки соединения и 429."""
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429


async def retry3(name: str, fn, retryable=retryable_read):
    """3 ретрая после первой попытки, паузы 1/5/15 секунд между вызовами."""
    last = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 — политику решает retryable
            last = e
            if not retryable(e):
                log.warning("%s: не повторяю (%s)", name, short_error(e))
                raise
            log.warning("%s: попытка %d не удалась: %s", name, attempt + 1, short_error(e))
            if attempt < len(RETRY_DELAYS):
                await asyncio.sleep(RETRY_DELAYS[attempt])
    raise last

# ---------------------------------------------------------------- файлы состояния

def atomic_write_json(path: Path, payload) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False))
    os.replace(tmp, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — битое/отсутствующее состояние = дефолт
        return default

# ---------------------------------------------------------------- текст

def chunk_text(text: str, size: int = TOGGLE_CHUNK) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def clean_title(title: str) -> str:
    """Мягкий лимит названия: режем ТОЛЬКО по границе слова, без «…и какой обр»."""
    title = " ".join((title or "Без названия").split())
    if len(title) <= TITLE_LIMIT:
        return title
    cut = title[:TITLE_LIMIT + 1]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(",;:—- ") + "…"


def normalize_cmd(text: str) -> str:
    return re.sub(r"[.,!?…«»\"']+", "", (text or "").strip().lower()).strip()


def plural(n: int, forms: tuple[str, str, str]) -> str:
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
    iso = (iso or "").split("T")[0]
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


def similar_title(title: str, existing: list[str], threshold: float = 0.72) -> str | None:
    """Самое похожее активное название (для пометки «похоже на существующую»)."""
    t = title.lower()
    best, best_score = None, 0.0
    for ex in existing:
        e = ex.lower()
        if t == e:
            return ex
        score = difflib.SequenceMatcher(None, t, e).ratio()
        if (t in e or e in t) and min(len(t), len(e)) >= 12:
            score = max(score, 0.8)
        if score > best_score:
            best, best_score = ex, score
    return best if best_score >= threshold else None


def parse_team(goals_text: str) -> list[str]:
    """Люди из раздела «Команда» страницы Целей: строки «Имя — описание».
    Имя может быть с уточнением в скобках: «Игорь (Финансы)»."""
    people: list[str] = []
    in_team = False
    for line in (goals_text or "").splitlines():
        s = line.strip().lstrip("- ").strip()
        low = s.lower()
        if not in_team:
            if low.startswith("команда"):
                in_team = True
            continue
        if not s:
            continue
        m = re.match(r"([А-ЯЁA-Z][\w.ёЁ-]*(?:\s*\([^)]{1,30}\))?)\s*[—–-]\s+\S", s)
        if m:
            name = " ".join(m.group(1).split())
            if name not in people:
                people.append(name)
        elif people:
            # строка не похожа на «Имя — …» и раздел уже читали — секция кончилась
            if not re.match(r"[А-ЯЁ]", s) or len(s.split()) > 6 or "—" not in s:
                break
    return people
