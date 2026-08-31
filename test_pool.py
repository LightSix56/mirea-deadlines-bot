import socket, asyncio, httpx, os, time
from dotenv import load_dotenv
load_dotenv("D:\\mirea_deadlines_bot\\.env")

orig_getaddrinfo = socket.getaddrinfo
def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        res = orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        if res:
            return res
    except Exception:
        pass
    return orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = ipv4_getaddrinfo

token = os.getenv("TELEGRAM_BOT_TOKEN")
chat_id = os.getenv("TELEGRAM_CHAT_ID")
url = f"https://api.telegram.org/bot{token}"

API_CLIENT = None
def get_api_client():
    global API_CLIENT
    if API_CLIENT is None or API_CLIENT.is_closed:
        API_CLIENT = httpx.AsyncClient(timeout=15.0)
    return API_CLIENT

async def tg_call(method, payload=None):
    client = get_api_client()
    t0 = time.time()
    resp = await client.post(f"{url}/{method}", json=payload or {})
    dur = time.time() - t0
    print(f"[{method}] {resp.status_code} in {dur*1000:.1f} ms, ok={resp.json().get('ok')}")

async def test_concurrent():
    print("Testing 5 concurrent Telegram API calls with connection reuse...")
    tasks = [
        tg_call("getMe"),
        tg_call("getChat", {"chat_id": chat_id}),
        tg_call("getMe"),
        tg_call("getChat", {"chat_id": chat_id}),
        tg_call("getMe")
    ]
    await asyncio.gather(*tasks)

asyncio.run(test_concurrent())
