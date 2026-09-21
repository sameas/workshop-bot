from __future__ import annotations

import logging
import time

from aiogram import Bot, html
from aiogram.exceptions import TelegramMigrateToChat, TelegramNetworkError, TelegramRetryAfter, TelegramServerError

from .config import Config
from .db import Booking, Database
from .gcal import CalendarUnavailable, CalEvent, GoogleCalendar, NullCalendar, describe_error, event_id_for
from .slots import Interval, booking_intervals, fmt_booking

log = logging.getLogger(__name__)

# больше 5 за проход руками не удаляют, похоже на сбой или чистку календаря: не отменяем, пишем админам
MAX_DELETIONS_PER_PASS = 5

# проходят сами: ретраим, админов не дёргаем
TRANSIENT_ERRORS = (TelegramNetworkError, TelegramRetryAfter, TelegramServerError)


class CalendarConflict(Exception):
    """Слот занят ручным событием в календаре."""

    def __init__(self, events: list[CalEvent]):
        super().__init__(f"{len(events)} calendar event(s) overlap")
        self.events = events


class SlotInPast(Exception):
    """Время начала уже прошло."""


class Services:
    def __init__(self, bot: Bot, db: Database, cfg: Config, gcal: GoogleCalendar | NullCalendar):
        self.bot = bot
        self.db = db
        self.cfg = cfg
        self.gcal = gcal
        self._chat_id = cfg.masters_chat_id   # сменится, если группа станет супергруппой
        self._admins_told: set[str] = set()   # уже отправленные алерты, чтобы не спамить

    # ---------- сообщения ----------

    async def notify_admins(self, key: str, text: str) -> None:
        """Пишет админам в личку, один раз на key за жизнь процесса."""
        if key in self._admins_told:
            return
        self._admins_told.add(key)
        log.error("%s", text)
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, f"⚠️ {html.quote(text)}")
            except Exception as e:
                log.warning("Не удалось написать админу %s: %s", admin_id, e)

    async def announce(self, text: str, silent: bool = False) -> bool:
        """False значит стоит повторить позже; если чат не задан, True."""
        if self._chat_id is None:
            return True
        try:
            try:
                await self.bot.send_message(self._chat_id, text, disable_notification=silent)
            except TelegramMigrateToChat as e:
                old, self._chat_id = self._chat_id, e.migrate_to_chat_id
                await self.bot.send_message(self._chat_id, text, disable_notification=silent)
                await self.notify_admins(
                    "chat-migrated",
                    f"Чат мастеров стал супергруппой: ID сменился с {old} на {self._chat_id}. "
                    f"Впиши MASTERS_CHAT_ID={self._chat_id} в .env и пересоздай контейнер (docker compose up -d).",
                )
            return True
        except TRANSIENT_ERRORS as e:
            log.warning("Анонс в чат мастеров не ушёл (временный сбой): %s", e)
            return False
        except Exception as e:
            log.warning("Анонс в чат мастеров не ушёл: %s", e)
            await self.notify_admins(
                "announce-failed",
                f"Не могу писать в чат мастеров ({self._chat_id}): {e}. Проверь MASTERS_CHAT_ID и что бот в чате.",
            )
            return False

    async def tell_user(self, tg_id: int, text: str) -> bool:
        try:
            await self.bot.send_message(tg_id, text)
            return True
        except Exception as e:
            log.warning("Не удалось написать пользователю %s: %s", tg_id, e)
            return False

    # ---------- занятость ----------

    async def external_events(self, start_ts: int, end_ts: int) -> list[CalEvent]:
        """События календаря в окне, кроме созданных ботом."""
        if not self.gcal.enabled:
            return []
        try:
            events = await self.gcal.list_events(start_ts, end_ts)
        except Exception as e:
            reason, permanent = describe_error(e)
            log.warning("Google Calendar недоступен: %s", reason)
            if permanent:
                await self.report_calendar_misconfig(reason, blocking=True)
            raise CalendarUnavailable(reason, permanent) from e
        own = self.db.known_event_ids()
        return [ev for ev in events if ev.id not in own]

    async def report_calendar_misconfig(self, reason: str, blocking: bool) -> None:
        """blocking: календарь не читается, бронировать нельзя; иначе не пишется, брони без зеркала."""
        email = getattr(self.gcal, "account_email", "сервис-аккаунта")
        share = f"календарь расшарен на {email} с правом «Внесение изменений в мероприятия»"
        if blocking:
            await self.notify_admins(
                "gcal-misconfig",
                f"Google Calendar не читается ({reason}), бронирование заблокировано. Проверь GCAL_CALENDAR_ID, что "
                f"{share}, что ключ сервис-аккаунта не отозван и часы сервера верны.",
            )
        else:
            await self.notify_admins(
                "gcal-readonly",
                f"Брони создаются, но в Google Calendar не попадают ({reason}). Проверь, что {share} "
                "(а не только «Просмотр»). Накопившиеся брони досинхронизируются сами.",
            )

    async def busy_intervals(self, start_ts: int, end_ts: int) -> list[Interval]:
        busy = booking_intervals(self.db.list_active(start_ts, end_ts))
        busy += [(ev.start_ts, ev.end_ts) for ev in await self.external_events(start_ts, end_ts)]
        return busy

    # ---------- бронь / отмена ----------

    async def book(self, master_tg_id: int, master_name: str, title: str, start_ts: int, end_ts: int) -> Booking:
        """Бросает SlotInPast, ConflictError, CalendarConflict, CalendarUnavailable."""
        if start_ts <= time.time():
            raise SlotInPast()
        buf = self.db.buffer
        events = await self.external_events(start_ts - buf, end_ts + buf)
        hits = [ev for ev in events if ev.start_ts < end_ts + buf and ev.end_ts > start_ts - buf]
        if hits:
            raise CalendarConflict(hits)
        booking = self.db.create_booking(master_tg_id, master_name, title, start_ts, end_ts)
        # анонс до календаря, чтобы медленный Google не тормозил сообщение в чат
        details = fmt_booking(booking, self.cfg.tz, with_master=False)
        await self.announce(f"📅 {html.quote(master_name)} забронировал(а) мастерскую\n{details}")
        await self.sync_to_calendar(booking)
        return self.db.get_booking(booking.id) or booking  # перечитываем ради gcal_event_id

    async def cancel(self, booking_id: int, by_name: str, by_id: int | None = None) -> Booking | None:
        booking = self.db.cancel_booking(booking_id)
        if booking is None:
            return None
        foreign = by_id is not None and by_id != booking.master_tg_id
        details = fmt_booking(booking, self.cfg.tz, with_master=foreign)
        await self.announce(f"❌ {html.quote(by_name)} отменил(а) бронь\n{details}")
        if foreign:
            await self.tell_user(
                booking.master_tg_id,
                f"❌ {html.quote(by_name)} отменил(а) твою бронь\n{fmt_booking(booking, self.cfg.tz, with_master=False)}",
            )
        await self.remove_from_calendar(booking)
        return booking

    # ---------- зеркало в календаре ----------

    async def sync_to_calendar(self, booking: Booking) -> bool:
        if not self.gcal.enabled or booking.gcal_event_id:
            return True
        try:
            event_id = await self.gcal.create_event(booking)
        except Exception as e:
            reason, permanent = describe_error(e)
            log.warning("Google Calendar: не удалось создать событие для брони #%s: %s", booking.id, reason)
            if permanent:
                await self.report_calendar_misconfig(reason, blocking=False)
            return False
        self.db.set_gcal_event(booking.id, event_id)
        return True

    async def remove_from_calendar(self, booking: Booking) -> bool:
        if not self.gcal.enabled:
            # id оставляем, после включения календаря фоновая задача удалит событие
            return not booking.gcal_event_id
        # id в базе может не быть, а событие есть (ответ Google потерялся): удаляем по вычисляемому id,
        # иначе сирота будет блокировать слот как ручное событие
        event_id = booking.gcal_event_id or event_id_for(booking)
        try:
            await self.gcal.delete_event(event_id)
        except Exception as e:
            if not booking.gcal_event_id:
                self.db.set_gcal_event(booking.id, event_id)  # чтобы фоновая задача повторила удаление
            log.warning("Google Calendar: не удалось удалить событие брони #%s: %s", booking.id, describe_error(e)[0])
            return False
        self.db.set_gcal_event(booking.id, None)
        return True

    async def detect_deleted_events(self) -> None:
        """Отменяет брони, чьи события удалили из календаря: только status=cancelled, при 404 событие пересоздаём."""
        if not self.gcal.enabled:
            return
        bookings = [b for b in self.db.list_active(int(time.time()), 2**40) if b.gcal_event_id]
        if not bookings:
            return
        try:
            statuses = await self.gcal.event_statuses(min(b.start_ts for b in bookings) - 86400,
                                                      max(b.end_ts for b in bookings) + 86400)
        except Exception as e:
            log.warning("Google Calendar: не удалось сверить события с бронями: %s", describe_error(e)[0])
            return
        deleted: list[Booking] = []
        for b in bookings:
            status = statuses.get(b.gcal_event_id, "missing")
            if status == "missing":  # не попало в окно: перенесли далеко или события нет, спрашиваем точечно
                try:
                    status = await self.gcal.event_status(b.gcal_event_id)
                except Exception as e:
                    log.warning("Google Calendar: не удалось проверить событие брони #%s: %s", b.id, describe_error(e)[0])
                    continue
                if status is None:
                    log.warning("Google Calendar: события брони #%s в календаре нет, создам заново", b.id)
                    self.db.set_gcal_event(b.id, None)
                    continue
            if status == "cancelled":
                deleted.append(b)
        if len(deleted) > MAX_DELETIONS_PER_PASS:
            ids = ", ".join(f"#{b.id}" for b in deleted)
            await self.notify_admins(
                f"mass-deletion:{ids}",
                f"Из Google Calendar разом пропали события {len(deleted)} броней ({ids}). Автоматически их не отменяю, "
                "проверь календарь. Отменить брони можно через /all.",
            )
            return
        for b in deleted:
            await self.cancel_deleted_in_calendar(b)

    async def cancel_deleted_in_calendar(self, b: Booking) -> None:
        booking = self.db.cancel_booking(b.id)
        if booking is None:
            return
        self.db.set_gcal_event(b.id, None)
        log.info("Бронь #%s отменена: её событие удалили из Google Calendar", b.id)
        await self.announce(f"❌ Бронь отменена — её событие удалили из Google Calendar\n{fmt_booking(booking, self.cfg.tz)}")
        await self.tell_user(
            booking.master_tg_id,
            f"❌ Твоя бронь отменена — её событие удалили из Google Calendar\n"
            f"{fmt_booking(booking, self.cfg.tz, with_master=False)}\n\nЕсли это ошибка, забронируй заново: /book",
        )

    # ---------- проверки на старте ----------

    async def startup_checks(self) -> None:
        if not self.cfg.admin_ids:
            log.warning("ADMIN_IDS пуст, добавлять мастеров некому. Напиши боту /start, впиши свой ID в .env.")
        if self._chat_id is not None:
            try:
                await self.bot.get_chat(self._chat_id)
            except TelegramMigrateToChat as e:
                await self.notify_admins(
                    "chat-migrated", f"Чат мастеров стал супергруппой: впиши MASTERS_CHAT_ID={e.migrate_to_chat_id} в .env."
                )
                self._chat_id = e.migrate_to_chat_id
            except TRANSIENT_ERRORS as e:
                log.warning("Не удалось проверить чат мастеров (временный сбой): %s", e)
            except Exception as e:
                await self.notify_admins(
                    "announce-failed",
                    f"Чат мастеров {self._chat_id} недоступен: {e}. Проверь MASTERS_CHAT_ID и что бот в чате.",
                )
        if isinstance(self.gcal, GoogleCalendar):
            try:
                await self.gcal.probe()
                log.info("Google Calendar: чтение календаря проверено")
            except Exception as e:
                reason, permanent = describe_error(e)
                if permanent:
                    await self.report_calendar_misconfig(reason, blocking=True)
                else:
                    log.warning("Google Calendar пока не отвечает (%s), продолжаю, попробую при первой брони", reason)
