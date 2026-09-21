from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

from aiogram import Bot, F, Router, html
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardButton, InlineKeyboardMarkup, Message, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import MAX_DURATION_MINUTES, MIN_DURATION_MINUTES, Config
from .db import Booking, ConflictError, Database
from .gcal import CalendarUnavailable
from .services import CalendarConflict, Services, SlotInPast
from .slots import (
    busy_window,
    day_start_ts,
    fmt_booking,
    fmt_date,
    fmt_dt_range,
    fmt_duration,
    fmt_slot,
    free_start_times,
    has_future_starts,
    parse_duration,
    week_agenda,
)

log = logging.getLogger(__name__)

HELP = (
    "Бронирование мастерской под мастер-классы. Бронировать можно на любое время суток, "
    "МК может идти через полночь.\n\n"
    "/book — забронировать\n"
    "/my — мои брони (и отмена)\n"
    "/week — занятость на неделю\n"
    "/cancel — прервать бронирование\n"
    "/id — мой Telegram ID"
)
ADMIN_HELP = (
    "\n\nАдмин:\n"
    "/addmaster &lt;id&gt; [имя] — добавить мастера (или ответом на его сообщение)\n"
    "/delmaster &lt;id&gt; — убрать\n"
    "/masters — список: имя, @тег, ID\n"
    "/all — все брони (и отмена любой)"
)
ADDMASTER_USAGE = "Формат: /addmaster &lt;id&gt; [имя] — или ответом на сообщение мастера"
TITLE_MAX = 100
LIST_MAX = 40       # броней в одном списке с кнопками отмены
MESSAGE_MAX = 3900  # запас до лимита Telegram в 4096 символов


# ---------- доступ ----------

def is_admin(user: User, cfg: Config) -> bool:
    return user.id in cfg.admin_ids


def is_master(user: User, db: Database, cfg: Config) -> bool:
    return is_admin(user, cfg) or db.is_master(user.id)


def display_name(user: User, db: Database) -> str:
    """Имя из /addmaster, иначе из Telegram."""
    master = db.get_master(user.id)
    return master.name if master and master.name != str(user.id) else user.full_name


class MasterFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, db: Database, cfg: Config) -> bool:
        return event.from_user is not None and is_master(event.from_user, db, cfg)


class AdminFilter(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, cfg: Config) -> bool:
        return event.from_user is not None and is_admin(event.from_user, cfg)


# ---------- callback data / FSM ----------

class BookCb(CallbackData, prefix="bk"):
    step: str   # date | dur | time | notitle | back | confirm | abort
    value: str = ""


class CancelCb(CallbackData, prefix="cn"):
    booking_id: int
    action: str = "ask"   # ask | do | keep; "0"/"1" приходят со старых кнопок


class BookFlow(StatesGroup):
    date = State()
    duration = State()
    time = State()
    title = State()
    confirm = State()


# ---------- клавиатуры ----------

def _btn(text: str, cb: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


BTN_ABORT = _btn("✖ Отмена", BookCb(step="abort"))
BTN_BACK = _btn("← Назад", BookCb(step="back"))


def kb_dates(cfg: Config) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    today = datetime.now(cfg.tz).date()
    first = 0 if has_future_starts(today, cfg) else 1
    for i in range(first, first + cfg.days_ahead):
        d = today + timedelta(days=i)
        b.button(text=fmt_date(d), callback_data=BookCb(step="date", value=d.isoformat()))
    b.adjust(3)
    b.row(BTN_ABORT)
    return b.as_markup()


def kb_durations(cfg: Config) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in cfg.durations_minutes:
        b.button(text=fmt_duration(m), callback_data=BookCb(step="dur", value=str(m)))
    b.adjust(3)
    b.row(BTN_BACK, BTN_ABORT)
    return b.as_markup()


def kb_times(times: list[datetime], cfg: Config) -> InlineKeyboardMarkup:
    # ряд = 4 шага сетки, чтобы занятые слоты не сдвигали кнопки; в callback unix-время, не зависит от даты в стейте
    b = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    row_key = None
    for t in times:
        key = (t.hour * 60 + t.minute) // (cfg.slot_step_minutes * 4)
        if row and key != row_key:
            b.row(*row)
            row = []
        row_key = key
        row.append(_btn(f"{t:%H:%M}", BookCb(step="time", value=str(int(t.timestamp())))))
    if row:
        b.row(*row)
    b.row(BTN_BACK, BTN_ABORT)
    return b.as_markup()


def kb_title() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("Без названия", BookCb(step="notitle")))
    b.row(BTN_BACK, BTN_ABORT)
    return b.as_markup()


def kb_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Забронировать", callback_data=BookCb(step="confirm"))
    b.button(text="✖ Отмена", callback_data=BookCb(step="abort"))
    return b.as_markup()


def kb_my(bookings: list[Booking]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bk in bookings[:LIST_MAX]:
        b.button(text=f"Отменить #{bk.id}", callback_data=CancelCb(booking_id=bk.id))
    b.adjust(2)
    return b.as_markup()


def kb_cancel_confirm(booking_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Да, отменить", callback_data=CancelCb(booking_id=booking_id, action="do"))
    b.button(text="Нет", callback_data=CancelCb(booking_id=booking_id, action="keep"))
    return b.as_markup()


# ---------- хелперы ----------

async def safe_edit(cq: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        if isinstance(cq.message, Message):
            await cq.message.edit_text(text, reply_markup=reply_markup)
        else:
            await cq.bot.send_message(cq.from_user.id, text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def answer_long(m: Message, lines: list[str], reply_markup: InlineKeyboardMarkup | None = None) -> None:
    chunks: list[str] = []
    cur = ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > MESSAGE_MAX:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    chunks.append(cur)
    for i, chunk in enumerate(chunks):
        await m.answer(chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)


def booking_lines(bookings: list[Booking], cfg: Config, with_master: bool) -> list[str]:
    lines = [f"#{b.id} {fmt_booking(b, cfg.tz, with_master=with_master)}" for b in bookings[:LIST_MAX]]
    if len(bookings) > LIST_MAX:
        lines.append(f"…и ещё {len(bookings) - LIST_MAX}")
    return lines


async def drop_flow_keyboard(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Убирает клавиатуру с предыдущего сообщения /book."""
    msg_id = (await state.get_data()).get("flow_msg")
    if msg_id:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg_id, reply_markup=None)
        except Exception:
            pass  # сообщение удалено или кнопок уже нет


async def is_flow_msg(cq: CallbackQuery, state: FSMContext, editable: bool = True) -> bool:
    """Тап с текущего сообщения /book. editable=False нужен для "Отмена" на недоступном сообщении."""
    if cq.message is None or (editable and not isinstance(cq.message, Message)):
        return False
    return (await state.get_data()).get("flow_msg") == cq.message.message_id


class FlowMsg(BaseFilter):
    """Шаги /book только с текущего сообщения."""

    async def __call__(self, cq: CallbackQuery, state: FSMContext) -> bool:
        return await is_flow_msg(cq, state)


def calendar_down_text(e: CalendarUnavailable) -> str:
    if e.permanent:
        return ("⚠️ Не могу проверить занятость: Google Calendar настроен с ошибкой. "
                "Админу уже ушло сообщение — бронирование заработает, когда он поправит.")
    return "⚠️ Google Calendar не отвечает, не могу проверить занятость. Попробуй чуть позже."


async def times_screen(state: FSMContext, svc: Services, cfg: Config, d: date, dur: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран выбора времени, сам выставляет стейт."""
    try:
        busy = await svc.busy_intervals(*busy_window(d, dur, cfg))
    except CalendarUnavailable as e:
        await state.set_state(BookFlow.duration)
        return calendar_down_text(e), kb_durations(cfg)
    times = free_start_times(d, dur, busy, cfg)
    header = f"Дата: <b>{fmt_date(d)}</b>, {fmt_duration(dur)}\n"
    if not times and not has_future_starts(d, cfg):
        await state.set_state(BookFlow.date)
        return (f"На {fmt_date(d)} время уже вышло. Часы после полуночи — это уже следующая дата.\nВыбери дату:",
                kb_dates(cfg))
    if not times:
        await state.set_state(BookFlow.duration)
        return header + "Свободного времени нет. Другая длительность или ← Назад к дате.", kb_durations(cfg)
    await state.set_state(BookFlow.time)
    return header + "Время начала:", kb_times(times, cfg)


def duration_prompt(d: date) -> str:
    return (f"Дата: <b>{fmt_date(d)}</b>\nДлительность — кнопкой или напиши часы текстом: "
            "<code>9</code>, <code>2,5</code>, <code>2:30</code>")


def confirm_text(data: dict, cfg: Config) -> str:
    title = data.get("title", "")
    t = f"\n«{html.quote(title)}»" if title else ""
    return f"Бронируем?\n<b>{fmt_slot(data['start_ts'], data['end_ts'], cfg.tz)}</b>{t}"


def build_routers() -> tuple[Router, ...]:
    """Новые роутеры на каждый диспетчер: aiogram не даёт переиспользовать роутер."""
    # ---------- ошибки ----------

    errors = Router(name="errors")

    @errors.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("Ошибка в хендлере", exc_info=event.exception)
        try:
            if event.update.callback_query:
                await event.update.callback_query.answer("Что-то пошло не так. Попробуй ещё раз: /book", show_alert=True)
            elif event.update.message and event.update.message.chat.type == ChatType.PRIVATE:
                await event.update.message.answer("Что-то пошло не так. Попробуй ещё раз.")
        except Exception:
            pass
        return True

    # ---------- публичные ----------

    public = Router(name="public")

    @public.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
    async def cmd_start(m: Message, db: Database, cfg: Config) -> None:
        if is_master(m.from_user, db, cfg):
            await m.answer(HELP + (ADMIN_HELP if is_admin(m.from_user, cfg) else ""))
        else:
            await m.answer(
                f"Твой ID: <code>{m.from_user.id}</code>\n"
                f"Попроси админа добавить тебя: <code>/addmaster {m.from_user.id} {html.quote(m.from_user.full_name)}</code>"
            )

    @public.message(Command("id"))
    async def cmd_id(m: Message) -> None:
        who = f"Твой ID: <code>{m.from_user.id}</code>\n" if m.from_user else ""
        await m.answer(f"{who}ID чата: <code>{m.chat.id}</code>")

    @public.message(Command("help"), F.chat.type == ChatType.PRIVATE)
    async def cmd_help(m: Message, cfg: Config) -> None:
        await m.answer(HELP + (ADMIN_HELP if is_admin(m.from_user, cfg) else ""))

    # ---------- админ ----------

    admin = Router(name="admin")
    admin.message.filter(AdminFilter())

    @admin.message(Command("addmaster"))
    async def cmd_addmaster(m: Message, command: CommandObject, db: Database) -> None:
        parts = (command.args or "").split(maxsplit=1)
        reply = m.reply_to_message
        if parts and parts[0].lstrip("-").isdigit():
            # явный ID важнее reply: в теме форума Telegram сам подставляет reply на создание темы
            tg_id = int(parts[0])
            name = parts[1].strip() if len(parts) > 1 else str(tg_id)
        elif reply and reply.from_user and not reply.from_user.is_bot and not reply.forum_topic_created:
            tg_id = reply.from_user.id
            name = (command.args or "").strip() or reply.from_user.full_name
        else:
            await m.answer(ADDMASTER_USAGE)
            return
        if tg_id <= 0:
            await m.answer(ADDMASTER_USAGE)
            return
        name = " ".join(name.split())[:64]
        db.add_master(tg_id, name)
        await m.answer(f"✅ {html.quote(name)} (<code>{tg_id}</code>) добавлен(а) в мастера")

    @admin.message(Command("delmaster"))
    async def cmd_delmaster(m: Message, command: CommandObject, db: Database, cfg: Config) -> None:
        try:
            tg_id = int((command.args or "").strip())
        except ValueError:
            await m.answer("Формат: /delmaster &lt;id&gt;")
            return
        if not db.remove_master(tg_id):
            await m.answer("Такого мастера нет")
            return
        left = db.list_by_master(tg_id, int(time.time()))
        if not left:
            await m.answer("Удалён")
            return
        await answer_long(
            m, [f"Удалён. У него остались брони ({len(left)}) — отмени лишние:"] + booking_lines(left, cfg, with_master=True),
            reply_markup=kb_my(left),
        )

    @admin.message(Command("masters"))
    async def cmd_masters(m: Message, db: Database) -> None:
        masters = db.list_masters()
        if not masters:
            await m.answer("Мастеров пока нет")
            return

        # @тег не храним, они меняются; get_chat работает только для тех, кто писал боту
        async def tag(tg_id: int) -> str:
            try:
                chat = await m.bot.get_chat(tg_id)
            except Exception:
                return "ещё не писал(а) боту — напоминания в личку не дойдут"
            return f"@{chat.username}" if chat.username else "без @тега"

        tags = await asyncio.gather(*(tag(x.tg_id) for x in masters))
        await answer_long(m, [
            f'• <a href="tg://user?id={x.tg_id}">{html.quote(x.name)}</a> — {t} — <code>{x.tg_id}</code>'
            for x, t in zip(masters, tags, strict=True)
        ])

    @admin.message(Command("all"))
    async def cmd_all(m: Message, db: Database, cfg: Config) -> None:
        if m.chat.type != ChatType.PRIVATE:
            await m.answer("Эта команда — в личке")
            return
        bookings = db.list_active(int(time.time()), 2**40)
        if not bookings:
            await m.answer("Активных броней нет")
            return
        await answer_long(m, ["Все брони:"] + booking_lines(bookings, cfg, with_master=True), reply_markup=kb_my(bookings))

    # ---------- мастера ----------

    master = Router(name="master")
    master.message.filter(MasterFilter())
    master.callback_query.filter(MasterFilter())

    @master.message(Command("week"))
    async def cmd_week(m: Message, db: Database, cfg: Config, svc: Services) -> None:
        today = datetime.now(cfg.tz).date()
        start, end = day_start_ts(today, cfg.tz), day_start_ts(today + timedelta(days=7), cfg.tz)
        items: list = db.list_active(start, end)
        note = ""
        try:
            items += await svc.external_events(start, end)
        except CalendarUnavailable:
            note = "\n\n⚠️ События из Google Calendar получить не удалось — показаны только брони."
        agenda = week_agenda(items, today, 7, cfg.tz)
        if not agenda:
            await m.answer("На ближайшую неделю броней нет" + note)
            return
        lines = ["Занятость на неделю:"]
        for d, day_items in agenda.items():
            lines.append(f"\n<b>{fmt_date(d)}</b>")
            for label, item in day_items:
                if isinstance(item, Booking):
                    title = f" «{html.quote(item.title)}»" if item.title else ""
                    lines.append(f"{label} {html.quote(item.master_name)}{title}")
                else:
                    lines.append(f"{label} 📌 {html.quote(item.summary)} <i>(из календаря)</i>")
        if note:
            lines.append(note)
        await answer_long(m, lines)

    @master.message(Command("my"))
    async def cmd_my(m: Message, db: Database, cfg: Config) -> None:
        bookings = db.list_by_master(m.from_user.id, int(time.time()))
        if not bookings:
            await m.answer("У тебя нет активных броней")
            return
        await answer_long(m, ["Твои брони:"] + booking_lines(bookings, cfg, with_master=False), reply_markup=kb_my(bookings))

    def _may_cancel(b: Booking, user: User, cfg: Config) -> bool:
        return b.master_tg_id == user.id or is_admin(user, cfg)

    @master.callback_query(CancelCb.filter(F.action.in_({"ask", "0"})))
    async def cb_cancel_ask(cq: CallbackQuery, callback_data: CancelCb, db: Database, cfg: Config) -> None:
        b = db.get_booking(callback_data.booking_id)
        if not b or not b.active:
            await cq.answer("Бронь уже неактивна", show_alert=True)
            return
        if not _may_cancel(b, cq.from_user, cfg):
            await cq.answer("Это не твоя бронь", show_alert=True)
            return
        await safe_edit(cq, f"Отменить бронь?\n{fmt_booking(b, cfg.tz)}", kb_cancel_confirm(b.id))
        await cq.answer()

    @master.callback_query(CancelCb.filter(F.action == "keep"))
    async def cb_cancel_keep(cq: CallbackQuery, callback_data: CancelCb, db: Database, cfg: Config) -> None:
        b = db.get_booking(callback_data.booking_id)
        kept = f"\n{fmt_booking(b, cfg.tz)}" if b and b.active else ""
        await safe_edit(cq, f"Ок, бронь остаётся{kept}")
        await cq.answer()

    @master.callback_query(CancelCb.filter(F.action.in_({"do", "1"})))
    async def cb_cancel_do(cq: CallbackQuery, callback_data: CancelCb, db: Database, cfg: Config, svc: Services) -> None:
        b = db.get_booking(callback_data.booking_id)
        if not b or not _may_cancel(b, cq.from_user, cfg):
            await cq.answer("Нельзя", show_alert=True)
            return
        cancelled = await svc.cancel(b.id, display_name(cq.from_user, db), by_id=cq.from_user.id)
        # сначала answer: отмена уже прошла, мастер должен это увидеть, даже если edit упадёт
        await cq.answer("Бронь отменена" if cancelled else "Бронь уже была отменена")
        if cancelled is None:
            await safe_edit(cq, f"Бронь уже была отменена\n{fmt_booking(b, cfg.tz)}")
        else:
            await safe_edit(cq, f"❌ Бронь отменена\n{fmt_booking(cancelled, cfg.tz)}")

    # --- /book: дата -> длительность -> время -> название -> подтверждение ---

    @master.message(Command("book"))
    async def cmd_book(m: Message, state: FSMContext, cfg: Config) -> None:
        if m.chat.type != ChatType.PRIVATE:
            me = await m.bot.me()
            await m.answer(f"Бронировать — в личке: @{me.username}")
            return
        await drop_flow_keyboard(m.bot, m.chat.id, state)
        await state.clear()
        await state.set_state(BookFlow.date)
        sent = await m.answer("Выбери дату начала:", reply_markup=kb_dates(cfg))
        await state.update_data(flow_msg=sent.message_id)

    @master.message(Command("cancel"))
    async def cmd_cancel(m: Message, state: FSMContext) -> None:
        if await state.get_state() is None:
            await m.answer("Нечего прерывать. Отменить бронь — через /my")
            return
        await drop_flow_keyboard(m.bot, m.chat.id, state)
        await state.clear()
        await m.answer("Бронирование прервано")

    @master.callback_query(BookCb.filter(F.step == "abort"))
    async def cb_abort(cq: CallbackQuery, state: FSMContext) -> None:
        # "Отмена" на старом сообщении не должна сбрасывать активный сценарий
        if await is_flow_msg(cq, state, editable=False):
            await state.clear()
        await safe_edit(cq, "Отменено")
        await cq.answer()

    @master.callback_query(BookFlow.date, BookCb.filter(F.step == "date"), FlowMsg())
    async def cb_date(cq: CallbackQuery, callback_data: BookCb, state: FSMContext, cfg: Config) -> None:
        d = date.fromisoformat(callback_data.value)
        await state.update_data(date=d.isoformat())
        await state.set_state(BookFlow.duration)
        await safe_edit(cq, duration_prompt(d), kb_durations(cfg))
        await cq.answer()

    @master.callback_query(BookFlow.duration, BookCb.filter(F.step == "dur"), FlowMsg())
    async def cb_duration(cq: CallbackQuery, callback_data: BookCb, state: FSMContext, cfg: Config, svc: Services) -> None:
        data = await state.get_data()
        dur = int(callback_data.value)
        await state.update_data(duration=dur)
        text, markup = await times_screen(state, svc, cfg, date.fromisoformat(data["date"]), dur)
        await safe_edit(cq, text, markup)
        await cq.answer()

    @master.message(BookFlow.duration, F.text)
    async def msg_duration(m: Message, state: FSMContext, cfg: Config, svc: Services) -> None:
        if m.text.startswith("/"):
            await m.answer("Сначала закончи бронирование или прерви его: /cancel")
            return
        dur = parse_duration(m.text)
        if dur is None:
            await m.answer(
                "Не понял длительность. Напиши часы: <code>9</code>, <code>2,5</code> или <code>2:30</code> "
                f"(от {fmt_duration(MIN_DURATION_MINUTES)} до {fmt_duration(MAX_DURATION_MINUTES)})."
            )
            return
        data = await state.get_data()
        await drop_flow_keyboard(m.bot, m.chat.id, state)
        await state.update_data(duration=dur)
        text, markup = await times_screen(state, svc, cfg, date.fromisoformat(data["date"]), dur)
        sent = await m.answer(text, reply_markup=markup)
        await state.update_data(flow_msg=sent.message_id)

    @master.callback_query(BookFlow.time, BookCb.filter(F.step == "time"), FlowMsg())
    async def cb_time(cq: CallbackQuery, callback_data: BookCb, state: FSMContext, cfg: Config, svc: Services) -> None:
        data = await state.get_data()
        start_ts = int(callback_data.value)
        if start_ts <= time.time():
            text, markup = await times_screen(state, svc, cfg, date.fromisoformat(data["date"]), data["duration"])
            await safe_edit(cq, text, markup)
            await cq.answer("Это время уже прошло, выбери другое", show_alert=True)
            return
        end_ts = start_ts + data["duration"] * 60
        await state.update_data(start_ts=start_ts, end_ts=end_ts)
        await state.set_state(BookFlow.title)
        await safe_edit(
            cq, f"<b>{fmt_slot(start_ts, end_ts, cfg.tz)}</b>\n\nНазвание мастер-класса? Отправь текстом.", kb_title()
        )
        await cq.answer()

    @master.callback_query(BookFlow.title, BookCb.filter(F.step == "notitle"), FlowMsg())
    async def cb_notitle(cq: CallbackQuery, state: FSMContext, cfg: Config) -> None:
        await state.update_data(title="")
        await state.set_state(BookFlow.confirm)
        await safe_edit(cq, confirm_text(await state.get_data(), cfg), kb_confirm())
        await cq.answer()

    @master.message(StateFilter(BookFlow.title, BookFlow.confirm), F.text)
    async def msg_title(m: Message, state: FSMContext, cfg: Config) -> None:
        if m.text.startswith("/"):
            await m.answer("Сначала закончи бронирование или прерви его: /cancel")
            return
        title = " ".join(m.text.split())  # без переносов: название идёт в однострочные списки
        if title == "-":
            title = ""
        if len(title) > TITLE_MAX:
            await m.answer(f"Слишком длинно ({len(title)} симв.) — уложись в {TITLE_MAX}.")
            return
        await drop_flow_keyboard(m.bot, m.chat.id, state)
        await state.update_data(title=title)
        sent = await m.answer(confirm_text(await state.get_data(), cfg), reply_markup=kb_confirm())
        await state.update_data(flow_msg=sent.message_id)
        await state.set_state(BookFlow.confirm)

    @master.callback_query(BookFlow.confirm, BookCb.filter(F.step == "confirm"), FlowMsg())
    async def cb_confirm(cq: CallbackQuery, state: FSMContext, db: Database, cfg: Config, svc: Services) -> None:
        data = await state.get_data()
        # стейт чистим до брони, иначе двойной тап забронирует дважды; done_msg нужен, чтобы второй тап не словил алерт
        await state.clear()
        await state.update_data(done_msg=data.get("flow_msg"))
        try:
            b = await svc.book(cq.from_user.id, display_name(cq.from_user, db), data.get("title", ""),
                               data["start_ts"], data["end_ts"])
        except SlotInPast:
            await safe_edit(cq, "⚠️ Это время уже прошло. Начни заново: /book")
        except ConflictError as e:
            lines = "\n".join(fmt_booking(c, cfg.tz) for c in e.conflicts)
            await safe_edit(cq, f"⚠️ Это время уже занято:\n{lines}\n\nНачни заново: /book")
        except CalendarConflict as e:
            lines = "\n".join(f"{fmt_dt_range(ev.start_ts, ev.end_ts, cfg.tz)} «{html.quote(ev.summary)}»" for ev in e.events)
            await safe_edit(cq, f"⚠️ В Google Calendar на это время уже есть событие:\n{lines}\n\nНачни заново: /book")
        except CalendarUnavailable as e:
            await state.set_data(data)
            await state.set_state(BookFlow.confirm)
            await safe_edit(cq, f"{calendar_down_text(e)}\n\n{confirm_text(data, cfg)}", kb_confirm())
            # алерт, потому что при ретрае текст сообщения тот же и кнопка выглядит мёртвой
            await cq.answer("Бронь не создана: не могу проверить занятость по Google Calendar.", show_alert=True)
            return
        else:
            cal = "\n(в Google Calendar появится чуть позже)" if svc.gcal.enabled and not b.gcal_event_id else ""
            try:
                await safe_edit(cq, f"✅ Забронировано\n{fmt_booking(b, cfg.tz, with_master=False)}{cal}")
            except Exception:
                # бронь уже есть, общий on_error соврал бы про "попробуй ещё раз"
                log.exception("Бронь #%s создана, но сообщение не обновилось", b.id)
                await cq.answer("✅ Забронировано. Детали — в /my", show_alert=True)
                return
        await cq.answer()

    @master.callback_query(BookCb.filter(F.step == "back"))
    async def cb_back(cq: CallbackQuery, state: FSMContext, cfg: Config, svc: Services) -> None:
        if not await is_flow_msg(cq, state):
            await cq.answer("Это старая кнопка. Начни заново: /book", show_alert=True)
            return
        cur = await state.get_state()
        data = await state.get_data()
        if cur == BookFlow.title.state:
            text, markup = await times_screen(state, svc, cfg, date.fromisoformat(data["date"]), data["duration"])
            await safe_edit(cq, text, markup)
        elif cur == BookFlow.time.state:
            await state.set_state(BookFlow.duration)
            await safe_edit(cq, duration_prompt(date.fromisoformat(data["date"])), kb_durations(cfg))
        else:
            await state.set_state(BookFlow.date)
            await safe_edit(cq, "Выбери дату начала:", kb_dates(cfg))
        await cq.answer()

    @master.callback_query(BookCb.filter())
    async def cb_stale(cq: CallbackQuery, state: FSMContext) -> None:
        # сюда же падает второй тап по уже пройденному шагу: сообщение то же, молчим
        done = (await state.get_data()).get("done_msg")
        if await is_flow_msg(cq, state) or (done and isinstance(cq.message, Message) and done == cq.message.message_id):
            await cq.answer()
            return
        await cq.answer("Это старая кнопка. Начни заново: /book", show_alert=True)

    # ---------- всё остальное ----------

    fallback = Router(name="fallback")

    @fallback.message(F.chat.type == ChatType.PRIVATE)
    async def msg_unknown(m: Message, db: Database, cfg: Config) -> None:
        if is_master(m.from_user, db, cfg):
            await m.answer("Не понял. " + HELP)
        else:
            await m.answer(f"Доступ только для мастеров. Твой ID: <code>{m.from_user.id}</code>")

    @fallback.callback_query()
    async def cb_unknown(cq: CallbackQuery) -> None:
        await cq.answer("Недоступно", show_alert=True)

    return errors, public, admin, master, fallback
