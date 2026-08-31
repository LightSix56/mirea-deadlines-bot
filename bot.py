import sys
import os
import json
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import httpx

# Настройка кодировки Windows
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

# Настройка логирования
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

THREE_WEEKS_SECONDS = 21 * 24 * 3600  # 3 недели в секундах


def load_events_state() -> Dict[str, Any]:
    """Загружает состояние событий"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Миграция старого формата в новый если нужно
                if "events" not in data and isinstance(data, dict):
                    return {"events": data}
                return data
        except Exception as e:
            logger.warning(f"Не удалось прочитать {STATE_FILE}: {e}")
    return {"events": {}}


def save_events_state(data: Dict[str, Any]):
    """Сохраняет состояние событий"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Ошибка сохранения {STATE_FILE}: {e}")


def get_client() -> MoodleClient:
    return MoodleClient(calendar_url=CALENDAR_URL, token=MOODLE_TOKEN)


def format_deadline_card(event: DeadlineEvent, num: Optional[int] = None) -> str:
    prefix = f"{num}. " if num is not None else ""
    icon = "🔓" if event.is_opening else "📌"
    action_label = "Открытие:" if event.is_opening else "Срок сдачи:"
    return (
        f"{prefix}{icon} *{event.clean_name}*\n"
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


async def tg_request(method: str, payload: Optional[Dict] = None):
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(f"{TG_API_URL}/{method}", json=payload or {})
        return resp.json()


async def set_bot_menu_commands():
    """Регистрирует команды в официальном меню Telegram"""
    commands = [
        {"command": "deadlines", "description": "📋 Ближайшие дедлайны (до 3 недель)"},
        {"command": "completed", "description": "✅ Сданные / закрытые работы"},
        {"command": "menu", "description": "📱 Главное интерактивное меню"},
        {"command": "check", "description": "🔄 Принудительно обновить СДО"}
    ]
    await tg_request("setMyCommands", {"commands": commands})


def get_main_reply_keyboard() -> Dict:
    """Постоянная клавиатура внизу экрана"""
    return {
        "keyboard": [
            [{"text": "📋 Дедлайны (до 3 нед.)"}, {"text": "✅ Сданные работы"}],
            [{"text": "🔄 Обновить СДО"}, {"text": "📱 Главное меню"}]
        ],
        "resize_keyboard": True
    }


async def handle_start_or_menu(chat_id: str):
    kb = {
        "inline_keyboard": [
            [{"text": "📋 Ближайшие дедлайны (до 3 нед.)", "callback_data": "show_deadlines"}],
            [{"text": "✅ Сданные / закрытые работы", "callback_data": "show_completed"}],
            [{"text": "🔄 Проверить обновления в СДО", "callback_data": "refresh_deadlines"}]
        ]
    }
    text = (
        "👋 *Главное меню бота СДО РТУ МИРЭА*\n\n"
        "⚡️ *Фильтрация:* показываются только работы с дедлайном **до 3 недель**.\n"
        "🆕 *Новинки:* новые задания показываются отдельно при первом просмотре.\n"
        "✅ *Отметки:* любую работу можно отметить сданной, и она скроется из активных.\n\n"
        "Выберите действие кнопками ниже:"
    )
    await tg_request("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": kb
    })


async def handle_deadlines_command(chat_id: str):
    client = get_client()
    try:
        all_events = await client.get_upcoming_deadlines(limit=100)
        state_data = load_events_state()
        events_dict = state_data.get("events", {})

        now = datetime.now(MSK_TZ)
        now_ts = int(now.timestamp())

        # Фильтруем события:
        # 1. Не закрытые (not is_completed)
        # 2. До которых осталось 3 недели и меньше (diff <= 21 days)
        active_events = []
        new_events = []
        closing_events = []
        opening_events = []

        for event in all_events:
            event_key = str(event.event_id)
            ev_state = events_dict.get(event_key, {})
            
            # Пропускаем выполненные
            if ev_state.get("is_completed", False):
                continue

            time_diff = event.due_timestamp - now_ts
            # Оставляем только те, что в пределах 3 недель (и не прошли более 2 часов назад)
            if -7200 <= time_diff <= THREE_WEEKS_SECONDS:
                if ev_state.get("is_new_unseen", False):
                    new_events.append(event)
                elif event.is_opening:
                    opening_events.append(event)
                else:
                    closing_events.append(event)
                
                active_events.append(event)

        if not active_events:
            text = (
                "🎉 *Отличные новости!* В ближайшие **3 недели** горящих дедлайнов нет.\n\n"
                "_Все остальные практические работы и тесты запланированы позже._"
            )
            kb = {"inline_keyboard": [[{"text": "✅ Посмотреть сданные работы", "callback_data": "show_completed"}]]}
            await tg_request("sendMessage", {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "reply_markup": kb
            })
            return

        # Формируем сообщение
        msg_parts = []
        inline_keyboard = []
        num_counter = 1
        num_to_event = {}

        # 1. Секция НОВЫХ заданий
        if new_events:
            msg_parts.append(f"🆕 *НОВЫЕ ДЕДЛАЙНЫ (добавлены недавно)* — {len(new_events)}:")
            new_cards = []
            for ev in new_events:
                new_cards.append(format_deadline_card(ev, num_counter))
                num_to_event[num_counter] = ev
                num_counter += 1
            msg_parts.append("\n────────────────────\n".join(new_cards))

        # 2. Секция активных дедлайнов (сдача)
        if closing_events:
            header = "📋 *Ближайшие дедлайны к сдаче (до 3 недель)*:" if not new_events else "📋 *Остальные дедлайны (до 3 недель)*:"
            msg_parts.append(f"{header} ({len(closing_events)})")
            close_cards = []
            for ev in closing_events:
                close_cards.append(format_deadline_card(ev, num_counter))
                num_to_event[num_counter] = ev
                num_counter += 1
            msg_parts.append("\n────────────────────\n".join(close_cards))

        # 3. Секция открытий тестов
        if opening_events:
            msg_parts.append(f"🔓 *Открытия тестов / заданий (в течение 3 нед.)* — {len(opening_events)}:")
            open_cards = []
            for ev in opening_events:
                open_cards.append(format_deadline_card(ev, num_counter))
                num_to_event[num_counter] = ev
                num_counter += 1
            msg_parts.append("\n────────────────────\n".join(open_cards))

        # Создаем интерактивные кнопки закрытия работ (по 3-4 кнопки в ряд)
        complete_buttons = []
        row = []
        for n, ev in num_to_event.items():
            row.append({"text": f"✅ Сдал #{n}", "callback_data": f"done_{ev.event_id}"})
            if len(row) == 3:
                complete_buttons.append(row)
                row = []
        if row:
            complete_buttons.append(row)

        # Дополнительные кнопки меню
        complete_buttons.append([
            {"text": "✅ Посмотреть сданные", "callback_data": "show_completed"},
            {"text": "🔄 Обновить", "callback_data": "refresh_deadlines"}
        ])

        final_text = "\n\n".join(msg_parts)
        await tg_request("sendMessage", {
            "chat_id": chat_id,
            "text": final_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": complete_buttons}
        })

        # Помечаем показанные новые задания как "просмотренные" (is_new_unseen = False)
        if new_events:
            for ev in new_events:
                ev_key = str(ev.event_id)
                if ev_key in events_dict:
                    events_dict[ev_key]["is_new_unseen"] = False
            state_data["events"] = events_dict
            save_events_state(state_data)

    except Exception as e:
        logger.error(f"Ошибка в handle_deadlines: {e}", exc_info=True)
        await tg_request("sendMessage", {
            "chat_id": chat_id,
            "text": f"❌ Не удалось получить дедлайны: {e}\n\nПроверьте `CALENDAR_URL` в `.env`."
        })


async def handle_completed_command(chat_id: str):
    """Показывает список сданных / закрытых работ"""
    state_data = load_events_state()
    events_dict = state_data.get("events", {})

    completed_list = []
    for ev_id, ev_data in events_dict.items():
        if ev_data.get("is_completed", False):
            completed_list.append((ev_id, ev_data))

    if not completed_list:
        await tg_request("sendMessage", {
            "chat_id": chat_id,
            "text": "📭 *Список сданных работ пуст.*\n\nКогда вы сдадите работу, нажмите кнопку *«✅ Сдал #N»* в списке дедлайнов.",
            "parse_mode": "Markdown"
        })
        return

    # Сортируем
    completed_list.sort(key=lambda x: x[1].get("completed_at", 0), reverse=True)

    text_parts = [f"✅ *Сданные и закрытые работы* (всего: {len(completed_list)}):\n"]
    buttons = []
    row = []

    for idx, (ev_id, ev_data) in enumerate(completed_list[:15], 1):
        name = ev_data.get("name", "Без названия")
        course = ev_data.get("course", "СДО")
        text_parts.append(f"{idx}. <s>{name}</s>\n   📚 _{course}_\n")
        row.append({"text": f"↩️ Вернуть #{idx}", "callback_data": f"undone_{ev_id}"})
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([{"text": "📋 Вернуться к дедлайнам", "callback_data": "show_deadlines"}])

    await tg_request("sendMessage", {
        "chat_id": chat_id,
        "text": "\n".join(text_parts),
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": buttons}
    })


async def handle_mark_done(chat_id: str, callback_id: str, event_id: str):
    """Отмечает работу как сданную"""
    state_data = load_events_state()
    events_dict = state_data.get("events", {})

    if event_id in events_dict:
        events_dict[event_id]["is_completed"] = True
        events_dict[event_id]["completed_at"] = int(time.time())
        events_dict[event_id]["is_new_unseen"] = False
        state_data["events"] = events_dict
        save_events_state(state_data)
        
        name = events_dict[event_id].get("name", "Задание")
        await tg_request("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": f"✅ Задание «{name[:30]}...» перенесено в сданные!"
        })
        # Обновляем список дедлайнов
        await handle_deadlines_command(chat_id)
    else:
        # Если события еще не было в базе, создаем его
        events_dict[event_id] = {
            "is_completed": True,
            "completed_at": int(time.time()),
            "is_new_unseen": False
        }
        state_data["events"] = events_dict
        save_events_state(state_data)
        await tg_request("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "✅ Задание отмечено как выполненное!"
        })
        await handle_deadlines_command(chat_id)


async def handle_mark_undone(chat_id: str, callback_id: str, event_id: str):
    """Возвращает работу из сданных обратно в активные"""
    state_data = load_events_state()
    events_dict = state_data.get("events", {})

    if event_id in events_dict:
        events_dict[event_id]["is_completed"] = False
        events_dict[event_id]["completed_at"] = None
        state_data["events"] = events_dict
        save_events_state(state_data)

        await tg_request("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "↩️ Задание возвращено в активный список дедлайнов!"
        })
        await handle_completed_command(chat_id)


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
            state_data = load_events_state()
            events_dict = state_data.get("events", {})

            now = datetime.now(MSK_TZ)
            now_ts = int(now.timestamp())

            for event in events:
                event_key = str(event.event_id)
                event_state = events_dict.get(event_key)

                # 1. ОБНАРУЖЕНИЕ НОВОГО СОБЫТИЯ
                if not event_state:
                    event_state = {
                        "name": event.name,
                        "course": event.course_name,
                        "due_timestamp": event.due_timestamp,
                        "is_opening": event.is_opening,
                        "is_completed": False,
                        "completed_at": None,
                        "is_new_unseen": True,  # Новое задание!
                        "opened_alert_sent": False,
                        "alerts_sent": ["discovered"]
                    }

                    hours_diff = (event.due_timestamp - now_ts) / 3600.0
                    # Если до дедлайна более 12 часов и он в пределах 3 недель
                    if hours_diff > 12:
                        title = "🔓 *Запланировано открытие теста / задания:*" if event.is_opening else "🆕 *В СДО МИРЭА добавлено новое задание:*"
                        card = format_deadline_card(event)
                        kb = {"inline_keyboard": [[{"text": "✅ Отметить сданным", "callback_data": f"done_{event.event_id}"}]]}
                        await tg_request("sendMessage", {
                            "chat_id": TELEGRAM_CHAT_ID,
                            "text": f"{title}\n\n{card}",
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": True,
                            "reply_markup": kb
                        })

                    events_dict[event_key] = event_state
                    continue

                # Если работа уже закрыта пользователем — не спамим напоминаниями
                if event_state.get("is_completed", False):
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
                    kb = {"inline_keyboard": [[{"text": "✅ Отметить сданным", "callback_data": f"done_{event.event_id}"}]]}
                    await tg_request("sendMessage", {
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                        "reply_markup": kb
                    })

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
                            kb = {"inline_keyboard": [[{"text": "✅ Отметить выполненным", "callback_data": f"done_{event.event_id}"}]]}
                            await tg_request("sendMessage", {
                                "chat_id": TELEGRAM_CHAT_ID,
                                "text": text,
                                "parse_mode": "Markdown",
                                "disable_web_page_preview": True,
                                "reply_markup": kb
                            })
                            event_state["opened_alert_sent"] = True

                # 4. НАПОМИНАНИЯ О ДЕДЛАЙНЕ (24 ЧАСА И 3 ЧАСА)
                if not event.is_opening:
                    hours_left = (event.due_timestamp - now_ts) / 3600.0
                    alerts_sent = event_state.get("alerts_sent", [])

                    if 0 < hours_left <= 24 and "24h" not in alerts_sent:
                        card = format_deadline_card(event)
                        kb = {"inline_keyboard": [[{"text": "✅ Отметить сданным", "callback_data": f"done_{event.event_id}"}]]}
                        await tg_request("sendMessage", {
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
                        await tg_request("sendMessage", {
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
    logger.info("Long polling Telegram запущен...")
    offset = 0

    async with httpx.AsyncClient(timeout=35.0) as client:
        while True:
            try:
                params = {"offset": offset, "timeout": 20}
                resp = await client.get(f"{TG_API_URL}/getUpdates", params=params)
                
                if resp.status_code != 200:
                    await asyncio.sleep(2)
                    continue

                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(2)
                    continue

                updates = data.get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1

                    # 1. Текстовые сообщения и кнопки клавиатуры
                    if "message" in update:
                        msg = update["message"]
                        chat_id = str(msg["chat"]["id"])
                        text = (msg.get("text") or "").strip()

                        if text in ["/start", "/menu", "📱 Главное меню"]:
                            await handle_start_or_menu(chat_id)
                        elif text in ["/deadlines", "📋 Дедлайны (до 3 нед.)"]:
                            await handle_deadlines_command(chat_id)
                        elif text in ["/completed", "/completed_deadlines", "✅ Сданные работы"]:
                            await handle_completed_command(chat_id)
                        elif text in ["/check", "🔄 Обновить СДО"]:
                            await handle_deadlines_command(chat_id)

                    # 2. Интерактивные нажатия Inline-кнопок
                    elif "callback_query" in update:
                        cb = update["callback_query"]
                        cb_id = cb["id"]
                        chat_id = str(cb["message"]["chat"]["id"])
                        cb_data = cb.get("data", "")

                        if cb_data in ["show_deadlines", "refresh_deadlines"]:
                            await tg_request("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Загружаю дедлайны..."})
                            await handle_deadlines_command(chat_id)
                        elif cb_data == "show_completed":
                            await tg_request("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Загружаю сданные работы..."})
                            await handle_completed_command(chat_id)
                        elif cb_data.startswith("done_"):
                            ev_id = cb_data.replace("done_", "")
                            await handle_mark_done(chat_id, cb_id, ev_id)
                        elif cb_data.startswith("undone_"):
                            ev_id = cb_data.replace("undone_", "")
                            await handle_mark_undone(chat_id, cb_id, ev_id)
                        else:
                            await tg_request("answerCallbackQuery", {"callback_query_id": cb_id})

            except httpx.TimeoutException:
                pass
            except Exception as e:
                logger.error(f"Ошибка в polling: {e}")
                await asyncio.sleep(2)


async def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ОШИБКА: TELEGRAM_BOT_TOKEN не задан в .env!")
        return

    # Регистрируем меню команд в Telegram
    try:
        await set_bot_menu_commands()
    except Exception as e:
        logger.warning(f"Не удалось установить команды меню: {e}")

    logger.info("Бот успешно запущен и отслеживает дедлайны...")
    
    await asyncio.gather(
        background_monitoring_task(),
        poll_telegram_updates()
    )


if __name__ == "__main__":
    asyncio.run(main())
