"""Расписание: 7:30 (утро), 19:00 пн–пт (вечер), пт 17:00 (рефлексия),
пн (компактизация памяти), полуминутный тик точных напоминаний."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from aiogram import Bot

from config import (EVENING_AT, MORNING_AT, MSK, NOTION_MEMORY_PAGE_ID,
                    REFLECTION_AT, SLOT_GRACE, STATE_DIR, STATUS_ARCHIVE,
                    STATUS_DONE, TIMED_REMINDER_LATE, log)
import llm
import notion_api
import state
from summaries import join_blocks
from util import atomic_write_json, human_date, read_json, short_error

MONDAY_TEXT = ("Понедельник. Обзор недели, 60–90 мин:\n"
               "блокнот → «Цели» (3 результата) → доска и делегированные → идеи → календарь.\n"
               "Открой Cowork: «прочитай STATUS.md, проведи обзор».")

SLOT_RETRY_COOLDOWN = timedelta(minutes=10)


def slot_state(now: datetime, slot_time: dtime, grace: timedelta,
               sent_date: str | None) -> str | None:
    """None — рано или уже отправлено; 'send' — пора; 'expire' — слот пропущен."""
    slot_dt = now.replace(hour=slot_time.hour, minute=slot_time.minute,
                          second=0, microsecond=0)
    if now < slot_dt or sent_date == now.date().isoformat():
        return None
    return "send" if now - slot_dt <= grace else "expire"


async def _reminder_context(page_id: str) -> str:
    """1–2 строки контекста из тела карточки; расшифровки голосовых не тащим."""
    try:
        text = await notion_api.page_text(page_id, skip_toggles=True, max_blocks=20)
        lines = [l.strip() for l in text.splitlines()
                 if l.strip() and not l.strip().lower().startswith("расшифровка")][:2]
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — контекст опционален
        return ""


async def _reminder_lines(pages: list[dict]) -> list[str]:
    today_iso = datetime.now(MSK).date().isoformat()
    lines = []
    for p in pages:
        title = notion_api.page_title(p)
        start = ((p["properties"].get("Когда", {}).get("date") or {}).get("start") or "")
        line = title
        if start and start.split("T")[0] < today_iso:
            line += f" (с {human_date(start)})"   # догнали после простоя
        ctx = await _reminder_context(p["id"])
        lines.append(line + (f"\n{ctx}" if ctx else ""))
    return lines


async def send_morning(bot: Bot, send) -> None:
    today = datetime.now(MSK)
    pages = await notion_api.reminders_for(today.date().isoformat())
    # напоминания с точным временем шлёт тик — утром только «дневные»
    pages = [p for p in pages
             if "T" not in ((p["properties"].get("Когда", {}).get("date") or {}).get("start") or "")]
    lines = await _reminder_lines(pages)

    if today.weekday() == 0:
        blocks = [MONDAY_TEXT]
        blocks.append(await _last_reflection_block())
        blocks.append(await _stale_block())
        blocks.append(await _goals_stale_warning())
        if lines:
            blocks.append("Сегодня:\n" + "\n".join(f"— {l}" for l in lines))
        await send(join_blocks(blocks))
    elif lines:
        await send(join_blocks(lines))
    else:
        log.info("утро: напоминаний нет — молчу")
        return
    for p in pages:
        try:
            await notion_api.set_page_status(p["id"], STATUS_ARCHIVE)
        except Exception:  # noqa: BLE001
            log.exception("утро: не смог перевести «%s» в Архив", notion_api.page_title(p))


async def _last_reflection_block() -> str:
    """Свежие «Итоги недели …» из памяти — для блока понедельника."""
    memory = await notion_api.get_memory_text(fresh=True)
    lines = memory.splitlines()
    start = None
    for i, l in enumerate(lines):
        if l.strip().lower().startswith("итоги недели"):
            start = i  # берём ПОСЛЕДНИЙ заголовок — самый свежий
    if start is None:
        return ""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", lines[start])
    if m:
        try:
            written = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                               tzinfo=MSK).date()
            if (datetime.now(MSK).date() - written).days > 8:
                return f"Итогов прошлой недели нет (последние — от {m.group(0)})."
        except ValueError:
            pass
    chunk = []
    for l in lines[start + 1:]:
        low = l.strip().lower()
        if low.startswith("итоги недели") or low.startswith("расшифровка"):
            break
        if l.strip():
            chunk.append(l.strip())
        if len(chunk) >= 5:
            break
    return ("Прошлая неделя:\n" + "\n".join(chunk)) if chunk else ""


async def _stale_block() -> str:
    try:
        cards = await notion_api.stale_from_last_week()
    except Exception:  # noqa: BLE001
        log.exception("утро: не собрал незакрытое")
        return ""
    if not cards:
        return ""
    lines = [f"— {c['title']}" for c in cards[:8]]
    if len(cards) > 8:
        lines.append(f"…и ещё {len(cards) - 8}")
    return "С прошлой недели не закрыто:\n" + "\n".join(lines) + "\nАктуально?"


async def _goals_stale_warning() -> str:
    try:
        edited = await notion_api.goals_last_edited()
    except Exception:  # noqa: BLE001
        return ""
    if edited is None:
        return ""
    today = datetime.now(MSK).date()
    prev_monday = today - timedelta(days=today.weekday() + 7)
    if edited.date() < prev_monday:
        return "Страница целей не обновлялась с прошлой недели — актуализируй."
    return ""


def _done_snapshot_file() -> Path:
    return STATE_DIR / "done_ids.json"


async def _count_closed_today() -> int:
    """«Закрыто» = Done сейчас минус Done из вчерашнего снапшота."""
    pages = await notion_api.query_pages(
        {"property": "Статус", "select": {"equals": STATUS_DONE}}, max_pages=3)
    now_ids = {p["id"] for p in pages}
    prev = set(read_json(_done_snapshot_file(), {"ids": []}).get("ids", []))
    atomic_write_json(_done_snapshot_file(), {"ids": sorted(now_ids)})
    if not prev:
        return await notion_api.count_done_today()   # первый запуск — старый прокси
    return len(now_ids - prev)


async def send_evening(bot: Bot, send) -> None:
    if datetime.now(MSK).weekday() >= 5:
        log.info("вечер: выходной — молчу")
        return
    try:
        created = await notion_api.count_created_today()
        done = await _count_closed_today()
        totals = f"Итог: занесено {created}, закрыто {done}."
    except Exception:  # noqa: BLE001
        log.exception("вечер: счётчики не собрались")
        totals = "Итог: не сосчитал."
    week_block = ""
    try:
        lines = await llm.week_lines_for_evening()
        if lines:
            week_block = "Неделя:\n" + "\n".join(f"{i}) {l}" for i, l in enumerate(lines, 1))
    except Exception:  # noqa: BLE001
        log.exception("вечер: сжатие недели не удалось")
    await send(join_blocks([
        "Алексей, рабочий день закончен.",
        totals,
        week_block,
        "Вечерний разбор, 10 мин: Inbox → 0, спланируй завтра.",
    ]))


def _next_monday_morning(now: datetime) -> datetime:
    days = (7 - now.weekday()) % 7 or 7
    target = (now + timedelta(days=days)).replace(hour=MORNING_AT.hour,
                                                  minute=MORNING_AT.minute,
                                                  second=0, microsecond=0)
    return target


async def send_reflection(bot: Bot, send) -> None:
    try:
        week = await llm.week_text(short=True)
    except Exception:  # noqa: BLE001
        log.exception("рефлексия: сжатие недели не удалось")
        week = ""
    blocks = ["Пятница."]
    if week:
        blocks.append("Цели недели были:\n" + week)
    blocks.append("Надиктуй голосом: что продвинулось, что нет, что переносим.")
    await send(join_blocks(blocks))
    # окно до понедельничного утра: ответ в воскресенье вечером — тоже ответ
    state.awaiting_reflection["until"] = _next_monday_morning(datetime.now(MSK))
    state.save_awaiting()


async def compact_memory(bot: Bot, send) -> None:
    """Пн после утреннего: компактизация памяти — тихо, только чтение+зачёркивание."""
    memory = await notion_api.get_memory_text(fresh=True)
    if not memory.strip():
        log.info("компактизация: память пуста или недоступна — пропускаю")
        return
    reply, _ = await llm.run_agent(
        "Компактизация памяти: вызови read_page(memory), найди явные дубли и устаревшие "
        "факты, зачеркни их strike_block (id — в квадратных скобках). Раздел «Профиль» "
        "не трогай. «Итоги недель» старше месяца можно зачеркнуть. Максимум 10 правок. "
        "Ответь одной строкой: что почистил.",
        mode="command",
        allowed_tools={"read_page", "strike_block"})
    log.info("компактизация: %s", reply[:200])


_timed_sent: set[str] = set()   # отправленные, но ещё не заархивированные


async def tick_timed_reminders(bot: Bot, send) -> None:
    """Точные напоминания: «напомни завтра в 15:00 …»."""
    try:
        pages = await notion_api.due_timed_reminders()
    except Exception as e:  # noqa: BLE001
        log.warning("тик напоминаний: запрос не удался: %s", short_error(e))
        return
    now = datetime.now(MSK)
    for p in pages:
        pid = p["id"]
        title = notion_api.page_title(p)
        if pid in _timed_sent:
            # уже отправляли — только дожимаем архивацию, без повторного пинга
            try:
                await notion_api.set_page_status(pid, STATUS_ARCHIVE)
                _timed_sent.discard(pid)
            except Exception:  # noqa: BLE001
                log.warning("тик: архивация «%s» снова не удалась", title)
            continue
        start = (p["properties"].get("Когда", {}).get("date") or {}).get("start") or ""
        try:
            due = datetime.fromisoformat(start).astimezone(MSK)
        except ValueError:
            due = now
        line = f"⏰ {title}"
        if now - due > TIMED_REMINDER_LATE:
            line += f" (было на {due.strftime('%H:%M %d.%m')})"
        ctx = await _reminder_context(pid)
        try:
            await send(join_blocks([line, ctx]))
        except Exception:  # noqa: BLE001
            log.exception("тик: не отправил «%s»", title)
            continue
        _timed_sent.add(pid)
        try:
            await notion_api.set_page_status(pid, STATUS_ARCHIVE)
            _timed_sent.discard(pid)
        except Exception:  # noqa: BLE001
            log.exception("тик: отправил, но не заархивировал «%s» — дожму на следующем тике", title)


async def scheduler_loop(bot: Bot, send) -> None:
    """Полуминутный тик: слоты с отметками на диске + точные напоминания.
    Сбой слота → повтор через кулдаун, пока не истечёт грейс (не сдаёмся за 90 секунд)."""
    sent = state.load_sched()
    next_try: dict = {}
    log.info("расписание: утро %s, вечер %s (пн–пт), рефлексия пт %s; отметки: %s",
             MORNING_AT, EVENING_AT, REFLECTION_AT, sent or "-")
    while True:
        now = datetime.now(MSK)
        today_iso = now.date().isoformat()
        slots: list[tuple[str, dtime, object]] = [
            ("morning", MORNING_AT, send_morning),
            ("evening", EVENING_AT, send_evening),
        ]
        if now.weekday() == 4:
            slots.append(("reflection", REFLECTION_AT, send_reflection))
        if now.weekday() == 0:
            slots.append(("compact", dtime(7, 45), compact_memory))
        for slot, slot_time, sender in slots:
            st = slot_state(now, slot_time, SLOT_GRACE[slot], sent.get(slot))
            if st is None:
                continue
            if st == "expire":
                log.warning("расписание: %s за %s пропущено (простой/сбои дольше грейса)",
                            slot, today_iso)
                sent[slot] = today_iso
                state.save_sched(sent)
                continue
            if next_try.get(slot) and now < next_try[slot]:
                continue
            try:
                await sender(bot, send)
                sent[slot] = today_iso
                state.save_sched(sent)
                next_try.pop(slot, None)
                log.info("расписание: %s отработал", slot)
            except Exception:  # noqa: BLE001 — расписание не должно умирать
                next_try[slot] = now + SLOT_RETRY_COOLDOWN
                log.exception("расписание: %s не отработал — повтор через %s",
                              slot, SLOT_RETRY_COOLDOWN)
        try:
            await tick_timed_reminders(bot, send)
        except Exception:  # noqa: BLE001
            log.exception("тик точных напоминаний упал")
        await asyncio.sleep(30)
