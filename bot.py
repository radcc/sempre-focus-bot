#!/usr/bin/env python3
"""Sempre Focus Bot v3 «Мозг» — Telegram → Notion + агент.

Текст/голос → роутер (запись | агент) → конвейер записи или tool-use агент
→ Notion → короткий ответ. Кнопки, точные напоминания, расписание, память.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from config import (AWAITING_DATE_TTL, BUTTONS, MAX_ITEMS, MSK, OWNER_TELEGRAM_ID,
                    STATUS_INBOX, STATUS_REMINDER, TELEGRAM_BOT_TOKEN,
                    VOICE_TOGGLE_THRESHOLD, log)
import llm
import notion_api
import scheduler
import state
import summaries
from util import chunk_text, human_date, normalize_cmd, reason, retry3, similar_title

KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=b) for b in row] for row in BUTTONS],
    resize_keyboard=True)

UNDO_RE = re.compile(
    r"(отмени|отмена|убери|удали|/?undo)(\s+(последн\w*|это|её|его|запись|карточку|пачку|пожалуйста)){0,3}")

# ---------------------------------------------------------------- вспомогательные

async def answer_chunked(message: Message, text: str) -> None:
    for chunk in chunk_text(text, 4000):
        await message.answer(chunk)


async def answer_logged(message: Message, user_text: str, reply_text: str) -> None:
    """Ответ + запись пары в диалог-буфер: агент видит, на что отвечает Алексей."""
    await answer_chunked(message, reply_text)
    state.dialog_append("user", user_text)
    state.dialog_append("bot", reply_text)


class Typing:
    """Индикатор «печатает…» на время долгих операций (агент, ресёрч, Fable)."""

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot, self._chat_id, self._task = bot, chat_id, None

    async def _loop(self) -> None:
        while True:
            try:
                await self._bot.send_chat_action(self._chat_id, "typing")
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(5)

    async def __aenter__(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()

# ---------------------------------------------------------------- команды-слова

async def cmd_week(message: Message, short: bool) -> None:
    try:
        text = await llm.week_text(short=short)
    except Exception as e:  # noqa: BLE001
        log.exception("неделя не собралась")
        await message.answer(f"Не смог собрать ({reason(e)}). Попробуй ещё раз.")
        return
    state.mark_processed(message.message_id)
    await answer_logged(message, message.text or "неделя", text or "На странице целей пусто.")


async def cmd_goals(message: Message) -> None:
    try:
        text = await llm.goals_text_brief()
    except Exception as e:  # noqa: BLE001
        log.exception("цели не собрались")
        await message.answer(f"Не смог собрать ({reason(e)}). Попробуй ещё раз.")
        return
    state.mark_processed(message.message_id)
    await answer_logged(message, message.text or "цели", text or "На странице целей пусто.")


async def cmd_assignments(message: Message) -> None:
    try:
        text = await summaries.assignments_text()
    except Exception as e:  # noqa: BLE001
        log.exception("поручения не собрались")
        await message.answer(f"Не смог собрать ({reason(e)}).")
        return
    state.mark_processed(message.message_id)
    await answer_logged(message, message.text or "поручения", text)


async def cmd_posts(message: Message) -> None:
    try:
        text = await summaries.posts_text()
    except Exception as e:  # noqa: BLE001
        log.exception("посты не собрались")
        await message.answer(f"Не смог собрать ({reason(e)}).")
        return
    state.mark_processed(message.message_id)
    await answer_logged(message, message.text or "посты", text)


_BUG_PREFIX_RE = re.compile(r"^\s*баг[\s,:.…—-]*", re.IGNORECASE)


async def cmd_bug(message: Message, text: str) -> None:
    body = _BUG_PREFIX_RE.sub("", text).strip()
    if not body:
        await message.answer("Что за баг? Напиши «баг: …».")
        return
    try:
        await notion_api.append_bug(body)
    except Exception as e:  # noqa: BLE001
        log.exception("баг не записался")
        await message.answer(f"Не записал ({reason(e)}). Повтори позже.")
        return
    state.mark_processed(message.message_id)
    await message.answer("Записал в карту багов.")


UNDO_CONFIRM = {"да", "точно", "убери", "давай", "ага", "подтверждаю", "уверен"}


async def do_undo(message: Message, confirmed: bool = False) -> None:
    if not state.last_batch["pages"] and not state.last_batch["blocks"]:
        if state.undo_done["flag"]:
            await message.answer("Уже убрал — дальше руками.")
        else:
            await message.answer("Не помню последнюю — скажи агенту, что поправить, или удали руками.")
        return
    titles = list(state.last_batch["titles"])
    total = len(state.last_batch["pages"]) + len(state.last_batch["blocks"])
    if total > 1 and not confirmed:
        # пачку не сносим без подтверждения — цена ошибки высока
        state.pending_undo["until"] = datetime.now(MSK) + timedelta(minutes=5)
        await message.answer("Уберу всю пачку: " + ", ".join(titles)
                             + ".\n\nТочно? («да» — убрать, любой другой ответ — оставить)")
        return
    archived = list(state.last_batch["pages"])
    try:
        for page_id in state.last_batch["pages"]:
            await notion_api.archive_page(page_id)
        for block_id in state.last_batch["blocks"]:
            await notion_api.delete_block(block_id)
    except Exception as e:  # noqa: BLE001
        log.exception("отмена не удалась")
        await message.answer(f"Не смог убрать ({reason(e)}). Удали руками.")
        return
    state.last_batch.update({"pages": [], "blocks": [], "titles": []})
    state.undo_done["flag"] = True
    state.awaiting_dates["queue"] = [s for s in state.awaiting_dates["queue"]
                                     if s["page_id"] not in archived]
    if not state.awaiting_dates["queue"]:
        state.awaiting_dates["ts"] = None
    state.save_awaiting()
    state.mark_processed(message.message_id)
    log.info("отмена: убрано %d объектов", len(titles))
    await message.answer("Убрал: " + ", ".join(titles))

# ---------------------------------------------------------------- конвейер записи

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


def _task_children(item: dict, transcript: str | None) -> list[dict]:
    children: list[dict] = []
    summary = (item.get("summary_md") or "").strip()
    if summary and summary.lower() != item["title"].lower():
        children.extend(notion_api.md_blocks(summary))
    if item.get("context_md"):
        children.extend(notion_api.section_blocks("Контекст", item["context_md"]))
    if item.get("next_step"):
        children.extend(notion_api.section_blocks("Следующий шаг", item["next_step"]))
    head = f"→Цель {item['goal_score']}/3 · CEO {item['ceo_score']}/3 · {item['recommendation']}"
    children.append(notion_api.paragraph(notion_api.md_rich(head, prefix_bold="🤖 Анализ: ")))
    children.extend(notion_api.md_blocks(item.get("analysis_md") or ""))
    children = notion_api.cap_blocks(children, 99)
    if transcript:
        children.append(notion_api.toggle_block("Расшифровка", transcript))
    return children


async def save_items(message: Message, items: list[dict], text: str,
                     transcript: str | None) -> None:
    now = datetime.now(MSK)
    created: list[dict] = []
    updated: list[dict] = []
    pages: list[str] = []
    blocks: list[str] = []
    noise: list[str] = []
    dupes: list[tuple[str, str]] = []
    try:
        existing = await notion_api.active_titles()
        transcript_pending = (transcript if transcript
                              and len(transcript) > VOICE_TOGGLE_THRESHOLD else None)
        single_voice_idea = (transcript is not None and len(items) == 1
                             and items[0]["type"] == "idea")
        last_idea_block: str | None = None
        for item in items:
            kind = item["type"]
            if kind == "noise":
                noise.append(item["title"])
            elif kind == "set_reminder_date":
                if state.awaiting_dates["queue"] and item.get("deadline_iso"):
                    slot = state.awaiting_dates["queue"].pop(0)
                    await notion_api.set_page_date(slot["page_id"], llm.when_iso(item))
                    updated.append({"title": slot["title"], "deadline_iso": item["deadline_iso"]})
                    if not state.awaiting_dates["queue"]:
                        state.awaiting_dates["ts"] = None
                    state.save_awaiting()
                # вне сценария — молча пропускаем, задачей не становится
            elif kind == "idea":
                with_toggle = single_voice_idea and transcript_pending is not None
                today = now.strftime("%d.%m")
                ib: list[dict] = [notion_api.paragraph(
                    notion_api.md_rich("", prefix_bold=f"{today} — {item['title']}"))]
                body = transcript if single_voice_idea else (item.get("summary_md") or "")
                if with_toggle:
                    summary = (item.get("summary_md") or "").strip() or item["title"]
                    ib.extend(notion_api.md_blocks(summary))
                    ib = notion_api.cap_blocks(ib, 99)
                    ib.append(notion_api.toggle_block("Расшифровка", transcript))
                    transcript_pending = None
                else:
                    b = (body or "").strip()
                    if b and b.lower() != item["title"].lower():
                        ib.extend(notion_api.md_blocks(b))
                    ib = notion_api.cap_blocks(ib, 100)
                ids = await notion_api.insert_idea_blocks(ib, after_block=last_idea_block)
                blocks.extend(ids)
                if ids:
                    last_idea_block = ids[-1]
                created.append({"kind": "idea", "title": item["title"]})
            elif kind == "reminder":
                when = llm.when_iso(item) or (now.date() + timedelta(days=1)).isoformat()
                children = ([notion_api.toggle_block("Расшифровка", transcript_pending)]
                            if transcript_pending else None)
                props = notion_api.card_properties(item["title"], STATUS_REMINDER,
                                                   tag=item["tag"], who=item["who"],
                                                   when_iso=when)
                page_id = await notion_api.create_card(props, children)
                transcript_pending = None
                pages.append(page_id)
                state.remember_card(page_id, item["title"], "reminder")
                created.append({"kind": "reminder", "title": item["title"],
                                "deadline_iso": item.get("deadline_iso") or when,
                                "time_hm": item.get("time_hm"),
                                "date_missing": item["date_missing"]})
                if item["date_missing"]:
                    state.awaiting_dates["queue"].append({"page_id": page_id,
                                                          "title": item["title"]})
                    state.awaiting_dates["ts"] = now
                    state.save_awaiting()
            else:  # task
                dup = similar_title(item["title"], existing)
                props = notion_api.card_properties(item["title"], STATUS_INBOX,
                                                   tag=item["tag"], who=item["who"],
                                                   when_iso=llm.when_iso(item),
                                                   recommendation=item["recommendation"])
                page_id = await notion_api.create_card(props,
                                                       _task_children(item, transcript_pending))
                transcript_pending = None
                pages.append(page_id)
                state.remember_card(page_id, item["title"], "task")
                created.append({"kind": "task", "title": item["title"],
                                "deadline_iso": item.get("deadline_iso"), "who": item["who"]})
                if dup:
                    dupes.append((item["title"], dup))
    except Exception as e:  # noqa: BLE001 — сбой не должен терять текст
        log.exception("не сохранил запись")
        if created:
            state.last_batch.update({"pages": pages, "blocks": blocks,
                                     "titles": [c["title"] for c in created]})
            state.undo_done["flag"] = False
        planned = len([i for i in items if i.get("type") in ("task", "reminder", "idea")])
        head = (f"⚠️ Занёс {len(created)} из {planned}, дальше сбой ({reason(e)})."
                if created else f"⚠️ Не сохранил ({reason(e)}).")
        await message.answer(head + " Текст ниже — кинь позже:")
        for chunk in chunk_text(text, 4000):
            await message.answer(chunk)
        return

    if created:
        state.last_batch.update({"pages": pages, "blocks": blocks,
                                 "titles": [c["title"] for c in created]})
        state.undo_done["flag"] = False
    state.mark_processed(message.message_id)
    log.info("сохранено: %d пунктов (%s), дат: %d, шум: %d",
             len(created), ", ".join(c["kind"] for c in created) or "-", len(updated), len(noise))
    ask_title = (state.awaiting_dates["queue"][0]["title"]
                 if state.awaiting_dates["queue"] else None)
    reply = summaries.build_summary(created, updated, noise, dupes,
                                    truncated=len(items) >= MAX_ITEMS, ask_title=ask_title)
    try:
        await answer_chunked(message, reply)
    except Exception:  # noqa: BLE001
        log.exception("сохранено, но сводка не отправилась")
        try:
            await message.answer(f"Занёс {len(created)} — сводка не дошла, детали в Notion.")
        except Exception:  # noqa: BLE001
            pass
    state.dialog_append("user", text)
    state.dialog_append("bot", reply)

# ---------------------------------------------------------------- маршрутизация

COMMANDS = {
    "неделя": lambda m, t: cmd_week(m, short=False),
    "/неделя": lambda m, t: cmd_week(m, short=False),
    "неделя коротко": lambda m, t: cmd_week(m, short=True),
    "цели": lambda m, t: cmd_goals(m),
    "цели 90": lambda m, t: cmd_goals(m),
    "поручения": lambda m, t: cmd_assignments(m),
    "посты": lambda m, t: cmd_posts(m),
    "что умеешь": lambda m, t: m.answer(summaries.HELP_TEXT, reply_markup=KEYBOARD),
}


async def handle_text_like(message: Message, text: str, transcript: str | None = None) -> None:
    norm = normalize_cmd(text)
    now = datetime.now(MSK)

    # ждём подтверждения «убрать всю пачку?»
    if state.pending_undo["until"]:
        pending_active = now < state.pending_undo["until"]
        state.pending_undo["until"] = None
        if pending_active and norm in UNDO_CONFIRM:
            await do_undo(message, confirmed=True)
            return
        # любой другой ответ = передумал; сообщение обрабатываем как обычно

    if UNDO_RE.fullmatch(norm):
        await do_undo(message)
        return
    handler = COMMANDS.get(norm)
    if handler:
        await handler(message, text)
        return
    if norm == "баг" or norm.startswith("баг ") or norm.startswith("баг:") \
            or text.strip().lower().startswith("баг:"):
        await cmd_bug(message, text)
        return

    source = forward_source(message)
    input_text = f"Переслано от {source}:\n{text}" if source else text
    if transcript is not None:
        input_text = f"(голосовое сообщение, расшифровка)\n{input_text}"

    if (state.awaiting_dates["queue"] and state.awaiting_dates["ts"]
            and now - state.awaiting_dates["ts"] > AWAITING_DATE_TTL):
        state.awaiting_dates.update({"queue": [], "ts": None})
        state.save_awaiting()
    date_hint = (state.awaiting_dates["queue"][0]["title"]
                 if state.awaiting_dates["queue"] else None)
    reflection_hint = bool(state.awaiting_reflection["until"]
                           and now < state.awaiting_reflection["until"])
    deep = llm.wants_deep(text)
    if deep:
        await message.answer("Думаю (Fable) — займёт минуту-другую.")

    async with Typing(message.bot, message.chat.id):
        try:
            kind, payload = await llm.route_message(input_text, date_hint, reflection_hint)
        except Exception as e:  # noqa: BLE001
            log.exception("роутер упал")
            await message.answer(f"⚠️ Не разобрал ({reason(e)}). Текст ниже — кинь позже:")
            for chunk in chunk_text(text, 4000):
                await message.answer(chunk)
            return

        if kind == "items":
            if date_hint and not any(i["type"] == "set_reminder_date" for i in payload):
                state.awaiting_dates.update({"queue": [], "ts": None})
                state.save_awaiting()
            await save_items(message, payload, text, transcript)
            return

        # агент: помечаем ДО запуска — повтор после рестарта хуже потери
        # (правки карточек задвоятся; потерянный запрос Алексей просто повторит)
        state.mark_processed(message.message_id)
        try:
            answer, model_note = await llm.run_agent(input_text, payload, deep=deep)
        except Exception as e:  # noqa: BLE001
            log.exception("агент упал")
            await message.answer(f"⚠️ Не справился ({reason(e)}). Попробуй переформулировать.")
            return
    if model_note:
        answer = f"🧠 {model_note}:\n\n{answer}"
    try:
        await answer_chunked(message, answer)
    except Exception:  # noqa: BLE001
        log.exception("ответ агента не отправился")
    state.dialog_append("user", text)
    state.dialog_append("bot", answer)

# ---------------------------------------------------------------- Telegram

class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        if event.from_user is None or event.from_user.id != OWNER_TELEGRAM_ID:
            uid = event.from_user.id if event.from_user else "?"
            log.info("чужое сообщение от id=%s — молча игнорирую", uid)
            return None
        if state.is_seen(event.message_id):
            log.info("дедуп: сообщение %d уже обработано — пропускаю", event.message_id)
            return None
        return await handler(event, data)


dp = Dispatcher()
dp.message.middleware(OwnerOnlyMiddleware())


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(summaries.HELP_TEXT, reply_markup=KEYBOARD)


@dp.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("жив")


@dp.message(F.voice | F.audio | F.video_note)
async def on_voice(message: Message, bot: Bot) -> None:
    media = message.voice or message.audio or message.video_note
    try:
        transcript = await llm.transcribe_voice(bot, media.file_id)
    except Exception as e:  # noqa: BLE001
        log.exception("расшифровка не удалась")
        await message.answer(f"⚠️ Не сохранил (расшифровка не удалась: {reason(e)}). Отправь текстом.")
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
    if message.caption:
        # мысль в подписи к фото/видео дороже, чем само вложение
        await handle_text_like(message, f"(подпись к вложению, само вложение не вижу)\n{message.caption}")
        return
    await message.answer("Пока понимаю только текст и голос")


async def main() -> None:
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    me = await bot.get_me()
    from config import ANTHROPIC_MODEL, ANTHROPIC_MODEL_DEEP
    log.info("старт v3: @%s, модель %s, deep %s", me.username, ANTHROPIC_MODEL,
             ANTHROPIC_MODEL_DEEP)
    state.load_seen()
    state.load_dialog()
    state.load_last_cards()
    state.load_awaiting()
    try:
        anchor = await retry3("notion-anchor", notion_api.get_ideas_anchor)
        log.info("якорь страницы идей: %s", anchor)
        await notion_api.get_goals_text()
        if await notion_api.memory_available():
            log.info("память бота доступна")
        else:
            log.warning("память бота НЕДОСТУПНА — агент работает без неё")
    except Exception:  # noqa: BLE001
        log.exception("прогрев не удался — продолжу, подтяну по ходу")

    async def send(text: str) -> None:
        for chunk in chunk_text(text, 4000):
            await bot.send_message(OWNER_TELEGRAM_ID, chunk, reply_markup=KEYBOARD)
        # плановые сообщения — часть диалога: агент должен видеть, на что отвечает Алексей
        state.dialog_append("bot", text)

    # ссылку держим, иначе задачу может убрать сборщик мусора
    scheduler_task = asyncio.create_task(scheduler.scheduler_loop(bot, send))
    try:
        # handle_as_tasks=False: offset подтверждается после обработки —
        # рестарт не теряет сообщение, дедуп закрывает повторную доставку
        await dp.start_polling(bot, allowed_updates=["message"], handle_as_tasks=False)
    finally:
        scheduler_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
