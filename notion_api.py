"""Все операции с Notion: карточки базы, страницы Целей/Идей/Памяти, карточка багов."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

from config import (CACHE_RETRY_PENALTY, CACHE_TTL, MSK, NOTION_BUGS_CARD_ID,
                    NOTION_DATA_SOURCE_ID, NOTION_GOALS_PAGE_ID, NOTION_IDEAS_PAGE_ID,
                    NOTION_MEMORY_PAGE_ID, STATUS_ARCHIVE, STATUS_DONE, STATUS_INBOX,
                    STATUS_REMINDER, STATUSES, TAGS, notion, log)
from util import chunk_text, retry3, retryable_write, short_error

# ---------------------------------------------------------------- markdown → блоки

def md_rich(text: str, prefix_bold: str = "") -> list[dict]:
    rich: list[dict] = []
    if prefix_bold:
        rich.append({"type": "text", "text": {"content": prefix_bold},
                     "annotations": {"bold": True}})
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
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": md_rich(line[2:])}})
        elif line.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": md_rich(line[4:])}})
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
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": [{"type": "text", "text": {"content": title}}],
                       "children": [paragraph([{"type": "text", "text": {"content": c}}])
                                    for c in chunk_text(text)]}}

# ---------------------------------------------------------------- чтение страниц (кэш 10 мин)

_page_cache: dict = {}  # key -> {"text": str, "ts": float}


async def _fetch_children(block_id: str, depth: int = 0, with_ids: bool = False,
                          skip_toggles: bool = False, max_blocks: int = 0) -> list[str]:
    if depth > 2:
        return []
    lines: list[str] = []
    cursor = None
    count = 0
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        r = await notion.get(f"/blocks/{block_id}/children", params=params)
        r.raise_for_status()
        data = r.json()
        for b in data.get("results", []):
            count += 1
            if max_blocks and depth == 0 and count > max_blocks:
                lines.append("…(дальше обрезано)")
                return lines
            btype = b.get("type", "")
            payload = b.get(btype, {}) or {}
            rich = payload.get("rich_text", [])
            text = "".join(x.get("plain_text", "") for x in rich).strip()
            struck = bool(rich) and all(
                (x.get("annotations") or {}).get("strikethrough") for x in rich)
            if struck and not with_ids:
                continue  # зачёркнутое = устаревшее: агенту и сводкам не показываем
            if btype == "to_do" and text:
                text = ("[x] " if payload.get("checked") else "[ ] ") + text
            if text:
                marker = "- " if "list_item" in btype or btype == "to_do" else ""
                prefix = f"[{b['id']}]{'[зачёркнуто]' if struck else ''} " if with_ids else ""
                lines.append(prefix + "  " * depth + marker + text)
            if skip_toggles and btype == "toggle":
                continue  # расшифровки и прочие «спрятанные» простыни не тащим
            if b.get("has_children") and btype not in ("child_page", "child_database"):
                lines.extend(await _fetch_children(b["id"], depth + 1, with_ids,
                                                   skip_toggles, 0))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return lines


async def page_text(page_id: str, cache_key: str | None = None, fresh: bool = False,
                    with_ids: bool = False, skip_toggles: bool = False,
                    max_blocks: int = 0) -> str:
    """Текст страницы; кэш ≤10 минут по cache_key (fresh=True — мимо кэша)."""
    now = asyncio.get_event_loop().time()
    entry = _page_cache.get(cache_key) if cache_key else None
    if entry and not fresh and not with_ids and now - entry["ts"] < CACHE_TTL:
        return entry["text"]
    try:
        lines = await retry3(f"notion-page:{cache_key or page_id[:8]}",
                             lambda: _fetch_children(page_id, with_ids=with_ids,
                                                     skip_toggles=skip_toggles,
                                                     max_blocks=max_blocks))
        text = "\n".join(lines)
        if cache_key and not with_ids:
            _page_cache[cache_key] = {"text": text, "ts": now}
        return text
    except Exception as e:
        if entry is None:
            raise
        _page_cache[cache_key]["ts"] = now - CACHE_TTL + CACHE_RETRY_PENALTY
        log.warning("страница %s: не обновилась, кэш: %s", cache_key, short_error(e))
        return entry["text"]


async def get_goals_text(fresh: bool = False) -> str:
    return await page_text(NOTION_GOALS_PAGE_ID, "goals", fresh=fresh)


async def get_memory_text(fresh: bool = False) -> str:
    try:
        # toggle-расшифровки рефлексий остаются в Notion, но контекст не раздувают
        return await page_text(NOTION_MEMORY_PAGE_ID, "memory", fresh=fresh,
                               skip_toggles=True, max_blocks=300)
    except Exception as e:  # noqa: BLE001 — память может быть ещё не расшарена
        log.warning("память: недоступна (%s) — работаю без неё", short_error(e))
        return ""


async def goals_last_edited() -> datetime | None:
    r = await notion.get(f"/pages/{NOTION_GOALS_PAGE_ID}")
    if r.status_code != 200:
        return None
    iso = r.json().get("last_edited_time")
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MSK) if iso else None

# ---------------------------------------------------------------- база: запросы

async def query_pages(filter_: dict, page_size: int = 100, max_pages: int = 5) -> list[dict]:
    results: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        body: dict = {"filter": filter_, "page_size": page_size}
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


def page_brief(page: dict) -> dict:
    """Компактное представление карточки для агента и сводок."""
    props = page.get("properties", {})

    def sel(name):
        v = props.get(name, {}).get("select")
        return v["name"] if v else None

    when = props.get("Когда", {}).get("date") or {}
    return {"id": page["id"], "title": page_title(page), "status": sel("Статус"),
            "tag": sel("Тег"), "who": sel("Кто"), "when": when.get("start")}


ACTIVE_NOT = [STATUS_DONE, STATUS_ARCHIVE]


def _not_status_filter(excluded: list[str]) -> list[dict]:
    return [{"property": "Статус", "select": {"does_not_equal": s}} for s in excluded]


async def find_cards(query: str = "", who: str | None = None, status: str | None = None,
                     tag: str | None = None, only_active: bool = True,
                     limit: int = 15) -> list[dict]:
    conds: list[dict] = []
    if query:
        conds.append({"property": "Name", "title": {"contains": query}})
    if who:
        conds.append({"property": "Кто", "select": {"equals": who}})
    if status:
        conds.append({"property": "Статус", "select": {"equals": status}})
    if tag:
        conds.append({"property": "Тег", "select": {"equals": tag}})
    if only_active and not status:
        conds.extend(_not_status_filter(ACTIVE_NOT))
    filter_ = {"and": conds} if conds else {"property": "Статус",
                                            "select": {"is_not_empty": True}}
    pages = await retry3("notion-find", lambda: query_pages(filter_))
    return [page_brief(p) for p in pages[:limit]]


_active_titles_cache: dict = {"titles": [], "ts": 0.0}


async def active_titles() -> list[str]:
    """Названия активных карточек — для пометки «похоже на существующую»."""
    now = asyncio.get_event_loop().time()
    if now - _active_titles_cache["ts"] < CACHE_TTL:
        return _active_titles_cache["titles"]
    try:
        pages = await query_pages({"and": _not_status_filter(ACTIVE_NOT + [STATUS_REMINDER])},
                                  max_pages=2)
        _active_titles_cache.update({"titles": [page_title(p) for p in pages], "ts": now})
    except Exception as e:  # noqa: BLE001 — пометка о дублях не критична
        log.warning("активные названия: %s", short_error(e))
    return _active_titles_cache["titles"]


async def reminders_for(iso_date: str) -> list[dict]:
    """Напоминания со сроком ≤ даты: просроченные из-за простоя не теряются."""
    filter_ = {"and": [
        {"property": "Статус", "select": {"equals": STATUS_REMINDER}},
        {"property": "Когда", "date": {"on_or_before": iso_date}},
    ]}
    return await retry3("notion-reminders", lambda: query_pages(filter_))


async def due_timed_reminders() -> list[dict]:
    """Напоминания с точным временем, срок которых наступил."""
    now_iso = datetime.now(MSK).isoformat(timespec="seconds")
    filter_ = {"and": [
        {"property": "Статус", "select": {"equals": STATUS_REMINDER}},
        {"property": "Когда", "date": {"on_or_before": now_iso}},
    ]}
    pages = await retry3("notion-due", lambda: query_pages(filter_))
    return [p for p in pages
            if "T" in ((p["properties"].get("Когда", {}).get("date") or {}).get("start") or "")]


async def count_created_today() -> int:
    midnight = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    filter_ = {"timestamp": "created_time", "created_time": {"on_or_after": midnight.isoformat()}}
    return len(await retry3("notion-count", lambda: query_pages(filter_)))


async def count_done_today() -> int:
    midnight = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    filter_ = {"and": [
        {"property": "Статус", "select": {"equals": STATUS_DONE}},
        {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": midnight.isoformat()}},
    ]}
    return len(await retry3("notion-done", lambda: query_pages(filter_)))


async def stale_from_last_week() -> list[dict]:
    """Не закрытое с прошлой недели: созданные ИЛИ помеченные (Когда) на прошлой
    неделе, без запланированных на будущее — они живут на доске нормально."""
    today = datetime.now(MSK).date()
    monday = today - timedelta(days=today.weekday())
    prev_monday = monday - timedelta(days=7)
    excl = _not_status_filter(ACTIVE_NOT + [STATUS_REMINDER])
    created_f = {"and": [
        {"timestamp": "created_time",
         "created_time": {"on_or_after": datetime.combine(prev_monday, datetime.min.time(),
                                                          tzinfo=MSK).isoformat()}},
        {"timestamp": "created_time",
         "created_time": {"before": datetime.combine(monday, datetime.min.time(),
                                                     tzinfo=MSK).isoformat()}},
        *excl,
    ]}
    when_f = {"and": [
        {"property": "Когда", "date": {"on_or_after": prev_monday.isoformat()}},
        {"property": "Когда", "date": {"before": monday.isoformat()}},
        *excl,
    ]}
    pages = await retry3("notion-stale", lambda: query_pages(created_f))
    pages += await retry3("notion-stale2", lambda: query_pages(when_f))
    seen: set[str] = set()
    briefs = []
    for p in pages:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        b = page_brief(p)
        when = (b.get("when") or "").split("T")[0]
        if when and when >= today.isoformat():
            continue  # запланировано на будущее — не «незакрытое»
        briefs.append(b)
    # просроченные по дате — первыми
    briefs.sort(key=lambda b: (b.get("when") or "9999-99-99"))
    return briefs


async def assignments() -> list[dict]:
    """Активные поручения: Кто ≠ Я, статус не Done/Архив."""
    filter_ = {"and": [
        {"property": "Кто", "select": {"does_not_equal": "Я"}},
        {"property": "Кто", "select": {"is_not_empty": True}},
        *_not_status_filter(ACTIVE_NOT),
    ]}
    pages = await retry3("notion-assign", lambda: query_pages(filter_))
    return [page_brief(p) for p in pages]


async def future_reminders(limit: int = 30) -> list[dict]:
    today = datetime.now(MSK).date().isoformat()
    filter_ = {"and": [
        {"property": "Статус", "select": {"equals": STATUS_REMINDER}},
        {"property": "Когда", "date": {"on_or_after": today}},
    ]}
    pages = await retry3("notion-future-rem", lambda: query_pages(filter_))
    briefs = [page_brief(p) for p in pages]
    briefs.sort(key=lambda b: b.get("when") or "")
    return briefs[:limit]

# ---------------------------------------------------------------- база: запись

async def create_card(properties: dict, children: list[dict] | None = None) -> str:
    body: dict = {"parent": {"type": "data_source_id", "data_source_id": NOTION_DATA_SOURCE_ID},
                  "properties": properties}
    if children:
        body["children"] = children

    async def _create():
        r = await notion.post("/pages", json=body)
        r.raise_for_status()
        return r.json()["id"]

    return await retry3("notion-create", _create, retryable=retryable_write)


def card_properties(title: str, status: str, tag: str | None = None, who: str | None = None,
                    when_iso: str | None = None, recommendation: str | None = None) -> dict:
    props: dict = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Статус": {"select": {"name": status}},
    }
    if tag:
        props["Тег"] = {"select": {"name": tag}}
    props["Кто"] = {"select": {"name": who or "Я"}}   # Кто=null не оставляем
    if when_iso:
        props["Когда"] = {"date": {"start": when_iso}}
    if recommendation:
        props["Рекомендация"] = {"select": {"name": recommendation}}
    return props


async def update_card(page_id: str, title: str | None = None, status: str | None = None,
                      tag: str | None = None, who: str | None = None,
                      when_iso: str | None = None) -> None:
    props: dict = {}
    if title:
        props["Name"] = {"title": [{"text": {"content": title}}]}
    if status:
        props["Статус"] = {"select": {"name": status}}
    if tag:
        props["Тег"] = {"select": {"name": tag}}
    if who:
        props["Кто"] = {"select": {"name": who}}
    if when_iso:
        props["Когда"] = {"date": {"start": when_iso}}
    if not props:
        return

    async def _update():
        r = await notion.patch(f"/pages/{page_id}", json={"properties": props})
        r.raise_for_status()

    await retry3("notion-update", _update)


async def append_card_blocks(page_id: str, blocks: list[dict]) -> list[str]:
    async def _append():
        r = await notion.patch(f"/blocks/{page_id}/children",
                               json={"children": cap_blocks(blocks, 100)})
        r.raise_for_status()
        return [b["id"] for b in r.json().get("results", [])]

    return await retry3("notion-append", _append, retryable=retryable_write)


async def archive_page(page_id: str) -> None:
    async def _archive():
        r = await notion.patch(f"/pages/{page_id}", json={"archived": True})
        r.raise_for_status()

    await retry3("notion-archive", _archive)


async def set_page_status(page_id: str, status: str) -> None:
    await update_card(page_id, status=status)


async def set_page_date(page_id: str, iso: str) -> None:
    await update_card(page_id, when_iso=iso)


async def delete_block(block_id: str) -> None:
    async def _delete():
        r = await notion.delete(f"/blocks/{block_id}")
        if r.status_code == 404:
            return
        r.raise_for_status()

    await retry3("notion-delblock", _delete)


async def strike_block(block_id: str) -> None:
    """Зачеркнуть блок (пост «про это уже написал», пункт памяти)."""
    r = await notion.get(f"/blocks/{block_id}")
    r.raise_for_status()
    b = r.json()
    btype = b["type"]
    rich = (b.get(btype) or {}).get("rich_text", [])
    for x in rich:
        x.setdefault("annotations", {})["strikethrough"] = True

    async def _patch():
        rr = await notion.patch(f"/blocks/{block_id}", json={btype: {"rich_text": rich}})
        rr.raise_for_status()

    await retry3("notion-strike", _patch)

# ---------------------------------------------------------------- страница «Идеи»

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


async def insert_idea_blocks(blocks: list[dict], after_block: str | None = None) -> list[str]:
    async def _append():
        for attempt in (0, 1):
            anchor = after_block or await get_ideas_anchor()
            r = await notion.patch(f"/blocks/{NOTION_IDEAS_PAGE_ID}/children",
                                   json={"children": blocks, "after": anchor})
            if r.status_code in (400, 404) and attempt == 0 and not after_block:
                _ideas_anchor["id"] = None   # якорь протух — перечитать и повторить
                continue
            r.raise_for_status()
            return [b["id"] for b in r.json().get("results", [])]
        raise RuntimeError("вставка идеи не удалась")

    return await retry3("notion-idea", _append, retryable=retryable_write)

# ---------------------------------------------------------------- память и баги

async def append_memory(md: str, heading: str | None = None) -> list[str]:
    blocks = ([{"object": "block", "type": "heading_3",
                "heading_3": {"rich_text": md_rich(heading)}}] if heading else []) + md_blocks(md)

    async def _append():
        r = await notion.patch(f"/blocks/{NOTION_MEMORY_PAGE_ID}/children",
                               json={"children": cap_blocks(blocks, 100)})
        r.raise_for_status()
        return [b["id"] for b in r.json().get("results", [])]

    return await retry3("notion-memory", _append, retryable=retryable_write)


async def append_bug(text: str) -> list[str]:
    today = datetime.now(MSK).strftime("%d.%m")
    block = {"object": "block", "type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": md_rich(f"{today} — {text}")}}

    async def _append():
        r = await notion.patch(f"/blocks/{NOTION_BUGS_CARD_ID}/children",
                               json={"children": [block]})
        r.raise_for_status()
        return [b["id"] for b in r.json().get("results", [])]

    return await retry3("notion-bug", _append, retryable=retryable_write)


async def memory_available() -> bool:
    r = await notion.get(f"/pages/{NOTION_MEMORY_PAGE_ID}")
    return r.status_code == 200


def validate_status(status: str) -> str | None:
    for s in STATUSES:
        if s.lower() == (status or "").lower():
            return s
    return None


def validate_tag(tag: str) -> str | None:
    for t in TAGS:
        if t.lower() == (tag or "").lower():
            return t
    return None
