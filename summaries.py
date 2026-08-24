"""Форматирование сообщений бота: сводка записи, поручения, посты, напоминания.
Железное правило: пустая строка между смысловыми блоками."""

from __future__ import annotations

from datetime import datetime

from config import MAX_ITEMS, MSK, NOTION_IDEAS_PAGE_ID
import notion_api
from util import human_date, plural


def join_blocks(blocks: list[str]) -> str:
    return "\n\n".join(b.strip() for b in blocks if b and b.strip())


def build_summary(created: list[dict], updated: list[dict], noise: list[str],
                  dupes: list[tuple[str, str]] | None = None,
                  truncated: bool = False, ask_title: str | None = None) -> str:
    """created: [{kind, title, deadline_iso, time_hm, who, date_missing}];
    updated: [{title, deadline_iso}] — напоминания, которым проставили дату;
    dupes: [(новое, похожее существующее)]."""
    tasks = [c for c in created if c["kind"] == "task"]
    rems = [c for c in created if c["kind"] == "reminder"]
    ideas = [c for c in created if c["kind"] == "idea"]
    blocks: list[str] = []

    if created:
        counts = []
        if tasks:
            counts.append(plural(len(tasks), ("задачу", "задачи", "задач")))
        if rems:
            counts.append(plural(len(rems), ("напоминание", "напоминания", "напоминаний")))
        if ideas:
            counts.append(plural(len(ideas), ("идею", "идеи", "идей")))
        head = ["Занёс " + ", ".join(counts) + ":"]
        for t in tasks:
            line = "— "
            if t.get("who") and t["who"] != "Я":
                line += f"{t['who']}: "
            line += t["title"]
            if t.get("deadline_iso"):
                line += f" ({human_date(t['deadline_iso'])})"
            head.append(line)
        blocks.append("\n".join(head))

        if rems:
            rem_lines = []
            for r in rems:
                when = human_date(r["deadline_iso"]) if r.get("deadline_iso") else "завтра"
                if r.get("time_hm"):
                    when += f" {r['time_hm']}"
                rem_lines.append(f"⏰ {when} {r['title']}")
            blocks.append("\n".join(rem_lines))

        if ideas:
            blocks.append("\n".join(f"💡 {i['title']}" for i in ideas))

    if updated:
        blocks.append("\n".join(f"Ок, напомню {human_date(u['deadline_iso'])}: {u['title']}"
                                for u in updated))
    if dupes:
        blocks.append("\n".join(f"«{new}» похоже на существующую: «{old}»"
                                for new, old in dupes))
    if noise:
        blocks.append("Пропустил как шум: " + ", ".join(f"«{n}»" for n in noise))
    if not blocks:
        return "Не понял, что занести. Скажи иначе."
    if truncated:
        blocks.append(f"Вошло {MAX_ITEMS} пунктов — потолок одного сообщения. "
                      "Остальное продиктуй отдельно.")
    if ask_title:
        blocks.append(f"На какую дату напомнить «{ask_title}»? Пока поставил завтра.")
    return join_blocks(blocks)


async def assignments_text() -> str:
    """Сводка поручений: по людям, просроченное помечено."""
    cards = await notion_api.assignments()
    if not cards:
        return "Активных поручений нет."
    today = datetime.now(MSK).date().isoformat()
    by_person: dict[str, list[dict]] = {}
    for c in cards:
        by_person.setdefault(c["who"] or "?", []).append(c)
    blocks = []
    for person in sorted(by_person):
        lines = [f"{person}:"]
        for c in sorted(by_person[person], key=lambda x: x.get("when") or "9999"):
            line = f"— {c['title']}"
            when = (c.get("when") or "").split("T")[0]
            if when:
                line += f" · {human_date(when)}"
                if when < today:
                    line += " · просрочено"
            lines.append(line)
        blocks.append("\n".join(lines))
    return join_blocks(blocks)


async def reminders_text() -> str:
    rems = await notion_api.future_reminders()
    if not rems:
        return "Будущих напоминаний нет."
    lines = []
    for r in rems:
        when = r.get("when") or ""
        d = human_date(when)
        if "T" in when:
            d += f" {when.split('T')[1][:5]}"
        lines.append(f"⏰ {d} {r['title']}")
    return "\n".join(lines)


async def posts_text() -> str:
    """Сводка постов: незачёркнутые блоки «Пост: …» со страницы идей."""
    text = await notion_api.page_text(NOTION_IDEAS_PAGE_ID, with_ids=True, max_blocks=300)
    posts = []
    for line in text.splitlines():
        if "[зачёркнуто]" in line:
            continue
        # строка вида "[id] 21.08 — Пост: …"
        body = line.split("] ", 1)[-1].strip()
        if "пост:" in body.lower():
            posts.append("— " + body)
    if not posts:
        return "Постов в очереди нет."
    return "Посты:\n" + "\n".join(posts)


HELP_TEXT = """Кидай текст или голос — разберу: задачи в Inbox, напоминания, идеи. Можно много пунктов одним сообщением.

Понимаю без команд: вопросы по базе и целям, исправления («это не идея, а задача»), поручения-ресёрч («найди информацию про…»), «запомни …».

Слова-команды: «неделя», «неделя коротко», «цели 90», «поручения», «посты», «отмени», «баг: …».

«Подумай хорошенько» — включу глубокую модель. Утром 7:30 — напоминания дня, вечером 19:00 (пн–пт) — итог."""
