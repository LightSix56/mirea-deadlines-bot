import sys
import os
import json
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from moodle_client import MoodleClient, DeadlineEvent, MSK_TZ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("DeadlinesChecker")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CALENDAR_URL = os.getenv("CALENDAR_URL")

STATE_FILE = os.path.join(BASE_DIR, "events_state.json")
TG_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""


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


async def send_tg_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы!")
        return

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(f"{TG_API_URL}/sendMessage", json=payload)
        if resp.status_code != 200:
            logger.error(f"Ошибка отправки Telegram: {resp.status_code} - {resp.text}")


async def check():
    if not CALENDAR_URL:
        logger.error("CALENDAR_URL не задан!")
        return

    logger.info("Запрашиваю календарь СДО МИРЭА...")
    client = MoodleClient(calendar_url=CALENDAR_URL)
    events = await client.get_upcoming_deadlines(limit=100)
    logger.info(f"Получено событий из календаря: {len(events)}")

    state = load_events_state()
    is_initial_run = len(state) == 0
    now = datetime.now(MSK_TZ)
    now_ts = int(now.timestamp())

    notifications_sent = 0

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

            # Если это не самый первый запуск базы, отправляем алерт о новом задании
            if not is_initial_run:
                hours_diff = (event.due_timestamp - now_ts) / 3600.0
                if hours_diff > 0:
                    title = "🔓 *Запланировано открытие теста / задания:*" if event.is_opening else "🆕 *В СДО МИРЭА добавлено новое задание:*"
                    text = f"{title}\n\n" + format_deadline_card(event)
                    await send_tg_message(text)
                    notifications_sent += 1

            state[event_key] = event_state
            continue

        # 2. ОТСЛЕЖИВАНИЕ ПЕРЕНОСА ДЕДЛАЙНА ПРЕПОДАВАТЕЛЕМ
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
            await send_tg_message(text)
            notifications_sent += 1

            event_state["due_timestamp"] = event.due_timestamp
            event_state["alerts_sent"] = ["discovered"]

        # 3. МОМЕНТ ОТКРЫТИЯ ДОСТУПА
        if event.is_opening:
            if now_ts >= event.due_timestamp and (now_ts - event.due_timestamp) <= 86400:
                if not event_state.get("opened_alert_sent", False):
                    text = (
                        f"🔓 *Открыт доступ к тесту / заданию!*\n\n"
                        f"📌 *{event.clean_name}*\n"
                        f"📚 *Предмет:* {event.course_name}\n"
                        f"⏰ *Открыто с:* `{event.formatted_date}`\n"
                        f"🔗 [Начать выполнение]({event.url})\n"
                    )
                    await send_tg_message(text)
                    notifications_sent += 1
                    event_state["opened_alert_sent"] = True

        # 4. НАПОМИНАНИЯ (ЗА 24 ЧАСА И ЗА 3 ЧАСА)
        if not event.is_opening:
            hours_left = (event.due_timestamp - now_ts) / 3600.0
            alerts_sent = event_state.get("alerts_sent", [])

            if 0 < hours_left <= 24 and "24h" not in alerts_sent:
                text = f"⚠️ *Внимание! До дедлайна остались сутки:*\n\n" + format_deadline_card(event)
                await send_tg_message(text)
                notifications_sent += 1
                alerts_sent.append("24h")

            if 0 < hours_left <= 3 and "3h" not in alerts_sent:
                text = f"🚨 *Срочно! До окончания сдачи меньше 3 часов:*\n\n" + format_deadline_card(event)
                await send_tg_message(text)
                notifications_sent += 1
                alerts_sent.append("3h")

            event_state["alerts_sent"] = alerts_sent

        state[event_key] = event_state

    save_events_state(state)
    logger.info(f"Проверка завершена. Отправлено уведомлений: {notifications_sent}")


if __name__ == "__main__":
    asyncio.run(check())
