"""Инструменты агента: схемы и исполнитель с лимитом изменений."""

from __future__ import annotations

import json
from datetime import datetime

from config import AGENT_MAX_MUTATIONS, MSK, STATUS_INBOX, STATUS_REMINDER, log
import notion_api
import state
import summaries
from util import clean_title, short_error

TOOL_DEFS: list[dict] = [
    {
        "name": "find_cards",
        "description": "Найти карточки в базе задач по тексту названия и/или фильтрам.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Подстрока названия (можно пусто)"},
                "who": {"type": "string"},
                "status": {"type": "string"},
                "tag": {"type": "string"},
                "only_active": {"type": "boolean", "description": "true = без Done/Архив (дефолт)"},
            },
        },
    },
    {
        "name": "create_card",
        "description": "Создать карточку: задачу или напоминание.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["task", "reminder"]},
                "title": {"type": "string"},
                "tag": {"type": "string"},
                "who": {"type": "string"},
                "when_iso": {"type": "string",
                             "description": "YYYY-MM-DD или YYYY-MM-DDTHH:MM:00+03:00"},
                "description_md": {"type": "string",
                                   "description": "Тело карточки (### заголовки, - списки)"},
            },
            "required": ["kind", "title"],
        },
    },
    {
        "name": "update_card",
        "description": "Изменить карточку: название/статус/тег/кто/когда, дописать описание.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "title": {"type": "string"},
                "status": {"type": "string"},
                "tag": {"type": "string"},
                "who": {"type": "string"},
                "when_iso": {"type": "string"},
                "append_description_md": {"type": "string"},
            },
            "required": ["page_id"],
        },
    },
    {
        "name": "archive_card",
        "description": "Убрать карточку в архив (единственный способ «удалить»).",
        "input_schema": {"type": "object", "properties": {"page_id": {"type": "string"}},
                         "required": ["page_id"]},
    },
    {
        "name": "read_page",
        "description": "Прочитать страницу: goals | memory | ideas (с id блоков) | bugs.",
        "input_schema": {"type": "object",
                         "properties": {"page": {"type": "string",
                                                 "enum": ["goals", "memory", "ideas", "bugs"]}},
                         "required": ["page"]},
    },
    {
        "name": "append_page",
        "description": "Дописать в страницу: memory (факты) | ideas (новая идея сверху) | bugs (пункт).",
        "input_schema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "enum": ["memory", "ideas", "bugs"]},
                "markdown": {"type": "string"},
            },
            "required": ["page", "markdown"],
        },
    },
    {
        "name": "strike_block",
        "description": "Зачеркнуть блок на странице (пост «про это уже написал», устаревший факт памяти). Id блока — из read_page(ideas/memory/bugs).",
        "input_schema": {"type": "object", "properties": {"block_id": {"type": "string"}},
                         "required": ["block_id"]},
    },
    {
        "name": "assignments_summary",
        "description": "Готовая сводка поручений по людям (Кто ≠ Я, активные).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reminders_summary",
        "description": "Готовая сводка будущих напоминаний.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_reflection",
        "description": "Сохранить итоги недели в память: сжатую версию и полную расшифровку.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary_md": {"type": "string",
                               "description": "3–5 строк: продвинулось / не продвинулось / переносим"},
                "full_text": {"type": "string"},
            },
            "required": ["summary_md"],
        },
    },
]


class ToolExecutor:
    """Исполняет вызовы инструментов; считает изменения (лимит на запрос),
    копит список сделанного (для честных отчётов при сбое) и созданное (для «отмени»)."""

    def __init__(self, people: list[str] | None = None,
                 allowed: set | None = None) -> None:
        self.mutations = 0
        self.limit_hit = False
        self.done: list[str] = []          # человекочитаемые записи успешных изменений
        self.created_pages: list[str] = []
        self.created_titles: list[str] = []
        self.created_blocks: list[str] = []
        self.people = people or ["Я"]
        self.allowed = allowed             # None = все инструменты

    def _mutate(self) -> bool:
        if self.mutations >= AGENT_MAX_MUTATIONS:
            self.limit_hit = True
            return False
        self.mutations += 1
        return True

    def _valid_person(self, who: str | None) -> str | None:
        if not who:
            return None
        for p in self.people:
            if p.lower() == who.lower():
                return p
        return None

    async def run(self, name: str, args: dict) -> str:
        if self.allowed is not None and name not in self.allowed:
            return f"инструмент {name} недоступен в этом режиме"
        before = self.mutations
        try:
            result = await self._dispatch(name, args)
            # любое изменение помечает пачку конвейера неактуальной для «отмени»
            if self.mutations > before:
                state.last_batch.update({"pages": [], "blocks": [], "titles": []})
            return result
        except Exception as e:  # noqa: BLE001 — агент должен увидеть ошибку, не упасть
            self.mutations = before  # упавшая правка не сжигает лимит
            log.exception("агент: инструмент %s упал", name)
            return f"ошибка: {short_error(e)}"

    async def _dispatch(self, name: str, args: dict) -> str:
        if name == "find_cards":
            cards = await notion_api.find_cards(
                query=args.get("query") or "", who=args.get("who"),
                status=notion_api.validate_status(args.get("status") or "") if args.get("status") else None,
                tag=args.get("tag"),
                only_active=args.get("only_active", True))
            if not cards:
                return "ничего не найдено"
            return json.dumps(cards, ensure_ascii=False)

        if name == "create_card":
            if not self._mutate():
                return "лимит 10 изменений за запрос исчерпан — остановись и переспроси Алексея"
            kind = args.get("kind") or "task"
            title = clean_title(args.get("title") or "Без названия")
            status = STATUS_REMINDER if kind == "reminder" else STATUS_INBOX
            who = self._valid_person(args.get("who"))
            if args.get("who") and not who:
                self.mutations -= 1
                return f"неизвестный человек «{args['who']}» — допустимы: {', '.join(self.people)}"
            props = notion_api.card_properties(
                title, status, tag=notion_api.validate_tag(args.get("tag") or "") or "Личное",
                who=who, when_iso=args.get("when_iso"))
            children = notion_api.md_blocks(args.get("description_md") or "") or None
            page_id = await notion_api.create_card(props, children)
            state.remember_card(page_id, title, kind)
            self.created_pages.append(page_id)
            self.created_titles.append(title)
            self.done.append(f"создал карточку «{title}»")
            return f"создана карточка {page_id}: {title}"

        if name == "update_card":
            if not self._mutate():
                return "лимит 10 изменений за запрос исчерпан — остановись и переспроси Алексея"
            status = args.get("status")
            if status:
                valid = notion_api.validate_status(status)
                if not valid:
                    self.mutations -= 1
                    return f"неизвестный статус «{status}»"
                status = valid
            who = None
            if args.get("who"):
                who = self._valid_person(args["who"])
                if not who:
                    self.mutations -= 1
                    return f"неизвестный человек «{args['who']}» — допустимы: {', '.join(self.people)}"
            await notion_api.update_card(
                args["page_id"],
                title=clean_title(args["title"]) if args.get("title") else None,
                status=status, tag=notion_api.validate_tag(args["tag"]) if args.get("tag") else None,
                who=who, when_iso=args.get("when_iso"))
            if args.get("append_description_md"):
                await notion_api.append_card_blocks(
                    args["page_id"], notion_api.md_blocks(args["append_description_md"]))
            self.done.append(f"обновил карточку {args['page_id'][:8]}")
            return "обновлено"

        if name == "archive_card":
            if not self._mutate():
                return "лимит 10 изменений за запрос исчерпан — остановись и переспроси Алексея"
            await notion_api.set_page_status(args["page_id"], "Архив")
            self.done.append(f"заархивировал карточку {args['page_id'][:8]}")
            return "в архиве"

        if name == "read_page":
            page = args.get("page")
            from config import NOTION_BUGS_CARD_ID, NOTION_IDEAS_PAGE_ID, NOTION_MEMORY_PAGE_ID
            if page == "goals":
                return await notion_api.get_goals_text(fresh=True) or "(пусто)"
            if page == "memory":
                return await notion_api.page_text(NOTION_MEMORY_PAGE_ID, with_ids=True,
                                                  skip_toggles=True, max_blocks=300) or "(пусто)"
            if page == "ideas":
                return await notion_api.page_text(NOTION_IDEAS_PAGE_ID, with_ids=True,
                                                  max_blocks=300) or "(пусто)"
            if page == "bugs":
                return await notion_api.page_text(NOTION_BUGS_CARD_ID, with_ids=True,
                                                  max_blocks=300) or "(пусто)"
            return "неизвестная страница"

        if name == "append_page":
            if not self._mutate():
                return "лимит 10 изменений за запрос исчерпан — остановись и переспроси Алексея"
            page, md = args.get("page"), args.get("markdown") or ""
            if page == "memory":
                await notion_api.append_memory(md)
                self.done.append("дописал в память")
                return "записано в память"
            if page == "ideas":
                today = datetime.now(MSK).strftime("%d.%m")
                first = md.splitlines()[0] if md.splitlines() else md
                title = clean_title(first)
                blocks = [notion_api.paragraph(
                    notion_api.md_rich("", prefix_bold=f"{today} — {title}"))]
                rest = "\n".join(md.splitlines()[1:])
                blocks.extend(notion_api.md_blocks(rest))
                ids = await notion_api.insert_idea_blocks(notion_api.cap_blocks(blocks, 100))
                if ids:
                    state.remember_card(ids[0], title, "idea")
                    self.created_blocks.extend(ids)
                    self.created_titles.append(title)
                self.done.append(f"добавил идею «{title}»")
                return f"добавлено на страницу идей ({len(ids)} блоков)"
            if page == "bugs":
                await notion_api.append_bug(md)
                self.done.append("дописал в карту багов")
                return "записано в карту багов"
            return "неизвестная страница"

        if name == "strike_block":
            if not self._mutate():
                return "лимит 10 изменений за запрос исчерпан — остановись и переспроси Алексея"
            await notion_api.strike_block(args["block_id"])
            self.done.append(f"зачеркнул блок {args['block_id'][:8]}")
            return "зачёркнуто"

        if name == "assignments_summary":
            return await summaries.assignments_text()

        if name == "reminders_summary":
            return await summaries.reminders_text()

        if name == "save_reflection":
            if not self._mutate():
                return "лимит 10 изменений за запрос исчерпан"
            today = datetime.now(MSK).strftime("%d.%m.%Y")
            md = args.get("summary_md") or ""
            full = (args.get("full_text") or "").strip()
            blocks = ([{"object": "block", "type": "heading_3",
                        "heading_3": {"rich_text": notion_api.md_rich(f"Итоги недели {today}")}}]
                      + notion_api.md_blocks(md))
            if full and full != md:
                blocks.append(notion_api.toggle_block("Расшифровка", full))
            from config import NOTION_MEMORY_PAGE_ID
            r = await notion_api.append_card_blocks(NOTION_MEMORY_PAGE_ID, blocks)
            state.awaiting_reflection["until"] = None
            state.save_awaiting()
            self.done.append("сохранил итоги недели в память")
            return f"итоги недели сохранены ({len(r)} блоков)"

        return f"неизвестный инструмент {name}"
