"""E2E: апдейты Telegram -> диспетчер -> фейковая сессия Bot API."""
from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Any

import httplib2
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
)
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetChat,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, ChatFullInfo, Message, MessageEntity, Update, User
from googleapiclient.errors import HttpError

from bot import handlers, tasks
from bot.db import Database
from bot.gcal import CalEvent, NullCalendar, event_id_for
from bot.handlers import BookCb, CancelCb
from bot.main import build_dispatcher
from bot.services import MAX_DELETIONS_PER_PASS, Services
from bot.slots import fmt_date

from .test_core import TZ, make_cfg

ADMIN = User(id=1, is_bot=False, first_name="Admin")
MASHA = User(id=10, is_bot=False, first_name="Маша")
DASHA = User(id=11, is_bot=False, first_name="Даша")
STRANGER = User(id=99, is_bot=False, first_name="Некто")
PRIVATE = {u.id: Chat(id=u.id, type="private") for u in (ADMIN, MASHA, DASHA, STRANGER)}
GROUP = Chat(id=-100500, type="supergroup", title="Мастера")


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._mid = 100
        self.last_mid: dict[int, int] = {}  # chat_id -> id последнего сообщения бота
        self.fail = None  # предикат: какие SendMessage падают сетевой ошибкой
        self.shown: dict[tuple[int, int], tuple] = {}  # что сейчас показано в сообщении, для "not modified"
        self.usernames: dict[int, str | None] = {}  # id -> @тег, None если тега нет

    async def close(self) -> None:
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        if self.fail is not None and isinstance(method, SendMessage) and self.fail(method):
            raise TelegramNetworkError(method=method, message="fake network down")
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=777, is_bot=True, first_name="Bot", username="workshop_bot")
        if isinstance(method, SendMessage):
            self._mid += 1
            self.last_mid[method.chat_id] = self._mid
            return Message(message_id=self._mid, date=datetime.now(TZ), chat=Chat(id=method.chat_id, type="private"),
                           text=method.text, reply_markup=method.reply_markup)
        if isinstance(method, EditMessageText):  # правка не меняет id сообщения
            key, shown = (method.chat_id, method.message_id), (method.text, method.reply_markup)
            if self.shown.get(key) == shown:
                raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
            self.shown[key] = shown
            return Message(message_id=method.message_id, date=datetime.now(TZ),
                           chat=Chat(id=method.chat_id, type="private"),
                           text=method.text, reply_markup=method.reply_markup)
        if isinstance(method, GetChat):  # Telegram знает только тех, кто писал боту
            if method.chat_id not in self.usernames:
                raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
            return ChatFullInfo(id=method.chat_id, type="private", username=self.usernames[method.chat_id],
                                accent_color_id=0, max_reaction_count=0,
                                accepted_gift_types={"unlimited_gifts": True, "limited_gifts": True,
                                                     "unique_gifts": True, "premium_subscription": True,
                                                     "gifts_from_channels": True})
        return True

    # --- helpers ---
    def sent(self, cls=SendMessage) -> list:
        return [c for c in self.calls if isinstance(c, cls)]

    def last_text(self) -> str:
        for c in reversed(self.calls):
            if isinstance(c, (SendMessage, EditMessageText)):
                return c.text
        raise AssertionError("no messages sent")

    def last_markup(self):
        for c in reversed(self.calls):
            if isinstance(c, (SendMessage, EditMessageText)):
                return c.reply_markup
        raise AssertionError("no messages sent")

    def buttons(self) -> dict[str, str]:
        """text → callback_data последней клавиатуры."""
        mk = self.last_markup()
        assert mk is not None, "last message has no keyboard"
        return {b.text: b.callback_data for row in mk.inline_keyboard for b in row}


class Env:
    def __init__(self, cfg):
        self.session = FakeSession()
        self.bot = Bot("42:TEST", session=self.session)
        self.db = Database(":memory:", cfg.buffer_minutes)
        self.cfg = cfg
        self.svc = Services(self.bot, self.db, cfg, NullCalendar())
        self.dp = build_dispatcher(self.bot, self.db, cfg, self.svc)
        self._uid = 0
        self._mid = 0

    async def msg(self, user: User, text: str, chat: Chat | None = None, reply_to: Message | None = None):
        self._uid += 1
        self._mid += 1
        chat = chat or PRIVATE[user.id]
        entities = []
        if text.startswith("/"):
            cmd = text.split()[0]
            entities = [MessageEntity(type="bot_command", offset=0, length=len(cmd))]
        m = Message(message_id=self._mid, date=datetime.now(TZ), chat=chat, from_user=user, text=text,
                    entities=entities, reply_to_message=reply_to)
        await self.dp.feed_update(self.bot, Update(update_id=self._uid, message=m))
        return m

    async def click(self, user: User, data: str, message_id: int | None = None):
        """Тап по кнопке под последним сообщением бота в личке (или под message_id)."""
        self._uid += 1
        mid = message_id or self.session.last_mid.get(user.id, 1)
        m = Message(message_id=mid, date=datetime.now(TZ), chat=PRIVATE[user.id], text="…")
        cq = CallbackQuery(id=str(self._uid), from_user=user, chat_instance="ci", message=m, data=data)
        await self.dp.feed_update(self.bot, Update(update_id=self._uid, callback_query=cq))

    async def click_text(self, user: User, pattern: str):
        btns = self.session.buttons()
        for text, data in btns.items():
            if re.fullmatch(pattern, text):
                await self.click(user, data)
                return
        raise AssertionError(f"button {pattern!r} not in {list(btns)}")


@pytest.fixture
def env():
    cfg = make_cfg(masters_chat_id=GROUP.id, days_ahead=14)
    e = Env(cfg)
    e.db.add_master(MASHA.id, "Маша")
    e.db.add_master(DASHA.id, "Даша")
    return e


def tomorrow_label() -> str:
    return fmt_date(datetime.now(TZ).date() + timedelta(days=1))


async def book(env: Env, user: User, dur_label: str, time_label: str, title: str = "-"):
    await env.msg(user, "/book")
    await env.click_text(user, re.escape(tomorrow_label()))
    await env.click_text(user, re.escape(dur_label))
    await env.click_text(user, time_label)
    await env.msg(user, title)
    await env.click_text(user, "✅ Забронировать")


async def test_stranger_denied(env):
    await env.msg(STRANGER, "/start")
    assert "addmaster 99" in env.session.last_text()
    await env.msg(STRANGER, "/book")
    assert "только для мастеров" in env.session.last_text()


async def test_full_booking_and_conflict(env):
    await book(env, MASHA, "2 ч", "14:00", "Кольцо с камнем")
    assert "✅ Забронировано" in env.session.last_text()
    assert "Кольцо с камнем" in env.session.last_text()

    announces = [c for c in env.session.sent() if c.chat_id == GROUP.id]
    assert len(announces) == 1 and "Маша" in announces[0].text and "14:00–16:00" in announces[0].text

    # Даша бронирует тот же день: 14:00-16:30 с буфером недоступны
    await env.msg(DASHA, "/book")
    await env.click_text(DASHA, re.escape(tomorrow_label()))
    await env.click_text(DASHA, "1 ч")
    times = list(env.session.buttons())
    assert "12:30" in times and "13:00" not in times and "16:00" not in times and "16:30" in times

    # /week видит бронь Маши
    await env.msg(DASHA, "/week")
    assert "Маша" in env.session.last_text() and "Кольцо" in env.session.last_text()


async def test_race_lost_at_confirm(env):
    # Даша дошла до подтверждения, но Маша заняла слот раньше
    await env.msg(DASHA, "/book")
    await env.click_text(DASHA, re.escape(tomorrow_label()))
    await env.click_text(DASHA, "1 ч")
    await env.click_text(DASHA, "15:00")
    await env.msg(DASHA, "-")
    await book(env, MASHA, "2 ч", "14:00")
    # кнопка подтверждения под сообщением Даши, жмём напрямую
    await env.click(DASHA, BookCb(step="confirm").pack())
    assert "уже занято" in env.session.last_text()
    assert len(env.db.list_active(0, 2**40)) == 1


async def test_cancel_own_only(env):
    await book(env, MASHA, "1 ч", "12:00")
    b = env.db.list_active(0, 2**40)[0]
    # Даша не может отменить чужую
    await env.click(DASHA, CancelCb(booking_id=b.id).pack())
    assert env.db.get_booking(b.id).active
    # Маша может
    await env.msg(MASHA, "/my")
    assert f"#{b.id}" in env.session.last_text()
    await env.click_text(MASHA, f"Отменить #{b.id}")
    await env.click_text(MASHA, "Да, отменить")
    assert not env.db.get_booking(b.id).active
    assert "отменена" in env.session.last_text()
    announces = [c for c in env.session.sent() if c.chat_id == GROUP.id]
    assert "отменил" in announces[-1].text


async def test_admin_addmaster_by_reply_in_group(env):
    victim = await env.msg(STRANGER, "привет", chat=GROUP)
    await env.msg(ADMIN, "/addmaster", chat=GROUP, reply_to=victim)
    assert env.db.is_master(STRANGER.id)
    await env.msg(STRANGER, "/delmaster 99", chat=GROUP)
    assert env.db.is_master(STRANGER.id)
    await env.msg(ADMIN, "/delmaster 99")
    assert not env.db.is_master(STRANGER.id)


async def test_book_in_group_redirects(env):
    await env.msg(MASHA, "/book@workshop_bot", chat=GROUP)
    assert "в личке" in env.session.last_text()


async def test_stale_button(env):
    await env.click(MASHA, BookCb(step="date", value=date.today().isoformat()).pack())
    ans = env.session.sent(AnswerCallbackQuery)[-1]
    assert "старая" in ans.text


async def test_reminders(env, monkeypatch):

    d2 = datetime.now(TZ).date() + timedelta(days=2)
    start = int(datetime(d2.year, d2.month, d2.day, 12, 30, tzinfo=TZ).timestamp())  # МК послезавтра в 12:30
    env.db.create_booking(MASHA.id, "Маша", "Серьги", start, start + 3600)

    # за 24 ч (окно [start-24h, start-23h)) ещё рано
    await tasks.send_reminders(env.svc)
    assert env.session.sent() == []

    monkeypatch.setattr(tasks.time, "time", lambda: start - 1440 * 60 + 60)
    await tasks.send_reminders(env.svc)
    msgs = env.session.sent()
    assert {m.chat_id for m in msgs} == {MASHA.id, GROUP.id}
    assert "Завтра" in msgs[0].text and "Серьги" in msgs[0].text

    # повторный тик без дублей
    await tasks.send_reminders(env.svc)
    assert len(env.session.sent()) == 2

    # за час
    monkeypatch.setattr(tasks.time, "time", lambda: start - 3600 + 5)
    await tasks.send_reminders(env.svc)
    assert "Через час" in env.session.sent()[-1].text

    # проспали 24-часовое напоминание другой брони: с опозданием не шлём
    b2 = env.db.create_booking(DASHA.id, "Даша", "", start + 7200 + 1800, start + 7200 + 5400)
    monkeypatch.setattr(tasks.time, "time", lambda: b2.start_ts - 1440 * 60 + 2 * 3600)
    n = len(env.session.sent())
    await tasks.send_reminders(env.svc)
    assert len(env.session.sent()) == n


# ---------- ручные события в Google Calendar ----------



class FakeCalendar:
    enabled = True

    def __init__(self):
        self.events: list[CalEvent] = []
        self.deleted: set[str] = set()
        self.down = False
        self._n = 0

    async def create_event(self, booking):
        self._n += 1
        ev = CalEvent(f"bot-{self._n}", booking.start_ts, booking.end_ts, "bot")
        self.events.append(ev)
        return ev.id

    async def delete_event(self, event_id):
        self.events = [e for e in self.events if e.id != event_id]

    def manual_delete(self, event_id):
        """Удаление руками: Google ещё помнит событие как удалённое."""
        self.events = [e for e in self.events if e.id != event_id]
        self.deleted.add(event_id)

    async def event_statuses(self, start_ts, end_ts):
        if self.down:
            raise ConnectionError("googleapis unreachable")
        found = {e.id: "confirmed" for e in self.events if e.start_ts < end_ts and e.end_ts > start_ts}
        return found | {i: "cancelled" for i in self.deleted}

    async def event_status(self, event_id):
        if self.down:
            raise ConnectionError("googleapis unreachable")
        if event_id in self.deleted:
            return "cancelled"
        return "confirmed" if any(e.id == event_id for e in self.events) else None

    async def list_events(self, start_ts, end_ts):
        if self.down:
            raise ConnectionError("googleapis unreachable")
        return [e for e in self.events if e.start_ts < end_ts and e.end_ts > start_ts]


@pytest.fixture
def cal_env(env):
    env.svc.gcal = FakeCalendar()
    return env


def _tomorrow_ts(h, m=0):
    d = datetime.now(TZ).date() + timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=TZ).timestamp())


async def test_manual_event_blocks_slots_and_confirm(cal_env):
    env = cal_env
    env.svc.gcal.events.append(CalEvent("manual1", _tomorrow_ts(14), _tomorrow_ts(16), "Аренда"))
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    times = list(env.session.buttons())
    assert "13:00" not in times and "16:00" not in times and "12:30" in times and "16:30" in times

    # событие добавили руками уже после показа слотов, ловим на подтверждении
    await env.click_text(MASHA, "12:30")
    env.svc.gcal.events.append(CalEvent("manual2", _tomorrow_ts(13), _tomorrow_ts(13, 30), "Курьер"))
    await env.msg(MASHA, "-")
    await env.click_text(MASHA, "✅ Забронировать")
    assert "уже есть событие" in env.session.last_text() and "Курьер" in env.session.last_text()
    assert env.db.list_active(0, 2**40) == []


async def test_own_events_not_counted_as_manual(cal_env):
    env = cal_env
    await book(env, MASHA, "2 ч", "14:00")
    b = env.db.list_active(0, 2**40)[0]
    assert b.gcal_event_id == "bot-1" and len(env.svc.gcal.events) == 1
    # для Даши слот занят бронью (сообщение про бронь, не про календарь), своё событие блокировку не дублирует
    await env.msg(DASHA, "/book")
    await env.click_text(DASHA, re.escape(tomorrow_label()))
    await env.click_text(DASHA, "1 ч")
    assert "16:30" in env.session.buttons()
    assert await env.svc.external_events(0, 2**40) == []


async def test_calendar_down_blocks_booking(cal_env):
    env = cal_env
    env.svc.gcal.down = True
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    assert "не отвечает" in env.session.last_text()
    assert env.db.list_active(0, 2**40) == []


async def test_allday_event_blocks_whole_day(cal_env):
    env = cal_env
    d0 = _tomorrow_ts(0)
    env.svc.gcal.events.append(CalEvent("holiday", d0, d0 + 86400, "Выходной"))
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    assert "Свободного времени нет" in env.session.last_text()


# ---------- круглосуточные брони, ручная длительность ----------

def _day_label(days: int) -> str:
    return fmt_date(datetime.now(TZ).date() + timedelta(days=days))


def last_alert(env: Env) -> str | None:
    return env.session.sent(AnswerCallbackQuery)[-1].text


async def test_overnight_booking_end_to_end(env):
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "9 ч")
    assert "23:30" in env.session.buttons() and "00:00" in env.session.buttons()
    await env.click_text(MASHA, "21:00")
    # экран названия и подтверждение показывают дату конца и длительность
    expected = f"{_day_label(1)} 21:00 – {_day_label(2)} 06:00"
    assert expected in env.session.last_text() and "(9 ч)" in env.session.last_text()
    await env.msg(MASHA, "Ночная керамика")
    assert expected in env.session.last_text()
    await env.click_text(MASHA, "✅ Забронировать")
    assert "✅ Забронировано" in env.session.last_text() and expected in env.session.last_text()
    b = env.db.list_active(0, 2**40)[0]
    assert b.end_ts - b.start_ts == 9 * 3600
    assert expected in [c for c in env.session.sent() if c.chat_id == GROUP.id][0].text

    # Даша: послезавтра раннее утро занято хвостом ночного МК (до 06:00 + буфер)
    await env.msg(DASHA, "/book")
    await env.click_text(DASHA, re.escape(_day_label(2)))
    await env.click_text(DASHA, "1 ч")
    times = list(env.session.buttons())
    assert "05:30" not in times and "06:00" not in times and "06:30" in times

    # /week показывает бронь под днём начала и продолжением под днём конца
    await env.msg(DASHA, "/week")
    assert f"21:00 – {_day_label(2)} 06:00" in env.session.last_text() and "…до 06:00" in env.session.last_text()


async def test_typed_duration(env):
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.msg(MASHA, "сколько-нибудь")
    assert "Не понял длительность" in env.session.last_text()
    await env.msg(MASHA, "2,5")
    assert "2 ч 30 мин" in env.session.last_text()
    await env.click_text(MASHA, "10:00")
    await env.click_text(MASHA, "Без названия")
    await env.click_text(MASHA, "✅ Забронировать")
    b = env.db.list_active(0, 2**40)[0]
    assert (b.end_ts - b.start_ts) == 150 * 60 and b.title == ""


async def test_title_rules_and_fix_at_confirm(env):
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    await env.click_text(MASHA, "12:00")
    await env.msg(MASHA, "x" * 101)
    assert "Слишком длинно" in env.session.last_text()
    await env.msg(MASHA, "Керамика\nдля детей")
    assert "«Керамика для детей»" in env.session.last_text()
    await env.msg(MASHA, "Керамика для взрослых")
    assert "«Керамика для взрослых»" in env.session.last_text()
    await env.click_text(MASHA, "✅ Забронировать")
    assert env.db.list_active(0, 2**40)[0].title == "Керамика для взрослых"


async def test_back_from_title_and_cancel_command(env):
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    await env.click_text(MASHA, "12:00")
    await env.click_text(MASHA, "← Назад")
    assert "12:30" in env.session.buttons()
    await env.click_text(MASHA, "← Назад")
    assert "1 ч" in env.session.buttons()
    await env.msg(MASHA, "/cancel")
    assert "прервано" in env.session.last_text()
    await env.msg(MASHA, "просто текст")
    assert "Не понял" in env.session.last_text()


async def test_master_name_from_addmaster(env):
    env.db.add_master(MASHA.id, "Маша К.")
    await book(env, MASHA, "1 ч", "12:00")
    assert env.db.list_active(0, 2**40)[0].master_name == "Маша К."
    assert "Маша К." in [c for c in env.session.sent() if c.chat_id == GROUP.id][0].text


# ---------- устойчивость к кликам ----------

async def test_repeat_click_same_text_is_harmless(cal_env):
    env = cal_env
    d0 = _tomorrow_ts(0)
    env.svc.gcal.events.append(CalEvent("holiday", d0 - 3600, d0 + 90000, "Выходной"))
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    await env.click_text(MASHA, "1 ч")  # тот же текст: "message is not modified" не должен ронять хендлер
    assert last_alert(env) is None


async def test_double_tap_is_silent_but_stale_button_alerts(env):
    await env.msg(MASHA, "/book")
    date_btn = env.session.buttons()[tomorrow_label()]
    await env.click(MASHA, date_btn)
    await env.click(MASHA, date_btn)  # второй тап по уже пройденному шагу
    assert last_alert(env) is None
    await env.click(MASHA, date_btn, message_id=5)  # кнопка на другом, старом сообщении
    assert "старая" in last_alert(env)
    # двойной тап по «Забронировать»: бронь одна, алерта поверх результата нет
    await env.click_text(MASHA, "1 ч")
    await env.click_text(MASHA, "12:00")
    await env.click_text(MASHA, "Без названия")
    await env.click(MASHA, BookCb(step="confirm").pack())
    await env.click(MASHA, BookCb(step="confirm").pack())
    assert last_alert(env) is None and len(env.db.list_active(0, 2**40)) == 1


async def test_old_abort_button_does_not_kill_new_flow(env):
    await env.msg(MASHA, "/book")
    old = env.session.last_mid[MASHA.id]
    await env.msg(MASHA, "/book")
    date_btn = env.session.buttons()[tomorrow_label()]
    await env.click(MASHA, BookCb(step="abort").pack(), message_id=old)
    await env.click(MASHA, date_btn)
    assert "1 ч" in env.session.buttons()


async def test_past_slot_rejected(env, monkeypatch):
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    slot = env.session.buttons()["12:00"]
    real = time.time
    monkeypatch.setattr("bot.handlers.time.time", lambda: real() + 3 * 86400)  # экран провисел три дня
    await env.click(MASHA, slot)
    assert "уже прошло" in last_alert(env)
    monkeypatch.setattr("bot.handlers.time.time", real)
    # тот же пробел на подтверждении
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    await env.click_text(MASHA, "12:00")
    await env.click_text(MASHA, "Без названия")
    monkeypatch.setattr("bot.services.time.time", lambda: real() + 3 * 86400)
    await env.click_text(MASHA, "✅ Забронировать")
    assert "уже прошло" in env.session.last_text() and env.db.list_active(0, 2**40) == []


# ---------- отмена ----------

async def test_cancel_no_keeps_booking_and_flow(env):
    await book(env, MASHA, "1 ч", "12:00")
    b = env.db.list_active(0, 2**40)[0]
    await env.msg(MASHA, "/my")
    my_msg = env.session.last_mid[MASHA.id]
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    flow_msg = env.session.last_mid[MASHA.id]
    await env.click(MASHA, CancelCb(booking_id=b.id).pack(), message_id=my_msg)
    await env.click(MASHA, CancelCb(booking_id=b.id, action="keep").pack(), message_id=my_msg)
    assert "остаётся" in env.session.last_text() and env.db.get_booking(b.id).active
    await env.click(MASHA, [d for t, d in _buttons_of(env, flow_msg).items() if t == "2 ч"][0], message_id=flow_msg)
    assert "Время начала" in env.session.last_text()


def _buttons_of(env: Env, message_id: int) -> dict[str, str]:
    for c in reversed(env.session.calls):
        if isinstance(c, EditMessageText) and c.message_id == message_id and c.reply_markup:
            return {b.text: b.callback_data for row in c.reply_markup.inline_keyboard for b in row}
    raise AssertionError("no keyboard")


async def test_admin_cancels_foreign_booking_via_all(env):
    await book(env, MASHA, "1 ч", "12:00", "Свечи")
    b = env.db.list_active(0, 2**40)[0]
    await env.msg(MASHA, "/all")
    assert "Не понял" in env.session.last_text()
    await env.msg(ADMIN, "/all")
    assert "Маша" in env.session.last_text() and f"#{b.id}" in env.session.last_text()
    await env.click_text(ADMIN, f"Отменить #{b.id}")
    await env.click_text(ADMIN, "Да, отменить")
    assert not env.db.get_booking(b.id).active
    to_masha = [c.text for c in env.session.sent() if c.chat_id == MASHA.id]
    assert "отменил(а) твою бронь" in to_masha[-1]
    to_group = [c.text for c in env.session.sent() if c.chat_id == GROUP.id]
    assert "Admin отменил" in to_group[-1] and "Маша" in to_group[-1]


async def test_delmaster_hands_bookings_to_admin(env):
    await book(env, MASHA, "1 ч", "12:00")
    b = env.db.list_active(0, 2**40)[0]
    await env.msg(ADMIN, f"/delmaster {MASHA.id}")
    assert "остались брони" in env.session.last_text() and f"Отменить #{b.id}" in env.session.buttons()
    await env.msg(ADMIN, f"/delmaster {MASHA.id}")
    assert "нет" in env.session.last_text()


async def test_addmaster_explicit_id_beats_reply(env):
    other = await env.msg(DASHA, "привет", chat=GROUP)
    await env.msg(ADMIN, "/addmaster 555 Оля", chat=GROUP, reply_to=other)
    assert env.db.get_master(555).name == "Оля"
    await env.msg(ADMIN, "/addmaster Дарья П.", chat=GROUP, reply_to=other)  # в reply-режиме аргумент это имя
    assert env.db.get_master(DASHA.id).name == "Дарья П."
    await env.msg(ADMIN, "/addmaster")
    assert "Формат" in env.session.last_text()


async def test_start_and_help_silent_in_group(env):
    await env.msg(STRANGER, "/start", chat=GROUP)
    await env.msg(STRANGER, "/help", chat=GROUP)
    assert env.session.sent() == []
    await env.msg(STRANGER, "/id", chat=GROUP)
    assert str(GROUP.id) in env.session.last_text()


# ---------- напоминания ----------

async def test_no_reminder_right_after_late_booking(env):
    start = int(time.time()) + 1800  # бронь за полчаса до начала
    env.db.create_booking(MASHA.id, "Маша", "", start, start + 3600)
    await tasks.send_reminders(env.svc)
    assert env.session.sent() == []


async def test_reminder_retried_after_network_error(env, monkeypatch):
    now = int(time.time())
    b = env.db.create_booking(MASHA.id, "Маша", "", now + 7200, now + 10800)
    monkeypatch.setattr(tasks.time, "time", lambda: b.start_ts - 3600 + 5)
    env.session.fail = lambda method: method.chat_id == MASHA.id
    await tasks.send_reminders(env.svc)
    assert [c.chat_id for c in env.session.sent()] == [GROUP.id]
    env.session.fail = None
    monkeypatch.setattr(tasks.time, "time", lambda: b.start_ts - 3600 + 65)
    await tasks.send_reminders(env.svc)
    await tasks.send_reminders(env.svc)
    assert [c.chat_id for c in env.session.sent()] == [GROUP.id, MASHA.id]  # личка дослана, чат без дубля


async def test_night_reminder_to_chat_is_silent(env, monkeypatch):
    d = datetime.now(TZ).date() + timedelta(days=3)
    start = int(datetime(d.year, d.month, d.day, 3, 0, tzinfo=TZ).timestamp())
    env.db.create_booking(MASHA.id, "Маша", "", start, start + 3600)
    monkeypatch.setattr(tasks.time, "time", lambda: start - 3600 + 5)  # 02:00
    await tasks.send_reminders(env.svc)
    by_chat = {c.chat_id: c for c in env.session.sent()}
    assert by_chat[GROUP.id].disable_notification is True and not by_chat[MASHA.id].disable_notification


# ---------- календарь: сбои и настройка ----------

async def test_calendar_note_only_when_event_missing(cal_env):
    env = cal_env
    await book(env, MASHA, "1 ч", "12:00")
    assert "✅ Забронировано" in env.session.last_text() and "позже" not in env.session.last_text()

    async def boom(booking):
        raise ConnectionError("down")
    env.svc.gcal.create_event = boom
    await book(env, DASHA, "1 ч", "18:00")
    assert "появится чуть позже" in env.session.last_text()
    assert len(env.db.unsynced_active()) == 1
    del env.svc.gcal.create_event
    await tasks.sync_calendar(env.svc)
    assert env.db.unsynced_active() == []


async def test_calendar_down_at_confirm_allows_retry(cal_env):
    env = cal_env
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    await env.click_text(MASHA, "12:00")
    await env.click_text(MASHA, "Без названия")
    env.svc.gcal.down = True
    await env.click_text(MASHA, "✅ Забронировать")
    assert "не отвечает" in env.session.last_text() and env.db.list_active(0, 2**40) == []
    env.svc.gcal.down = False
    await env.click_text(MASHA, "✅ Забронировать")  # та же кнопка, без прохождения сценария заново
    assert "✅ Забронировано" in env.session.last_text()


async def test_calendar_misconfig_tells_admin_once(cal_env):
    env = cal_env

    async def forbidden(start_ts, end_ts):
        raise HttpError(httplib2.Response({"status": 404}), b'{"error": {"message": "Not Found"}}')
    env.svc.gcal.list_events = forbidden
    for _ in range(2):
        await env.msg(MASHA, "/book")
        await env.click_text(MASHA, re.escape(tomorrow_label()))
        await env.click_text(MASHA, "1 ч")
        assert "настроен с ошибкой" in env.session.last_text()
    to_admin = [c.text for c in env.session.sent() if c.chat_id == ADMIN.id]
    assert len(to_admin) == 1 and "GCAL_CALENDAR_ID" in to_admin[0]


async def test_cancel_while_calendar_disabled_keeps_event_id(cal_env):
    env = cal_env
    await book(env, MASHA, "1 ч", "12:00")
    b = env.db.list_active(0, 2**40)[0]
    cal, env.svc.gcal = env.svc.gcal, NullCalendar()  # календарь временно выключили
    await env.svc.cancel(b.id, "Маша", by_id=MASHA.id)
    assert env.db.get_booking(b.id).gcal_event_id == "bot-1"
    env.svc.gcal = cal
    await tasks.sync_calendar(env.svc)
    assert cal.events == [] and env.db.get_booking(b.id).gcal_event_id is None


def test_event_id_is_deterministic_and_valid():
    db = Database(":memory:")
    b = db.create_booking(1, "A", "", 2_000_000_000, 2_000_003_600)
    assert event_id_for(b) == event_id_for(db.get_booking(b.id))
    assert re.fullmatch(r"[a-v0-9]{5,1024}", event_id_for(b))


async def test_chat_migration_followed(env):
    new_id = -1009999
    orig = env.session.make_request

    async def migrating(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.chat_id == GROUP.id:
            raise TelegramMigrateToChat(method=method, message="migrated", migrate_to_chat_id=new_id)
        return await orig(bot, method, timeout)
    env.session.make_request = migrating
    await book(env, MASHA, "1 ч", "12:00")
    assert [c.text for c in env.session.sent() if c.chat_id == new_id]
    assert any(str(new_id) in c.text for c in env.session.sent() if c.chat_id == ADMIN.id)


# ---------- сценарий /book, анонсы, календарь: крайние случаи ----------

async def test_stale_keyboard_cannot_drive_new_flow(env):
    await env.msg(MASHA, "/book")
    old = env.session.last_mid[MASHA.id]
    date_btn = env.session.buttons()[tomorrow_label()]
    await env.msg(MASHA, "/book")  # новый сценарий, старая клавиатура пережила сбой
    await env.click(MASHA, date_btn, message_id=old)
    assert "старая" in last_alert(env)
    await env.click(MASHA, date_btn)
    assert "1 ч" in env.session.buttons()
    await env.click(MASHA, BookCb(step="back").pack(), message_id=old)
    assert "старая" in last_alert(env)


async def test_late_evening_keeps_number_of_dates(env, monkeypatch):
    real_dt = datetime

    class LateEvening(datetime):
        @classmethod
        def now(cls, tz=None):
            n = real_dt.now(tz)
            return n.replace(hour=23, minute=45)
    monkeypatch.setattr(handlers, "datetime", LateEvening)
    monkeypatch.setattr("bot.slots.datetime", LateEvening)
    kb = handlers.kb_dates(env.cfg)
    labels = [b.text for row in kb.inline_keyboard for b in row if b.text != "✖ Отмена"]
    today = real_dt.now(TZ).date()
    assert len(labels) == env.cfg.days_ahead and labels[0] == fmt_date(today + timedelta(days=1))


async def test_cancel_removes_event_whose_id_was_lost(cal_env):
    """Ответ Google потерялся, бронь отменили до досинхронизации: сироты в календаре быть не должно."""
    env = cal_env
    real_create = env.svc.gcal.create_event

    async def created_but_response_lost(booking):
        env.svc.gcal.events.append(CalEvent(event_id_for(booking), booking.start_ts, booking.end_ts, "bot"))
        raise TimeoutError("response lost")
    env.svc.gcal.create_event = created_but_response_lost
    await book(env, MASHA, "1 ч", "12:00")
    b = env.db.list_active(0, 2**40)[0]
    assert b.gcal_event_id is None and len(env.svc.gcal.events) == 1
    await env.svc.cancel(b.id, "Маша", by_id=MASHA.id)
    assert env.svc.gcal.events == []
    env.svc.gcal.create_event = real_create
    await book(env, DASHA, "1 ч", "12:00")
    assert "✅ Забронировано" in env.session.last_text()


async def test_failed_delete_of_lost_event_is_retried_in_background(cal_env):
    env = cal_env
    b = env.db.create_booking(MASHA.id, "Маша", "", _tomorrow_ts(12), _tomorrow_ts(13))
    env.svc.gcal.events.append(CalEvent(event_id_for(b), b.start_ts, b.end_ts, "bot"))  # событие есть, id в базе нет
    real_delete = env.svc.gcal.delete_event

    async def down(event_id):
        raise ConnectionError("down")
    env.svc.gcal.delete_event = down
    await env.svc.cancel(b.id, "Маша", by_id=MASHA.id)
    assert env.db.get_booking(b.id).gcal_event_id == event_id_for(b)
    env.svc.gcal.delete_event = real_delete
    await tasks.sync_calendar(env.svc)
    assert env.svc.gcal.events == [] and env.db.get_booking(b.id).gcal_event_id is None


async def test_readonly_calendar_alerts_admin(cal_env):
    env = cal_env

    async def forbidden(booking):
        raise HttpError(httplib2.Response({"status": 403}), b'{"error": {"message": "Forbidden"}}')
    env.svc.gcal.create_event = forbidden
    await book(env, MASHA, "1 ч", "12:00")
    assert "✅ Забронировано" in env.session.last_text()
    to_admin = [c.text for c in env.session.sent() if c.chat_id == ADMIN.id]
    assert len(to_admin) == 1 and "Брони создаются" in to_admin[0] and "заблокировано" not in to_admin[0]


async def test_transient_announce_failure_does_not_silence_real_one(env):
    env.session.fail = lambda method: method.chat_id == GROUP.id  # разовый сбой сети
    assert await env.svc.announce("раз") is False
    assert [c for c in env.session.sent() if c.chat_id == ADMIN.id] == []
    env.session.fail = None
    orig = env.session.make_request

    async def kicked(bot, method, timeout=None):
        if isinstance(method, SendMessage) and method.chat_id == GROUP.id:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked from the supergroup chat")
        return await orig(bot, method, timeout)
    env.session.make_request = kicked
    assert await env.svc.announce("два") is False
    assert await env.svc.announce("три") is False
    to_admin = [c.text for c in env.session.sent() if c.chat_id == ADMIN.id]
    assert len(to_admin) == 1 and "MASTERS_CHAT_ID" in to_admin[0]


async def test_repeated_confirm_while_calendar_down_gives_feedback(cal_env):
    env = cal_env
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    await env.click_text(MASHA, "12:00")
    await env.click_text(MASHA, "Без названия")
    env.svc.gcal.down = True
    for _ in range(2):  # второй раз текст не меняется, нужен алерт
        await env.click_text(MASHA, "✅ Забронировать")
        assert "Бронь не создана" in last_alert(env)


async def test_success_survives_failed_message_edit(env, monkeypatch):
    await env.msg(MASHA, "/book")
    await env.click_text(MASHA, re.escape(tomorrow_label()))
    await env.click_text(MASHA, "1 ч")
    await env.click_text(MASHA, "12:00")
    await env.click_text(MASHA, "Без названия")
    real = handlers.safe_edit

    async def flaky(cq, text, reply_markup=None):
        if text.startswith("✅"):
            raise ConnectionError("telegram hiccup")
        await real(cq, text, reply_markup)
    monkeypatch.setattr(handlers, "safe_edit", flaky)
    await env.click_text(MASHA, "✅ Забронировать")
    assert "Забронировано" in last_alert(env) and len(env.db.list_active(0, 2**40)) == 1


async def test_masters_list_shows_name_and_tag(env):
    env.session.usernames = {MASHA.id: "masha_rings", DASHA.id: None}
    env.db.add_master(555, "Оля <новая>")  # добавлена по ID, боту ещё не писала
    await env.msg(ADMIN, "/masters")
    lines = env.session.last_text().split("\n")
    assert "Маша" in lines[0] and "@masha_rings" in lines[0] and f"<code>{MASHA.id}</code>" in lines[0]
    assert "Даша" in lines[1] and "без @тега" in lines[1]
    assert "Оля &lt;новая&gt;" in lines[2] and "не писал(а) боту" in lines[2]
    assert f'href="tg://user?id={MASHA.id}"' in lines[0]
    await env.msg(MASHA, "/masters")
    assert "Не понял" in env.session.last_text()


async def test_week_shows_manual_calendar_events(cal_env):
    env = cal_env
    env.svc.gcal.events.append(CalEvent("manual1", _tomorrow_ts(18), _tomorrow_ts(19), "Аренда <зал>"))
    d3 = _tomorrow_ts(0) + 2 * 86400
    env.svc.gcal.events.append(CalEvent("holiday", d3, d3 + 86400, "Выходной"))
    await book(env, MASHA, "2 ч", "10:00", "Свечи")
    await env.msg(DASHA, "/week")
    text = env.session.last_text()
    assert "10:00–12:00 Маша «Свечи»" in text
    assert "18:00–19:00 📌 Аренда &lt;зал&gt;" in text and "весь день 📌 Выходной" in text
    assert text.index("10:00–12:00") < text.index("18:00–19:00")
    assert text.count("10:00–12:00") == 1  # своё событие бота не дублирует бронь

    env.svc.gcal.down = True
    await env.msg(DASHA, "/week")
    assert "10:00–12:00 Маша" in env.session.last_text() and "получить не удалось" in env.session.last_text()


async def test_week_with_only_manual_events(cal_env):
    env = cal_env
    env.svc.gcal.events.append(CalEvent("manual1", _tomorrow_ts(18), _tomorrow_ts(19), "Аренда"))
    await env.msg(MASHA, "/week")
    assert "📌 Аренда" in env.session.last_text()


# ---------- событие брони удалили из календаря руками ----------

async def test_event_deleted_by_hand_cancels_booking(cal_env):
    env = cal_env
    await book(env, MASHA, "2 ч", "14:00", "Свечи")
    b = env.db.list_active(0, 2**40)[0]
    await tasks.sync_calendar(env.svc)
    assert env.db.get_booking(b.id).active

    env.svc.gcal.manual_delete(b.gcal_event_id)
    await tasks.sync_calendar(env.svc)
    after = env.db.get_booking(b.id)
    assert not after.active and after.gcal_event_id is None
    assert "удалили из Google Calendar" in [c.text for c in env.session.sent() if c.chat_id == GROUP.id][-1]
    to_masha = [c.text for c in env.session.sent() if c.chat_id == MASHA.id][-1]
    assert "Твоя бронь отменена" in to_masha and "Свечи" in to_masha
    n = len(env.session.sent())
    await tasks.sync_calendar(env.svc)
    assert len(env.session.sent()) == n
    await book(env, DASHA, "2 ч", "14:00")
    assert "✅ Забронировано" in env.session.last_text()


async def test_missing_event_is_recreated_not_cancelled(cal_env):
    """После смены календаря в .env событий нет вообще: это не удаление, брони не трогаем."""
    env = cal_env
    await book(env, MASHA, "1 ч", "12:00")
    b = env.db.list_active(0, 2**40)[0]
    env.svc.gcal.events.clear()  # без пометки "удалено"
    await tasks.sync_calendar(env.svc)
    assert env.db.get_booking(b.id).active and env.db.get_booking(b.id).gcal_event_id is None
    await tasks.sync_calendar(env.svc)
    assert env.db.get_booking(b.id).gcal_event_id and len(env.svc.gcal.events) == 1


async def test_moved_event_and_calendar_outage_do_not_cancel(cal_env):
    env = cal_env
    await book(env, MASHA, "1 ч", "12:00")
    b = env.db.list_active(0, 2**40)[0]
    ev = env.svc.gcal.events[0]
    env.svc.gcal.events[0] = CalEvent(ev.id, ev.start_ts + 90 * 86400, ev.end_ts + 90 * 86400, ev.summary)  # перенесли далеко
    await tasks.sync_calendar(env.svc)
    assert env.db.get_booking(b.id).active and env.db.get_booking(b.id).gcal_event_id == ev.id
    env.svc.gcal.manual_delete(ev.id)
    env.svc.gcal.down = True
    await tasks.sync_calendar(env.svc)
    assert env.db.get_booking(b.id).active


async def test_mass_deletion_is_not_applied_automatically(cal_env):
    env = cal_env
    for i in range(MAX_DELETIONS_PER_PASS + 1):
        b = env.db.create_booking(MASHA.id, "Маша", "", _tomorrow_ts(0) + i * 7200, _tomorrow_ts(0) + i * 7200 + 3600)
        await env.svc.sync_to_calendar(b)
    for ev in list(env.svc.gcal.events):
        env.svc.gcal.manual_delete(ev.id)
    await tasks.sync_calendar(env.svc)
    assert len(env.db.list_active(0, 2**40)) == MAX_DELETIONS_PER_PASS + 1
    to_admin = [c.text for c in env.session.sent() if c.chat_id == ADMIN.id]
    assert len(to_admin) == 1 and "/all" in to_admin[0]
    assert [c for c in env.session.sent() if c.chat_id == GROUP.id] == []
