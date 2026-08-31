import sys
import os
import json
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx

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

# Выделенные клиенты
API_CLIENT: Optional[httpx.AsyncClient] = None
POLL_CLIENT: Optional[httpx.AsyncClient] = None

# Кэш дедлайнов в оперативной памяти (кнопки читают ТОЛЬКО его, 0 запросов к сайту МИРЭА)
CACHED_EVENTS: List[DeadlineEvent] = []


def load_events_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "events" not in data and isinstance(data, dict):
                    return {"events": data}
                return data
        except Exception as e:
            logger.warning(f"Не удалось прочитать {STATE_FILE}: {e}")
    return {"events": {}}


def save_events_state(data: Dict[str, Any]):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения {STATE_FILE}: {e}")


def get_moodle_client() -> MoodleClient:
    return MoodleClient(calendar_url=CALENDAR_URL, token=MOODLE_TOKEN)


async def fetch_moodle_calendar_to_cache() -> List[DeadlineEvent]:
    """Единственная функция, делающая сетевой запрос к СДО МИРЭА"""
    global CACHED_EVENTS
    logger.info("--> Запрос к календарю СДО МИРЭА...")
    client = get_moodle_client()
    try:
        events = await client.get_upcoming_deadlines(limit=100)
        CACHED_EVENTS = events
        logger.info(f"<-- Успешно получено {len(events)} событий из СДО МИРЭА")
        return events
    except Exception as e:
        logger.error(f"Ошибка запроса к СДО МИРЭА: {e}")
        return CACHED_EVENTS


def format_deadline_card(event: DeadlineEvent, num: Optional[int] = None) -> str:
    prefix = f"{num}. " if num is not None else ""
    icon = "📌"
    
    task_link = f"[📝 К заданию]({event.url})"
    event_link = f"[📚 Страница в СДО]({event.event_view_url})"
    
    return (
        f"{prefix}{icon} *{event.clean_name}*\n"
        f"📚 *Предмет:* {event.course_name}\n"
        f"⏰ *Срок сдачи:* `{event.formatted_date}`\n"
        f"⏳ *Статус:* _{event.time_left_str}_\n"
        f"🔗 {task_link} • {event_link}\n"
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


async def tg_api(method: str, payload: Optional[Dict] = None) -> Dict:
    global API_CLIENT
    if API_CLIENT is None or API_CLIENT.is_closed:
        API_CLIENT = httpx.AsyncClient(timeout=15.0)
    try:
        resp = await API_CLIENT.post(f"{TG_API_URL}/{method}", json=payload or {})
        return resp.json()
    except Exception as e:
        logger.error(f"Ошибка Telegram API ({method}): {e}")
        return {}


async def set_bot_menu_commands():
    commands = [
        {"command": "deadlines", "description": "📋 Доступные дедлайны (до 3 недель)"},
        {"command": "completed", "description": "✅ Сданные / закрытые работы"},
        {"command": "menu", "description": "📱 Главное меню"},
        {"command": "check", "description": "🔄 Принудительно обновить СДО"}
    ]
    await tg_api("setMyCommands", {"commands": commands})


async def handle_start_or_menu(chat_id: str, message_id: Optional[int] = None):
    """Показывает главное меню (0 запросов к МИРЭА)"""
    kb = {
        "inline_keyboard": [
            [{"text": "📋 Ближайшие дедлайны (до 3 нед.)", "callback_data": "show_deadlines"}],
            [{"text": "📅 Дедлайны на 30 дней", "callback_data": "show_deadlines_30"}],
            [{"text": "✅ Сданные / закрытые работы", "callback_data": "show_completed"}],
            [{"text": "🔄 Проверить обновления СДО", "callback_data": "refresh_deadlines"}]
        ]
    }
    text = (
        "👋 *Главное меню бота СДО РТУ МИРЭА*\n\n"
        "⚡️ *Только доступные задания:* закрытые тесты скрыты до момента открытия.\n"
        "🎯 *Фильтр по времени:* дедлайны на ближайшие **3 недели**.\n"
        "🆕 *Новинки:* новые добавленные работы выделяются при первом показе.\n"
        "✅ *Отметки:* любую работу можно отметить сданной кнопкой `[✅ Сдал #N]`.\n\n"
        "Выберите действие кнопками ниже:"
    )
    if message_id:
        await tg_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": kb
        })
    else:
        await tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": kb
        })


async def handle_deadlines_command(chat_id: str, days_limit: int = 21, message_id: Optional[int] = None):
    """Показывает дедлайны ТОЛЬКО из локальной памяти (0 запросов к МИРЭА)"""
    global CACHED_EVENTS
    try:
        all_events = CACHED_EVENTS
        state_data = load_events_state()
        events_dict = state_data.get("events", {})

        now = datetime.now(MSK_TZ)
        now_ts = int(now.timestamp())
        max_seconds = days_limit * 24 * 3600

        new_events = []
        regular_events = []
        future_nearest_event: Optional[DeadlineEvent] = None

        for event in all_events:
            # Скрываем не открывшиеся тесты
            if event.is_opening and event.due_timestamp > now_ts:
                continue

            event_key = str(event.event_id)
            ev_state = events_dict.get(event_key, {})
            
            if ev_state.get("is_completed", False):
                continue

            time_diff = event.due_timestamp - now_ts
            if time_diff < -7200:
                continue

            if time_diff <= max_seconds:
                if ev_state.get("is_new_unseen", False):
                    new_events.append(event)
                else:
                    regular_events.append(event)
            elif future_nearest_event is None:
                future_nearest_event = event

        all_shown = new_events + regular_events

        if not all_shown:
            nearest_info = ""
            if future_nearest_event:
                days_away = (future_nearest_event.due_timestamp - now_ts) / 86400.0
                nearest_info = (
                    f"\n\n🗓 *Ближайшая сдача:* `{future_nearest_event.formatted_date}` (через {days_away:.0f} дн.)\n"
                    f"📌 *{future_nearest_event.clean_name}* ({future_nearest_event.course_name})"
                )

            text = (
                f"🎉 *Отличные новости!* В ближайшие **{days_limit} дней** горящих дедлайнов нет.\n"
                f"_Все доступные работы запланированы позже, а закрытые тесты еще не начались_{nearest_info}"
            )
            kb = {
                "inline_keyboard": [
                    [{"text": "📅 Показать на 30 дней", "callback_data": "show_deadlines_30"}],
                    [{"text": "✅ Посмотреть сданные работы", "callback_data": "show_completed"}],
                    [{"text": "📱 Главное меню", "callback_data": "show_menu"}]
                ]
            }
            if message_id:
                await tg_api("editMessageText", {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": kb
                })
            else:
                await tg_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": kb
                })
            return

        msg_parts = []
        inline_keyboard = []
        num_counter = 1
        num_to_event = {}

        if new_events:
            msg_parts.append(f"🆕 *НОВЫЕ ДЕДЛАЙНЫ (добавлены недавно)* — {len(new_events)}:")
            cards = []
            for ev in new_events:
                cards.append(format_deadline_card(ev, num_counter))
                num_to_event[num_counter] = ev
                num_counter += 1
            msg_parts.append("\n────────────────────\n".join(cards))

        if regular_events:
            header_title = f"📋 *Доступные дедлайны (до {days_limit} дн.)*" if not new_events else f"📋 *Остальные дедлайны (до {days_limit} дн.)*"
            msg_parts.append(f"{header_title} — {len(regular_events)}:")
            cards = []
            for ev in regular_events:
                cards.append(format_deadline_card(ev, num_counter))
                num_to_event[num_counter] = ev
                num_counter += 1
            msg_parts.append("\n────────────────────\n".join(cards))

        row = []
        for n, ev in num_to_event.items():
            row.append({"text": f"✅ Сдал #{n}", "callback_data": f"done_{ev.event_id}"})
            if len(row) == 3:
                inline_keyboard.append(row)
                row = []
        if row:
            inline_keyboard.append(row)

        menu_row = []
        if days_limit == 21:
            menu_row.append({"text": "📅 На 30 дней", "callback_data": "show_deadlines_30"})
        else:
            menu_row.append({"text": "🎯 На 3 недели", "callback_data": "show_deadlines"})
        menu_row.append({"text": "✅ Сданные", "callback_data": "show_completed"})
        menu_row.append({"text": "🔄 Обновить", "callback_data": "refresh_deadlines"})
        inline_keyboard.append(menu_row)

        final_text = "\n\n".join(msg_parts)
        if message_id:
            await tg_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": final_text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": inline_keyboard}
            })
        else:
            await tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": final_text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": inline_keyboard}
            })

        if new_events:
            for ev in new_events:
                ev_key = str(ev.event_id)
                if ev_key in events_dict:
                    events_dict[ev_key]["is_new_unseen"] = False
            state_data["events"] = events_dict
            save_events_state(state_data)

    except Exception as e:
        logger.error(f"Ошибка в handle_deadlines: {e}", exc_info=True)


async def handle_completed_command(chat_id: str, message_id: Optional[int] = None):
    """Показывает сданные работы из локальной базы (0 запросов к МИРЭА)"""
    state_data = load_events_state()
    events_dict = state_data.get("events", {})

    completed_list = []
    for ev_id, ev_data in events_dict.items():
        if ev_data.get("is_completed", False):
            completed_list.append((ev_id, ev_data))

    if not completed_list:
        kb = {
            "inline_keyboard": [
                [{"text": "📋 Показать дедлайны", "callback_data": "show_deadlines"}],
                [{"text": "📱 Главное меню", "callback_data": "show_menu"}]
            ]
        }
        text = "📭 *Список сданных работ пуст.*\n\nКогда вы сдадите работу, нажмите кнопку *«✅ Сдал #N»* в списке дедлайнов."
        if message_id:
            await tg_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": kb
            })
        else:
            await tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": kb
            })
        return

    completed_list.sort(key=lambda x: x[1].get("completed_at", 0), reverse=True)

    text_parts = [f"✅ *Сданные и закрытые работы* (всего: {len(completed_list)}):\n"]
    buttons = []
    row = []

    for idx, (ev_id, ev_data) in enumerate(completed_list[:15], 1):
        name = ev_data.get("name", "Без названия")
        course = ev_data.get("course", "СДО")
        clean = name.replace(" - срок сдачи", "").replace(" срок сдачи", "").replace(" is due", "")
        text_parts.append(f"{idx}. *{clean}* — _Сдано_\n   📚 _{course}_\n")
        row.append({"text": f"↩️ Вернуть #{idx}", "callback_data": f"undone_{ev_id}"})
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        {"text": "📋 К дедлайнам", "callback_data": "show_deadlines"},
        {"text": "📱 Меню", "callback_data": "show_menu"}
    ])

    final_text = "\n".join(text_parts)
    if message_id:
        await tg_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": final_text,
            "parse_mode": "Markdown",
            "reply_markup": {"inline_keyboard": buttons}
        })
    else:
        await tg_api("sendMessage", {
            "chat_id": chat_id,
            "text": final_text,
            "parse_mode": "Markdown",
            "reply_markup": {"inline_keyboard": buttons}
        })


async def handle_mark_done(chat_id: str, message_id: Optional[int], event_id: str):
    """Помечает работу сданной в локальной памяти (0 запросов к МИРЭА)"""
    state_data = load_events_state()
    events_dict = state_data.get("events", {})

    if event_id in events_dict:
        events_dict[event_id]["is_completed"] = True
        events_dict[event_id]["completed_at"] = int(time.time())
        events_dict[event_id]["is_new_unseen"] = False
        state_data["events"] = events_dict
        save_events_state(state_data)
    else:
        events_dict[event_id] = {
            "is_completed": True,
            "completed_at": int(time.time()),
            "is_new_unseen": False
        }
        state_data["events"] = events_dict
        save_events_state(state_data)

    await handle_deadlines_command(chat_id, message_id=message_id)


async def handle_mark_undone(chat_id: str, message_id: Optional[int], event_id: str):
    """Возвращает работу из сданных в локальной памяти (0 запросов к МИРЭА)"""
    state_data = load_events_state()
    events_dict = state_data.get("events", {})

    if event_id in events_dict:
        events_dict[event_id]["is_completed"] = False
        events_dict[event_id]["completed_at"] = None
        state_data["events"] = events_dict
        save_events_state(state_data)

    await handle_completed_command(chat_id, message_id=message_id)


async def handle_force_refresh_command(chat_id: str, message_id: Optional[int] = None):
    """Принудительное обновление: опрашивает МИРЭА и показывает свежие данные"""
    await fetch_moodle_calendar_to_cache()
    await handle_deadlines_command(chat_id, message_id=message_id)


async def background_monitoring_task():
    logger.info("Фоновый мониторинг дедлайнов и открытий тестов запущен...")
    
    while True:
        try:
            if not TELEGRAM_CHAT_ID:
                logger.warning("TELEGRAM_CHAT_ID не задан в .env, фоновые алерты отключены")
                await asyncio.sleep(CHECK_INTERVAL * 60)
                continue

            events = await fetch_moodle_calendar_to_cache()
            state_data = load_events_state()
            events_dict = state_data.get("events", {})

            now = datetime.now(MSK_TZ)
            now_ts = int(now.timestamp())

            for event in events:
                event_key = str(event.event_id)
                event_state = events_dict.get(event_key)

                if not event_state:
                    event_state = {
                        "name": event.name,
                        "course": event.course_name,
                        "due_timestamp": event.due_timestamp,
                        "is_opening": event.is_opening,
                        "is_completed": False,
                        "completed_at": None,
                        "is_new_unseen": True,
                        "opened_alert_sent": False,
                        "alerts_sent": ["discovered"]
                    }

                    hours_diff = (event.due_timestamp - now_ts) / 3600.0
                    if hours_diff > 12:
                        title = "🔓 *Запланировано открытие теста / задания:*" if event.is_opening else "🆕 *В СДО МИРЭА добавлено новое задание:*"
                        card = format_deadline_card(event)
                        kb = {"inline_keyboard": [[{"text": "✅ Отметить сданным", "callback_data": f"done_{event.event_id}"}]]}
                        await tg_api("sendMessage", {
                            "chat_id": TELEGRAM_CHAT_ID,
                            "text": f"{title}\n\n{card}",
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                            "reply_markup": kb
                        })

                    events_dict[event_key] = event_state
                    continue

                if event_state.get("is_completed", False):
                    continue

                # Перенос дедлайна
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
                        f"🔗 [📝 К заданию]({event.url}) • [📚 Страница в СДО]({event.event_view_url})\n"
                    )
                    kb = {"inline_keyboard": [[{"text": "✅ Отметить сданным", "callback_data": f"done_{event.event_id}"}]]}
                    await tg_api("sendMessage", {
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                        "reply_markup": kb
                    })

                    event_state["due_timestamp"] = event.due_timestamp
                    event_state["alerts_sent"] = ["discovered"]

                # Открытие теста
                if event.is_opening:
                    if now_ts >= event.due_timestamp and (now_ts - event.due_timestamp) <= 43200:
                        if not event_state.get("opened_alert_sent", False):
                            text = (
                                f"🔓 *Открыт доступ к тесту / заданию!*\n\n"
                                f"📌 *{event.clean_name}*\n"
                                f"📚 *Предмет:* {event.course_name}\n"
                                f"⏰ *Открыто с:* `{event.formatted_date}`\n"
                                f"🔗 [📝 Начать выполнение]({event.url}) • [📚 В СДО]({event.event_view_url})\n"
                            )
                            kb = {"inline_keyboard": [[{"text": "✅ Отметить выполненным", "callback_data": f"done_{event.event_id}"}]]}
                            await tg_api("sendMessage", {
                                "chat_id": TELEGRAM_CHAT_ID,
                                "text": text,
                                "parse_mode": "Markdown",
                                "disable_web_page_preview": True,
                                "reply_markup": kb
                            })
                            event_state["opened_alert_sent"] = True

                # Напоминания
                if not event.is_opening:
                    hours_left = (event.due_timestamp - now_ts) / 3600.0
                    alerts_sent = event_state.get("alerts_sent", [])

                    if 0 < hours_left <= 24 and "24h" not in alerts_sent:
                        card = format_deadline_card(event)
                        kb = {"inline_keyboard": [[{"text": "✅ Отметить сданным", "callback_data": f"done_{event.event_id}"}]]}
                        await tg_api("sendMessage", {
                            "chat_id": TELEGRAM_CHAT_ID,
                            "text": f"⚠️ *Внимание! До дедлайна остались сутки:*\n\n{card}",
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                            "reply_markup": kb
                        })
                        alerts_sent.append("24h")

                    if 0 < hours_left <= 3 and "3h" not in alerts_sent:
                        card = format_deadline_card(event)
                        kb = {"inline_keyboard": [[{"text": "✅ Отметить сданным", "callback_data": f"done_{event.event_id}"}]]}
                        await tg_api("sendMessage", {
                            "chat_id": TELEGRAM_CHAT_ID,
                            "text": f"🚨 *Срочно! До окончания сдачи меньше 3 часов:*\n\n{card}",
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                            "reply_markup": kb
                        })
                        alerts_sent.append("3h")

                    event_state["alerts_sent"] = alerts_sent

                events_dict[event_key] = event_state

            state_data["events"] = events_dict
            save_events_state(state_data)

        except Exception as e:
            logger.error(f"Ошибка в фоновом цикле проверки: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL * 60)


async def poll_telegram_updates():
    global POLL_CLIENT
    logger.info("Long polling Telegram запущен...")
    offset = 0

    if POLL_CLIENT is None or POLL_CLIENT.is_closed:
        POLL_CLIENT = httpx.AsyncClient(timeout=25.0)

    while True:
        try:
            params = {"offset": offset, "timeout": 15}
            resp = await POLL_CLIENT.get(f"{TG_API_URL}/getUpdates", params=params)
            
            if resp.status_code != 200:
                await asyncio.sleep(1)
                continue

            data = resp.json()
            if not data.get("ok"):
                await asyncio.sleep(1)
                continue

            updates = data.get("result", [])
            for update in updates:
                offset = update["update_id"] + 1

                # 1. Текстовые команды (чисто локальная обработка без ожидания сайта МИРЭА)
                if "message" in update:
                    msg = update["message"]
                    chat_id = str(msg["chat"]["id"])
                    text = (msg.get("text") or "").strip()

                    if text in ["/start", "/menu", "📱 Главное меню"]:
                        asyncio.create_task(handle_start_or_menu(chat_id))
                    elif text in ["/deadlines", "📋 Дедлайны (до 3 нед.)"]:
                        asyncio.create_task(handle_deadlines_command(chat_id, days_limit=21))
                    elif text in ["/deadlines_30", "📅 Дедлайны на 30 дней"]:
                        asyncio.create_task(handle_deadlines_command(chat_id, days_limit=31))
                    elif text in ["/completed", "/completed_deadlines", "✅ Сданные работы"]:
                        asyncio.create_task(handle_completed_command(chat_id))
                    elif text in ["/check", "🔄 Обновить СДО"]:
                        asyncio.create_task(handle_force_refresh_command(chat_id))

                # 2. Кнопки меню (мгновенно из памяти, 0 сетевых запросов к МИРЭА)
                elif "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    msg = cb.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id"))
                    message_id = msg.get("message_id")
                    cb_data = cb.get("data", "")

                    asyncio.create_task(tg_api("answerCallbackQuery", {"callback_query_id": cb_id}))

                    if cb_data == "show_deadlines":
                        asyncio.create_task(handle_deadlines_command(chat_id, days_limit=21, message_id=message_id))
                    elif cb_data == "show_deadlines_30":
                        asyncio.create_task(handle_deadlines_command(chat_id, days_limit=31, message_id=message_id))
                    elif cb_data == "show_menu":
                        asyncio.create_task(handle_start_or_menu(chat_id, message_id=message_id))
                    elif cb_data == "refresh_deadlines":
                        asyncio.create_task(handle_force_refresh_command(chat_id, message_id=message_id))
                    elif cb_data == "show_completed":
                        asyncio.create_task(handle_completed_command(chat_id, message_id=message_id))
                    elif cb_data.startswith("done_"):
                        ev_id = cb_data.replace("done_", "")
                        asyncio.create_task(handle_mark_done(chat_id, message_id, ev_id))
                    elif cb_data.startswith("undone_"):
                        ev_id = cb_data.replace("undone_", "")
                        asyncio.create_task(handle_mark_undone(chat_id, message_id, ev_id))

        except httpx.TimeoutException:
            pass
        except Exception as e:
            logger.error(f"Ошибка в polling: {e}")
            await asyncio.sleep(1)


async def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ОШИБКА: TELEGRAM_BOT_TOKEN не задан в .env!")
        return

    # Загружаем первоначальный кэш при старте
    try:
        await fetch_moodle_calendar_to_cache()
    except Exception as e:
        logger.warning(f"Первоначальная загрузка кэша: {e}")

    try:
        await set_bot_menu_commands()
    except Exception as e:
        logger.warning(f"Команды меню: {e}")

    logger.info("Бот запущен. Навигация по кнопкам работает на 100% из памяти без запросов к МИРЭА!")
    
    await asyncio.gather(
        background_monitoring_task(),
        poll_telegram_updates()
    )


if __name__ == "__main__":
    asyncio.run(main())
