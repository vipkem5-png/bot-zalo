import { Zalo } from "zca-js";
import fs from "fs";

// ===== CẤU HÌNH =====
const SHOP_URL   = process.env.SHOP_URL || "https://vohoainam.net/ctv";
const SHOP_USER  = process.env.SHOP_USER;
const SHOP_PASS  = process.env.SHOP_PASS;
const KEY_PREFIX = process.env.KEY_PREFIX || "THIENDINH";
const MAX_QTY    = Number(process.env.MAX_QTY || 50);
const MAX_DEVICES = 1;
const ALLOWED = new Set((process.env.ALLOWED_IDS || "").split(",").filter(Boolean));

const PLANS = {            // sửa 'code' theo giá trị web thật sự gửi
  "10p": { code: "10m", label: "10 Phút" },
  "1h":  { code: "1h",  label: "1 Giờ" },
  "1d":  { code: "1d",  label: "1 Ngày" },
  "7d":  { code: "7d",  label: "7 Ngày" },
  "30d": { code: "30d", label: "30 Ngày" },
};
const CMD_RE = /^\.ikey(10p|1h|1d|7d|30d)\s+(\d+)\s*$/i;

// ===== MUA KEY TRÊN WEB (CẦN ĐIỀN ENDPOINT THẬT) =====
let shopToken = null;

async function shopLogin() {
  // TODO: thay bằng request đăng nhập thật của web
  const r = await fetch(`${SHOP_URL}/api/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: SHOP_USER, password: SHOP_PASS }),
  });
  if (!r.ok) throw new Error(`Đăng nhập web lỗi (${r.status})`);
  shopToken = (await r.json()).token;
}

async function buyKeys(planCode, qty, retry = true) {
  if (!shopToken) await shopLogin();
  // TODO: thay bằng request mua key thật
  const r = await fetch(`${SHOP_URL}/api/buy-key`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${shopToken}` },
    body: JSON.stringify({ duration: planCode, quantity: qty, max_devices: MAX_DEVICES, prefix: KEY_PREFIX }),
  });
  if ((r.status === 401 || r.status === 403) && retry) { shopToken = null; return buyKeys(planCode, qty, false); }
  if (!r.ok) throw new Error(`Web trả lỗi ${r.status}`);
  const data = await r.json();
  if (data.success === false) throw new Error(data.message || "Mua key thất bại");
  const keys = data.keys || [];
  if (keys.length !== qty) throw new Error(`Web trả ${keys.length}/${qty} key, kiểm tra Lịch sử.`);
  return keys;
}

// ===== ĐĂNG NHẬP ZALO =====
const zalo = new Zalo({ selfListen: true });   // nhận cả tin do chính bạn gõ
let api;
if (process.env.ZALO_CRED) {
  api = await zalo.login(JSON.parse(process.env.ZALO_CRED));
} else {
  api = await zalo.loginQR();                  // chạy LOCAL, quét QR bằng app Zalo
  const ctx = api.getContext();
  const cred = {
    cookie: api.getCookie().toJSON().cookies,
    imei: ctx.imei,
    userAgent: ctx.userAgent,
  };
  fs.writeFileSync("zalo_cred.json", JSON.stringify(cred));
  console.log("Đã lưu zalo_cred.json. Dán nội dung file vào biến ZALO_CRED trên Railway.");
}

// ===== XỬ LÝ LỆNH =====
let queue = Promise.resolve();                 // mua tuần tự, tránh trừ tiền chồng

api.listener.on("message", (message) => {
  const text = message.data?.content;
  if (typeof text !== "string") return;
  const m = text.trim().match(CMD_RE);
  if (!m) return;
  if (!message.isSelf && !ALLOWED.has(String(message.data.uidFrom))) return;

  const plan = PLANS[m[1].toLowerCase()];
  const qty = Number(m[2]);
  const reply = (msg) => api.sendMessage({ msg }, message.threadId, message.type);

  if (qty < 1 || qty > MAX_QTY) return void reply(`⚠️ Số lượng phải từ 1 đến ${MAX_QTY}.`);

  queue = queue.then(async () => {
    try {
      await reply(`⏳ Đang mua ${qty} key ${plan.label}...`);
      const keys = await buyKeys(plan.code, qty);
      const body = `✅ ${qty} key ${plan.label}:\n`;
      for (let i = 0; i < keys.length; i += 40)      // chia nhỏ tin dài
        await reply((i === 0 ? body : "") + keys.slice(i, i + 40).join("\n"));
      console.log(`bought ${plan.code} x${qty}`);
    } catch (e) {
      console.error(e);
      await reply(`❌ Lỗi: ${e.message}`);
    }
  });
});

api.listener.on("closed", (code) => { console.error("Zalo ngắt kết nối:", code); process.exit(1); }); // Railway tự khởi động lại
api.listener.start();
console.log("Bot đang chạy...");
