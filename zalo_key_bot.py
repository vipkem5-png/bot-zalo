# zalo_key_bot.py  —  pip install requests cryptography
import os, re, json, time, base64, hashlib, threading, logging
from collections import defaultdict
from urllib.parse import urljoin, urlparse
import requests
from cryptography.fernet import Fernet, InvalidToken

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("keybot")

# ===== CẤU HÌNH (đặt bằng biến môi trường, đừng hardcode) =====
ZALO_TOKEN    = os.environ["ZALO_BOT_TOKEN"]
ZALO_API_BASE = os.getenv("ZALO_API_BASE", "https://bot-api.zaloplatforms.com")
SHOP_URL      = os.getenv("SHOP_URL", "https://vohoainam.net").rstrip("/")
SHOP_USER     = os.environ["SHOP_USER"]            # tài khoản web của CHỦ BOT (admin)
SHOP_PASS     = os.environ["SHOP_PASS"]
KEY_PREFIX    = os.getenv("KEY_PREFIX", "THIEN")   # tiền tố của admin
LOGIN_PATH    = os.getenv("SHOP_LOGIN_PATH", "/api/ctv/login")
ME_PATH       = os.getenv("SHOP_ME_PATH", "/api/ctv/me")
BUY_PATH      = os.getenv("SHOP_BUY_PATH", "/api/ctv/buy-keys")
ADMIN_IDS     = set(filter(None, os.getenv("ALLOWED_IDS", "").split(",")))   # ID Zalo của admin
MAX_QTY       = int(os.getenv("MAX_QTY", "50"))
MAX_DEVICES   = 1
SECRET_KEY    = os.getenv("SECRET_KEY", "")        # chuỗi bí mật bất kỳ, dùng mã hóa mật khẩu khách
DATA_DIR      = os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") else "./data")

PLANS = {
    "10p": {"code": "10m", "label": "10 Phút"},
    "1h":  {"code": "1h",  "label": "1 Giờ"},
    "1d":  {"code": "1d",  "label": "1 Ngày"},
    "7d":  {"code": "7d",  "label": "7 Ngày"},
    "30d": {"code": "30d", "label": "30 Ngày"},
}
CMD_RE    = re.compile(r"^\.ikey(10p|1h|1d|7d|30d)\s+(\d+)\s*$", re.I)
ID_RE     = re.compile(r"^[A-Za-z0-9_\-]{6,64}$")
PREFIX_RE = re.compile(r"^[A-Z0-9]{2,12}$")


def money(x) -> str:
    try:
        return f"{int(x):,}".replace(",", ".") + "đ"
    except Exception:
        return str(x)


# ===== LƯU NGƯỜI DÙNG (mật khẩu được mã hóa) =====
class Store:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = os.path.join(DATA_DIR, "users.json")
        self.lock = threading.Lock()
        self.fernet = None
        if SECRET_KEY:
            self.fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))
        self.data = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Không đọc được users.json")
        log.info("Dữ liệu người dùng lưu ở %s (%d người)", self.path, len(self.data))

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def get(self, uid):
        return self.data.get(uid)

    def activate(self, uid):
        with self.lock:
            u = self.data.setdefault(uid, {})
            u["active"] = True
            u.setdefault("added", int(time.time()))
            self._save()

    def remove(self, uid) -> bool:
        with self.lock:
            existed = self.data.pop(uid, None) is not None
            self._save()
            return existed

    def set_creds(self, uid, username, password):
        if not self.fernet:
            raise RuntimeError("Admin chưa đặt SECRET_KEY nên chưa thể lưu tài khoản.")
        with self.lock:
            u = self.data[uid]
            u["username"] = username
            u["pw"] = self.fernet.encrypt(password.encode()).decode()
            self._save()

    def get_creds(self, uid):
        u = self.data.get(uid) or {}
        if not (self.fernet and u.get("username") and u.get("pw")):
            return None
        try:
            return u["username"], self.fernet.decrypt(u["pw"].encode()).decode()
        except InvalidToken:
            return None          # SECRET_KEY đã bị đổi

    def set_prefix(self, uid, prefix):
        with self.lock:
            self.data[uid]["prefix"] = prefix
            self._save()

    def clear_creds(self, uid):
        with self.lock:
            u = self.data.get(uid) or {}
            u.pop("username", None); u.pop("pw", None)
            self._save()

    def listing(self):
        return [(uid, u.get("active"), u.get("username"), u.get("prefix")) for uid, u in self.data.items()]


# ===== GỌI WEB BÁN KEY =====
class ShopClient:
    def __init__(self, username: str, password: str):
        self.username, self.password = username, password
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0"
        self.token = None

    def login(self):
        """POST /api/ctv/login -> token (giống trang đăng nhập của web)."""
        r = self.s.post(urljoin(SHOP_URL, LOGIN_PATH),
                        json={"username": self.username, "password": self.password}, timeout=20)
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code >= 400 or not data.get("success") or not data.get("token"):
            raise RuntimeError(data.get("error") or data.get("message") or f"Đăng nhập thất bại (HTTP {r.status_code})")
        self.s.headers["Authorization"] = f"Bearer {data['token']}"
        self.s.cookies.set("ctv_token", data["token"], domain=urlparse(SHOP_URL).hostname, path="/")
        self.token = data["token"]

    def me(self) -> dict:
        if not self.token:
            self.login()
        r = self.s.get(urljoin(SHOP_URL, ME_PATH), timeout=20)
        if r.status_code in (401, 403):
            self.login()
            r = self.s.get(urljoin(SHOP_URL, ME_PATH), timeout=20)
        r.raise_for_status()
        return r.json()

    def buy(self, plan_code: str, qty: int, prefix: str) -> dict:
        """Mua key qua POST /api/ctv/buy-keys (đúng như nút 'Mua Key Ngay' của web)."""
        m = re.match(r"^(\d+)([mhdMy])$", plan_code)
        if not m:
            raise RuntimeError(f"Mã chu kỳ không hợp lệ: {plan_code}")
        val, unit = int(m.group(1)), m.group(2)

        info = self.me()                                   # kiểm tra số dư trước khi mua
        price = (info.get("pricing") or {}).get(plan_code)
        balance = (info.get("user") or {}).get("balance")
        if price is not None and balance is not None and balance < price * qty:
            raise RuntimeError(f"Không đủ số dư: cần {money(price * qty)}, còn {money(balance)}")

        payload = {"preset": plan_code, "duration_val": val, "duration_unit": unit,
                   "max_devices": MAX_DEVICES, "count": qty, "prefix": prefix.upper()}
        r = None
        for attempt in (1, 2):
            try:
                r = self.s.post(urljoin(SHOP_URL, BUY_PATH), json=payload, timeout=60)
            except requests.RequestException as e:
                # KHÔNG tự thử lại: có thể web đã trừ tiền rồi
                raise RuntimeError(f"Mất kết nối khi mua ({e}). Kiểm tra mục Lịch sử trên web trước khi mua lại, có thể đã bị trừ tiền.")
            if r.status_code == 401 and attempt == 1:     # hết phiên, chưa xử lý gì -> đăng nhập lại
                self.login()
                continue
            break
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Web trả dữ liệu lạ (HTTP {r.status_code}). Kiểm tra mục Lịch sử trên web.")
        if not data.get("success"):
            raise RuntimeError(data.get("error") or f"Mua key thất bại (HTTP {r.status_code})")
        keys = data.get("keys") or []
        return {"keys": keys, "cost": data.get("total_cost"), "balance": data.get("new_balance"),
                "mismatch": len(keys) != qty}


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


# ===== NGƯỜI DÙNG & PHÂN QUYỀN =====
store = Store()
owner_client = ShopClient(SHOP_USER, SHOP_PASS)
clients: dict[str, ShopClient] = {}
user_locks = defaultdict(threading.Lock)

def resolve(uid: str, need_prefix: bool = True):
    """Trả về (client, prefix) của người dùng, hoặc ném lỗi có hướng dẫn."""
    if uid in ADMIN_IDS:
        return owner_client, KEY_PREFIX
    u = store.get(uid)
    if not u or not u.get("active"):
        raise RuntimeError("⛔ Bạn chưa được kích hoạt. Gõ .id rồi gửi ID cho admin để được kích hoạt.")
    creds = store.get_creds(uid)
    if not creds:
        raise RuntimeError("Bạn chưa đăng nhập web mua key. Gõ:\n.dangnhap <tài khoản> <mật khẩu>")
    prefix = u.get("prefix")
    if need_prefix and not prefix:
        raise RuntimeError("Bạn chưa đặt tiền tố key. Gõ:\n.tiento <TIỀN TỐ>  (2-12 ký tự chữ/số, ví dụ .tiento ABC)")
    c = clients.get(uid)
    if not c or (c.username, c.password) != creds:
        c = clients[uid] = ShopClient(*creds)
    return c, prefix

def help_text(uid: str) -> str:
    base = ("🤖 Lệnh:\n"
            ".id → xem ID Zalo của bạn (gửi cho admin để kích hoạt)\n"
            ".ikey10p|.ikey1h|.ikey1d|.ikey7d|.ikey30d <số lượng>  (ví dụ .ikey1h 1)\n"
            ".sodu → xem số dư tài khoản web của bạn")
    if uid not in ADMIN_IDS:
        base += ("\n\nLần đầu dùng (sau khi admin kích hoạt):\n"
                 ".dangnhap <tài khoản> <mật khẩu> → dùng tài khoản web CỦA BẠN\n"
                 ".tiento <TIỀN TỐ> → tiền tố cho key của bạn\n"
                 ".dangxuat → xóa tài khoản đã lưu")
    else:
        base += ("\n\nAdmin:\n"
                 ".kichhoat <id> → cho phép người dùng\n"
                 ".huy <id> → gỡ quyền và xóa dữ liệu của họ\n"
                 ".ds → danh sách người dùng")
    return base


# ===== XỬ LÝ LỆNH =====
def handle(chat_id: str, uid: str, text: str, chat_type: str = ""):
    text = (text or "").strip()
    parts = text.split()
    cmd = parts[0].lower() if parts else ""
    is_admin = uid in ADMIN_IDS

    if cmd == ".id":
        return send(chat_id, f"ID của bạn: {uid}\nGửi ID này cho admin để được kích hoạt.")
    if cmd in (".help", "/start", "help"):
        return send(chat_id, help_text(uid))

    # --- lệnh admin ---
    if cmd in (".kichhoat", ".huy", ".ds"):
        if not is_admin:
            return send(chat_id, "⛔ Chỉ admin dùng được lệnh này.")
        if cmd == ".ds":
            rows = store.listing()
            if not rows:
                return send(chat_id, "Chưa có người dùng nào.")
            return send(chat_id, "\n".join(
                f"{i} • {'đã kích hoạt' if a else 'chưa'} • tk: {n or '-'} • tiền tố: {p or '-'}" for i, a, n, p in rows))
        target = parts[1] if len(parts) > 1 else ""
        if not ID_RE.match(target):
            return send(chat_id, f"Cú pháp: {cmd} <id>")
        if cmd == ".kichhoat":
            store.activate(target)
            try:
                send(target, "✅ Admin đã kích hoạt cho bạn. Gõ .help để xem hướng dẫn, bắt đầu bằng:\n.dangnhap <tài khoản> <mật khẩu>")
            except Exception:
                pass
            return send(chat_id, f"✅ Đã kích hoạt {target}. Bảo họ gõ .dangnhap và .tiento.")
        existed = store.remove(target)
        clients.pop(target, None)
        return send(chat_id, f"🗑️ Đã gỡ {target}." if existed else "Không có người dùng này.")

    # --- lệnh của người dùng đã kích hoạt ---
    if cmd in (".dangnhap", ".tiento", ".dangxuat"):
        if is_admin:
            return send(chat_id, "Admin dùng tài khoản đặt trong biến môi trường, không cần lệnh này.")
        u = store.get(uid)
        if not u or not u.get("active"):
            return send(chat_id, "⛔ Bạn chưa được kích hoạt. Gõ .id rồi gửi ID cho admin.")
        if cmd == ".dangxuat":
            store.clear_creds(uid); clients.pop(uid, None)
            return send(chat_id, "✅ Đã xóa tài khoản web đã lưu.")
        if cmd == ".tiento":
            prefix = (parts[1] if len(parts) > 1 else "").upper()
            if not PREFIX_RE.match(prefix):
                return send(chat_id, "Cú pháp: .tiento <TIỀN TỐ>\nChỉ chữ và số, 2-12 ký tự. Ví dụ: .tiento ABC")
            store.set_prefix(uid, prefix)
            return send(chat_id, f"✅ Tiền tố key của bạn: {prefix}")
        # .dangnhap
        if "GROUP" in chat_type.upper():
            return send(chat_id, "⚠️ Hãy nhắn riêng cho bot để đăng nhập, đừng gửi mật khẩu trong nhóm.")
        sp = text.split(None, 2)
        if len(sp) < 3:
            return send(chat_id, "Cú pháp: .dangnhap <tài khoản> <mật khẩu>")
        username, password = sp[1], sp[2].strip()
        try:
            ShopClient(username, password).login()
            store.set_creds(uid, username, password)
        except Exception as e:
            return send(chat_id, f"❌ {e}")
        clients.pop(uid, None)
        return send(chat_id, f"✅ Đăng nhập OK với tài khoản {username}.\n"
                             "Nên xóa tin nhắn chứa mật khẩu khỏi khung chat.\n"
                             "Tiếp theo, nếu chưa đặt tiền tố: .tiento <TIỀN TỐ>")

    if cmd == ".sodu":
        try:
            client, _ = resolve(uid, need_prefix=False)
            u = client.me().get("user", {})
            return send(chat_id, f"💰 Số dư: {money(u.get('balance'))}\n🔑 Tổng key đã mua: {u.get('total_keys')}")
        except Exception as e:
            return send(chat_id, f"❌ {e}")

    # --- mua key ---
    m = CMD_RE.match(text)
    if not m:
        return
    plan_key, qty = m.group(1).lower(), int(m.group(2))
    if not 1 <= qty <= MAX_QTY:
        return send(chat_id, f"⚠️ Số lượng phải từ 1 đến {MAX_QTY}.")
    try:
        client, prefix = resolve(uid)
    except Exception as e:
        return send(chat_id, str(e))
    plan = PLANS[plan_key]
    send(chat_id, f"⏳ Đang mua {qty} key {plan['label']}...")
    with user_locks[uid]:                       # mỗi người mua tuần tự, tránh trừ tiền chồng
        try:
            res = client.buy(plan["code"], qty, prefix)
        except Exception as e:
            log.exception("buy failed")
            return send(chat_id, f"❌ Lỗi: {e}")
    if res["mismatch"]:
        msg += f"\n⚠️ Yêu cầu {qty} key nhưng web trả {len(res['keys'])}. Kiểm tra Lịch sử trên web."
    send(chat_id, msg)
    log.info("user=%s bought %s x%s cost=%s", uid, plan_key, qty, res["cost"])


def main():
    log.info("Bot đang chạy... admin=%d, SECRET_KEY=%s", len(ADMIN_IDS), "có" if SECRET_KEY else "CHƯA ĐẶT")
    while True:
        try:
            resp = zalo("getUpdates", {"timeout": 30}, timeout=45)
            for u in extract_updates(resp):
                msg = u.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                uid = str((msg.get("from") or {}).get("id", ""))
                if chat_id and msg.get("text"):
                    threading.Thread(target=handle, args=(chat_id, uid, msg["text"], str(chat.get("chat_type", ""))),
                                     daemon=True).start()
        except Exception:
            log.exception("polling error")
            time.sleep(3)

if __name__ == "__main__":
    main()
