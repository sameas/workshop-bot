"""SQLite: мастера, брони, отметки о напоминаниях. Календарь только зеркало этой базы."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# миграций нет: новую колонку добавлять через ALTER TABLE по user_version, иначе from_row упадёт на старой базе
SCHEMA = """
CREATE TABLE IF NOT EXISTS masters (
    tg_id      INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    added_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    master_tg_id  INTEGER NOT NULL,
    master_name   TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    start_ts      INTEGER NOT NULL,
    end_ts        INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | cancelled
    gcal_event_id TEXT,                              -- active+NULL: надо создать; cancelled+NOT NULL: надо удалить
    capacity      INTEGER,                           -- не используется
    created_at    INTEGER NOT NULL,
    cancelled_at  INTEGER,
    CHECK (end_ts > start_ts)
);
CREATE INDEX IF NOT EXISTS idx_bookings_active_start ON bookings(start_ts) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_bookings_master ON bookings(master_tg_id, start_ts);

CREATE TABLE IF NOT EXISTS reminders_sent (
    booking_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    sent_at     INTEGER NOT NULL,
    PRIMARY KEY (booking_id, kind)
);
"""


@dataclass(frozen=True)
class Master:
    tg_id: int
    name: str


@dataclass(frozen=True)
class Booking:
    id: int
    master_tg_id: int
    master_name: str
    title: str
    start_ts: int
    end_ts: int
    status: str
    gcal_event_id: str | None
    capacity: int | None
    created_at: int
    cancelled_at: int | None

    @property
    def active(self) -> bool:
        return self.status == "active"

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Booking:
        return cls(**{k: row[k] for k in cls.__dataclass_fields__})


class ConflictError(Exception):
    """Слот пересекается с другими бронями (с учётом буфера)."""

    def __init__(self, conflicts: list[Booking]):
        super().__init__(f"{len(conflicts)} conflicting booking(s)")
        self.conflicts = conflicts


class Database:
    def __init__(self, path: str, buffer_minutes: int = 0):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        # проба записи: база с чужим владельцем открывается на чтение без ошибок, а падает уже первая бронь
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        self.conn.execute(f"PRAGMA user_version = {int(version)}")
        self.buffer = buffer_minutes * 60

    def close(self) -> None:
        self.conn.close()

    # ---------- мастера ----------

    def add_master(self, tg_id: int, name: str) -> None:
        self.conn.execute(
            "INSERT INTO masters(tg_id, name, added_at) VALUES(?,?,?) "
            "ON CONFLICT(tg_id) DO UPDATE SET name = excluded.name",
            (tg_id, name, int(time.time())),
        )

    def remove_master(self, tg_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM masters WHERE tg_id = ?", (tg_id,))
        return cur.rowcount > 0

    def get_master(self, tg_id: int) -> Master | None:
        row = self.conn.execute("SELECT tg_id, name FROM masters WHERE tg_id = ?", (tg_id,)).fetchone()
        return Master(row["tg_id"], row["name"]) if row else None

    def is_master(self, tg_id: int) -> bool:
        return self.get_master(tg_id) is not None

    def list_masters(self) -> list[Master]:
        rows = self.conn.execute("SELECT tg_id, name FROM masters ORDER BY added_at").fetchall()
        return [Master(r["tg_id"], r["name"]) for r in rows]

    # ---------- брони ----------

    def _conflicts(self, start_ts: int, end_ts: int, exclude_id: int | None = None) -> list[Booking]:
        # конфликт, если между бронями меньше buffer секунд
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' "
            "AND start_ts < ? AND end_ts > ? AND (? IS NULL OR id != ?) ORDER BY start_ts",
            (end_ts + self.buffer, start_ts - self.buffer, exclude_id, exclude_id),
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    def find_conflicts(self, start_ts: int, end_ts: int) -> list[Booking]:
        return self._conflicts(start_ts, end_ts)

    def create_booking(
        self, master_tg_id: int, master_name: str, title: str, start_ts: int, end_ts: int,
        capacity: int | None = None,
    ) -> Booking:
        if end_ts <= start_ts:
            raise ValueError("end_ts must be after start_ts")
        # write-lock сразу, чтобы проверка и вставка были атомарны
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            conflicts = self._conflicts(start_ts, end_ts)
            if conflicts:
                raise ConflictError(conflicts)
            cur = self.conn.execute(
                "INSERT INTO bookings(master_tg_id, master_name, title, start_ts, end_ts, capacity, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (master_tg_id, master_name, title, start_ts, end_ts, capacity, int(time.time())),
            )
            booking_id = cur.lastrowid
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return self.get_booking(booking_id)  # type: ignore[return-value]

    def get_booking(self, booking_id: int) -> Booking | None:
        row = self.conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return Booking.from_row(row) if row else None

    def cancel_booking(self, booking_id: int) -> Booking | None:
        """None, если бронь уже была отменена."""
        cur = self.conn.execute(
            "UPDATE bookings SET status = 'cancelled', cancelled_at = ? WHERE id = ? AND status = 'active'",
            (int(time.time()), booking_id),
        )
        return self.get_booking(booking_id) if cur.rowcount else None

    def list_active(self, from_ts: int, to_ts: int) -> list[Booking]:
        """Активные брони, пересекающие [from_ts, to_ts)."""
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND start_ts < ? AND end_ts > ? ORDER BY start_ts",
            (to_ts, from_ts),
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    def list_by_master(self, master_tg_id: int, from_ts: int) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND master_tg_id = ? AND end_ts > ? ORDER BY start_ts",
            (master_tg_id, from_ts),
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    # ---------- синхронизация с Google Calendar ----------

    def set_gcal_event(self, booking_id: int, event_id: str | None) -> None:
        self.conn.execute("UPDATE bookings SET gcal_event_id = ? WHERE id = ?", (event_id, booking_id))

    def known_event_ids(self) -> set[str]:
        rows = self.conn.execute("SELECT gcal_event_id FROM bookings WHERE gcal_event_id IS NOT NULL").fetchall()
        return {r[0] for r in rows}

    def unsynced_active(self, now_ts: int = 0) -> list[Booking]:
        """Активные брони без события в календаре, прошедшие не берём."""
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND gcal_event_id IS NULL AND end_ts > ? ORDER BY id",
            (now_ts,),
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    def cancelled_with_event(self) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'cancelled' AND gcal_event_id IS NOT NULL ORDER BY id"
        ).fetchall()
        return [Booking.from_row(r) for r in rows]

    # ---------- напоминания ----------

    def reminder_sent(self, booking_id: int, kind: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM reminders_sent WHERE booking_id = ? AND kind = ?", (booking_id, kind)
        ).fetchone()
        return row is not None

    def mark_reminder(self, booking_id: int, kind: str) -> bool:
        """True, если напоминание ещё не слали."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO reminders_sent(booking_id, kind, sent_at) VALUES(?,?,?)",
            (booking_id, kind, int(time.time())),
        )
        return cur.rowcount > 0
