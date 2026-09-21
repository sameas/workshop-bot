from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .db import Booking
from .services import TRANSIENT_ERRORS, Services
from .slots import fmt_booking

log = logging.getLogger(__name__)

QUIET_HOURS = (23, 8)


async def sync_calendar(svc: Services) -> None:
    if not svc.gcal.enabled:
        return
    for b in svc.db.unsynced_active(int(time.time())):
        await svc.sync_to_calendar(b)
    for b in svc.db.cancelled_with_event():
        await svc.remove_from_calendar(b)
    await svc.detect_deleted_events()


def _label(minutes: int, b: Booking, now: datetime) -> str:
    start = datetime.fromtimestamp(b.start_ts, now.tzinfo)
    if minutes < 360:
        if minutes % 60 == 0:
            h = minutes // 60
            return "Через час" if h == 1 else f"Через {h} ч"
        return f"Через {minutes} мин"
    # дальние считаем по датам, иначе "завтра" про МК в 00:30 врёт на сутки
    if start.date() == now.date():
        return f"Сегодня в {start:%H:%M}"
    if start.date() == now.date() + timedelta(days=1):
        return f"Завтра в {start:%H:%M}"
    return f"Через {(start.date() - now.date()).days} дн."


async def _send_dm(svc: Services, b: Booking, text: str) -> bool:
    """False значит стоит повторить позже."""
    try:
        await svc.bot.send_message(b.master_tg_id, text)
    except TRANSIENT_ERRORS as e:
        log.warning("Напоминание мастеру %s не ушло, повторю: %s", b.master_tg_id, e)
        return False
    except Exception as e:
        log.warning("Напоминание мастеру %s не доставить: %s", b.master_tg_id, e)
    return True


async def send_reminders(svc: Services) -> None:
    now = int(time.time())
    cfg = svc.cfg
    now_dt = datetime.fromtimestamp(now, cfg.tz)
    quiet = now_dt.hour >= QUIET_HOURS[0] or now_dt.hour < QUIET_HOURS[1]
    for minutes in cfg.remind_before_minutes:
        lead = minutes * 60
        # окно [start - lead, +tolerance): если бот лежал дольше, пропускаем; внутри окна ретраим каждую минуту
        tolerance = min(lead, 3600)
        for b in svc.db.list_active(now + lead - tolerance, now + lead + 1):
            if b.start_ts - lead > now or b.start_ts - lead + tolerance <= now:
                continue
            if b.created_at > b.start_ts - lead:
                continue  # бронь создали позже момента напоминания
            kind = f"m{minutes}"
            if svc.db.reminder_sent(b.id, kind):  # отметка старого формата: ушло и в личку, и в чат
                continue
            text = f"⏰ {_label(minutes, b, now_dt)} мастер-класс\n{fmt_booking(b, cfg.tz)}"
            try:
                # отметка после отправки и отдельно на личку и чат, чтобы сбой Telegram не съел напоминание
                if not svc.db.reminder_sent(b.id, f"{kind}:dm") and await _send_dm(svc, b, text):
                    svc.db.mark_reminder(b.id, f"{kind}:dm")
                if not svc.db.reminder_sent(b.id, f"{kind}:chat") and await svc.announce(text, silent=quiet):
                    svc.db.mark_reminder(b.id, f"{kind}:chat")
            except Exception:
                log.exception("Напоминание по брони #%s сорвалось", b.id)


def build_scheduler(svc: Services) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=svc.cfg.tz)
    sched.add_job(sync_calendar, "interval", minutes=5, args=[svc], id="gcal_sync",
                  max_instances=1, coalesce=True)
    sched.add_job(send_reminders, "interval", minutes=1, args=[svc], id="reminders",
                  max_instances=1, coalesce=True)
    return sched
