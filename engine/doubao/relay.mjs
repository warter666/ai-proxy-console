// doubao/relay.mjs — 豆包工作 (DoubaoWork) → OpenAI / Anthropic 兼容反代
//
//   node doubao/relay.mjs [--port 18770] [--conv <conversationId>]
//
// 依赖 ~/.doubao-relay/ 下的状态（不进仓库）：
//   cookies-cdp.json   CDP 抓取的登录 cookie（优先，可用 doubao/cdp.js cookies 刷新）
//   cookies.json       DPAPI 解密脚本产出的 cookie（兜底，doubao/decrypt_cookies.py）
//   config.json        { port, key, conversationId } 可选
//
// 上游：POST https://www.doubao.com/chat/completion （SSE）
//   * 仅需 cookie（sessionid/ttwid/msToken...），无需 a_bogus（已实测）
//   * 无状态语义（对齐 AutoClaw relay）：调用方每次带来完整历史，本 relay
//     把历史拍平成一条消息发出去；上游的 conversation_id 只是传输草稿纸，
//     不构成任何跨请求的隐藏状态。
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { readFileSync, existsSync, statSync, writeFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const DIR = join(homedir(), ".doubao-relay");
const BOT_ID = "7338286299411103781";
const AID = "1044603";
// 设备指纹正常情况下从 cookie 的 device_id 取；取不到时用随机值，不硬编码任何真实设备 ID
const FALLBACK_DEVICE_ID = process.env.DOUBAO_DEVICE_ID || String(Math.floor(Math.random() * 9e15) + 1e15);
const DEFAULT_MODEL = "doubao";

// --------------------------------------------------------------- models

// model_item_key/mode_id 来自客户端 conversation/modify；本地无法枚举目录，
// 因此对外只暴露按能力区分的合成模型。
const MODELS = [
  { id: "doubao", label: "豆包工作 · 标准", needDeepThink: 0, reasoningEffort: "0", agentMode: 0 },
  { id: "doubao-think", label: "豆包工作 · 深度思考", needDeepThink: 9, reasoningEffort: "5", agentMode: 0 },
];
const MODEL_ALIASES = {
  "doubao-work": "doubao",
  "doubao-pro": "doubao",
  "doubao-deep": "doubao-think",
  "doubao-reasoner": "doubao-think",
  "doubao/deep": "doubao-think",
};

// --------------------------------------------------------------- config

const argv = process.argv.slice(2);
const argOf = (name, fallback) => {
  const i = argv.indexOf("--" + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : fallback;
};

function readJson(file, fallback = null) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

const CONFIG_FILE = join(DIR, "config.json");
const fileConfig = readJson(CONFIG_FILE, {}) || {};
const PORT = Number(process.env.DOUBAO_RELAY_PORT || argOf("port", fileConfig.port || 18770));
const HOST = process.env.DOUBAO_RELAY_HOST || argOf("host", fileConfig.host || "127.0.0.1");
const RELAY_KEY = process.env.DOUBAO_RELAY_KEY || fileConfig.key || "doubao-local-key";

// 会话隔离（方案 B）：所有代理请求固定发到一个「专用会话」，
// 绝不碰用户在手机/网页上正在聊的对话。
//   isolate  true  = 首次请求时新建专用会话，并记住 ID（推荐，默认）
//   isolate  false = 老行为（每次空 ID，会被豆包追加到活跃会话，会污染）
// 已持久化的专用会话 ID 存在 config.json 的 conversationId 字段。
const ISOLATE = fileConfig.isolate !== false;

/** 把上游分配的会话 ID 写回 config.json，下次启动直接复用。
 *
 *  安全网：豆包服务端对空 conversation_id 会归并到「该账号最近的会话」，
 *  而那很可能正是用户此刻在聊的对话。绑定时记录当时的时间戳与首条消息，
 *  并在日志里显式警告，提示用户用 doubao_session.py 核对。
 */
function persistConversationId(id) {
  if (!ISOLATE || !id) return;
  try {
    const cur = readJson(CONFIG_FILE, {}) || {};
    if (cur.conversationId === id) return;
    cur.conversationId = id;
    cur.conversationNote = "AI_Proxy 代理专用会话，勿与其他对话混用";
    cur.conversationBoundAt = new Date().toISOString();
    writeFileSync(CONFIG_FILE, JSON.stringify(cur, null, 1), "utf8");
    log(`已固化专用会话 conversationId=${id}（写入 config.json）`);
    log("⚠ 该会话由豆包服务端按「最近活跃」分配，若它正是你在用的对话，"
      + "请运行 `python doubao_session.py list` 核对，必要时 reset 重绑。");
  } catch (e) {
    log(`写入 config.json 失败（不影响本次运行）：${e.message || e}`);
  }
}

const state = {
  conversationId: process.env.DOUBAO_CONV_ID || argOf("conv", fileConfig.conversationId || ""),
};

// --------------------------------------------------------------- cookies

const COOKIE_FILES = [join(DIR, "cookies-cdp.json"), join(DIR, "cookies.json")];
const REQUIRED = ["sessionid", "ttwid"];
let cookieCache = { at: 0, mtime: 0, list: [], source: "" };

function loadCookies(force = false) {
  for (const file of COOKIE_FILES) {
    if (!existsSync(file)) continue;
    const mtime = statSync(file).mtimeMs;
    if (!force && cookieCache.source === file && cookieCache.mtime === mtime) return cookieCache;
    const raw = readJson(file, null);
    const list = Array.isArray(raw) ? raw : raw && Array.isArray(raw.cookies) ? raw.cookies : [];
    if (!list.length) continue;
    cookieCache = { at: Date.now(), mtime, list, source: file };
    return cookieCache;
  }
  throw new Error("cookie 快照缺失：请先运行 `node doubao/cdp.js cookies`（客户端需带 --remote-debugging-port=9222）");
}

const cookieOf = (name) => (loadCookies().list.find((c) => c.name === name) || {}).value || "";

function cookieHealth() {
  try {
    const c = loadCookies();
    const names = c.list.map((x) => x.name);
    return {
      ok: REQUIRED.every((n) => names.includes(n)),
      source: c.source,
      count: c.list.length,
      ageMinutes: Math.round((Date.now() - c.mtime) / 60000),
      missing: REQUIRED.filter((n) => !names.includes(n)),
      hasMsToken: names.includes("msToken"),
    };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

// --------------------------------------------------------------- upstream

function buildUrl() {
  const deviceId = cookieOf("device_id") || FALLBACK_DEVICE_ID;
  const q = new URLSearchParams({
    aid: AID, channel: "win", client_platform: "pc_client", device_platform: "web",
    doubao_device_platform: "desktop", doubao_pc_version: "2.31.10", language: "zh",
    pc_version: "2.31.10", pkg_type: "release_version", real_aid: AID, region: "CN",
    runtime: "web", runtime_version: "3.39.0", samantha_web: "1", sys_region: "CN",
    tz_name: "Asia/Shanghai", "use-olympus-account": "1", version_code: "20800",
    web_platform: "desktop", web_tab_id: crypto.randomUUID(),
    chromium_version: "147.0.7727.149", device_id: deviceId,
    fp: "verify_" + deviceId, tea_uuid: deviceId,
  });
  const webId = cookieOf("web_id");
  if (webId) q.set("web_id", webId);
  const msToken = cookieOf("msToken");
  if (msToken) q.set("msToken", msToken);
  return "https://www.doubao.com/chat/completion?" + q.toString();
}

function buildBody(text, { conversationId = "", model = DEFAULT_MODEL } = {}) {
  const spec = MODELS.find((m) => m.id === model) || MODELS[0];
  const uuid = () => crypto.randomUUID();
  const option = {
    send_message_scene: "", create_time_ms: Date.now(), collect_id: "", is_audio: false,
    answer_with_suggest: false, agent_mode: spec.agentMode, tts_switch: false,
    need_deep_think: spec.needDeepThink, click_clear_context: false, from_suggest: false,
    is_regen: false, is_replace: false, is_from_click_option: false,
    is_from_click_softlink: false, disable_sse_cache: false, select_text_action: "",
    is_select_text: false, resend_for_regen: false, scene_type: 0, unique_key: uuid(),
    start_seq: 0, need_create_conversation: !conversationId,
    sse_recv_event_options: { support_chunk_delta: true },
    support_lazy_fetch_stream: true,
  };
  if (spec.needDeepThink) option.reasoning_effort = spec.reasoningEffort;
  if (!conversationId) {
    option.conversation_init_option = { need_ack_conversation: true };
    option.conversation_init_ext = { model_item_key: "9", mode_id: "3", reasoning_effort: "5" };
  }
  return {
    client_meta: {
      local_conversation_id: conversationId ? "" : "local_" + (BigInt(Date.now()) * 1000n + BigInt(Math.floor(Math.random() * 900) + 100)).toString(),
      conversation_id: conversationId,
      bot_id: BOT_ID,
      last_section_id: "",
      last_message_index: null,
      local_permissions: [
        { permission_name: "ACCESS_COARSE_LOCATION", status: 2 },
        { permission_name: "ACCESS_FINE_LOCATION", status: 2 },
        { permission_name: "ACCESS_BACKGROUND_LOCATION", status: 2 },
      ],
    },
    messages: [{
      local_message_id: uuid(),
      content_block: [{
        block_type: 10000,
        content: { text_block: { text, icon_url: "", icon_url_dark: "", summary: "" } },
        pc_event_block: "", block_id: uuid(), parent_id: "", meta_info: [], append_fields: [],
      }],
      message_status: 0,
    }],
    option,
  };
}

function upstreamHeaders() {
  const cookies = loadCookies();
  return {
    "content-type": "application/json",
    "agw-js-conv": "str",
    accept: "*/*",
    origin: "https://www.doubao.com",
    referer: "https://www.doubao.com/",
    cookie: cookies.list.map((c) => `${c.name}=${c.value}`).join("; "),
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.7727.149 Safari/537.36 SamanthaDoubaoWork/2.31.10",
  };
}

/** 把 SSE 帧翻译成事件；返回最终 {text, think, conversationId} */
async function runCompletion(text, { model, conversationId, onText, onThink, signal, timeoutMs = 300000 }) {
  const body = buildBody(text, { conversationId, model });
  const res = await fetch(buildUrl(), {
    method: "POST", headers: upstreamHeaders(), body: JSON.stringify(body), signal,
  });
  if (!res.ok) {
    const t = await res.text().catch(() => "");
    const err = new Error(`upstream ${res.status}: ${t.slice(0, 200)}`);
    err.status = res.status;
    throw err;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "", ev = {};
  let acc = "", think = "", conv = conversationId, brief = "";

  const pushText = (t) => {
    if (!t) return;
    if (acc && t.length >= acc.length && t.startsWith(acc)) { // 整段重发
      const delta = t.slice(acc.length);
      if (delta) { acc = t; onText(delta); }
      return;
    }
    if (acc.endsWith(t) && t.length > 8) return; // 重复帧
    acc += t;
    onText(t);
  };
  const pushThink = (t) => { if (!t) return; think += t; onThink(t); };

  const handle = (name, data) => {
    let j;
    try { j = JSON.parse(data); } catch { return; }
    if (name === "SSE_ACK") {
      const id = j.ack_client_meta && j.ack_client_meta.conversation_id;
      if (id) {
        conv = id;
        // 关键：把上游分配的会话 ID 固化下来。
        // 之前这里只赋给局部变量 conv 用于打日志，state.conversationId 永远是空，
        // 于是每次请求都带着空 conversation_id 发出去 —— 豆包会把它**追加到当前
        // 活跃会话**（实测会污染用户手机/网页上正在聊的那个对话）。
        // 现在首次拿到就记住，后续请求固定用同一个「代理专用会话」。
        if (state.conversationId !== id) {
          state.conversationId = id;
          persistConversationId(id);
        }
      }
      return;
    }
    if (name === "SSE_REPLY_END") {
      if (j.end_type === 1 && j.msg_finish_attr && j.msg_finish_attr.brief) brief = j.msg_finish_attr.brief;
      return;
    }
    if (name === "STREAM_MSG_NOTIFY") {
      for (const b of (j.content && j.content.content_block) || []) {
        if (b.control_info && b.control_info.is_visible === false) continue;
        if (b.block_type === 10000) pushText(b.content && b.content.text_block && b.content.text_block.text);
        if (b.block_type === 10040) pushThink(b.content && b.content.thinking_block && b.content.thinking_block.text);
      }
      return;
    }
    if (name === "CHUNK_DELTA") { pushText(j.text); return; }
    if (name === "STREAM_CHUNK") {
      for (const op of j.patch_op || []) {
        const pv = op.patch_value || {};
        for (const b of pv.content_block || []) {
          if (b.control_info && b.control_info.is_visible === false) continue;
          if (b.block_type === 10000) pushText(b.content && b.content.text_block && b.content.text_block.text);
          if (b.block_type === 10040) pushThink(b.content && b.content.thinking_block && b.content.thinking_block.text);
        }
      }
    }
  };

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).replace(/\r$/, "");
      buf = buf.slice(i + 1);
      if (!line.trim()) { if (ev.event) { handle(ev.event, ev.data || ""); ev = {}; } continue; }
      const m = line.match(/^(event|data):\s?(.*)$/);
      if (m) ev[m[1]] = m[2];
    }
  }
  if (!acc && brief) { acc = brief; onText(brief); }
  return { text: acc, think, conversationId: conv, short: acc !== brief && brief.length > 0 && brief.length < acc.length };
}

// --------------------------------------------------------------- stateless
// 对齐 AutoClaw relay 的无状态语义：历史每次由调用方全量带来，网关不依赖
// 任何会话池/隐藏状态。上游的"会话"只是传输草稿纸——每次请求都把完整
// 历史拍平成一条消息发进固定会话，回答只取决于本次请求内容。

const textOf = (m) => (typeof m.content === "string" ? m.content : (m.content || []).map((p) => p.text || "").join(""));

/** 把调用方发来的完整历史拍平成一段带角色标签的文本（AutoClaw 语义：历史每次自带）。 */
function flatten(messages) {
  const parts = [];
  for (const m of messages) {
    const t = textOf(m).trim();
    if (!t) continue;
    if (m.role === "system") parts.push("[System instructions]\n" + t);
    else if (m.role === "user") parts.push("[User]\n" + t);
    else if (m.role === "assistant") parts.push("[Assistant]\n" + t);
  }
  if (parts.length <= 1) return parts[0] || "";
  return "下面是此前的对话记录（仅作为上下文，不要复述），请只回答最后一条 [User] 消息。\n\n" + parts.join("\n\n");
}

// --------------------------------------------------------------- http

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const json = (res, code, obj) => {
  const body = JSON.stringify(obj);
  res.writeHead(code, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
  res.end(body);
};
const readBody = (req) => new Promise((resolve, reject) => {
  let raw = "";
  req.on("data", (c) => { raw += c; if (raw.length > 8 << 20) reject(new Error("body too large")); });
  req.on("end", () => { try { resolve(raw ? JSON.parse(raw) : {}); } catch (e) { reject(e); } });
});
const sse = (res, event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

function checkKey(req) {
  const given = req.headers["x-api-key"] || String(req.headers.authorization || "").replace(/^Bearer\s+/i, "");
  return given === RELAY_KEY;
}

const resolveModel = (requested) => {
  let id = String(requested || DEFAULT_MODEL).trim();
  id = id.replace(/^(doubao|doubao-work|doubao-relay)\//, (m) => (m === "doubao/" ? "" : ""));
  id = MODEL_ALIASES[id] || id;
  return MODELS.some((m) => m.id === id) ? id : DEFAULT_MODEL;
};

async function handleChat(req, res) {
  const body = await readBody(req);
  const messages = Array.isArray(body.messages) ? body.messages : [];
  const model = resolveModel(body.model);
  const turns = messages.filter((m) => m.role === "user").length;
  const id = "chatcmpl-" + createHash("sha1").update(JSON.stringify(messages).slice(0, 4096) + turns + Date.now()).digest("hex").slice(0, 24);
  const created = Math.floor(Date.now() / 1000);
  const chunk = (delta, finish = null) => ({
    id, object: "chat.completion.chunk", created, model,
    choices: [{ index: 0, delta, finish_reason: finish }],
  });
  const stream = body.stream === true;

  if (stream) {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
    sse(res, "data", chunk({ role: "assistant", content: "" }));
  }

  let sent = "";
  const emit = (d) => { sent += d; if (stream) sse(res, "data", chunk({ content: d })); };
  const emitThink = (d) => { if (stream) sse(res, "data", chunk({ reasoning_content: d })); };

  try {
    const convId = state.conversationId || "";
    const prompt = flatten(messages); // 永远全量：无状态，不依赖任何池
    const ac = new AbortController();
    req.on("close", () => ac.abort());
    const result = await runCompletion(prompt, {
      model, conversationId: convId, signal: ac.signal,
      onText: emit, onThink: emitThink,
    });
    log(`chat model=${model} turns=${turns} stateless conv=${result.conversationId || convId || "(none)"} stream=${stream} len=${result.text.length}`);
    const text = result.text || sent;
    if (stream) {
      const tail = text.startsWith(sent) ? text.slice(sent.length) : "";
      if (tail) sse(res, "data", chunk({ content: tail }));
      sse(res, "data", chunk({}, "stop"));
      res.write("data: [DONE]\n\n");
      res.end();
    } else {
      json(res, 200, {
        id, object: "chat.completion", created, model,
        choices: [{ index: 0, message: { role: "assistant", content: text }, finish_reason: "stop" }],
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      });
    }
  } catch (err) {
    log("chat error:", err.message);
    if (stream) {
      sse(res, "data", { error: { message: err.message, type: "doubao_relay_error" } });
      res.write("data: [DONE]\n\n");
      res.end();
    } else {
      json(res, 502, { error: { message: "doubao relay error: " + err.message, type: "doubao_relay_error" } });
    }
  }
}

async function handleAnthropic(req, res) {
  const body = await readBody(req);
  const messages = Array.isArray(body.messages) ? body.messages : [];
  const system = typeof body.system === "string" ? body.system : (body.system || []).map((b) => b.text || "").join("\n");
  const full = system ? [{ role: "system", content: system }, ...messages] : messages;
  const model = resolveModel(body.model);
  const turns = messages.filter((m) => m.role === "user").length;
  const stream = body.stream === true;
  const id = "msg_" + createHash("sha1").update(JSON.stringify(full).slice(0, 4096) + turns + Date.now()).digest("hex").slice(0, 24);
  const usage = { input_tokens: 0, output_tokens: 0 };

  if (stream) {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    res.write(`event: message_start\ndata: ${JSON.stringify({ type: "message_start", message: { id, type: "message", role: "assistant", model, content: [], usage } })}\n\n`);
    res.write(`event: content_block_start\ndata: ${JSON.stringify({ type: "content_block_start", index: 0, content_block: { type: "text", text: "" } })}\n\n`);
  }

  let sent = "";
  try {
    const convId = state.conversationId || "";
    const prompt = flatten(full); // 永远全量：无状态，不依赖任何池
    const ac = new AbortController();
    req.on("close", () => ac.abort());
    const result = await runCompletion(prompt, {
      model, conversationId: convId, signal: ac.signal,
      onText: (d) => {
        sent += d;
        if (stream) res.write(`event: content_block_delta\ndata: ${JSON.stringify({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: d } })}\n\n`);
      },
      onThink: () => {},
    });
    const text = result.text || sent;
    log(`messages model=${model} turns=${turns} stateless conv=${result.conversationId || convId || "(none)"} stream=${stream} len=${text.length}`);
    if (stream) {
      const tail = text.startsWith(sent) ? text.slice(sent.length) : "";
      if (tail) res.write(`event: content_block_delta\ndata: ${JSON.stringify({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: tail } })}\n\n`);
      res.write(`event: content_block_stop\ndata: ${JSON.stringify({ type: "content_block_stop", index: 0 })}\n\n`);
      res.write(`event: message_delta\ndata: ${JSON.stringify({ type: "message_delta", delta: { stop_reason: "end_turn" }, usage })}\n\n`);
      res.write(`event: message_stop\ndata: ${JSON.stringify({ type: "message_stop" })}\n\n`);
      res.end();
    } else {
      json(res, 200, { id, type: "message", role: "assistant", model, content: [{ type: "text", text }], stop_reason: "end_turn", usage });
    }
  } catch (err) {
    log("messages error:", err.message);
    if (stream) {
      res.write(`event: error\ndata: ${JSON.stringify({ type: "error", error: { type: "doubao_relay_error", message: err.message } })}\n\n`);
      res.end();
    } else {
      json(res, 502, { type: "error", error: { type: "doubao_relay_error", message: "doubao relay error: " + err.message } });
    }
  }
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  try {
    if (req.method === "GET" && (url.pathname === "/health" || url.pathname === "/")) {
      return json(res, 200, {
        service: "doubao-relay",
        upstream: "https://www.doubao.com/chat/completion",
        cookies: cookieHealth(),
        conversation: state.conversationId || "(auto/new)",
        mode: "stateless",
        models: MODELS.map((m) => m.id),
      });
    }
    if (req.method === "GET" && url.pathname === "/v1/models") {
      return json(res, 200, {
        object: "list",
        data: MODELS.map((m) => ({
          id: m.id, object: "model", created: Math.floor(Date.now() / 1000), owned_by: "doubao",
          doubao: { label: m.label, deep_think: m.needDeepThink > 0 },
        })),
      });
    }
    if (req.method === "POST" && url.pathname === "/admin/conversation") {
      const b = await readBody(req);
      state.conversationId = String(b.conversationId || "");
      const cfg = { ...fileConfig, conversationId: state.conversationId };
      mkdirSync(DIR, { recursive: true });
      writeFileSync(CONFIG_FILE, JSON.stringify(cfg, null, 2));
      log("conversation set to", state.conversationId || "(auto)");
      return json(res, 200, { ok: true, conversationId: state.conversationId });
    }
    if (req.method === "POST" && url.pathname === "/admin/reload-cookies") {
      loadCookies(true);
      return json(res, 200, { ok: true, cookies: cookieHealth() });
    }
    if (!checkKey(req)) return json(res, 401, { error: { message: "invalid api key" } });
    if (req.method === "POST" && url.pathname === "/v1/chat/completions") return handleChat(req, res);
    if (req.method === "POST" && url.pathname === "/v1/messages") return handleAnthropic(req, res);
    json(res, 404, { error: { message: "not found: " + url.pathname } });
  } catch (err) {
    log("unhandled:", err.stack || err.message);
    json(res, 500, { error: { message: String(err.message || err) } });
  }
});

server.listen(PORT, HOST, () => {
  log(`doubao-relay listening on http://${HOST}:${PORT}`);
  log(`cookies: ${JSON.stringify(cookieHealth())}`);
  log(`conversation: ${state.conversationId || "(auto/new per session)"} | models: ${MODELS.map((m) => m.id).join(", ")}`);
});
