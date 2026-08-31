import sys
import os
import json
import time
import socket
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx

# 1. Принудительный IPv4 (устраняет любые зависания на сокетах IPv6 в РФ)
orig_getaddrinfo = socket.getaddrinfo
def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = ipv4_getaddrinfo

# 2. Настройка UTF-8 для консоли
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

# Кэш в памяти
CACHED_EVENTS: List[DeadlineEvent] = []


def clean_text_for_markdown(text: str) -> str:
    if not text:
        return ""
    text = text.replace("_", " ").replace("[", "(").replace("]", ")").replace("*", "").replace("`", "")
    return re.sub(r'\s+', ' ', text).strip()


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
    
    clean_name = clean_text_for_markdown(event.clean_name)
    clean_course = clean_text_for_markdown(event.course_name)
    
    task_link = f"[📝 К заданию]({event.url})"
    event_link = f"[📚 Страница в СДО]({event.event_view_url})"
    
    return (
        f"{prefix}{icon} *{clean_name}*\n"
        f"📚 *Предмет:* {clean_course}\n"
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
        API_CLIENT = httpx.AsyncClient(timeout=10.0)
    try:
        resp = await API_CLIENT.post(f"{TG_API_URL}/{method}", json=payload or {})
        data = resp.json()
        desc = data.get("description", "")
        if not data.get("ok") and "message is not modified" not in desc:
            logger.warning(f"Telegram API ({method}): {desc}")
        return data
    except Exception as e:
        logger.error(f"Сетевая ошибка Telegram API ({method}): {e}")
        return {}


async def send_or_edit_message(chat_id: str, message_id: Optional[int], text: str, reply_markup: Optional[Dict] = None):
    if message_id:
        res = await tg_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": reply_markup
        })
        if res.get("ok") or "message is not modified" in res.get("description", ""):
            return

    await tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
        "reply_markup": reply_markup
    })


async def set_bot_menu_commands():
    commands = [
        {"command": "deadlines", "description": "📋 Доступные дедлайны"},
        {"command": "completed", "description": "✅ Сданные / закрытые работы"},
        {"command": "menu", "description": "📱 Главное меню"},
        {"command": "check", "description": "🔄 Принудительно обновить СДО"}
    ]
    await tg_api("setMyCommands", {"commands": commands})


async def handle_start_or_menu(chat_id: str, message_id: Optional[int] = None):
    kb = {
        "inline_keyboard": [
            [{"text": "📋 Ближайшие дедлайны", "callback_data": "show_deadlines"}],
            [{"text": "✅ Сданные / закрытые работы", "callback_data": "show_completed"}],
            [{"text": "🔄 Проверить обновления СДО", "callback_data": "refresh_deadlines"}]
        ]
    }
    text = (
        "👋 *Главное меню бота СДО РТУ МИРЭА*\n\n"
        "⚡️ *Только доступные задания:* закрытые тесты скрыты до момента открытия.\n"
        "🎯 *Фильтр по времени:* дедлайны на ближайшие **3 недели** (или на месяц, если в 3 недели пусто).\n"
        "🆕 *Новинки:* новые добавленные работы выделяются при первом показе.\n"
        "✅ *Отметки:* любую работу можно отметить сданной кнопкой `[✅ Сдал #N]`.\n\n"
        "Выберите действие кнопками ниже:"
    )
    await send_or_edit_message(chat_id, message_id, text, kb)


async def handle_deadlines_command(chat_id: str, message_id: Optional[int] = None):
    global CACHED_EVENTS
    try:
        all_events = CACHED_EVENTS
        state_data = load_events_state()
        events_dict = state_data.get("events", {})

        now = datetime.now(MSK_TZ)
        now_ts = int(now.timestamp())
        
        three_weeks_seconds = 21 * 24 * 3600
        month_seconds = 31 * 24 * 3600

        # Собираем все доступные незакрытые работы
        available_events = []
        for event in all_events:
            if event.is_opening and event.due_timestamp > now_ts:
                continue

            event_key = str(event.event_id)
            ev_state = events_dict.get(event_key, {})
            
            if ev_state.get("is_completed", False):
                continue

            time_diff = event.due_timestamp - now_ts
            if time_diff < -7200:
                continue

            available_events.append(event)

        # 1. Проверяем, есть ли работы на 3 недели (21 день)
        target_events = [e for e in available_events if (e.due_timestamp - now_ts) <= three_weeks_seconds]
        is_month_extended = False

        # 2. Если на 3 недели пусто, но есть работы в пределах месяца (31 день) — показываем их
        if not target_events:
            target_events = [e for e in available_events if (e.due_timestamp - now_ts) <= month_seconds]
            is_month_extended = True

        # 3. Если вообще нет работ даже на месяц
        if not target_events:
            future_event = available_events[0] if available_events else None
            nearest_info = ""
            if future_event:
                days_away = (future_event.due_timestamp - now_ts) / 86400.0
                f_name = clean_text_for_markdown(future_event.clean_name)
                f_course = clean_text_for_markdown(future_event.course_name)
                nearest_info = (
                    f"\n\n🗓 *Ближайшая сдача:* `{future_event.formatted_date}` (через {days_away:.0f} дн.)\n"
                    f"📌 *{f_name}* ({f_course})"
                )

            text = (
                f"🎉 *Отличные новости!* В ближайший месяц горящих дедлайнов нет.\n"
                f"_Все доступные работы запланированы позже, а закрытые тесты еще не начались_{nearest_info}"
            )
            kb = {
                "inline_keyboard": [
                    [{"text": "✅ Посмотреть сданные работы", "callback_data": "show_completed"}],
                    [{"text": "📱 Главное меню", "callback_data": "show_menu"}]
                ]
            }
            await send_or_edit_message(chat_id, message_id, text, kb)
            return

        new_events = []
        regular_events = []
        for ev in target_events:
            ev_key = str(ev.event_id)
            ev_state = events_dict.get(ev_key, {})
            if ev_state.get("is_new_unseen", False):
                new_events.append(ev)
            else:
                regular_events.append(ev)

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
            if is_month_extended:
                header_title = f"📋 *В ближайшие 3 недели сдач нет. Работы на месяц (до 30 дн.)*" if not new_events else f"📋 *Остальные работы на месяц*"
            else:
                header_title = f"📋 *Доступные дедлайны (до 3 недель)*" if not new_events else f"📋 *Остальные дедлайны (до 3 недель)*"
            
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

        menu_row = [
            {"text": "✅ Сданные", "callback_data": "show_completed"},
            {"text": "🔄 Обновить", "callback_data": "refresh_deadlines"},
            {"text": "📱 Меню", "callback_data": "show_menu"}
        ]
        inline_keyboard.append(menu_row)

        final_text = "\n\n".join(msg_parts)
        await send_or_edit_message(chat_id, message_id, final_text, {"inline_keyboard": inline_keyboard})

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
    state_data = load_events_state()
    events_dict = state_data.get("events", {})

    completed_list = []
    for ev_id, ev_data in events_dict.items():
        if ev_data.get("is_completed", False):
            completed_list.append((ev_id, ev_data))

    if not completed_list:
        kb = {
            "inline_keyboard": [
                [{"text": "📋 К дедлайнам", "callback_data": "show_deadlines"}],
                [{"text": "📱 Главное меню", "callback_data": "show_menu"}]
            ]
        }
        text = "📭 *Список сданных работ пуст.*\n\nКогда вы сдадите работу, нажмите кнопку *«✅ Сдал #N»* в списке дедлайнов."
        await send_or_edit_message(chat_id, message_id, text, kb)
        return

    completed_list.sort(key=lambda x: x[1].get("completed_at", 0), reverse=True)

    text_parts = [f"✅ *Сданные и закрытые работы* (всего: {len(completed_list)}):\n"]
    buttons = []
    row = []

    for idx, (ev_id, ev_data) in enumerate(completed_list[:15], 1):
        name = ev_data.get("name", "Без названия")
        course = ev_data.get("course", "СДО")
        clean_name = clean_text_for_markdown(name.replace(" - срок сдачи", "").replace(" срок сдачи", "").replace(" is due", ""))
        clean_c = clean_text_for_markdown(course)
        text_parts.append(f"{idx}. *{clean_name}* — _Сдано_\n   📚 _{clean_c}_\n")
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
    await send_or_edit_message(chat_id, message_id, final_text, {"inline_keyboard": buttons})


async def handle_mark_done(chat_id: str, message_id: Optional[int], event_id: str):
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
    state_data = load_events_state()
    events_dict = state_data.get("events", {})

    if event_id in events_dict:
        events_dict[event_id]["is_completed"] = False
        events_dict[event_id]["completed_at"] = None
        state_data["events"] = events_dict
        save_events_state(state_data)

    await handle_completed_command(chat_id, message_id=message_id)


async def handle_force_refresh_command(chat_id: str, message_id: Optional[int] = None):
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
                    c_name = clean_text_for_markdown(event.clean_name)
                    c_course = clean_text_for_markdown(event.course_name)

                    text = (
                        f"🔄 *Внимание! Дедлайн изменен преподавателем:*\n\n"
                        f"📌 *{c_name}*\n"
                        f"📚 *Предмет:* {c_course}\n"
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
                            c_name = clean_text_for_markdown(event.clean_name)
                            c_course = clean_text_for_markdown(event.course_name)
                            text = (
                                f"🔓 *Открыт доступ к тесту / заданию!*\n\n"
                                f"📌 *{c_name}*\n"
                                f"📚 *Предмет:* {c_course}\n"
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

                if "message" in update:
                    msg = update["message"]
                    chat_id = str(msg["chat"]["id"])
                    text = (msg.get("text") or "").strip()

                    if text in ["/start", "/menu", "📱 Главное меню"]:
                        asyncio.create_task(handle_start_or_menu(chat_id))
                    elif text in ["/deadlines", "📋 Дедлайны (до 3 нед.)"]:
                        asyncio.create_task(handle_deadlines_command(chat_id))
                    elif text in ["/completed", "/completed_deadlines", "✅ Сданные работы"]:
                        asyncio.create_task(handle_completed_command(chat_id))
                    elif text in ["/check", "🔄 Обновить СДО"]:
                        asyncio.create_task(handle_force_refresh_command(chat_id))

                elif "callback_query" in update:
                    cb = update["callback_query"]
                    cb_id = cb["id"]
                    msg = cb.get("message", {})
                    chat_id = str(msg.get("chat", {}).get("id"))
                    message_id = msg.get("message_id")
                    cb_data = cb.get("data", "")

                    asyncio.create_task(tg_api("answerCallbackQuery", {"callback_query_id": cb_id}))

                    if cb_data == "show_deadlines":
                        asyncio.create_task(handle_deadlines_command(chat_id, message_id=message_id))
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

    try:
        await fetch_moodle_calendar_to_cache()
    except Exception as e:
        logger.warning(f"Первоначальная загрузка кэша: {e}")

    try:
        await set_bot_menu_commands()
    except Exception as e:
        logger.warning(f"Команды меню: {e}")

    logger.info("Бот запущен с автоматическим определением горизонта дедлайнов!")
    
    await asyncio.gather(
        background_monitoring_task(),
        poll_telegram_updates()
    )


if __name__ == "__main__":
    asyncio.run(main())
