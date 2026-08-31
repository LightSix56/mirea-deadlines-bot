import asyncio, httpx, os, time
from dotenv import load_dotenv
load_dotenv("D:\\mirea_deadlines_bot\\.env")

token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")
url = f"https://api.telegram.org/bot{token}"

async def test():
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
        t0 = time.time()
        resp = await client.post(f"{url}/getChat", json={"chat_id": chat_id})
        print(f"Transport local_address 0.0.0.0 time: {time.time() - t0:.3f} s, ok: {resp.json().get('ok')}")

asyncio.run(test())
