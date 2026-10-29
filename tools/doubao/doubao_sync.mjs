// 从正在运行的消费版豆包同步 cookie 与设备参数，供 daily_checkin.py 直接调用签到接口。
// 只输出摘要，不打印 cookie 值。需要 CDP 端口（默认 9223）。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const PORT = process.env.DOUBAO_CONSUMER_CDP_PORT || "9223";
const BASE = `http://127.0.0.1:${PORT}`;
const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_COOKIE = path.join(os.homedir(), ".doubao-relay", "cookies-consumer.json");
const OUT_CONFIG = path.join(HERE, "doubao-consumer-config.json");

function fail(msg, extra = {}) {
  console.log(JSON.stringify({ ok: false, error: msg, ...extra }));
  process.exit(2);
}

async function connectTarget() {
  let targets;
  try {
    const r = await fetch(`${BASE}/json/list`, { signal: AbortSignal.timeout(4000) });
    targets = await r.json();
  } catch (e) {
    fail("CDP 不可用: " + e.message);
  }
  const t = targets.find((x) => x.type === "page" && /doubao-chat\/chat/.test(x.url)) ||
    targets.find((x) => x.type === "page" && /doubao-launcher\/chat/.test(x.url)) ||
    targets.find((x) => x.type === "page" && /^doubao:/.test(x.url));
  if (!t) fail("没有找到豆包页面 target");
  return t;
}

const target = await connectTarget();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pending = new Map();
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) {
    const { res, rej } = pending.get(m.id);
    pending.delete(m.id);
    m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
  }
};
const send = (method, params = {}) => new Promise((res, rej) => {
  const n = ++id;
  pending.set(n, { res, rej });
  ws.send(JSON.stringify({ id: n, method, params }));
});

const ev = await send("Runtime.evaluate", {
  expression: `(() => {
    const out = { ua: navigator.userAgent, href: location.href };
    try { out.client_device_info = JSON.parse(localStorage.getItem('client_device_info') || '{}'); } catch {}
    try { out.desktop_device_id = localStorage.getItem('desktop_device_id') || ''; } catch {}
    try { out.web_id_json = localStorage.getItem('samantha_web_web_id') || ''; } catch {}
    try { out.uid = localStorage.getItem('flow_tea_user_id') || ''; } catch {}
    try {
      out.checkin_keys = [];
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i);
        if (/agent_explore_checkin_bubble_shown_at/.test(k)) out.checkin_keys.push(k);
      }
    } catch {}
    return JSON.stringify(out);
  })()`,
  returnByValue: true,
});
const info = JSON.parse(ev.result?.value || "{}");
const ua = info.ua || "";
const pcVersion = (ua.match(/SamanthaDoubao\/([\d.]+)/) || ua.match(/Doubao\/([\d.]+)/) || [])[1] || "2.33.3";
const chromiumVersion = (ua.match(/Chrome\/([\d.]+)/) || [])[1] || "";
let webId = info.desktop_device_id || "";
if (!webId && info.web_id_json) {
  try { webId = JSON.parse(info.web_id_json).web_id || ""; } catch {}
}
const deviceId = (info.client_device_info && info.client_device_info.deviceId) || "";
const campaign = (() => {
  for (const k of info.checkin_keys || []) {
    const parts = k.split(":");
    if (parts.length >= 3) return parts[parts.length - 1];
  }
  return "pc_incentive_task_mvp_v3";
})();

const ck = await send("Network.getAllCookies", {});
const mine = (ck.cookies || []).filter((c) => /(^|\.)doubao\.com$|(^|\.)byte/.test(c.domain || ""));
const jar = {};
for (const c of mine) if (c.name && c.value) jar[c.name] = c.value;
if (!jar.sessionid) fail("cookie 中没有 sessionid（登录态缺失）", { count: mine.length });

fs.mkdirSync(path.dirname(OUT_COOKIE), { recursive: true });
fs.writeFileSync(OUT_COOKIE, JSON.stringify({
  savedAt: new Date().toISOString(),
  count: mine.length,
  jar,
  cookies: mine,
}, null, 1));
fs.writeFileSync(OUT_CONFIG, JSON.stringify({
  device_id: deviceId,
  web_id: webId,
  uid: info.uid || "",
  pc_version: pcVersion,
  chromium_version: chromiumVersion,
  runtime_version: "3.42.4",
  version_code: "20800",
  campaign_group_code: campaign,
  updated_at: new Date().toISOString(),
}, null, 1));

console.log(JSON.stringify({
  ok: true,
  cookies: mine.length,
  sessionid_len: jar.sessionid.length,
  device_id: deviceId,
  web_id: webId,
  pc_version: pcVersion,
  chromium_version: chromiumVersion,
  campaign_group_code: campaign,
}));
ws.close();
