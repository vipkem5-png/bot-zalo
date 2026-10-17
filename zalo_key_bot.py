# zalo_key_bot.py  —  pip install requests
import os, re, time, threading, logging
import requests
from urllib.parse import urljoin, urlparse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("keybot")

# ===== CẤU HÌNH (đặt bằng biến môi trường, đừng hardcode) =====
ZALO_TOKEN    = os.environ["ZALO_BOT_TOKEN"]
ZALO_API_BASE = os.getenv("ZALO_API_BASE", "https://bot-api.zaloplatforms.com")  # kiểm tra lại theo docs Zalo
SHOP_URL      = os.getenv("SHOP_URL", "https://vohoainam.net")
SHOP_USER     = os.environ["SHOP_USER"]
SHOP_PASS     = os.environ["SHOP_PASS"]
ALLOWED_IDS   = set(filter(None, os.getenv("ALLOWED_IDS", "").split(",")))  # ID Zalo được phép mua
MAX_QTY       = int(os.getenv("MAX_QTY", "50"))
KEY_PREFIX    = os.getenv("KEY_PREFIX", "THIENDINH")
MAX_DEVICES   = 1
LOGIN_PATH    = os.getenv("SHOP_LOGIN_PATH", "/login")     # đường dẫn API đăng nhập của web
BUY_PATH      = os.getenv("SHOP_BUY_PATH", "/ctv")     # đường dẫn API mua key của web

# Lệnh -> mã chu kỳ trên web (sửa 'code' theo giá trị web thực sự gửi)
PLANS = {
    "10p": {"code": "10m", "label": "10 Phút"},
    "1h":  {"code": "1h",  "label": "1 Giờ"},
    "1d":  {"code": "1d",  "label": "1 Ngày"},
    "7d":  {"code": "7d",  "label": "7 Ngày"},
    "30d": {"code": "30d", "label": "30 Ngày"},
}
CMD_RE = re.compile(r"^\.ikey(10p|1h|1d|7d|30d)\s+(\d+)\s*$", re.I)


# ===== PHẦN MUA KEY TRÊN WEB (CẦN ĐIỀN ENDPOINT THẬT) =====
class ShopClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0"
        self.token = None

    def login(self):
        # TODO: thay bằng request đăng nhập thật của web
        r = self.s.post(f"{SHOP_URL}{LOGIN_PATH}",
                        json={"username": SHOP_USER, "password": SHOP_PASS}, timeout=20)
        r.raise_for_status()
        self.token = r.json().get("token")
        if self.token:
            self.s.headers["Authorization"] = f"Bearer {self.token}"

    def buy(self, plan_code: str, qty: int) -> list[str]:
        # TODO: thay bằng request mua key thật; trả về danh sách key
        for attempt in (1, 2):               # hết phiên -> đăng nhập lại 1 lần
            r = self.s.post(f"{SHOP_URL}{BUY_PATH}", json={
                "duration": plan_code, "quantity": qty,
                "max_devices": MAX_DEVICES, "prefix": KEY_PREFIX}, timeout=60)
            if r.status_code in (401, 403) and attempt == 1:
                self.login(); continue
            r.raise_for_status()
            data = r.json()
            if not data.get("success", True):
                raise RuntimeError(data.get("message", "Mua key thất bại"))
            keys = data.get("keys", [])
            if len(keys) != qty:             # đảm bảo trả đúng số lượng
                raise RuntimeError(f"Web trả {len(keys)} key, yêu cầu {qty}. Kiểm tra Lịch sử.")
            return keys


# ===== ZALO =====
def zalo(method: str, payload: dict | None = None, timeout=40):
    r = requests.post(f"{ZALO_API_BASE}/bot{ZALO_TOKEN}/{method}", json=payload or {}, timeout=timeout)
    return r.json()

def send(chat_id: str, text: str):
    for i in range(0, len(text), 1800):      # Zalo giới hạn độ dài tin
        zalo("sendMessage", {"chat_id": chat_id, "text": text[i:i+1800]})

def extract_updates(resp) -> list[dict]:
    res = resp.get("result") if isinstance(resp, dict) else None
    if not res:
        return []
    return res if isinstance(res, list) else [res]



# ===== LỆNH .scan: dò đường dẫn API trong code giao diện của web =====
PATH_RE = re.compile(r"""["'`](/[A-Za-z0-9_\-./]*(?:api|login|auth|buy|key|ctv|order|purchase)[A-Za-z0-9_\-./?=&]*)["'`]""", re.I)

def scan_site() -> str:
    hdr = {"User-Agent": "Mozilla/5.0"}
    host = urlparse(SHOP_URL).netloc
    out, scripts, paths, snippets = [], [], set(), []
    texts = []
    for url in (f"{SHOP_URL}/login", f"{SHOP_URL}/ctv"):
        try:
            r = requests.get(url, timeout=20, headers=hdr)
        except Exception as e:
            out.append(f"{url} -> lỗi {e}"); continue
        out.append(f"{url} -> HTTP {r.status_code}")
        html = r.text
        for f in re.findall(r"<form[^>]*>", html, re.I):
            out.append("FORM: " + f[:200])
        for n in re.findall(r"<input[^>]*name=[\"']([^\"']+)", html, re.I):
            out.append("INPUT: " + n)
        for src in re.findall(r"<script[^>]+src=[\"']([^\"']+)", html, re.I):
            u = urljoin(url, src)
            if urlparse(u).netloc == host and u not in scripts:
                scripts.append(u)
        texts.append(html)
    for u in scripts[:10]:
        try:
            texts.append(requests.get(u, timeout=20, headers=hdr).text)
        except Exception:
            pass
    for t in texts:
        paths.update(PATH_RE.findall(t))
        for m in re.finditer(r"fetch\(|axios\.|\$\.post\(|\$\.ajax\(", t):
            seg = t[m.start(): m.start() + 260].replace("\n", " ")
            if re.search(r"login|buy|key|order|purchase", seg, re.I) and seg not in snippets:
                snippets.append(seg)
    out.append("--- ĐƯỜNG DẪN TÌM THẤY ---")
    out.extend(sorted(paths)[:40] or ["(không thấy)"])
    out.append("--- ĐOẠN GỌI API ---")
    out.extend(snippets[:12] or ["(không thấy)"])
    return "\n".join(out)

# ===== XỬ LÝ LỆNH =====
shop = ShopClient()
buy_lock = threading.Lock()

HELP = ("🤖 Lệnh mua key:\n"
        ".ikey10p <sl> | .ikey1h <sl> | .ikey1d <sl> | .ikey7d <sl> | .ikey30d <sl>\n"
        "Ví dụ: .ikey1h 1\n"
        ".id  → xem ID Zalo của bạn")

def handle(chat_id: str, user_id: str, text: str):
    text = (text or "").strip()
    if text.lower() == ".id":
        return send(chat_id, f"ID của bạn: {user_id}")
    if text.lower() == ".scan":
        if not ALLOWED_IDS or user_id not in ALLOWED_IDS:
            return send(chat_id, "⛔ Cần đặt ALLOWED_IDS trước.")
        send(chat_id, "🔎 Đang dò web...")
        try:
            return send(chat_id, scan_site())
        except Exception as e:
            return send(chat_id, f"❌ Lỗi scan: {e}")
    if text.lower() in (".help", "/start", "help"):
        return send(chat_id, HELP)
    m = CMD_RE.match(text)
    if not m:
        return
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        return send(chat_id, "⛔ Bạn không có quyền dùng lệnh này.")
    plan_key, qty = m.group(1).lower(), int(m.group(2))
    if not 1 <= qty <= MAX_QTY:
        return send(chat_id, f"⚠️ Số lượng phải từ 1 đến {MAX_QTY}.")
    plan = PLANS[plan_key]
    send(chat_id, f"⏳ Đang mua {qty} key {plan['label']}...")
    with buy_lock:                           # mua tuần tự, tránh trừ tiền chồng
        try:
            if not shop.token:
                shop.login()
            keys = shop.buy(plan["code"], qty)
        except Exception as e:
            log.exception("buy failed")
            return send(chat_id, f"❌ Lỗi: {e}")
    send(chat_id, f"✅ {qty} key {plan['label']}:\n" + "\n".join(keys))
    log.info("user=%s bought %s x%s", user_id, plan_key, qty)

def main():
    log.info("Bot đang chạy...")
    while True:
        try:
            resp = zalo("getUpdates", {"timeout": 30}, timeout=45)
            for u in extract_updates(resp):
                msg = u.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                user_id = str((msg.get("from") or {}).get("id", ""))
                if chat_id and msg.get("text"):
                    threading.Thread(target=handle, args=(chat_id, user_id, msg["text"]), daemon=True).start()
        except Exception:
            log.exception("polling error")
            time.sleep(3)

if __name__ == "__main__":
    main()
