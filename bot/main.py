"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from aiogram.utils.token import TokenValidationError

from .config import Config, ConfigError
from .db import Database
from .gcal import make_calendar
from .handlers import build_routers
from .services import Services
from .slots import fmt_duration
from .tasks import build_scheduler

log = logging.getLogger("bot")

PRIVATE_COMMANDS = [
    BotCommand(command="book", description="Забронировать мастерскую"),
    BotCommand(command="my", description="Мои брони / отмена"),
    BotCommand(command="week", description="Занятость на неделю"),
    BotCommand(command="cancel", description="Прервать бронирование"),
    BotCommand(command="help", description="Помощь"),
]
GROUP_COMMANDS = [
    BotCommand(command="week", description="Занятость на неделю"),
    BotCommand(command="id", description="ID чата"),
]


def build_dispatcher(bot: Bot, db: Database, cfg: Config, svc: Services) -> Dispatcher:
    # апдейты одного юзера строго по очереди, иначе "Отмена" обгоняет шаг, который ждёт календарь, и тот воскрешает стейт
    dp = Dispatcher(db=db, cfg=cfg, svc=svc, events_isolation=SimpleEventIsolation())
    dp.include_routers(*build_routers())
    return dp


async def run() -> None:
    level = (os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    known_level = level in logging.getLevelNamesMapping()
    logging.basicConfig(
        level=level if known_level else "INFO",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not known_level:
        log.warning("LOG_LEVEL=%s не понимаю, беру INFO", level)
    # на INFO apscheduler сыпет по две строки в минуту
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    cfg = Config.from_env()
    if any(os.environ.get(k) for k in ("WORK_START_HOUR", "WORK_END_HOUR")):
        log.warning("WORK_START_HOUR/WORK_END_HOUR больше не используются: брони круглосуточные. Удали их из .env.")
    try:
        db = Database(cfg.db_path, cfg.buffer_minutes)
    except (OSError, sqlite3.Error) as e:
        raise ConfigError(f"база {cfg.db_path} недоступна на запись: {e}. Каталог data и ВСЕ файлы в нём должны "
                          "принадлежать пользователю контейнера: sudo chown -R 10001 data") from None
    gcal = make_calendar(cfg.google_sa_file, cfg.gcal_calendar_id, cfg.tz)
    try:
        bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    except TokenValidationError:
        raise ConfigError("BOT_TOKEN не похож на токен от @BotFather (вид: 123456:ABC-DEF…)") from None
    svc = Services(bot, db, cfg, gcal)
    dp = build_dispatcher(bot, db, cfg, svc)

    sched = build_scheduler(svc)
    try:
        await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())

        me = await bot.me()
        log.info("Запущен @%s; админы: %s; чат мастеров: %s", me.username, sorted(cfg.admin_ids), cfg.masters_chat_id)
        log.info("Брони круглосуточно; шаг %s, пауза между МК %s, длительности: %s (+ ручной ввод); часовой пояс %s",
                 fmt_duration(cfg.slot_step_minutes), fmt_duration(cfg.buffer_minutes) if cfg.buffer_minutes else "нет",
                 ", ".join(fmt_duration(m) for m in cfg.durations_minutes), cfg.tz)
        await svc.startup_checks()

        sched.start()
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    except TelegramUnauthorizedError:
        raise ConfigError("Telegram не принял BOT_TOKEN, проверь токен от @BotFather") from None
    finally:
        if sched.running:
            sched.shutdown(wait=False)
        await bot.session.close()
        db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except ConfigError as e:
        # без трейсбека, это читают глазами в docker compose logs
        sys.exit(f"Ошибка настройки: {e}")


if __name__ == "__main__":
    main()
