import sys
import os
import json
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx

# Настройка UTF-8 для консоли Windows (если запущено с консолью)
if sys.platform == "win32":
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stderr is not None:
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from dotenv import load_dotenv
from moodle_client import MoodleClient, DeadlineEvent, MSK_TZ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Настройка логов
log_handlers = [logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8")]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=log_handlers
)
logger = logging.getLogger("DeadlinesBot")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CALENDAR_URL = os.getenv("CALENDAR_URL")
MOODLE_TOKEN = os.getenv("MOODLE_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_MINUTES", "2"))

STATE_FILE = os.path.join(BASE_DIR, "events_state.json")
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


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


async def tg_send_message(chat_id: str, text: str, reply_markup: Optional[Dict] = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{TG_API_URL}/sendMessage", json=payload)
        return resp.json()


async def tg_answer_callback(callback_query_id: str, text: Optional[str] = None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{TG_API_URL}/answerCallbackQuery", json=payload)


async def handle_start_command(chat_id: str):
    kb = {
        "inline_keyboard": [
            [{"text": "📋 Показать дедлайны", "callback_data": "show_deadlines"}],
            [{"text": "🔄 Проверить СДО", "callback_data": "refresh_deadlines"}]
        ]
    }
    text = (
        "👋 *Привет! Я бот для отслеживания дедлайнов СДО РТУ МИРЭА.*\n\n"
        "Я непрерывно слежу за СДО 24/7:\n"
        "• 🔔 Присылаю уведомления, когда *открывается доступ к тесту* или работе\n"
        "• 🔄 Отслеживаю *переносы и изменения дедлайнов*\n"
        "• 🆕 Сообщаю о появлении *новых заданий*\n"
        "• ⚠️ Напоминаю за *24 часа* и *3 часа* до сдачи\n\n"
        "Команды:\n"
        "• `/deadlines` — список всех актуальных работ\n"
        "• `/check` — принудительно обновить список прямо сейчас"
    )
    await tg_send_message(chat_id, text, reply_markup=kb)


async def handle_deadlines_command(chat_id: str):
    client = get_client()
    try:
        events = await client.get_upcoming_deadlines()
        msg_text = build_deadlines_list_message(events)
        await tg_send_message(chat_id, msg_text)
    except Exception as e:
        logger.error(f"Ошибка получения дедлайнов: {e}")
        await tg_send_message(chat_id, f"❌ Не удалось получить дедлайны: {e}\n\nПроверьте `CALENDAR_URL` в `.env`.")


async def background_monitoring_task():
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
                        await tg_send_message(TELEGRAM_CHAT_ID, text)

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
                    await tg_send_message(TELEGRAM_CHAT_ID, text)

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
                            await tg_send_message(TELEGRAM_CHAT_ID, text)
                            event_state["opened_alert_sent"] = True

                # 4. НАПОМИНАНИЯ О ДЕДЛАЙНЕ (24 ЧАСА И 3 ЧАСА)
                if not event.is_opening:
                    hours_left = (event.due_timestamp - now_ts) / 3600.0
                    alerts_sent = event_state.get("alerts_sent", [])

                    if 0 < hours_left <= 24 and "24h" not in alerts_sent:
                        text = f"⚠️ *Внимание! До дедлайна остались сутки:*\n\n" + format_deadline_card(event)
                        await tg_send_message(TELEGRAM_CHAT_ID, text)
                        alerts_sent.append("24h")

                    if 0 < hours_left <= 3 and "3h" not in alerts_sent:
                        text = f"🚨 *Срочно! До окончания сдачи меньше 3 часов:*\n\n" + format_deadline_card(event)
                        await tg_send_message(TELEGRAM_CHAT_ID, text)
                        alerts_sent.append("3h")

                    event_state["alerts_sent"] = alerts_sent

                state[event_key] = event_state

            save_events_state(state)

        except Exception as e:
            logger.error(f"Ошибка в фоновом цикле проверки: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL * 60)


async def poll_telegram_updates():
    logger.info("Long polling Telegram запущен...")
    offset = 0

    async with httpx.AsyncClient(timeout=35.0) as client:
        while True:
            try:
                params = {"offset": offset, "timeout": 20}
                resp = await client.get(f"{TG_API_URL}/getUpdates", params=params)
                
                if resp.status_code != 200:
                    logger.warning(f"Telegram API status {resp.status_code}: {resp.text}")
                    await asyncio.sleep(3)
                    continue

                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(2)
                    continue

                updates = data.get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1

                    if "message" in update:
                        msg = update["message"]
                        chat_id = str(msg["chat"]["id"])
                        text = (msg.get("text") or "").strip()

                        if text == "/start":
                            await handle_start_command(chat_id)
                        elif text in ["/deadlines", "/check"]:
                            await handle_deadlines_command(chat_id)

                    elif "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb["id"]
                        chat_id = str(cb["message"]["chat"]["id"])
                        cb_data = cb.get("data")

                        if cb_data in ["show_deadlines", "refresh_deadlines"]:
                            await tg_answer_callback(cb_id, "Загружаю дедлайны...")
                            await handle_deadlines_command(chat_id)
                        else:
                            await tg_answer_callback(cb_id)

            except httpx.TimeoutException:
                pass
            except Exception as e:
                logger.error(f"Ошибка в polling: {e}")
                await asyncio.sleep(3)


async def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ОШИБКА: TELEGRAM_BOT_TOKEN не задан в .env!")
        return

    logger.info("Бот успешно запущен и отслеживает дедлайны...")
    
    await asyncio.gather(
        background_monitoring_task(),
        poll_telegram_updates()
    )


if __name__ == "__main__":
    asyncio.run(main())
