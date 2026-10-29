// 只读查询豆包最近会话列表。不发送任何消息。
// 用法：node doubao_probe_conv.mjs [limit] [cookie文件名]
//   cookie 文件名默认 cookies-cdp.json（当前绑定的账号）
//   查另一个账号：node doubao_probe_conv.mjs 15 cookies-consumer.json
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const OUT = path.join(os.homedir(), ".doubao-relay");
const cookieFile = process.argv[3] || "cookies-cdp.json";
const jarPath = path.join(OUT, cookieFile);
if (!fs.existsSync(jarPath)) {
  console.error("cookie 文件不存在:", jarPath);
  process.exit(2);
}
const jar = JSON.parse(fs.readFileSync(jarPath, "utf8"));
const cookies = jar.cookies || jar;
const pick = (n) => (cookies.find((c) => c.name === n) || {}).value || "";
const cookieHeader = cookies.map((c) => `${c.name}=${c.value}`).join("; ");

const deviceId = pick("device_id") || "0";
const webId = pick("web_id") || "";
console.log("查询账号 cookie:", cookieFile, "| 项数:", cookies.length);

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
    body: JSON.stringify({
      cmd, uplink_body: uplinkBody, sequence_id: String(Date.now()),
      channel: 2, version: "1",
    }),
  });
  const t = await res.text();
  try { return JSON.parse(t); } catch { return { raw: t.slice(0, 500) }; }
}

const limit = Number(process.argv[2] || 15);
const r = await im(3200, {
  pull_recent_conv_chain_uplink_body: {
    limit, message_count_per_conv: 0, api_version: 1, conv_version: 0,
    direction: 3, option: { not_need_message: true, need_complete_conversation: true },
  },
}, "/im/chain/recent_conv");

fs.writeFileSync(path.join(OUT, "probe-recent.json"), JSON.stringify(r, null, 1));

// 提取会话摘要（实际字段是 cells[].conversation）
const body = r.downlink_body || {};
const chain = body.pull_recent_conv_chain_downlink_body || body;
const cells = chain.cells || [];
const convs = cells.map((c) => Object.assign({ _id: c.id }, c.conversation || {}));
console.log("返回码:", r.status_code ?? r.code ?? "?", "| 会话数:", convs.length);

function ts(x) {
  const n = Number(x || 0);
  if (!n) return "?";
  const ms = n > 1e12 ? n : n * 1000;
  return new Date(ms).toLocaleString("zh-CN", { hour12: false });
}

for (const c of convs.slice(0, limit)) {
  const id = c.conversation_id || c._id || "?";
  const msgs = c.messages || [];
  const last = msgs[msgs.length - 1] || {};
  let snip = "";
  for (const b of (last.content_block || [])) {
    if (b.block_type === 10000) snip += (b.content?.text_block?.text || "");
  }
  if (!snip) snip = (last.content?.text_block?.text || "");
  const t = c.update_time || c.modify_time || c.create_time || last.create_time;
  console.log("  %s | 消息 %d | 末条: %s", String(id), msgs.length,
              String(snip).replace(/\s+/g, " ").slice(0, 48) || "(空)");
}
console.log("\n完整响应 -> ~/.doubao-relay/probe-recent.json");
