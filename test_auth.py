import sys
import os
import asyncio
from dotenv import load_dotenv
from moodle_client import MoodleClient

# Фикс кодировки консоли Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv()

async def test():
    calendar_url = os.getenv("CALENDAR_URL")
    token = os.getenv("MOODLE_TOKEN")

    if not calendar_url and not token:
        print("[!] Ошибка: В файле .env укажите CALENDAR_URL=... или MOODLE_TOKEN=...")
        return

    client = MoodleClient(calendar_url=calendar_url, token=token)

    try:
        if calendar_url:
            print("[+] Запрашиваю дедлайны по iCal ссылке...")
        else:
            print("[+] Запрашиваю дедлайны через REST API Moodle...")

        deadlines = await client.get_upcoming_deadlines()
        print(f"[OK] Успешно получено дедлайнов: {len(deadlines)}\n")

        if not deadlines:
            print("Ближайших дедлайнов в календаре нет.")
            return

        for idx, d in enumerate(deadlines, 1):
            print(f"{idx}. [{d.course_name}] {d.name}")
            print(f"   Срок: {d.formatted_date} ({d.time_left_str})")
            print(f"   Ссылка: {d.url}\n")

    except Exception as e:
        print(f"[ERR] Ошибка получения данных: {e}")

if __name__ == "__main__":
    asyncio.run(test())
