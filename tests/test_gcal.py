"""Тесты gcal без сети, запросы через HttpMockSequence."""
from __future__ import annotations

import json
import threading

import pytest
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpMockSequence

from bot.config import ConfigError
from bot.db import Database
from bot.gcal import GoogleCalendar, describe_error, event_id_for, make_calendar

from .test_core import TZ


def fake_calendar(responses) -> tuple[GoogleCalendar, HttpMockSequence]:
    http = HttpMockSequence(responses)
    cal = GoogleCalendar.__new__(GoogleCalendar)
    cal._svc = build("calendar", "v3", http=http, cache_discovery=False)
    cal._cal, cal._tz, cal._lock = "cal@group.calendar.google.com", TZ, threading.Lock()
    return cal, http


@pytest.fixture
def booking():
    db = Database(":memory:")
    return db.create_booking(1, "Маша", "Свечи", 2_000_000_000, 2_000_032_400)


def test_insert_sends_deterministic_id_and_409_is_success(booking):
    cal, http = fake_calendar([({"status": "200"}, json.dumps({"id": event_id_for(booking)}))])
    assert cal._create(booking) == event_id_for(booking)
    body = json.loads(http.request_sequence[0][2])
    assert body["id"] == event_id_for(booking) and body["summary"] == "МК: Свечи — Маша"
    assert body["start"]["timeZone"] == "Asia/Novosibirsk" and body["end"]["dateTime"] > body["start"]["dateTime"]

    # событие уже создано прошлой попыткой, не ошибка и не дубль
    cal, _ = fake_calendar([({"status": "409"}, '{"error": {"message": "The requested identifier already exists."}}')])
    assert cal._create(booking) == event_id_for(booking)

    cal, _ = fake_calendar([({"status": "403"}, '{"error": {"message": "Forbidden"}}')])
    with pytest.raises(HttpError) as ei:
        cal._create(booking)
    assert describe_error(ei.value) == ("HTTP 403: Forbidden", True)


def test_delete_of_missing_event_is_success():
    cal, _ = fake_calendar([({"status": "410"}, '{"error": {"message": "Resource has been deleted"}}')])
    cal._delete("bk1t1")


def test_list_parses_timed_allday_and_skips_free(booking):
    items = [
        {"id": "a", "summary": "Аренда", "start": {"dateTime": "2026-10-03T14:00:00+07:00"},
         "end": {"dateTime": "2026-10-03T16:00:00+07:00"}},
        {"id": "b", "start": {"date": "2026-10-04"}, "end": {"date": "2026-10-05"}},
        {"id": "c", "summary": "свободен", "transparency": "transparent",
         "start": {"dateTime": "2026-10-03T10:00:00+07:00"}, "end": {"dateTime": "2026-10-03T11:00:00+07:00"}},
    ]
    cal, http = fake_calendar([({"status": "200"}, json.dumps({"items": items}))])
    events = cal._list(1_790_000_000, 1_790_600_000)
    assert [(e.id, e.summary) for e in events] == [("a", "Аренда"), ("b", "(без названия)")]
    assert events[0].end_ts - events[0].start_ts == 7200 and events[1].end_ts - events[1].start_ts == 86400
    assert "singleEvents=true" in http.request_sequence[0][0]


def test_transient_errors_are_not_permanent(monkeypatch):
    monkeypatch.setattr("googleapiclient.http.time.sleep", lambda s: None)
    assert describe_error(ConnectionError("x"))[1] is False
    cal, _ = fake_calendar([({"status": "429"}, '{"error": {"message": "Rate Limit Exceeded"}}')] * 3)
    with pytest.raises(HttpError) as ei:
        cal._list(0, 1)
    assert describe_error(ei.value)[1] is False


def test_statuses_include_deleted_and_get_handles_missing():
    items = [{"id": "bk1t1", "status": "confirmed"}, {"id": "bk2t2", "status": "cancelled"}, {"id": "manual"}]
    cal, http = fake_calendar([({"status": "200"}, json.dumps({"items": items}))])
    assert cal._statuses(0, 1) == {"bk1t1": "confirmed", "bk2t2": "cancelled", "manual": "confirmed"}
    assert "showDeleted=true" in http.request_sequence[0][0]

    cal, _ = fake_calendar([({"status": "200"}, json.dumps({"id": "bk2t2", "status": "cancelled"}))])
    assert cal._status("bk2t2") == "cancelled"
    cal, _ = fake_calendar([({"status": "404"}, '{"error": {"message": "Not Found"}}')])
    assert cal._status("bk9t9") is None


def test_error_classification():
    import httplib2
    from google.auth.exceptions import RefreshError

    def http_error(status, reason):
        body = json.dumps({"error": {"message": reason, "errors": [{"reason": reason}]}}).encode()
        return HttpError(httplib2.Response({"status": status}), body)
    assert describe_error(http_error(404, "notFound"))[1] is True
    assert describe_error(http_error(403, "forbidden"))[1] is True
    assert describe_error(http_error(403, "rateLimitExceeded"))[1] is False   # квота - не ошибка настройки
    assert describe_error(http_error(500, "backendError"))[1] is False
    # отозванный ключ сам не починится, это к админу
    text, permanent = describe_error(RefreshError("invalid_grant: Invalid JWT Signature."))
    assert permanent is True and "ключ" in text


def test_stub_key_gives_readable_config_error(tmp_path):
    stub = tmp_path / "google-sa.json"
    stub.write_text("{}")
    with pytest.raises(ConfigError, match="GCAL_CALENDAR_ID"):
        make_calendar(str(stub), "cal@group.calendar.google.com", TZ)
    with pytest.raises(ConfigError):
        make_calendar(str(tmp_path / "missing.json"), "cal@group.calendar.google.com", TZ)
    assert make_calendar(str(stub), None, TZ).enabled is False   # календарь выключен, заглушка не мешает


def test_real_constructor_with_generated_key(tmp_path):
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
    from cryptography.hazmat.primitives import serialization

    key = crypto.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    sa = tmp_path / "sa.json"
    sa.write_text(json.dumps({
        "type": "service_account", "client_email": "bot@proj.iam.gserviceaccount.com",
        "private_key": pem.decode(), "token_uri": "https://oauth2.googleapis.com/token",
    }))
    cal = make_calendar(str(sa), "cal@group.calendar.google.com", TZ)
    assert cal.enabled and cal.account_email == "bot@proj.iam.gserviceaccount.com"
    assert cal._svc._http.http.timeout == 10
