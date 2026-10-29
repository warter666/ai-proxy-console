// 只读查看某个会话的消息链，判断是否被代理测试内容污染。
// 用法：node doubao_probe_msgs.mjs <conversationId> [limit] [cookie文件名]
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const OUT = path.join(os.homedir(), ".doubao-relay");
const convId = process.argv[2];
const limit = Number(process.argv[3] || 30);
const cookieFile = process.argv[4] || "cookies-cdp.json";
if (!convId) { console.log("用法: node doubao_probe_msgs.mjs <convId> [limit] [cookieFile]"); process.exit(2); }

const jarPath = path.join(OUT, cookieFile);
if (!fs.existsSync(jarPath)) { console.error("cookie 不存在:", jarPath); process.exit(2); }
const jar = JSON.parse(fs.readFileSync(jarPath, "utf8"));
const cookies = jar.cookies || jar;
const pick = (n) => (cookies.find((c) => c.name === n) || {}).value || "";
const cookieHeader = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
const deviceId = pick("device_id") || "0";
const webId = pick("web_id") || "";

function baseParams() {
  return new URLSearchParams({
    version_code: "20800", language: "zh", device_platform: "web",
    doubao_device_platform: "desktop", aid: "1044603", real_aid: "1044603",
    pkg_type: "release_version", device_id: deviceId, pc_version: "2.31.10",
    doubao_pc_version: "2.31.10", web_id: webId,
    tea_uuid: deviceId, region: "CN", sys_region: "CN", samantha_web: "1",
    web_platform: "desktop", "use-olympus-account": "1", runtime: "web",
    runtime_version: "3.39.0", client_platform: "pc_client",
    chromium_version: "147.0.7727.149", channel: "win", fp: "verify_" + deviceId,
    web_tab_id: "",
  });
}

async function im(cmd, uplinkBody, ep) {
  const q = baseParams();
  const res = await fetch(`https://www.doubao.com${ep}?${q}`, {
    method: "POST",
    headers: {
      "content-type": "application/json; encoding=utf-8",
      "agw-js-conv": "str",
      accept: "application/json, text/plain, */*",
      origin: "https://www.doubao.com",
      referer: "https://www.doubao.com/",
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
      cookie: cookieHeader,
    },
    body: JSON.stringify({ cmd, uplink_body: uplinkBody, sequence_id: String(Date.now()), channel: 2, version: "1" }),
  });
  const t = await res.text();
  try { return JSON.parse(t); } catch { return { raw: t.slice(0, 300) }; }
}

const r = await im(3100, {
  pull_singe_chain_uplink_body: {
    conversation_id: convId, anchor_index: Number.MAX_SAFE_INTEGER,
    conversation_type: 3, direction: 1, limit, ext: {},
    filter: { index_list: [] }, option: { lazy_load_strategy: 0 },
  },
}, "/im/chain/single");

fs.writeFileSync(path.join(OUT, `probe-msgs-${cookieFile.replace('.json','')}-${convId}.json`), JSON.stringify(r, null, 1));
const body = r.downlink_body || {};
const d = body.pull_singe_chain_downlink_body || body;
const msgs = d.messages || [];
console.log(`[${cookieFile}] 会话 ${convId} | 消息数: ${msgs.length} | 返回码: ${r.status_code}`);

function ts(x) {
  const n = Number(x || 0);
  if (!n) return "?";
  const ms = n > 1e12 ? n : n * 1000;
  return new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

for (const m of msgs) {
  const role = m.role || m.msg_role || "?";
  let text = "";
  for (const b of (m.content_block || [])) {
    if (b.block_type === 10000) text += (b.content?.text_block?.text || "");
    else if (b.block_type === 10040) text += "[思考]";
  }
  const t = m.create_time || m.send_time || m.update_time;
  console.log("  [%s] %-8s %s", ts(t), String(role).slice(0, 8), text.replace(/\s+/g, " ").slice(0, 70));
}
