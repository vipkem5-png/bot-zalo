# zalo_key_bot.py  —  pip install requests
import os, re, time, threading, logging
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("keybot")

# ===== CẤU HÌNH (đặt bằng biến môi trường, đừng hardcode) =====
ZALO_TOKEN    = os.environ["ZALO_BOT_TOKEN"]
ZALO_API_BASE = os.getenv("ZALO_API_BASE", "https://bot-api.zaloplatforms.com")  # kiểm tra lại theo docs Zalo
SHOP_URL      = os.getenv("SHOP_URL", "https://vohoainam.net").rstrip("/")      # KHÔNG có /ctv ở cuối
SHOP_USER     = os.environ["SHOP_USER"]
SHOP_PASS     = os.environ["SHOP_PASS"]
LOGIN_PATH    = os.getenv("SHOP_LOGIN_PATH", "/api/ctv/login")
ME_PATH       = os.getenv("SHOP_ME_PATH", "/api/ctv/me")
CTV_PATH      = os.getenv("SHOP_CTV_PATH", "/ctv")
BUY_PATH      = os.getenv("SHOP_BUY_PATH", "")          # điền sau khi .scan tìm ra
ALLOWED_IDS   = set(filter(None, os.getenv("ALLOWED_IDS", "").split(",")))
MAX_QTY       = int(os.getenv("MAX_QTY", "50"))
KEY_PREFIX    = os.getenv("KEY_PREFIX", "THIEN")
MAX_DEVICES   = 1

# Lệnh -> mã chu kỳ trên web (sửa 'code' theo giá trị web thực sự gửi)
PLANS = {
    "10p": {"code": "10m", "label": "10 Phút"},
    "1h":  {"code": "1h",  "label": "1 Giờ"},
    "1d":  {"code": "1d",  "label": "1 Ngày"},
    "7d":  {"code": "7d",  "label": "7 Ngày"},
    "30d": {"code": "30d", "label": "30 Ngày"},
}
CMD_RE = re.compile(r"^\.ikey(10p|1h|1d|7d|30d)\s+(\d+)\s*$", re.I)


# ===== ĐỌC HTML (form, input, nút bấm, script) =====
class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.cur = [], None
        self.inputs, self.onclicks = [], []
        self.scripts, self.script_src = [], []
        self._in_script, self._buf = False, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.cur = {"action": a.get("action", ""), "method": (a.get("method") or "get").lower(), "inputs": []}
            self.forms.append(self.cur)
        elif tag in ("input", "select", "textarea"):
            i = {"tag": tag, "name": a.get("name") or a.get("id") or "",
                 "id": a.get("id", ""), "type": (a.get("type") or "text").lower(), "value": a.get("value", "")}
            self.inputs.append(i)
            if self.cur is not None:
                self.cur["inputs"].append(i)
        elif tag == "script":
            if a.get("src"):
                self.script_src.append(a["src"])
            else:
                self._in_script, self._buf = True, []
        if a.get("onclick"):
            self.onclicks.append(f"<{tag}> onclick={a['onclick'][:120]}")

    def handle_endtag(self, tag):
        if tag == "form":
            self.cur = None
        if tag == "script" and self._in_script:
            self.scripts.append("".join(self._buf))
            self._in_script = False

    def handle_data(self, d):
        if self._in_script:
            self._buf.append(d)


# ===== PHẦN MUA KEY TRÊN WEB =====
class ShopClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0"
        self.token = None            # đánh dấu đã đăng nhập

    def login(self):
        """Đăng nhập qua API JSON của web: POST /api/ctv/login -> token."""
        r = self.s.post(urljoin(SHOP_URL, LOGIN_PATH),
                        json={"username": SHOP_USER, "password": SHOP_PASS}, timeout=20)
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code >= 400 or not data.get("success") or not data.get("token"):
            raise RuntimeError(f"Đăng nhập thất bại (HTTP {r.status_code}): {data.get('message', 'sai tài khoản/mật khẩu?')}")
        self.s.headers["Authorization"] = f"Bearer {data['token']}"
        chk = self.s.get(urljoin(SHOP_URL, ME_PATH), timeout=20)
        if chk.status_code != 200:
            raise RuntimeError(f"Có token nhưng /me trả HTTP {chk.status_code}. Có thể web dùng kiểu header khác Bearer.")
        self.token = data["token"]
        self.s.cookies.set("ctv_token", data["token"], domain=urlparse(SHOP_URL).hostname, path="/")

    def buy(self, plan_code: str, qty: int) -> list[str]:
        if not BUY_PATH:
            raise RuntimeError("Chưa cấu hình SHOP_BUY_PATH. Gõ .scan để dò đường dẫn mua key.")
        # TODO: sửa tên trường theo kết quả .scan
        for attempt in (1, 2):
            r = self.s.post(urljoin(SHOP_URL, BUY_PATH), json={
                "duration": plan_code, "quantity": qty,
                "max_devices": MAX_DEVICES, "prefix": KEY_PREFIX}, timeout=60)
            if r.status_code in (401, 403) and attempt == 1:
                self.login(); continue
            r.raise_for_status()
            data = r.json()
            if not data.get("success", True):
                raise RuntimeError(data.get("message", "Mua key thất bại"))
            keys = data.get("keys", [])
            if len(keys) != qty:
                raise RuntimeError(f"Web trả {len(keys)} key, yêu cầu {qty}. Kiểm tra Lịch sử.")
            return keys


# ===== LỆNH .scan: dò form + đường dẫn API trong trang web =====
PATH_RE = re.compile(r"""["'`](/[A-Za-z0-9_\-./]*(?:api|login|auth|buy|key|ctv|order|purchase)[A-Za-z0-9_\-./?=&]*)["'`]""", re.I)

def describe(page: Page, title: str) -> list[str]:
    out = [f"=== {title} ==="]
    for f in page.forms:
        names = ",".join(f"{i['name']}({i['type']})" for i in f["inputs"] if i["name"])
        out.append(f"FORM {f['method'].upper()} action={f['action'] or '(trống)'} : {names}")
    if not page.forms and page.inputs:
        out.append("Ô nhập (ngoài form): " + ",".join(f"{i['name']}({i['type']})" for i in page.inputs if i["name"]))
    out.extend(page.onclicks[:12])
    return out

COOKIE_GUESS = ["token", "ctv_token", "ctvToken", "auth_token", "authToken", "access_token", "jwt", "session", "ctv_session"]

def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s)

def page_scripts(c, url):
    r = c.s.get(url, timeout=20)
    p = Page(); p.feed(r.text)
    texts = list(p.scripts)
    host = urlparse(SHOP_URL).netloc
    for src in p.script_src[:10]:
        u = urljoin(r.url, src)
        if urlparse(u).netloc == host:
            try:
                texts.append(c.s.get(u, timeout=20).text)
            except Exception:
                pass
    return r, p, texts

def scan_site() -> str:
    out = []
    c = ShopClient()
    host = urlparse(SHOP_URL).hostname
    ctv_url = urljoin(SHOP_URL, CTV_PATH)

    # 1) JS xử lý đăng nhập (xem token được lưu ở đâu, chuyển trang thế nào)
    _, _, texts = page_scripts(c, urljoin(SHOP_URL, "/login"))
    for t in texts:
        i = t.find("/api/ctv/login")
        if i >= 0:
            out += ["=== JS XỬ LÝ ĐĂNG NHẬP ===", collapse(t[max(0, i - 50): i + 1500])]
            break

    # 2) đăng nhập bằng API
    try:
        c.login()
        out.append("Đăng nhập: OK")
    except Exception as e:
        return "\n".join(out + [f"Đăng nhập: LỖI {e}"])
    token = c.token
    out.append("Cookie web trả về khi login: " + (",".join(c.s.cookies.keys()) or "(không có)"))

    # 3) /ctv có thể chuyển về /login nếu thiếu cookie -> thử đoán tên cookie
    found = None
    r = c.s.get(ctv_url, timeout=20)
    if "/login" in urlparse(r.url).path:
        for name in COOKIE_GUESS:
            c.s.cookies.set(name, token, domain=host)
            r2 = c.s.get(ctv_url, timeout=20)
            if "/login" not in urlparse(r2.url).path:
                found = name
                break
            try:
                c.s.cookies.clear(domain=host, path="/", name=name)
            except Exception:
                pass
        out.append(f"/ctv chuyển về /login khi chưa có cookie. Cookie được chấp nhận: {found or 'KHÔNG TÌM RA'}")
    else:
        out.append("/ctv mở được ngay sau đăng nhập (không cần cookie)")

    # 4) đọc trang /ctv (trang đã đăng nhập nếu tìm ra)
    r, p, texts = page_scripts(c, ctv_url)
    out += [f"/ctv -> HTTP {r.status_code}, url cuối: {urlparse(r.url).path}"] + describe(p, "Trang mua key")
    urls, snippets = [], []
    for t in texts:
        for u in re.findall(r"fetch\(\s*[`'\"]([^`'\"]+)", t):
            if u not in urls:
                urls.append(u)
        for m in re.finditer(r"fetch\(|axios\.|\$\.post\(|XMLHttpRequest", t):
            seg = collapse(t[m.start(): m.start() + 700])
            if re.search(r"buy|mua|order|purchase|key", seg, re.I) and "/login" not in seg[:60] and seg not in snippets:
                snippets.append(seg)
    out.append("=== MỌI LỜI GỌI fetch ===")
    out += urls[:40] or ["(không thấy)"]
    out.append("=== ĐOẠN GỌI LIÊN QUAN MUA KEY ===")
    out += snippets[:6] or ["(không thấy)"]
    return "\n".join(out)


def src_dump() -> str:
    c = ShopClient()
    c.login()
    r, p, texts = page_scripts(c, urljoin(SHOP_URL, CTV_PATH))
    allt = "\n".join(texts)
    out = [f"/ctv -> HTTP {r.status_code}, url cuối: {urlparse(r.url).path}",
           f"số script: {len(texts)}, tổng {len(allt)} ký tự"]
    apis = sorted(set(re.findall(r"/api/[A-Za-z0-9_\-/.]+", allt)))
    out += ["=== MỌI ĐƯỜNG DẪN /api/ ==="] + (apis[:40] or ["(không thấy)"])
    shown, last = 0, -10000
    for m in re.finditer(r"keyCount", allt):
        if m.start() - last < 1500:
            continue
        last = m.start()
        out += ["=== ĐOẠN QUANH keyCount ===", collapse(allt[max(0, m.start() - 300): m.start() + 1200])]
        shown += 1
        if shown >= 2:
            break
    return "\n".join(out)


# ===== ZALO =====
def zalo(method: str, payload: dict | None = None, timeout=40):
    r = requests.post(f"{ZALO_API_BASE}/bot{ZALO_TOKEN}/{method}", json=payload or {}, timeout=timeout)
    return r.json()

def send(chat_id: str, text: str):
    for i in range(0, len(text), 1800):
        zalo("sendMessage", {"chat_id": chat_id, "text": text[i:i+1800]})

def extract_updates(resp) -> list[dict]:
    res = resp.get("result") if isinstance(resp, dict) else None
    if not res:
        return []
    return res if isinstance(res, list) else [res]


# ===== XỬ LÝ LỆNH =====
shop = ShopClient()
buy_lock = threading.Lock()

HELP = ("🤖 Lệnh mua key:\n"
        ".ikey10p <sl> | .ikey1h <sl> | .ikey1d <sl> | .ikey7d <sl> | .ikey30d <sl>\n"
        "Ví dụ: .ikey1h 1\n"
        ".id → xem ID Zalo của bạn\n"
        ".scan → dò form/API của web (chỉ chủ bot)\n"
        ".src → đọc mã trang mua key (chỉ chủ bot)")

def handle(chat_id: str, user_id: str, text: str):
    text = (text or "").strip()
    if text.lower() == ".id":
        return send(chat_id, f"ID của bạn: {user_id}")
    if text.lower() == ".scan":
        if not ALLOWED_IDS or user_id not in ALLOWED_IDS:
            return send(chat_id, "⛔ Cần đặt ALLOWED_IDS trước.")
        send(chat_id, "🔎 Đang đăng nhập và dò web...")
        try:
            return send(chat_id, scan_site())
        except Exception as e:
            return send(chat_id, f"❌ Lỗi scan: {e}")
    if text.lower() == ".src":
        if not ALLOWED_IDS or user_id not in ALLOWED_IDS:
            return send(chat_id, "⛔ Cần đặt ALLOWED_IDS trước.")
        send(chat_id, "🔎 Đang đọc mã trang mua key...")
        try:
            return send(chat_id, src_dump())
        except Exception as e:
            return send(chat_id, f"❌ Lỗi src: {e}")
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
    with buy_lock:
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
