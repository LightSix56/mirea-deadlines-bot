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
url = f"https://api.telegram.org/bot{token}/getMe"

async def test_robust():
    limits = httpx.Limits(max_keepalive_connections=10, max_connections=20, keepalive_expiry=30.0)
    async with httpx.AsyncClient(limits=limits, timeout=10.0, http2=False) as client:
        t0 = time.time()
        resp = await client.get(url)
        print(f"Time: {time.time() - t0:.3f} s | Status: {resp.status_code} | OK: {resp.json().get('ok')}")

asyncio.run(test_robust())
