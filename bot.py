import sys
import os
import json
import socket
import asyncio
import logging
import aiohttp
from aiohttp import web
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

# Исправление сетевого стека для Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession

from moodle_client import MoodleClient, DeadlineEvent, MSK_TZ

# Абсолютный путь к папке проекта
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("DeadlinesBot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CALENDAR_URL = os.getenv("CALENDAR_URL")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "2"))
PORT = int(os.getenv("PORT", "8000"))

STATE_FILE = os.path.join(BASE_DIR, "events_state.json")


class IPv4Session(AiohttpSession):
    """Сетевая сессия с чистым IPv4 и отключенным happy-eyeballs для стабильности на Windows"""
    async def create_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                family=socket.AF_INET,
                happy_eyeballs_delay=None,
                enable_cleanup_closed=True
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=45, connect=15)
            )
        return self._session


def load_events_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Не удалось прочитать {STATE_FILE}: {e}")
    return {}


def save_events_state(data: Dict[str, Any]):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения {STATE_FILE}: {e}")


def get_client() -> MoodleClient:
    return MoodleClient(calendar_url=CALENDAR_URL, token=MOODLE_TOKEN)


def format_deadline_card(event: DeadlineEvent) -> str:
    icon = "🔓" if event.is_opening else "📌"
    action_label = "Открытие:" if event.is_opening else "Срок сдачи:"
    return (
        f"{icon} *{event.clean_name}*\n"
        f"📚 *Предмет:* {event.course_name}\n"
        f"⏰ *{action_label}* `{event.formatted_date}`\n"
        f"⏳ *Статус:* _{event.time_left_str}_\n"
        f"🔗 [Открыть в СДО]({event.url})\n"
    )


def build_deadlines_list_message(events: List[DeadlineEvent]) -> str:
    if not events:
        return "🎉 *Отличные новости!* На ближайшее время нет активных дедлайнов и тестов."

    deadlines = [e for e in events if not e.is_opening]
    openings = [e for e in events if e.is_opening]

    parts = []
    if deadlines:
        parts.append(f"📋 *Ближайшие дедлайны (сдача работ)* — {len(deadlines)}:")
        parts.append("\n────────────────────\n".join([format_deadline_card(d) for d in deadlines[:12]]))

    if openings:
        parts.append(f"\n🔓 *Ближайшие открытия тестов / заданий* — {len(openings)}:")
        parts.append("\n────────────────────\n".join([format_deadline_card(o) for o in openings[:8]]))

    return "\n\n".join(parts)


def format_time_diff(diff_seconds: int) -> str:
    abs_diff = abs(diff_seconds)
    days = abs_diff // 86400
    hours = (abs_diff % 86400) // 3600

    time_str = ""
    if days > 0:
        time_str += f"{days} дн. "
    if hours > 0 or days == 0:
        time_str += f"{hours} ч."

    if diff_seconds > 0:
        return f"Продлен на {time_str.strip()}"
    else:
        return f"Сдвинут раньше на {time_str.strip()}"


dp = Dispatcher()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Показать дедлайны", callback_data="show_deadlines")],
        [InlineKeyboardButton(text="🔄 Проверить СДО", callback_data="refresh_deadlines")]
    ])
    await message.answer(
        "👋 *Привет! Я бот для отслеживания дедлайнов СДО РТУ МИРЭА.*\n\n"
        "Я непрерывно слежу за СДО 24/7:\n"
        "• 🔔 Присылаю уведомления, когда *открывается доступ к тесту* или работе\n"
        "• 🔄 Отслеживаю *переносы и изменения дедлайнов*\n"
        "• 🆕 Сообщаю о появлении *новых заданий*\n"
        "• ⚠️ Напоминаю за *24 часа* и *3 часа* до сдачи\n\n"
        "Команды:\n"
        "• `/deadlines` — список всех актуальных работ\n"
        "• `/check` — принудительно обновить список прямо сейчас",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )


@dp.message(Command("deadlines"))
async def cmd_deadlines(message: types.Message):
    await send_deadlines_response(message.answer)


@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    status_msg = await message.answer("🔄 Проверяю дедлайны в СДО МИРЭА...")
    client = get_client()
    try:
        events = await client.get_upcoming_deadlines()
        await status_msg.delete()
        await message.answer(build_deadlines_list_message(events), parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при получении данных: {e}")


@dp.callback_query(F.data == "show_deadlines")
async def cb_show_deadlines(callback: types.CallbackQuery):
    await callback.answer()
    await send_deadlines_response(callback.message.answer)


@dp.callback_query(F.data == "refresh_deadlines")
async def cb_refresh_deadlines(callback: types.CallbackQuery):
    await callback.answer("Обновляю список...")
    await send_deadlines_response(callback.message.answer)


async def send_deadlines_response(answer_func):
    client = get_client()
    try:
        events = await client.get_upcoming_deadlines()
        msg_text = build_deadlines_list_message(events)
        await answer_func(msg_text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Ошибка получения дедлайнов: {e}")
        await answer_func(f"❌ Не удалось получить дедлайны: {e}\n\nПроверьте `CALENDAR_URL` в `.env`.")


async def background_monitoring_task(bot: Bot):
    logger.info("Фоновый мониторинг дедлайнов и открытий тестов запущен...")
    
    while True:
        try:
            if not TELEGRAM_CHAT_ID:
                logger.warning("TELEGRAM_CHAT_ID не задан в .env, фоновые алерты отключены")
                await asyncio.sleep(CHECK_INTERVAL * 60)
                continue

            client = get_client()
            events = await client.get_upcoming_deadlines(limit=100)
            state = load_events_state()
            now = datetime.now(MSK_TZ)
            now_ts = int(now.timestamp())

            for event in events:
                event_key = str(event.event_id)
                event_state = state.get(event_key)

                # 1. ОБНАРУЖЕНИЕ НОВОГО СОБЫТИЯ
                if not event_state:
                    event_state = {
                        "name": event.name,
                        "course": event.course_name,
                        "due_timestamp": event.due_timestamp,
                        "is_opening": event.is_opening,
                        "opened_alert_sent": False,
                        "alerts_sent": ["discovered"]
                    }
                    
                    hours_diff = (event.due_timestamp - now_ts) / 3600.0
                    if hours_diff > 12:
                        title = "🔓 *Запланировано открытие теста / задания:*" if event.is_opening else "🆕 *В СДО МИРЭА добавлено новое задание:*"
                        text = f"{title}\n\n" + format_deadline_card(event)
                        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

                    state[event_key] = event_state
                    continue

                # 2. ОТСЛЕЖИВАНИЕ ПЕРЕНОСА ДЕДЛАЙНА
                old_ts = event_state.get("due_timestamp", event.due_timestamp)
                time_shift = event.due_timestamp - old_ts

                if abs(time_shift) > 300:
                    old_dt = datetime.fromtimestamp(old_ts, tz=MSK_TZ).strftime("%d.%m.%Y в %H:%M МСК")
                    shift_info = format_time_diff(time_shift)

                    text = (
                        f"🔄 *Внимание! Дедлайн изменен преподавателем:*\n\n"
                        f"📌 *{event.clean_name}*\n"
                        f"📚 *Предмет:* {event.course_name}\n"
                        f"🗓 *Было:* `{old_dt}`\n"
                        f"⏰ *Стало:* `{event.formatted_date}` ({shift_info})\n"
                        f"⏳ *Статус:* _{event.time_left_str}_\n"
                        f"🔗 [Перейти в СДО]({event.url})\n"
                    )
                    await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)

                    event_state["due_timestamp"] = event.due_timestamp
                    event_state["alerts_sent"] = ["discovered"]

                # 3. МОМЕНТ ОТКРЫТИЯ ДОСТУПА
                if event.is_opening:
                    if now_ts >= event.due_timestamp and (now_ts - event.due_timestamp) <= 43200:
                        if not event_state.get("opened_alert_sent", False):
                            text = (
                                f"🔓 *Открыт доступ к тесту / заданию!*\n\n"
                                f"📌 *{event.clean_name}*\n"
                                f"📚 *Предмет:* {event.course_name}\n"
                                f"⏰ *Открыто с:* `{event.formatted_date}`\n"
                                f"🔗 [Начать выполнение]({event.url})\n"
                            )
                            await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                            event_state["opened_alert_sent"] = True

                # 4. НАПОМИНАНИЯ О ДЕДЛАЙНЕ (24 ЧАСА И 3 ЧАСА)
                if not event.is_opening:
                    hours_left = (event.due_timestamp - now_ts) / 3600.0
                    alerts_sent = event_state.get("alerts_sent", [])

                    if 0 < hours_left <= 24 and "24h" not in alerts_sent:
                        text = f"⚠️ *Внимание! До дедлайна остались сутки:*\n\n" + format_deadline_card(event)
                        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                        alerts_sent.append("24h")

                    if 0 < hours_left <= 3 and "3h" not in alerts_sent:
                        text = f"🚨 *Срочно! До окончания сдачи меньше 3 часов:*\n\n" + format_deadline_card(event)
                        await bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                        alerts_sent.append("3h")

                    event_state["alerts_sent"] = alerts_sent

                state[event_key] = event_state

            save_events_state(state)

        except Exception as e:
            logger.error(f"Ошибка в фоновом цикле проверки: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL * 60)


async def start_healthcheck_server():
    async def handle(request):
        return web.Response(text="MIREA Deadlines Bot is running 24/7!")

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    try:
        await site.start()
        logger.info(f"Healthcheck сервер запущен на порту {PORT}")
    except Exception as e:
        logger.warning(f"Healthcheck сервер не запущен: {e}")


async def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ОШИБКА: TELEGRAM_BOT_TOKEN не задан в .env!")
        return

    await start_healthcheck_server()

    session = IPv4Session()
    bot = Bot(token=TELEGRAM_BOT_TOKEN, session=session)
    asyncio.create_task(background_monitoring_task(bot))
    logger.info("Бот успешно запущен и отслеживает дедлайны...")

    while True:
        try:
            await dp.start_polling(bot, handle_signals=False)
        except Exception as e:
            logger.error(f"Сбой polling Telegram ({e}). Переподключение через 5 сек...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
