from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from .config import ConfigError
from .db import Booking

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
HTTP_TIMEOUT = 10  # сек; дефолтные 60 у googleapiclient мастер на кнопке ждать не будет
QUOTA_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}  # тоже 403, но не ошибка настройки


@dataclass(frozen=True)
class CalEvent:
    id: str
    start_ts: int
    end_ts: int
    summary: str


class CalendarUnavailable(Exception):
    """Календарь включён, но не ответил. permanent: ошибка настройки, ретрай не поможет."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def event_id_for(b: Booking) -> str:
    # детерминированный id (алфавит base32hex): повторная вставка даёт 409, а не дубль
    # created_at разводит базы, пересозданные с нуля на том же календаре
    return f"bk{b.id}t{b.created_at}"


def describe_error(e: Exception) -> tuple[str, bool]:
    """Возвращает (описание, постоянная ли ошибка)."""
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError

    if isinstance(e, HttpError):
        status = e.resp.status
        details = e.error_details if isinstance(e.error_details, list) else []
        throttled = any(isinstance(d, dict) and d.get("reason") in QUOTA_REASONS for d in details)
        return f"HTTP {status}: {e.reason}", 400 <= status < 500 and status != 429 and not throttled
    if isinstance(e, RefreshError):  # ключ отозван или часы сервера сбиты
        return f"ключ сервис-аккаунта не принят: {e}", not getattr(e, "retryable", False)
    return f"{type(e).__name__}: {e}", False


class NullCalendar:
    enabled = False

    async def create_event(self, booking: Booking) -> str | None:
        return None

    async def delete_event(self, event_id: str) -> None:
        return None

    async def list_events(self, start_ts: int, end_ts: int) -> list[CalEvent]:
        return []

    async def event_statuses(self, start_ts: int, end_ts: int) -> dict[str, str]:
        return {}

    async def event_status(self, event_id: str) -> str | None:
        return None


class GoogleCalendar:
    enabled = True

    def __init__(self, sa_file: str, calendar_id: str, tz: ZoneInfo):
        import httplib2
        from google.oauth2 import service_account
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(sa_file, scopes=SCOPES)
        self.account_email: str = creds.service_account_email
        http = AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))
        # без cache_discovery=False googleapiclient ругается на отсутствие file_cache
        self._svc = build("calendar", "v3", http=http, cache_discovery=False)
        self._cal = calendar_id
        self._tz = tz
        # httplib2 не потокобезопасен, а вызовы идут из to_thread
        self._lock = threading.Lock()

    # ---- запись ----

    def _event_body(self, b: Booking) -> dict:
        title = f"МК: {b.title}" if b.title else "Мастер-класс"
        return {
            "id": event_id_for(b),
            "summary": f"{title} — {b.master_name}",
            "description": f"Мастер: {b.master_name}\nБронь #{b.id} из Telegram-бота",
            "start": {"dateTime": datetime.fromtimestamp(b.start_ts, self._tz).isoformat(), "timeZone": str(self._tz)},
            "end": {"dateTime": datetime.fromtimestamp(b.end_ts, self._tz).isoformat(), "timeZone": str(self._tz)},
        }

    def _create(self, b: Booking) -> str:
        from googleapiclient.errors import HttpError

        body = self._event_body(b)
        try:
            with self._lock:
                self._svc.events().insert(calendarId=self._cal, body=body).execute(num_retries=1)
        except HttpError as e:
            if e.resp.status != 409:  # 409: событие уже создано прошлой попыткой
                raise
        return body["id"]

    def _delete(self, event_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            with self._lock:
                self._svc.events().delete(calendarId=self._cal, eventId=event_id).execute(num_retries=2)
        except HttpError as e:
            if e.resp.status in (404, 410):  # уже удалено руками
                return
            raise

    # ---- чтение ----

    def _parse_edge(self, edge: dict) -> int:
        if "dateTime" in edge:
            return int(datetime.fromisoformat(edge["dateTime"]).timestamp())
        # all-day: date это локальная дата, end не включается
        return int(datetime.combine(date.fromisoformat(edge["date"]), time(0), tzinfo=self._tz).timestamp())

    def _list(self, start_ts: int, end_ts: int, limit: int | None = None) -> list[CalEvent]:
        out: list[CalEvent] = []
        page = None
        while True:
            with self._lock:
                res = self._svc.events().list(
                    calendarId=self._cal,
                    timeMin=datetime.fromtimestamp(start_ts, self._tz).isoformat(),
                    timeMax=datetime.fromtimestamp(end_ts, self._tz).isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=limit or 250,
                    pageToken=page,
                ).execute(num_retries=1)  # этого чтения ждёт мастер на кнопке, долго ретраить нельзя
            for ev in res.get("items", []):
                if ev.get("status") == "cancelled" or ev.get("transparency") == "transparent":
                    continue  # удалённые и помеченные "свободен"
                out.append(CalEvent(
                    id=ev["id"],
                    start_ts=self._parse_edge(ev["start"]),
                    end_ts=self._parse_edge(ev["end"]),
                    summary=ev.get("summary") or "(без названия)",
                ))
            page = res.get("nextPageToken")
            if not page or limit:
                return out

    def _statuses(self, start_ts: int, end_ts: int) -> dict[str, str]:
        """id -> status по всем событиям окна, включая удалённые (cancelled)."""
        out: dict[str, str] = {}
        page = None
        while True:
            with self._lock:
                res = self._svc.events().list(
                    calendarId=self._cal,
                    timeMin=datetime.fromtimestamp(start_ts, self._tz).isoformat(),
                    timeMax=datetime.fromtimestamp(end_ts, self._tz).isoformat(),
                    singleEvents=True,
                    showDeleted=True,
                    maxResults=250,
                    pageToken=page,
                ).execute(num_retries=2)
            for ev in res.get("items", []):
                out[ev["id"]] = ev.get("status", "confirmed")
            page = res.get("nextPageToken")
            if not page:
                return out

    def _status(self, event_id: str) -> str | None:
        """None, если события в календаре нет вообще (404/410)."""
        from googleapiclient.errors import HttpError

        try:
            with self._lock:
                ev = self._svc.events().get(calendarId=self._cal, eventId=event_id).execute(num_retries=2)
        except HttpError as e:
            if e.resp.status in (404, 410):
                return None
            raise
        return ev.get("status", "confirmed")

    async def create_event(self, booking: Booking) -> str | None:
        return await asyncio.to_thread(self._create, booking)

    async def event_statuses(self, start_ts: int, end_ts: int) -> dict[str, str]:
        return await asyncio.to_thread(self._statuses, start_ts, end_ts)

    async def event_status(self, event_id: str) -> str | None:
        return await asyncio.to_thread(self._status, event_id)

    async def delete_event(self, event_id: str) -> None:
        await asyncio.to_thread(self._delete, event_id)

    async def list_events(self, start_ts: int, end_ts: int) -> list[CalEvent]:
        return await asyncio.to_thread(self._list, start_ts, end_ts)

    async def probe(self) -> None:
        """Дешёвый запрос для проверки доступа на старте."""
        now = int(datetime.now(self._tz).timestamp())
        await asyncio.to_thread(self._list, now, now + 86400, 1)


def make_calendar(sa_file: str | None, calendar_id: str | None, tz: ZoneInfo) -> GoogleCalendar | NullCalendar:
    if not (sa_file and calendar_id):
        log.warning("Google Calendar: выключен (GCAL_CALENDAR_ID / GOOGLE_SA_FILE не заданы)")
        return NullCalendar()
    try:
        cal = GoogleCalendar(sa_file, calendar_id, tz)
    except (OSError, ValueError) as e:
        raise ConfigError(
            f"GCAL_CALENDAR_ID задан, но {sa_file} не читается как JSON-ключ сервис-аккаунта ({e}). "
            "Положи ключ в secrets/google-sa.json и дай на него права uid 10001, либо очисти GCAL_CALENDAR_ID."
        ) from None
    log.info("Google Calendar: включён (%s), сервис-аккаунт %s", calendar_id, cal.account_email)
    return cal
