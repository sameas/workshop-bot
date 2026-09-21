from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

MAX_DURATION_MINUTES = 24 * 60
MIN_DURATION_MINUTES = 15


class ConfigError(RuntimeError):
    """Ошибка в настройках, бот не стартует."""


def _int(name: str, default: str) -> int:
    raw = os.environ.get(name, "").strip() or default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r}: нужно целое число") from None


def _int_list(name: str, default: str = "", empty_ok: bool = False) -> tuple[int, ...]:
    """empty_ok: пустая переменная значит "ничего", а не дефолт."""
    raw = os.environ.get(name)
    raw = default if raw is None or (not raw.strip() and not empty_ok) else raw.strip()
    try:
        return tuple(int(x) for x in raw.replace(";", ",").split(",") if x.strip())
    except ValueError:
        raise ConfigError(f"{name}={raw!r}: нужны целые числа через запятую") from None


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    masters_chat_id: int | None      # None = анонсы выключены
    db_path: str
    tz: ZoneInfo
    buffer_minutes: int              # пауза между МК
    slot_step_minutes: int
    durations_minutes: tuple[int, ...]
    days_ahead: int
    gcal_calendar_id: str | None
    google_sa_file: str | None
    remind_before_minutes: tuple[int, ...]

    def __post_init__(self) -> None:
        step = self.slot_step_minutes
        # шаг должен делить сутки; мельче 15 мин клавиатура не влезет в лимит телеграма
        if not (15 <= step <= 240 and 1440 % step == 0):
            raise ConfigError(f"SLOT_STEP_MINUTES={step}: нужен делитель суток от 15 до 240, например 15, 20, 30, 60")
        if not 0 <= self.buffer_minutes <= 720:
            raise ConfigError(f"BUFFER_MINUTES={self.buffer_minutes}: допустимо от 0 до 720")
        if not self.durations_minutes:
            raise ConfigError("DURATIONS_MINUTES пуст")
        bad = [m for m in self.durations_minutes if not MIN_DURATION_MINUTES <= m <= MAX_DURATION_MINUTES]
        if bad:
            raise ConfigError(f"DURATIONS_MINUTES: {bad} вне {MIN_DURATION_MINUTES}-{MAX_DURATION_MINUTES} минут")
        if not 1 <= self.days_ahead <= 90:
            raise ConfigError(f"DAYS_AHEAD={self.days_ahead}: допустимо от 1 до 90")
        if any(m <= 0 for m in self.remind_before_minutes):
            raise ConfigError("REMIND_BEFORE_MINUTES: только положительные числа")

    @property
    def gcal_enabled(self) -> bool:
        return bool(self.gcal_calendar_id and self.google_sa_file)

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("BOT_TOKEN не задан")
        chat = _int_list("MASTERS_CHAT_ID")
        tz_name = os.environ.get("TZ", "").strip() or "Asia/Novosibirsk"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            raise ConfigError(f"TZ={tz_name!r}: неизвестный часовой пояс") from None
        return cls(
            bot_token=token,
            admin_ids=frozenset(_int_list("ADMIN_IDS")),
            masters_chat_id=chat[0] if chat else None,
            db_path=os.environ.get("DB_PATH", "data/bot.db"),
            tz=tz,
            buffer_minutes=_int("BUFFER_MINUTES", "30"),
            slot_step_minutes=_int("SLOT_STEP_MINUTES", "30"),
            durations_minutes=tuple(sorted(set(_int_list("DURATIONS_MINUTES", "60,90,120,180,240,300,360,480,540,600,720")))),
            days_ahead=_int("DAYS_AHEAD", "21"),
            gcal_calendar_id=os.environ.get("GCAL_CALENDAR_ID", "").strip() or None,
            google_sa_file=os.environ.get("GOOGLE_SA_FILE", "").strip() or None,
            remind_before_minutes=_int_list("REMIND_BEFORE_MINUTES", "1440,60", empty_ok=True),
        )
