import sys
import os
import socket
import asyncio
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import aiohttp
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession

load_dotenv("D:\\mirea_deadlines_bot\\.env")

class IPv4Session(AiohttpSession):
    async def create_session(self) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(family=socket.AF_INET, happy_eyeballs_delay=None)
        return aiohttp.ClientSession(connector=connector)

async def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    print(f"Connecting to bot with chat_id={chat_id}...")
    
    session = IPv4Session()
    bot = Bot(token=token, session=session)
    
    me = await bot.get_me()
    print(f"Bot info: @{me.username} ({me.first_name})")
    
    msg = await bot.send_message(
        chat_id=chat_id,
        text="?? *???????? ???????? ????? ?????? ???????!*\n\n??? ????? ??? Chat ID ? ????????? ? Telegram.",
        parse_mode=ParseMode.MARKDOWN
    )
    print(f"Message sent! Message ID: {msg.message_id}")
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
