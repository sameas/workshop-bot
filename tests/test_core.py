from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.config import Config, ConfigError
from bot.db import ConflictError, Database
from bot.slots import (
    booking_intervals,
    busy_window,
    fmt_slot,
    free_start_times,
    has_future_starts,
    parse_duration,
    week_agenda,
)

TZ = ZoneInfo("Asia/Novosibirsk")


def make_cfg(**over) -> Config:
    base = dict(
        bot_token="x", admin_ids=frozenset({1}), masters_chat_id=None, db_path=":memory:", tz=TZ,
        buffer_minutes=30, slot_step_minutes=30,
        durations_minutes=(60, 120, 180, 540), days_ahead=14, gcal_calendar_id=None, google_sa_file=None,
        remind_before_minutes=(1440, 60),
    )
    base.update(over)
    return Config(**base)


def ts(d: date, h: int, m: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).timestamp())


@pytest.fixture
def db():
    d = Database(":memory:", buffer_minutes=30)
    yield d
    d.close()


D = date(2026, 10, 3)


def test_conflict_overlap(db):
    db.create_booking(1, "A", "", ts(D, 14), ts(D, 17))
    with pytest.raises(ConflictError) as ei:
        db.create_booking(2, "B", "", ts(D, 16), ts(D, 18))
    assert [c.master_name for c in ei.value.conflicts] == ["A"]


def test_conflict_within_buffer(db):
    db.create_booking(1, "A", "", ts(D, 14), ts(D, 17))
    # пауза 17:00-17:15, но буфер 30 мин: конфликт
    with pytest.raises(ConflictError):
        db.create_booking(2, "B", "", ts(D, 17, 15), ts(D, 19))
    # 17:30, ровно буфер
    db.create_booking(2, "B", "", ts(D, 17, 30), ts(D, 19))
    # перед: 12:00-13:30, 13:30+30=14:00
    db.create_booking(2, "B", "", ts(D, 12), ts(D, 13, 30))
    with pytest.raises(ConflictError):
        db.create_booking(2, "B", "", ts(D, 10), ts(D, 13, 45))


def test_cancelled_does_not_conflict(db):
    b = db.create_booking(1, "A", "", ts(D, 14), ts(D, 17))
    assert db.cancel_booking(b.id) is not None
    assert db.cancel_booking(b.id) is None
    db.create_booking(2, "B", "", ts(D, 14), ts(D, 17))


def test_rollback_on_conflict_keeps_db_usable(db):
    db.create_booking(1, "A", "", ts(D, 14), ts(D, 17))
    with pytest.raises(ConflictError):
        db.create_booking(2, "B", "", ts(D, 14), ts(D, 15))
    assert len(db.list_active(ts(D, 0), ts(D, 23))) == 1
    db.create_booking(2, "B", "", ts(D, 18), ts(D, 19))  # транзакция закрыта, новая работает


def test_gcal_sync_queues(db):
    b = db.create_booking(1, "A", "", ts(D, 14), ts(D, 17))
    assert [x.id for x in db.unsynced_active()] == [b.id]
    db.set_gcal_event(b.id, "ev1")
    assert db.unsynced_active() == []
    db.cancel_booking(b.id)
    assert [x.id for x in db.cancelled_with_event()] == [b.id]
    db.set_gcal_event(b.id, None)
    assert db.cancelled_with_event() == []


def test_reminder_mark_once(db):
    b = db.create_booking(1, "A", "", ts(D, 14), ts(D, 17))
    assert db.mark_reminder(b.id, "m60") is True
    assert db.mark_reminder(b.id, "m60") is False
    assert db.mark_reminder(b.id, "m1440") is True


def test_masters(db):
    assert not db.is_master(5)
    db.add_master(5, "Маша")
    db.add_master(5, "Маша К.")  # upsert имени
    assert db.get_master(5).name == "Маша К."
    assert db.remove_master(5) and not db.remove_master(5)


def hhmm(times) -> list[str]:
    return [f"{t:%H:%M}" for t in times]


NOW = datetime(2026, 10, 1, tzinfo=TZ)


def test_free_start_times_round_the_clock_with_buffer(db):
    cfg = make_cfg()
    db.create_booking(1, "A", "", ts(D, 14), ts(D, 16))
    busy = booking_intervals(db.list_active(*busy_window(D, 120, cfg)))
    times = free_start_times(D, 120, busy, cfg, now=NOW)
    # 2ч МК: до брони старт ≤ 11:30 (13:30 + буфер = 14:00), после ≥ 16:30
    assert hhmm(times)[:3] == ["00:00", "00:30", "01:00"]
    assert "11:30" in hhmm(times) and "12:00" not in hhmm(times)
    assert "16:00" not in hhmm(times) and "16:30" in hhmm(times)
    assert hhmm(times)[-1] == "23:30"
    assert all(t.date() == D for t in times) and len(set(hhmm(times))) == len(times)


def test_overnight_booking_allowed_and_blocked_by_next_morning(db):
    cfg = make_cfg()
    nxt = D + timedelta(days=1)
    # 21:00 + 9 ч = 06:00 следующего дня
    assert "21:00" in hhmm(free_start_times(D, 540, [], cfg, now=NOW))
    # утренняя бронь с 06:00: ночной МК до 06:00 упирается в буфер
    db.create_booking(1, "A", "", ts(nxt, 6), ts(nxt, 8))
    busy = booking_intervals(db.list_active(*busy_window(D, 540, cfg)))
    times = hhmm(free_start_times(D, 540, busy, cfg, now=NOW))
    assert "21:00" not in times and "20:30" in times
    # ночная бронь конфликтует с тем, что попадает внутрь неё
    db.create_booking(2, "B", "", ts(D, 20, 30), ts(nxt, 5, 30))
    with pytest.raises(ConflictError):
        db.create_booking(3, "C", "", ts(nxt, 2), ts(nxt, 3))


def test_busy_window_covers_long_duration(db):
    cfg = make_cfg(buffer_minutes=60)
    far = D + timedelta(days=2)
    db.create_booking(1, "A", "", ts(far, 0, 15), ts(far, 1))
    busy = booking_intervals(db.list_active(*busy_window(D, 1440, cfg)))
    assert "23:30" not in hhmm(free_start_times(D, 1440, busy, cfg, now=NOW))


def test_free_start_times_skips_past(db):
    cfg = make_cfg()
    now = datetime(D.year, D.month, D.day, 15, 10, tzinfo=TZ)
    times = free_start_times(D, 60, [], cfg, now=now)
    assert f"{times[0]:%H:%M}" == "15:30"


def test_late_evening_today_has_no_starts():
    cfg = make_cfg()
    now = datetime(D.year, D.month, D.day, 23, 40, tzinfo=TZ)
    assert free_start_times(D, 60, [], cfg, now=now) == [] and not has_future_starts(D, cfg, now)
    nxt = D + timedelta(days=1)
    assert hhmm(free_start_times(nxt, 60, [], cfg, now=now))[0] == "00:00" and has_future_starts(nxt, cfg, now)


def test_buffer_across_midnight(db):
    cfg = make_cfg()
    prev = D - timedelta(days=1)
    db.create_booking(1, "A", "", ts(prev, 22), ts(prev, 23, 50))
    busy = booking_intervals(db.list_active(*busy_window(D, 60, cfg)))
    times = free_start_times(D, 60, busy, cfg, now=NOW)
    assert f"{times[0]:%H:%M}" == "00:30"  # 23:50 + буфер 30 мин = 00:20, следующий шаг 00:30


def test_dst_duration_is_real_time():
    # 25.10.2026 в Берлине часы назад: 9 реальных часов с 21:00 кончаются в 05:00, а не в 06:00
    tz = ZoneInfo("Europe/Berlin")
    cfg = make_cfg(tz=tz)
    d = date(2026, 10, 24)
    start = next(t for t in free_start_times(d, 540, [], cfg, now=datetime(2026, 10, 1, tzinfo=tz)) if t.hour == 21)
    end = datetime.fromtimestamp(int(start.timestamp()) + 540 * 60, tz)
    assert (end.hour, end.day) == (5, 25)
    # 29.03 часы вперёд: 02:30 не существует, в сетке его нет
    spring = free_start_times(date(2026, 3, 29), 60, [], cfg, now=datetime(2026, 3, 1, tzinfo=tz))
    assert "02:30" not in hhmm(spring) and "03:00" in hhmm(spring)


@pytest.mark.parametrize("text,minutes", [
    ("9", 540), ("2.5", 150), ("2,5", 150), ("2:30", 150), (" 1:40 ", 100), ("9 ч", 540), ("24", 1440), ("0:15", 15),
    ("2.50", 150), ("1,25", 75),
    ("0", None), ("25", None), ("abc", None), ("2:75", None), ("-3", None), ("", None), ("0:10", None),
    # "2.30" пишут про 2 ч 30 мин, а дробью это 2 ч 18 мин: не угадываем, переспрашиваем
    ("2.30", None), ("1.30", None), ("2,05", None), ("12,99", None),
])
def test_parse_duration(text, minutes):
    assert parse_duration(text) == minutes


def test_fmt_slot_overnight():
    nxt = D + timedelta(days=1)   # D - суббота 03.10.2026
    assert fmt_slot(ts(D, 21), ts(nxt, 6), TZ) == "Сб 03.10 21:00 – Вс 04.10 06:00 (9 ч)"
    assert fmt_slot(ts(D, 0, 30), ts(D, 3), TZ) == "Сб 03.10 00:30–03:00 (2 ч 30 мин)"


def test_readonly_db_file_fails_at_open(tmp_path):
    import os
    import sqlite3
    path = tmp_path / "bot.db"
    Database(str(path)).close()
    os.chmod(path, 0o444)             # файл скопирован от другого юзера: каталог наш, файл read-only
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        Database(str(path))


def test_week_agenda_shows_overnight_on_both_days(db):
    nxt = D + timedelta(days=1)
    db.create_booking(1, "A", "", ts(D, 21), ts(nxt, 6))
    db.create_booking(2, "B", "", ts(nxt, 12), ts(nxt, 14))
    db.create_booking(3, "C", "", ts(nxt, 22), ts(nxt + timedelta(days=1), 0))   # ровно до полуночи, это не "через полночь"
    agenda = week_agenda(db.list_active(ts(D, 0), ts(D + timedelta(days=7), 0)), D, 7, TZ)
    assert [label for label, _ in agenda[D]] == ["21:00 – Вс 04.10 06:00"]
    assert [label for label, _ in agenda[nxt]] == ["…до 06:00 (с Сб 03.10 21:00)", "12:00–14:00", "22:00–00:00"]
    assert list(agenda) == [D, nxt]
    # бронь началась до окна: только продолжение под первым днём
    agenda = week_agenda(db.list_active(ts(nxt, 0), ts(nxt + timedelta(days=7), 0)), nxt, 7, TZ)
    assert agenda[nxt][0][0].startswith("…до 06:00") and D not in agenda
    # сутки с полуночи, не "00:00–00:00"
    far = D + timedelta(days=4)
    db.create_booking(4, "D", "", ts(far, 0), ts(far + timedelta(days=1), 0))
    agenda = week_agenda(db.list_active(ts(D, 0), ts(D + timedelta(days=7), 0)), D, 7, TZ)
    assert [label for label, _ in agenda[far]] == ["весь день"] and far + timedelta(days=1) not in agenda


def test_week_agenda_multi_day_item():
    from types import SimpleNamespace
    # all-day событие на три дня, началось до окна
    ev = SimpleNamespace(start_ts=ts(D - timedelta(days=1), 0), end_ts=ts(D + timedelta(days=2), 0))
    agenda = week_agenda([ev], D, 7, TZ)
    assert {d: [label for label, _ in v] for d, v in agenda.items()} == {
        D: ["весь день"], D + timedelta(days=1): ["весь день"],
    }


def test_unsynced_skips_finished(db):
    b = db.create_booking(1, "A", "", ts(D, 14), ts(D, 17))
    assert [x.id for x in db.unsynced_active(ts(D, 16))] == [b.id]
    assert db.unsynced_active(ts(D, 17)) == []


@pytest.mark.parametrize("over", [
    dict(slot_step_minutes=0), dict(slot_step_minutes=10), dict(slot_step_minutes=45 + 1), dict(buffer_minutes=-5),
    dict(durations_minutes=()), dict(durations_minutes=(60, 5000)), dict(days_ahead=0), dict(remind_before_minutes=(0,)),
])
def test_config_validation(over):
    with pytest.raises(ConfigError):
        make_cfg(**over)


def test_config_from_env(monkeypatch):
    for k in ("MASTERS_CHAT_ID", "GCAL_CALENDAR_ID", "DURATIONS_MINUTES", "WORK_START_HOUR", "REMIND_BEFORE_MINUTES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.setenv("ADMIN_IDS", "1, 2")
    monkeypatch.setenv("WORK_END_HOUR", "21")          # старые переменные игнорируются
    monkeypatch.setenv("MASTERS_CHAT_ID", "")
    cfg = Config.from_env()
    assert cfg.admin_ids == {1, 2} and cfg.masters_chat_id is None and 540 in cfg.durations_minutes
    assert cfg.remind_before_minutes == (1440, 60)
    monkeypatch.setenv("REMIND_BEFORE_MINUTES", "")    # пусто = без напоминаний
    assert Config.from_env().remind_before_minutes == ()
    monkeypatch.setenv("ADMIN_IDS", "me")
    with pytest.raises(ConfigError):
        Config.from_env()
