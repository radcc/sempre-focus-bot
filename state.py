"""Состояние: дедуп, отметки расписания, кольцевой буфер диалога,
последние карточки, ожидания (дата напоминания, рефлексия), «отмени»."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from config import MSK, STATE_DIR, log
from util import atomic_write_json, read_json

# ---------------------------------------------------------------- дедуп message_id

_seen_ids: set[int] = set()
_seen_order: list[int] = []
_SEEN_LIMIT = 2000


def _seen_file() -> Path:
    return STATE_DIR / "processed_ids.json"


def load_seen() -> None:
    data = read_json(_seen_file(), {"ids": []})
    _seen_order.extend(int(x) for x in data.get("ids", []))
    _seen_ids.update(_seen_order)
    if _seen_ids:
        log.info("дедуп: загружено %d обработанных id", len(_seen_ids))


def is_seen(message_id: int) -> bool:
    return message_id in _seen_ids


def mark_processed(message_id: int) -> None:
    if message_id in _seen_ids:
        return
    _seen_ids.add(message_id)
    _seen_order.append(message_id)
    while len(_seen_order) > _SEEN_LIMIT:
        _seen_ids.discard(_seen_order.pop(0))
    try:
        atomic_write_json(_seen_file(), {"ids": _seen_order})
    except Exception:  # noqa: BLE001
        log.exception("дедуп: не смог сохранить состояние")

# ---------------------------------------------------------------- отметки расписания

def _sched_file() -> Path:
    return STATE_DIR / "schedule.json"


def load_sched() -> dict:
    return read_json(_sched_file(), {})


def save_sched(sent: dict) -> None:
    try:
        atomic_write_json(_sched_file(), sent)
    except Exception:  # noqa: BLE001
        log.exception("расписание: не смог сохранить состояние")

# ---------------------------------------------------------------- кольцевой буфер диалога

_DIALOG_LIMIT = 20


def _dialog_file() -> Path:
    return STATE_DIR / "dialog.json"


_dialog: list[dict] = []


def load_dialog() -> None:
    _dialog.extend(read_json(_dialog_file(), []))


def dialog_append(role: str, text: str) -> None:
    _dialog.append({"role": role, "text": (text or "")[:1500],
                    "ts": datetime.now(MSK).isoformat(timespec="minutes")})
    del _dialog[:-_DIALOG_LIMIT]
    try:
        atomic_write_json(_dialog_file(), _dialog)
    except Exception:  # noqa: BLE001
        log.exception("диалог: не смог сохранить буфер")


def dialog_history() -> list[dict]:
    return list(_dialog)

# ---------------------------------------------------------------- последние карточки

_CARDS_LIMIT = 10


def _cards_file() -> Path:
    return STATE_DIR / "last_cards.json"


_last_cards: list[dict] = []


def load_last_cards() -> None:
    _last_cards.extend(read_json(_cards_file(), []))


def remember_card(page_id: str, title: str, kind: str) -> None:
    _last_cards.append({"id": page_id, "title": title, "kind": kind,
                        "ts": datetime.now(MSK).isoformat(timespec="minutes")})
    del _last_cards[:-_CARDS_LIMIT]
    try:
        atomic_write_json(_cards_file(), _last_cards)
    except Exception:  # noqa: BLE001
        log.exception("карточки: не смог сохранить список")


def last_cards() -> list[dict]:
    return list(_last_cards)

# ---------------------------------------------------------------- память процесса

# последняя пачка созданного (для «отмени»); после рестарта пусто — так и задумано
last_batch: dict = {"pages": [], "blocks": [], "titles": []}
undo_done: dict = {"flag": False}
# ожидание подтверждения «отмени» для пачки: {"until": datetime|None}
pending_undo: dict = {"until": None}
# очередь напоминаний, ждущих дату: [{page_id, title}] — persist (авто-деплой рестартует бота)
awaiting_dates: dict = {"queue": [], "ts": None}
# ждём голосовой итог недели после пятничного вопроса — persist
awaiting_reflection: dict = {"until": None}


def _awaiting_file() -> Path:
    return STATE_DIR / "awaiting.json"


def load_awaiting() -> None:
    data = read_json(_awaiting_file(), {})
    awaiting_dates["queue"] = data.get("dates_queue", [])
    ts = data.get("dates_ts")
    awaiting_dates["ts"] = datetime.fromisoformat(ts) if ts else None
    until = data.get("reflection_until")
    awaiting_reflection["until"] = datetime.fromisoformat(until) if until else None


def save_awaiting() -> None:
    try:
        atomic_write_json(_awaiting_file(), {
            "dates_queue": awaiting_dates["queue"],
            "dates_ts": awaiting_dates["ts"].isoformat() if awaiting_dates["ts"] else None,
            "reflection_until": (awaiting_reflection["until"].isoformat()
                                 if awaiting_reflection["until"] else None),
        })
    except Exception:  # noqa: BLE001
        log.exception("ожидания: не смог сохранить состояние")
