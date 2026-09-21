from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import MAX_DURATION_MINUTES, MIN_DURATION_MINUTES, Config
from .db import Booking

WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def day_start_ts(d: date, tz: ZoneInfo) -> int:
    return int(datetime.combine(d, time(0, 0), tzinfo=tz).timestamp())


Interval = tuple[int, int]  # (start_ts, end_ts)


def booking_intervals(bookings: list[Booking]) -> list[Interval]:
    return [(b.start_ts, b.end_ts) for b in bookings if b.active]


def busy_window(d: date, duration_min: int, cfg: Config) -> Interval:
    """Окно, в котором чужая занятость может задеть МК с началом в день d."""
    buf = cfg.buffer_minutes * 60
    return day_start_ts(d, cfg.tz) - buf, day_start_ts(d + timedelta(days=1), cfg.tz) + duration_min * 60 + buf


def grid_starts(d: date, cfg: Config) -> list[datetime]:
    out: list[datetime] = []
    for minute in range(0, 1440, cfg.slot_step_minutes):
        wall = datetime.combine(d, time(minute // 60, minute % 60))
        real = datetime.fromtimestamp(wall.replace(tzinfo=cfg.tz).timestamp(), cfg.tz)
        if real.replace(tzinfo=None) == wall:  # при переводе часов вперёд такого времени нет
            out.append(real)
    return out


def free_start_times(
    d: date,
    duration_min: int,
    busy_intervals: list[Interval],
    cfg: Config,
    now: datetime | None = None,
) -> list[datetime]:
    """Свободные старты в день d. busy_intervals: брони и события календаря из busy_window()."""
    now_ts = (now or datetime.now(cfg.tz)).timestamp()
    buf = cfg.buffer_minutes * 60
    dur = duration_min * 60
    result: list[datetime] = []
    for t in grid_starts(d, cfg):
        start = int(t.timestamp())
        end = start + dur
        if start > now_ts and not any(bs - buf < end and be + buf > start for bs, be in busy_intervals):
            result.append(t)
    return result


def has_future_starts(d: date, cfg: Config, now: datetime | None = None) -> bool:
    now_ts = (now or datetime.now(cfg.tz)).timestamp()
    return any(t.timestamp() > now_ts for t in grid_starts(d, cfg))


# дробь только .5/.25/.75: "2.30" пишут про 2 ч 30 мин, а как дробь это 2 ч 18 мин, пусть лучше переспросит
_DURATION_RE = re.compile(r"^(\d{1,2})(?:[.,](\d|25|50|75)|:([0-5]\d))?$")


def parse_duration(text: str) -> int | None:
    """Часы ("9", "2.5", "2:30") в минуты; None, если не разобрали или вне лимитов."""
    m = _DURATION_RE.match(text.strip().lower().removesuffix("ч").strip())
    if not m:
        return None
    hours, frac, mins = m.groups()
    total = int(hours) * 60
    if frac:
        total += round(float(f"0.{frac}") * 60)
    elif mins:
        total += int(mins)
    return total if MIN_DURATION_MINUTES <= total <= MAX_DURATION_MINUTES else None


# ---------- форматирование ----------

def fmt_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"


def fmt_dt_range(start_ts: int, end_ts: int, tz: ZoneInfo) -> str:
    s = datetime.fromtimestamp(start_ts, tz)
    e = datetime.fromtimestamp(end_ts, tz)
    if s.time() == time(0) and e.time() == time(0):  # all-day событие
        last = (e - timedelta(days=1)).date()
        return f"{fmt_date(s.date())} весь день" if last == s.date() else f"{fmt_date(s.date())}–{fmt_date(last)} весь день"
    if e.date() != s.date():
        return f"{fmt_date(s.date())} {s:%H:%M} – {fmt_date(e.date())} {e:%H:%M}"
    return f"{fmt_date(s.date())} {s:%H:%M}–{e:%H:%M}"


def fmt_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def fmt_slot(start_ts: int, end_ts: int, tz: ZoneInfo) -> str:
    return f"{fmt_dt_range(start_ts, end_ts, tz)} ({fmt_duration((end_ts - start_ts) // 60)})"


def fmt_booking(b: Booking, tz: ZoneInfo, with_master: bool = True) -> str:
    from aiogram import html  # локально, чтобы тесты слотов не тянули aiogram

    parts = [fmt_dt_range(b.start_ts, b.end_ts, tz)]
    if with_master:
        parts.append(html.quote(b.master_name))
    if b.title:
        parts.append(f"«{html.quote(b.title)}»")
    return " — ".join(parts)


def week_agenda(items: list, first: date, days: int, tz: ZoneInfo) -> dict[date, list[tuple[str, object]]]:
    """Занятость по дням; то, что идёт через полночь, попадает в каждый задетый день."""
    agenda: dict[date, list[tuple[str, object]]] = {}
    for item in sorted(items, key=lambda x: x.start_ts):
        s, e = datetime.fromtimestamp(item.start_ts, tz), datetime.fromtimestamp(item.end_ts, tz)
        for n in range(days):
            d = first + timedelta(days=n)
            day_start = datetime.combine(d, time(0), tzinfo=tz)
            day_end = datetime.combine(d + timedelta(days=1), time(0), tzinfo=tz)
            if e <= day_start or s >= day_end:
                continue
            if s <= day_start and e >= day_end:
                label = "весь день"
            elif s >= day_start and e <= day_end:
                label = f"{s:%H:%M}–{e:%H:%M}"
            elif s >= day_start:
                label = f"{s:%H:%M} – {fmt_date(e.date())} {e:%H:%M}"
            else:
                label = f"…до {e:%H:%M} (с {fmt_date(s.date())} {s:%H:%M})"
            agenda.setdefault(d, []).append((label, item))
    return dict(sorted(agenda.items()))
