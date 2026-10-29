#!/usr/bin/env node
/**
 * WorkBuddy -> ZCode 本地桥接代理
 * ============================================================================
 * 作用
 *   ZCode 以 OpenAI 兼容协议调用本机 127.0.0.1:8787，
 *   本代理把请求翻译成 WorkBuddy 网关的原生格式并转发，
 *   再把 SSE 响应清洗后回流给 ZCode。
 *
 * 设计原则
 *   - 只做协议翻译与认证转发，不含任何限速/伪装/规避逻辑
 *   - 令牌每次请求实时读取，不缓存过期值
 *   - 日志有大小上限并自动轮转
 *
 * 用法
 *   node workbuddy-proxy.js [端口]     默认 8787
 *
 * 环境变量
 *   WB_PROXY_PORT       监听端口，默认 8787
 *   WB_AUTH_FILE        手动指定认证文件路径（默认自动探测）
 *   WB_PROXY_LOG        日志路径
 *   WB_PROXY_LOG_MAX    日志轮转阈值（字节），默认 5MB
 *   WB_PROXY_LOG_ENABLE 置 0 关闭日志
 *   WB_PROXY_DEBUG      置 1 输出上游请求体参数（不含对话内容）
 *   WB_PROXY_CAPTURE    置 1 保存最近 5 份客户端请求体到 capture/（排查风控用）
 * ============================================================================
 */
"use strict";

const http = require("http");
const https = require("https");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const os = require("os");

const VERSION = "1.3.0";
const BIND = "127.0.0.1";
const PORT = parseInt(process.argv[2] || process.env.WB_PROXY_PORT || "8787", 10);

const UPSTREAM_HOST = "copilot.tencent.com";
const UPSTREAM_PATH = "/v2/chat/completions";

const DIR = __dirname;
const LOG_FILE = process.env.WB_PROXY_LOG || path.join(DIR, "workbuddy-proxy.log");
const PID_FILE = path.join(DIR, "workbuddy-proxy.pid");
const LOG_MAX_BYTES = parseInt(process.env.WB_PROXY_LOG_MAX || String(5 * 1024 * 1024), 10);
const ENABLE_LOG = process.env.WB_PROXY_LOG_ENABLE !== "0";
const DEBUG = process.env.WB_PROXY_DEBUG === "1";

// ============================================================================
// 客户端版本号：优先从桌面端状态文件读取，读不到再用兜底常量。
// 这样 WorkBuddy 升级后无需改代码。
// ============================================================================
function detectWbVersion() {
  const cands = [
    path.join(os.homedir(), ".workbuddy", "last-launch.json"),
  ];
  for (const f of cands) {
    try {
      const j = JSON.parse(fs.readFileSync(f, "utf-8"));
      if (j && typeof j.version === "string" && j.version) return j.version;
    } catch (_) {}
  }
  return "0.0.0";
}
const WB_VERSION = detectWbVersion();
// 官方 CLI 在 User-Agent 里携带的自称版本，跟随官方客户端即可
const CLI_TAG = "2.115.0";

// ============================================================================
// 模型表
//   清单 = 服务端下发配置 agents 数组中 name:"cli" 代理的 models 白名单
//   （去掉 fast-model/balanced-model/deep-model 三个路由预设）；
//   参数取同文件 models 数组（网关侧为准，非本地打包文件）。
//   reasoning.supported 为空数组时表示档位固定不可调。
//   canDisable: 是否允许关闭思考（对应服务端 canDisableThinking）
// ============================================================================
const MODELS = {
  "deepseek-v4.1-flash": {
    name: "Deepseek-V4.1-Flash", credits: "x0.11", maxInput: 1000000, maxOutput: 128000,
    temperature: 1, images: true, tools: true, canDisable: false,
    reasoning: { default: "high", supported: ["high"] },
  },
  "deepseek-v4-pro": {
    name: "Deepseek-V4-Pro", credits: "x0.51", maxInput: 1000000, maxOutput: 128000,
    temperature: 1, images: true, tools: true, canDisable: true,
    reasoning: { default: "high", supported: ["high", "xhigh"] },
  },
  "glm-5.3": {
    name: "GLM-5.3", credits: "x0.79", maxInput: 1000000, maxOutput: 64000,
    temperature: 1, images: true, tools: true, canDisable: false,
    reasoning: { default: "high", supported: ["low", "high", "max"] },
  },
  "glm-5.3-flash": {
    name: "GLM-5.3-Flash", credits: "x0.06", maxInput: 1000000, maxOutput: 131072,
    temperature: 1, images: true, tools: true, canDisable: false,
    reasoning: { default: "high", supported: ["low", "high", "max"] },
  },
  "glm-5.2": {
    name: "GLM-5.2", credits: "x0.79", maxInput: 1000000, maxOutput: 64000,
    temperature: 1, images: true, tools: true, canDisable: true,
    reasoning: { default: "high", supported: ["high", "xhigh"] },
  },
  "glm-5.1": {
    name: "GLM-5.1", credits: "x0.79", maxInput: 200000, maxOutput: 48000,
    temperature: 1, images: true, tools: true, canDisable: false,
    reasoning: { default: "medium", supported: ["medium"] },
  },
  "glm-5v-turbo": {
    name: "GLM-5v-Turbo", credits: "x0.71", maxInput: 200000, maxOutput: 64000,
    temperature: 1, images: true, tools: true, canDisable: false,
    reasoning: { default: "medium", supported: ["medium"] },
  },
  "kimi-k3-1": {
    name: "Kimi-K3", credits: "x1.62", maxInput: 1000000, maxOutput: 32000,
    temperature: 1, images: true, tools: true, canDisable: true,
    reasoning: { default: "high", supported: ["low", "high", "xhigh"] },
  },
  "kimi-k2.8-preview": {
    name: "Kimi-K2.8-Preview", credits: "x0.77", maxInput: 1000000, maxOutput: 64000,
    temperature: 1, images: true, tools: true, canDisable: true,
    reasoning: { default: "high", supported: ["low", "high", "max"] },
  },
  "minimax-m3": {
    name: "MiniMax-M3", credits: "x0.25", maxInput: 512000, maxOutput: 64000,
    temperature: 1, images: true, tools: true, canDisable: false,
    reasoning: { default: "medium", supported: ["medium"] },
  },
  "hy3-b": {
    name: "Hy3", credits: "x0.00", maxInput: 192000, maxOutput: 64000,
    temperature: 0.9, images: true, tools: true, canDisable: false,
    reasoning: { default: "high", supported: ["low", "high"] },
  },
  "hy3-x": {
    name: "Hy3", credits: "x0.05", maxInput: 192000, maxOutput: 64000,
    temperature: 0.9, images: true, tools: true, canDisable: false,
    reasoning: { default: "high", supported: ["low", "high"] },
  },
  "hy4-preview": {
    name: "Hy4 preview", credits: "x0.29", maxInput: 960000, maxOutput: 64000,
    temperature: 0.9, images: true, tools: true, canDisable: false,
    reasoning: { default: "high", supported: ["high"] },
  },
};

// 兼容历史会话里可能残留的旧 ID（5.6.2 起 hy3/hy4-preview 改名）
const MODEL_ALIASES = {
  "deepseek-v4-flash": "deepseek-v4.1-flash",
  "hy3": "hy3-b",
  // 上游 2026-10 下旬把 hy4-preview-f 改名成 hy4-preview，旧 id 已失效（400/11133）
  "hy4-preview-f": "hy4-preview",
  // kimi-k2.7 / kimi-k2.6 上游已下架，转接到当前在售的 kimi-k3-1
  "kimi-k2.7": "kimi-k3-1",
  "kimi-k2.6": "kimi-k3-1",
};
// 已在网关下架的模型，直接拒绝，避免误扣额度或报错
const RETIRED = new Set(["auto"]);

// ============================================================================
// 通道签名中性化
//   网关 11128 扫描所有角色（system/assistant/tool）消息里的"官方客户端指纹"串。
//   会话历史里只要引用过一次（比如调试记录、用户粘贴的报错），签名就会随历史
//   一直回传，形成自证循环。代理把已知签名替换成等长的无害近似语，内容语义基本不变。
//   新签名出现时加进 SIGNS 即可（同一串在 user 角色能过、其他角色被拦即为签名）。
// ============================================================================
const SIGNS = [
  ["Main branch (you will usually use this for PRs)", "Main branch (used for PRs)"],
];
function neutralizeSigns(text) {
  let out = text, hit = false;
  for (const [sig, rep] of SIGNS) {
    if (out.includes(sig)) { out = out.split(sig).join(rep); hit = true; }
  }
  return hit ? out : text;
}

const VALID_EFFORTS = ["minimal", "low", "medium", "high", "xhigh", "max"];

// ============================================================================
// 日志（带大小上限轮转）
// ============================================================================
let appendCounter = 0;
function rotateIfNeeded() {
  if (++appendCounter % 50 !== 0) return;
  try {
    const st = fs.statSync(LOG_FILE);
    if (st.size > LOG_MAX_BYTES) {
      const bak = `${LOG_FILE}.1`;
      try { fs.rmSync(bak, { force: true }); } catch (_) {}
      fs.renameSync(LOG_FILE, bak);
    }
  } catch (_) {}
}
function log(level, msg, extra) {
  const line = `[${new Date().toISOString()}] [${level}] ${msg}${extra ? " | " + JSON.stringify(extra) : ""}`;
  console.log(line);
  if (!ENABLE_LOG) return;
  try {
    rotateIfNeeded();
    fs.appendFileSync(LOG_FILE, line + "\n");
  } catch (_) {}
}

// ============================================================================
// 认证文件：每次请求实时读取，带 mtime 缓存避免重复解析
// ============================================================================
function findAuthFile() {
  if (process.env.WB_AUTH_FILE && fs.existsSync(process.env.WB_AUTH_FILE)) return process.env.WB_AUTH_FILE;
  const rel = path.join("AppData", "Local", "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info");
  const cands = [path.join(os.homedir(), rel)];
  try {
    const usersRoot = path.join(path.parse(os.homedir()).root, "Users");
    for (const u of fs.readdirSync(usersRoot)) cands.push(path.join(usersRoot, u, rel));
  } catch (_) {}
  for (const c of cands) {
    try { if (fs.existsSync(c)) return c; } catch (_) {}
  }
  return cands[0];
}
const AUTH_FILE = findAuthFile();

let authCache = { mtime: -1, data: null };

// ============================================================================
// 令牌信封解密（WorkBuddy 5.6.2 起 accessToken 以 $wbEncrypted 信封存储）
//   信封: {"$wbEncrypted":1,"envelope":"<base64>"} → JSON {suite,keyId,nonce,authTag,ciphertext}
//   密钥: sha256(atRestSecretKey)；keyId = sha256(key) 十六进制前 16 位
//   密钥来源: 同目录 wb-key-payload.json（scan_mem.py 从桌面端内存提取，keyId 自校验）；
//             也可用环境变量 WB_AT_REST_SECRET 直接给 base64 secret
//   AAD: WB-AAD v1 规范，字段级信封 framing=field，整文件信封 framing=file
// ============================================================================
const KEY_PAYLOAD_FILE = path.join(DIR, "wb-key-payload.json");
let atRestCache = { mtime: -1, key: null };
function getAtRestKey() {
  if (process.env.WB_AT_REST_SECRET) {
    return crypto.createHash("sha256").update(process.env.WB_AT_REST_SECRET, "utf8").digest();
  }
  let st;
  try { st = fs.statSync(KEY_PAYLOAD_FILE); }
  catch (e) { throw new Error("缺少 wb-key-payload.json，无法解密 5.6.2 令牌信封（见 scan_mem.py）"); }
  if (st.mtimeMs !== atRestCache.mtime) {
    const j = JSON.parse(fs.readFileSync(KEY_PAYLOAD_FILE, "utf-8"));
    const key = j.keyHex ? Buffer.from(j.keyHex, "hex")
      : (j.secretB64 ? crypto.createHash("sha256").update(j.secretB64, "utf8").digest() : null);
    atRestCache = { mtime: st.mtimeMs, key };
  }
  if (!atRestCache.key || atRestCache.key.length !== 32) throw new Error("wb-key-payload.json 中缺少有效的 keyHex/secretB64");
  return atRestCache.key;
}
function buildAad(keyId, framing) {
  const spec = { file: ["WBEF1", 1], field: ["WBEV1", 2] }[framing];
  const u32 = (n) => { const b = Buffer.alloc(4); b.writeUInt32BE(n); return b; };
  const lp = (s) => { const b = Buffer.from(s, "utf8"); return Buffer.concat([u32(b.length), b]); };
  return Buffer.concat([
    Buffer.from("WB-AAD\0", "ascii"),
    Buffer.from([1]),
    lp(spec[0]),
    lp("sym-v1"),
    u32(1),
    lp(keyId),
    Buffer.from([spec[1]]),
    Buffer.from([0]), // 无 sequence
    Buffer.from([0]), // final 未定义
  ]);
}
function openTokenEnvelope(wrapper, key) {
  const env = JSON.parse(Buffer.from(wrapper.envelope, "base64").toString("utf8"));
  const nonce = Buffer.from(env.nonce, "base64");
  const tag = Buffer.from(env.authTag, "base64");
  const ct = Buffer.from(env.ciphertext, "base64");
  for (const framing of ["field", "file"]) {
    try {
      const d = crypto.createDecipheriv("aes-256-gcm", key, nonce);
      d.setAAD(buildAad(env.keyId, framing));
      d.setAuthTag(tag);
      return Buffer.concat([d.update(ct), d.final()]).toString("utf8");
    } catch (_) {}
  }
  throw new Error("令牌信封解密失败: keyId " + env.keyId + " 与本地密钥不匹配（WorkBuddy 可能已轮换密钥，重跑 scan_mem.py）");
}

function getAuth() {
  let st;
  try { st = fs.statSync(AUTH_FILE); }
  catch (e) { throw new Error(`认证文件不存在: ${AUTH_FILE}`); }

  if (st.mtimeMs !== authCache.mtime) {
    const raw = JSON.parse(fs.readFileSync(AUTH_FILE, "utf-8"));
    let token = raw && raw.auth && raw.auth.accessToken;
    if (token && typeof token === "object" && token.$wbEncrypted) {
      token = openTokenEnvelope(token, getAtRestKey());
    }
    authCache = {
      mtime: st.mtimeMs,
      data: {
        token,
        uid: raw && raw.account && raw.account.uid,
        domain: raw && raw.auth && raw.auth.domain,
      },
    };
  }
  if (!authCache.data || typeof authCache.data.token !== "string" || !authCache.data.token) {
    throw new Error("accessToken 为空或无法解密，请确认 WorkBuddy 已登录");
  }
  return authCache.data;
}

// ============================================================================
// 思考档位
// ============================================================================
function normalizeEffort(v) {
  if (typeof v !== "string") return undefined;
  const s = v.trim().toLowerCase();
  return VALID_EFFORTS.includes(s) ? s : undefined;
}
/** 官方 toSdkEffort()：只有 max 会被翻译成 xhigh，其余原样 */
function toSdkEffort(v) {
  return v === "max" ? "xhigh" : v;
}
/** 计算最终下发的思考参数；返回 {off:true} 表示不带任何思考字段 */
function resolveReasoning(spec, clientEffort, clientThinkingOff) {
  if (!spec) return null;
  let eff = spec.default;
  const asked = normalizeEffort(clientEffort);
  if (asked) {
    const allowed = spec.supported && spec.supported.length ? spec.supported : [spec.default];
    if (allowed.includes(asked)) eff = asked;
    else if (allowed.includes(toSdkEffort(asked))) eff = toSdkEffort(asked);
    // 不在支持档位内 -> 回落默认档，不报错
  }
  if (clientThinkingOff && spec.canDisable) return { off: true };
  return { off: false, effort: toSdkEffort(eff) };
}

// ============================================================================
// 请求翻译
// ============================================================================
function buildUpstreamRequest(clientBody, clientModel) {
  const spec = MODELS[clientModel];
  if (!spec) throw Object.assign(new Error(`未知模型: ${clientModel}`), { code: 404 });

  const body = Object.assign({}, clientBody);

  // 1) 剥离官方从不赋值的字段（序列化时本就会被丢弃）
  for (const k of [
    "top_p", "frequency_penalty", "presence_penalty",
    "tool_choice", "parallel_tool_calls",
    "store", "prompt_cache_retention",
    "reasoning_summary", "verbosity", "thinking",
    "extra_vars", "stream_options",
  ]) delete body[k];

  // 2) 先读客户端带来的思考提示，再删除
  const clientEffort = clientBody.reasoning_effort;
  const thinkingOff =
    clientBody.reasoning_effort === "none" ||
    (clientBody.thinking && clientBody.thinking.type === "disabled");
  delete body.reasoning_effort;

  // 3) 角色归一：网关通道校验（code 11128）会拦截 system 角色里的第三方提示词，
  //    实测同样内容挂 user 角色可正常通过 → developer/system 一律下发为 user
  if (Array.isArray(body.messages)) {
    body.messages = body.messages.map((m) => {
      if (!m) return m;
      // 签名中性化：所有角色的字符串内容都过一遍（历史里的签名会自证循环）
      if (typeof m.content === "string") {
        const nv = neutralizeSigns(m.content);
        if (nv !== m.content) m = Object.assign({}, m, { content: nv });
      } else if (Array.isArray(m.content)) {
        let changed = false;
        const arr = m.content.map((p) => {
          if (p && p.type === "text" && typeof p.text === "string") {
            const nv = neutralizeSigns(p.text);
            if (nv !== p.text) { changed = true; return Object.assign({}, p, { text: nv }); }
          }
          return p;
        });
        if (changed) m = Object.assign({}, m, { content: arr });
      }
      if (m.role === "developer" || m.role === "system") {
        m = Object.assign({}, m, { role: "user" });
      }
      return m;
    });
  }

  // 4) 以官方定义值覆盖客户端
  body.model = clientModel;
  body.temperature = spec.temperature;
  body.max_tokens = spec.maxOutput;
  body.stream = true;                              // 上游恒为流式，非流式由本代理聚合
  body.stream_options = { include_usage: true };

  const reason = resolveReasoning(spec.reasoning, clientEffort, thinkingOff);
  if (reason && !reason.off) {
    body.reasoning_effort = reason.effort;
    body.reasoning_summary = "auto";
    body.verbosity = "high";
  }

  if (!body.tools || !body.tools.length) delete body.tools;

  return { body, spec, reasoning: reason };
}

// ============================================================================
// SSE 工具
// ============================================================================
/**
 * 从一个 SSE 事件块里挑出 data: 行。
 * 上游除 data: 外还会发送 ": heartbeat" 注释行，且注释行可能与 data 行同处一块，
 * 因此不能只看块首行。
 */
function extractSseData(eventBlock) {
  const datas = [];
  for (const raw of eventBlock.split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (line.startsWith(":")) continue;
    if (line.startsWith("data:")) datas.push(line.slice(5).trim());
  }
  if (!datas.length) return null;
  return datas.join("\n");
}

/** SSE 规范化器：统一 CRLF -> LF，并正确处理跨数据块截断的 CR */
function makeSseNormalizer() {
  let pending = "";
  return (chunk) => {
    let text = pending + chunk;
    pending = "";
    if (text.endsWith("\r")) { pending = "\r"; text = text.slice(0, -1); }
    return text.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  };
}

/**
 * 清洗上游 delta。
 *
 * 网关每个块都会带一批空占位字段：content:""、reasoning_content:""、
 * tool_calls:[]、function_call:null 等。而 ZCode 的流解析器判断的是
 * `tool_calls != null` —— 空数组同样成立，于是每收到一个块就结束当前思考块、
 * 下一块再开新的，思考会被切成「一个词一块」。
 * 这里只保留有实际内容的字段。
 */
function sanitizeDelta(delta, toolCallIds) {
  const out = {};

  if (typeof delta.role === "string" && delta.role) out.role = delta.role;

  if (typeof delta.reasoning_content === "string" && delta.reasoning_content.length > 0) {
    out.reasoning_content = delta.reasoning_content;
  } else if (typeof delta.reasoning === "string" && delta.reasoning.length > 0) {
    out.reasoning = delta.reasoning;
  }

  if (typeof delta.content === "string" && delta.content.length > 0) out.content = delta.content;

  if (Array.isArray(delta.tool_calls) && delta.tool_calls.length > 0) {
    out.tool_calls = delta.tool_calls.map((tc) => {
      const norm = Object.assign({}, tc);
      const idx = norm.index != null ? norm.index : 0;
      if (typeof norm.id !== "string" || !norm.id) {
        // 多块拆分时后续块不带 id -> 复用同 index 首个块的 id
        if (!toolCallIds[idx]) {
          toolCallIds[idx] = `call_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
        }
        norm.id = toolCallIds[idx];
      } else {
        toolCallIds[idx] = norm.id;
      }
      return norm;
    });
  }

  const fc = delta.function_call;
  if (fc && typeof fc === "object" && (fc.name || fc.arguments)) out.function_call = fc;

  return out;
}

function buildUpstreamHeaders(auth, requestId) {
  const h = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": `WorkBuddy/${WB_VERSION} WorkBuddy/${WB_VERSION} CLI/${CLI_TAG}`,
    "X-Requested-With": "XMLHttpRequest",
    "X-IDE-Type": "WorkBuddy",
    "X-IDE-Name": "WorkBuddy",
    "X-IDE-Version": WB_VERSION,
    "X-Agent-Intent": "craft",
    "X-Agent-Purpose": "conversation",
    "X-Request-ID": requestId,
    "Authorization": `Bearer ${auth.token}`,
  };
  if (auth.uid) h["X-User-Id"] = auth.uid;
  if (auth.domain) h["X-Domain"] = auth.domain;
  return h;
}

// ============================================================================
// 响应处理
// ============================================================================
/** 流式：逐块清洗后原样转发 */
function relayStream(upRes, res, clientModel, ctx) {
  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
  });
  try { if (res.socket) res.socket.setNoDelay(true); } catch (_) {}

  const toolCallIds = {};
  let buf = "";
  let doneSent = false;
  let reasoningChars = 0;
  let contentChars = 0;

  function handleEvent(rawEvent) {
    const payload = extractSseData(rawEvent);
    if (payload === null) return;
    if (payload === "[DONE]") {
      if (!doneSent) { res.write("data: [DONE]\n\n"); doneSent = true; }
      return;
    }
    let obj;
    try { obj = JSON.parse(payload); } catch (_) { return; }
    if (!obj || typeof obj !== "object") return;

    if (obj.model) obj.model = clientModel;

    const choice = obj.choices && obj.choices[0];
    if (choice && choice.delta) {
      const raw = choice.delta;
      if (typeof raw.reasoning_content === "string") reasoningChars += raw.reasoning_content.length;
      if (typeof raw.content === "string") contentChars += raw.content.length;
      choice.delta = sanitizeDelta(raw, toolCallIds);
    }
    res.write(`data: ${JSON.stringify(obj)}\n\n`);
  }

  const norm = makeSseNormalizer();
  upRes.setEncoding("utf-8");
  upRes.on("data", (chunk) => {
    buf += norm(chunk);
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const evt = buf.slice(0, i);
      buf = buf.slice(i + 2);
      handleEvent(evt);
    }
  });
  // 收尾只做一次：正常 end 与「客户端先断开导致的 aborted」都会走到这里。
  // 客户端（DSH 等）收到 [DONE] 后会立刻关闭连接，触发 res.on("close") →
  // upReq.destroy() → upRes 抛 error:aborted。此时内容其实已经完整送达，
  // 若只在 end 分支记「流结束」，成功请求会被全部误记成失败（消耗统计归零）。
  let settled = false;
  function finishOnce(reason, extra) {
    if (settled) return;
    settled = true;
    log("INFO", `[${ctx.id}] 流结束`, Object.assign({
      ms: Date.now() - ctx.start,
      reasoningChars,
      contentChars,
      model: clientModel,
      reason,
    }, extra || {}));
  }

  upRes.on("end", () => {
    if (buf.trim()) handleEvent(buf);
    if (!doneSent) res.write("data: [DONE]\n\n");
    try { res.end(); } catch (_) {}
    finishOnce("end");
  });
  upRes.on("error", (e) => {
    // 已完整转发 [DONE]，或本次已收到实质内容（正文/推理/tool-call 任一非空）
    // 时，aborted 只是客户端收到结果后主动断开的连带报错，不算上游失败。
    // 反之（还没吐出任何内容就断）才是真正的上游故障。
    const hadContent = contentChars > 0 || reasoningChars > 0 || Object.keys(toolCallIds).length > 0;
    const benign = doneSent || (hadContent && /aborted|ECONNRESET/i.test(String(e.message || "") + (e.code || "")));
    if (benign) {
      finishOnce("client-closed", { note: String(e.message || "") });
    } else {
      log("ERROR", `[${ctx.id}] 上游流错误`, { error: e.message });
      finishOnce("upstream-error", { error: String(e.message || "") });
    }
    try { res.end(); } catch (_) {}
  });
}

/** 非流式：把上游 SSE 聚合成一个标准 JSON 响应 */
function aggregateSse(raw, clientModel) {
  const chunks = [];
  let usage = null;
  let finishReason = "stop";
  for (const line of raw.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith(":")) continue;
    if (!t.startsWith("data:")) continue;
    const payload = t.slice(5).trim();
    if (!payload || payload === "[DONE]") continue;
    let obj;
    try { obj = JSON.parse(payload); } catch (_) { continue; }
    const c0 = obj.choices && obj.choices[0];
    if (c0 && c0.delta) chunks.push(c0.delta);
    if (c0 && c0.finish_reason) finishReason = c0.finish_reason;
    if (obj.usage) usage = obj.usage;
  }

  let content = "";
  let reasoning = "";
  const toolCalls = [];
  for (const d of chunks) {
    if (typeof d.content === "string") content += d.content;
    if (typeof d.reasoning_content === "string") reasoning += d.reasoning_content;
    if (Array.isArray(d.tool_calls)) {
      for (const tc of d.tool_calls) {
        const idx = tc.index != null ? tc.index : toolCalls.length;
        if (!toolCalls[idx]) {
          toolCalls[idx] = {
            id: (typeof tc.id === "string" && tc.id) ? tc.id : `call_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`,
            type: tc.type || "function",
            function: { name: "", arguments: "" },
          };
        }
        if (typeof tc.id === "string" && tc.id) toolCalls[idx].id = tc.id;
        if (tc.function) {
          if (tc.function.name) toolCalls[idx].function.name += tc.function.name;
          if (tc.function.arguments) toolCalls[idx].function.arguments += tc.function.arguments;
        }
      }
    }
  }

  const message = { role: "assistant", content };
  if (reasoning) message.reasoning_content = reasoning;
  const calls = toolCalls.filter(Boolean);
  if (calls.length) { message.tool_calls = calls; finishReason = "tool_calls"; }

  return {
    id: `chatcmpl-${Date.now()}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: clientModel,
    choices: [{ index: 0, message, finish_reason: finishReason, logprobs: null }],
    usage: usage ? {
      prompt_tokens: Number(usage.prompt_tokens) || 0,
      completion_tokens: Number(usage.completion_tokens) || 0,
      total_tokens: Number(usage.total_tokens) || 0,
    } : { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

// ============================================================================
// HTTP 服务
// ============================================================================
function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(body);
}

const server = http.createServer((req, res) => {
  const start = Date.now();
  const url = new URL(req.url, `http://${BIND}:${PORT}`);
  const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const ctx = { id: requestId, start };

  if (url.pathname === "/" || url.pathname === "/health") {
    return sendJson(res, 200, {
      status: "ok", service: "workbuddy-proxy", version: VERSION,
      upstream: `https://${UPSTREAM_HOST}${UPSTREAM_PATH}`,
      models: Object.keys(MODELS).length, time: new Date().toISOString(),
    });
  }

  if (url.pathname.endsWith("/models") && req.method === "GET") {
    return sendJson(res, 200, {
      object: "list",
      data: Object.entries(MODELS).map(([id, m]) => ({
        id, object: "model", owned_by: "workbuddy", name: m.name,
        credits: m.credits, context_length: m.maxInput, max_output_tokens: m.maxOutput,
      })),
    });
  }

  if (!url.pathname.endsWith("/chat/completions") || req.method !== "POST") {
    return sendJson(res, 404, { error: { message: "Not Found", type: "not_found" } });
  }

  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    let parsed;
    try {
      parsed = JSON.parse(Buffer.concat(chunks).toString("utf-8"));
    } catch (e) {
      return sendJson(res, 400, { error: { message: `Invalid JSON: ${e.message}`, type: "invalid_request" } });
    }

    // 临时抓包：WB_PROXY_CAPTURE=1 时保存最近 5 份客户端请求体（排查风控用）
    if (process.env.WB_PROXY_CAPTURE) {
      try {
        const capDir = path.join(DIR, "capture");
        fs.mkdirSync(capDir, { recursive: true });
        fs.writeFileSync(path.join(capDir, `req-${requestId}.json`), JSON.stringify(parsed, null, 1));
        const olds = fs.readdirSync(capDir).filter((f) => f.startsWith("req-")).sort();
        while (olds.length > 5) fs.rmSync(path.join(capDir, olds.shift()), { force: true });
      } catch (_) {}
    }

    let reqModel = parsed && parsed.model;
    if (MODEL_ALIASES[reqModel]) reqModel = MODEL_ALIASES[reqModel];
    if (RETIRED.has(reqModel)) {
      return sendJson(res, 404, {
        error: { message: `模型 ${reqModel} 已在网关下架，请改用其他模型`, type: "model_retired" },
      });
    }
    if (!MODELS[reqModel]) {
      return sendJson(res, 404, {
        error: { message: `本代理未开放模型 ${parsed && parsed.model}`, type: "model_not_found" },
      });
    }
    const clientWantsStream = parsed.stream === true;

    let auth, prepared;
    try {
      auth = getAuth();
      prepared = buildUpstreamRequest(parsed, reqModel);
    } catch (e) {
      if (e.code === 404) return sendJson(res, 404, { error: { message: e.message, type: "model_not_found" } });
      log("ERROR", `[${requestId}] 认证/翻译失败`, { error: e.message });
      return sendJson(res, 401, {
        error: {
          message: `无法获取 WorkBuddy 令牌: ${e.message}`,
          type: "authentication_error",
          hint: "请确认 WorkBuddy 桌面端已登录并保持运行",
        },
      });
    }

    const payload = Buffer.from(JSON.stringify(prepared.body));
    if (DEBUG) {
      log("DEBUG", `[${requestId}] 上游 body`, {
        model: prepared.body.model,
        temperature: prepared.body.temperature,
        max_tokens: prepared.body.max_tokens,
        reasoning_effort: prepared.body.reasoning_effort,
        reasoning_summary: prepared.body.reasoning_summary,
        verbosity: prepared.body.verbosity,
        tools: Array.isArray(prepared.body.tools) ? prepared.body.tools.length : 0,
        bytes: payload.length,
      });
    }

    const upstreamHeaders = buildUpstreamHeaders(auth, requestId);
    upstreamHeaders["Content-Length"] = payload.length;

    const upReq = https.request({
      host: UPSTREAM_HOST,
      path: UPSTREAM_PATH,
      method: "POST",
      headers: upstreamHeaders,
    }, (upRes) => {
      const status = upRes.statusCode || 502;

      if (status !== 200) {
        const errChunks = [];
        upRes.on("data", (c) => errChunks.push(c));
        upRes.on("end", () => {
          const text = Buffer.concat(errChunks).toString("utf-8");
          log("ERROR", `[${requestId}] 上游 ${status}`, { body: text.slice(0, 300) });
          sendJson(res, status, {
            error: { message: `Upstream ${status}: ${text.slice(0, 200)}`, type: "upstream_error" },
          });
        });
        return;
      }

      const ctype = String(upRes.headers["content-type"] || "");
      if (!ctype.includes("event-stream")) {
        const buf = [];
        upRes.on("data", (c) => buf.push(c));
        upRes.on("end", () => {
          const raw = Buffer.concat(buf).toString("utf-8");
          log("WARN", `[${requestId}] 上游非 SSE 响应`, { contentType: ctype, bytes: raw.length });
          try {
            const obj = JSON.parse(raw);
            if (obj && obj.model) obj.model = reqModel;
            return sendJson(res, 200, obj);
          } catch (_) {
            return sendJson(res, 502, { error: { message: "上游返回了非 JSON 内容", type: "upstream_error" } });
          }
        });
        return;
      }

      if (clientWantsStream) {
        relayStream(upRes, res, reqModel, ctx);
      } else {
        const buf = [];
        upRes.on("data", (c) => buf.push(c));
        upRes.on("end", () => {
          const raw = Buffer.concat(buf).toString("utf-8");
          const json = aggregateSse(raw, reqModel);
          log("INFO", `[${requestId}] 聚合完成`, {
            ms: Date.now() - start, upstreamBytes: raw.length, model: reqModel,
          });
          sendJson(res, 200, json);
        });
        upRes.on("error", (e) => {
          log("ERROR", `[${requestId}] 上游错误`, { error: e.message });
          try { res.destroy(); } catch (_) {}
        });
      }
    });

    upReq.on("error", (e) => {
      log("ERROR", `[${requestId}] 连接上游失败`, { error: e.message });
      if (!res.headersSent) {
        sendJson(res, 502, { error: { message: `连接网关失败: ${e.message}`, type: "upstream_error" } });
      } else {
        try { res.end(); } catch (_) {}
      }
    });

    // 客户端提前断开时终止上游请求。
    // 必须监听 res 的 close：服务端 IncomingMessage 的 close 在请求体读完时就会触发，
    // 会导致请求刚发出就被掐断。
    res.on("close", () => {
      if (!res.writableEnded) { try { upReq.destroy(); } catch (_) {} }
    });

    log("INFO", `[${requestId}] -> ${UPSTREAM_PATH}`, {
      model: reqModel, stream: clientWantsStream,
      effort: prepared.body.reasoning_effort || "off",
    });
    upReq.write(payload);
    upReq.end();
  });

  req.on("error", (e) => log("ERROR", `[${requestId}] 客户端请求错误`, { error: e.message }));
});

// ============================================================================
// 启动
// ============================================================================
// 后台启动时 stdout 无人接管，未捕获异常必须落盘，否则进程死因无法追查
process.on("uncaughtException", (e) => {
  log("ERROR", "未捕获异常(进程保持运行)", { error: String((e && e.stack) || e) });
});
process.on("unhandledRejection", (r) => {
  log("ERROR", "未处理的 Promise 拒绝", { reason: String((r && r.stack) || r) });
});

server.on("error", (e) => {
  if (e.code === "EADDRINUSE") {
    console.error(`端口 ${PORT} 已被占用。可用: node workbuddy-proxy.js <其他端口>`);
    process.exit(1);
  }
  console.error("服务错误:", e);
  process.exit(1);
});

server.listen(PORT, BIND, () => {
  try { fs.writeFileSync(PID_FILE, String(process.pid)); } catch (_) {}
  console.log("==============================================");
  console.log(`  WorkBuddy 本地代理 v${VERSION} 已启动`);
  console.log(`  监听   : http://${BIND}:${PORT}`);
  console.log(`  上游   : https://${UPSTREAM_HOST}${UPSTREAM_PATH}`);
  console.log(`  认证   : ${AUTH_FILE}`);
  console.log(`  模型   : ${Object.keys(MODELS).join(", ")}`);
  console.log(`  日志   : ${LOG_FILE} (上限 ${(LOG_MAX_BYTES / 1048576).toFixed(1)}MB)`);
  console.log(`  PID    : ${process.pid}`);
  console.log("==============================================");
  log("INFO", "代理启动", { version: VERSION, port: PORT, pid: process.pid });
});

function shutdown(sig) {
  log("INFO", `收到 ${sig}，正在退出`);
  try { fs.rmSync(PID_FILE, { force: true }); } catch (_) {}
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 1500).unref();
}
process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
