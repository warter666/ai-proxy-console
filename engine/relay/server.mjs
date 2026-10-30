/**
 * AutoClaw GLM 反代服务 —— 把 AutoClaw 的模型算力反代为标准 Anthropic / OpenAI 端点
 *
 *   ZCode (Anthropic /v1/messages  或  OpenAI /v1/chat/completions)
 *     -> 本服务做 Anthropic <-> OpenAI 翻译
 *       -> 上游二选一（自动选，见 getUpstream）：
 *          1) http://127.0.0.1:<port>/internal/model-proxy   桌面端进程里的 Model Broker
 *          2) https://autoglm-api.autoglm.ai/autoclaw-proxy/proxy/autoclaw  云端直连
 *         -> GLM-5.3 / Auto / Auto-Fast / DeepSeek-V4-Pro
 *
 * 云端直连这条让"退出桌面端就断供"不再成立：桌面端没开时用本机凭证直接打云端同一个上游。
 * 两条路都要有凭证；都没有时返回 503，不挂死。
 *
 * 账号池（第三档）：A-SWITCH 每轮把 {uid,name,auth,points,expiring} 原子写进
 *   ~/.openclaw-autoclaw/aswitch_cloud_pool.json，这边**按请求**选号 ——
 *   先烧当天就要过期的份额、余额兜底、空闲摊平，撞见"额度打空/票失效/限流"就给那个号上冷却、
 *   当场换下一个号重试。于是"用完一个号的 2W 分自动切下一个号"不需要谁去点界面。
 *   开关：AUTOCLAW_POOL_MODE=off 退回"只有 broker / 单凭证"；AUTOCLAW_POOL_TRIES 每次换几个号。
 *   冷却与选号历史存 pool_state.json（重启不丢），池子里 auth 变了就自动解除票失效冷却。
 *
 * 零第三方依赖，仅用 Node 内置模块（bundled node v22 有全局 fetch）。
 * 默认监听 0.0.0.0:18766（非 loopback 时强制 Bearer 鉴权）。
 */
import os from "node:os";
import http from "node:http";
import https from "node:https";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { randomUUID, createHash } from "node:crypto";

// ---------------- 配置 ----------------
// 监听地址：默认 0.0.0.0（dsh 容器需经 192.168.65.254 访问）；
// 非 loopback 监听时强制 Bearer 鉴权（PROXY_TOKEN），安全性不受影响。
// 设 AUTOCLAW_BIND_HOST=127.0.0.1 可退回仅本机。
const HOST = process.env.AUTOCLAW_BIND_HOST || process.env.AUTOCLAW_HOST || "0.0.0.0";
const PORT = Number(process.env.AUTOCLAW_PORT || 18766);
const REQUIRE_AUTH = HOST !== "127.0.0.1" && HOST !== "::1" && HOST !== "localhost";
const PROXY_TOKEN = process.env.AUTOCLAW_PROXY_TOKEN || "autoclaw-dsh";
const STATE_DIR = process.env.AUTOCLAW_STATE_DIR || path.join(os.homedir(), ".openclaw-autoclaw");
const TOKEN_FILE = path.join(STATE_DIR, ".gateway-token");
const HEADERS_FILE = path.join(STATE_DIR, "request-headers.json");
const MODEL_CONFIG_URL = "https://autoglm-api.autoglm.ai/autoclaw-proxy/proxy/autoclaw-model-config";

const BASE_DIR = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"));
// 离线验收会同时起十几个实例，它们**必须**别往生产 server.log 里写：
// 09-21 02:48 就因此把"真机失败率"统计污染成假数据（测试号 A/B/C 的 429 混进了生产计数）。
const LOG_FILE = process.env.AUTOCLAW_LOG_FILE || path.join(BASE_DIR, "server.log");
const LOG_MAX = 2 * 1024 * 1024;

const DEFAULT_ROUTE = "zaicoding_glm-5.3";
// 兜底清单（云端模型配置 API 不可用时用）
const FALLBACK_ROUTES = [
  { id: "zaicoding_glm-5.3", name: "GLM-5.3", contextWindow: 1048576, maxTokens: 307200 },
  { id: "tdpsk_deepseek-v4-flash-202605", name: "Deepseek-V4.1-Flash", contextWindow: 1048576, maxTokens: 393216 },
  { id: "tdpsk_deepseek-v4-pro-202606", name: "DeepSeek-V4-Pro", contextWindow: 1048576, maxTokens: 393216 },
  { id: "zai_glm-5.3-flash", name: "GLM-5.3-Flash", contextWindow: 1048576, maxTokens: 131072 },
  { id: "zai_auto", name: "Auto", contextWindow: 1048576, maxTokens: 131072 },
  { id: "zai_auto-fast", name: "Auto-Fast", contextWindow: 1048576, maxTokens: 393216 },
];
// 对外暴露的友好别名（与 AutoClaw UI 的六个可选模型保持一致）。
// DSH 的 dsh-deepseek-* / deepseek-flash 兼容名仍由 DSH_ALIASES 解析，
// 但不放进公开清单，避免污染 ZCode 的模型选择列表。
const ALIASES = [
  "GLM-5.3",
  "Deepseek-V4.1-Flash",
  "DeepSeek-V4-Pro",
  "GLM-5.3-Flash",
  "Auto",
  "Auto-Fast",
];

// 显式模型名 -> route。要优先于下面的关键词正则：正则 /glm-5.3/ 会子串命中
// "glm-5.3-flash"，把 GLM-5.3-Flash 错送到 zaicoding_glm-5.3（免费额度已耗尽 →
// 403 → 降级到 zai_auto），而它的专属 route zai_glm-5.3-flash 实测可用且能读图。
const EXPLICIT_ROUTES = {
  "glm-5.3-flash": "zai_glm-5.3-flash",
  "glm-5.3": "zaicoding_glm-5.3",
  "auto": "zai_auto",
  "auto-fast": "zai_auto-fast",
  "deepseek-v4.1-flash": "tdpsk_deepseek-v4-flash-202605",
  "deepseek-v4-pro": "tdpsk_deepseek-v4-pro-202606",
  "deepseek-v4-flash": "tdpsk_deepseek-v4-flash-202605",
};

// 额度/付费类错误：不该降级到别的模型（用户选 GLM 却拿到 DeepSeek 且毫无察觉）。
// 810000 = 免费额度用尽；810002 = 需付费**或**高负载。
// 2026-09-20 拆分：810002 带 "high demand" 文案的是**瞬态高峰限流**（同一模型过会儿就能过），
// 与真·额度耗尽语义完全不同。旧版一律按额度拍死 400，ZCode 收到不可重试硬错误，
// 用户整段会话被反复打断。现在瞬态高峰走同模型重试，耗尽后返回 429（ZCode 会自动稍后重试），
// 绝不跨模型降级、绝不触碰其他反代（Qoder CN 只有 qwen3.8-flash 免费，其余烧积分，禁用）。
const QUOTA_ERROR_RE = /quota|used up|subscribe|insufficient|balance|810000|810002/i;
const TRANSIENT_PEAK_RE = /high\s*demand|810002/i;
// 真·额度耗尽判定（顺序敏感）：810002 高峰限流的官方文案必然捎带
// "upgrade ... subscription" 付费引导（实测 02:10 用户 21330 分仍被回 810002+pay-view），
// 所以 QUOTA_HARD_RE **不能**含 subscribe/balance 这类宽词，也不能先于 TRANSIENT 判定——
// 否则每次高峰限流都被当成"没钱"拍 400 终态，用户会话反复被打断。
// 只认明确额度耗尽信号：810000 码、quota/used up/insufficient 字样。
const QUOTA_HARD_RE = /quota|used\s*up|insufficient|810000/i;

const BROKER_TTL_MS = 45_000;      // broker 端口缓存
const BROKER_NEG_TTL_MS = 15_000;  // 找不到 broker 时的负缓存（避免频繁 tasklist/netstat）
const MODELS_TTL_MS = 600_000;     // 云端模型清单缓存
const UPSTREAM_TIMEOUT_MS = 600_000;
const DISCOVERY_TIMEOUT_MS = 4_000;
const MAX_BODY = 64 * 1024 * 1024;
// 上游 GLM 通道的 max_tokens 实测天花板（131072 全过 / 163840 起必 500）
const MAX_OUTPUT_TOKENS = process.env.AUTOCLAW_MAX_OUTPUT_TOKENS === undefined
  ? 131072
  : Number(process.env.AUTOCLAW_MAX_OUTPUT_TOKENS) || 0;
// 上游 5xx 容错：先同路由重试一次；默认不跨模型降级，避免用户选 DeepSeek/GLM
// 却静默拿到 Auto。确需降级时只能显式设置 AUTOCLAW_FALLBACK_ROUTE。
const RETRY_ON_UPSTREAM_ERROR = process.env.AUTOCLAW_NO_RETRY !== "1";
const FALLBACK_ROUTE = process.env.AUTOCLAW_FALLBACK_ROUTE === undefined
  ? ""
  : process.env.AUTOCLAW_FALLBACK_ROUTE;
// 瞬态高峰限流（810002 high demand）的同模型重试参数：总窗口 ~90s，
// 吸收 AutoClaw 上游的短高峰；全部失败返回 429 让 ZCode 稍后自动重试。
// 高峰限流退避：第一次只等 1 秒（配合账号池换号，多数抖动用户完全感觉不到），
// 后面逐级放大；总时长压在 60 秒内，别把一次请求拖成"看起来卡死了"。
// 可用 AUTOCLAW_PEAK_RETRY_MS="1000,2000" 调档（离线验收就靠它把等待压成 0）。
const PEAK_RETRY_DELAYS_MS = String(process.env.AUTOCLAW_PEAK_RETRY_MS || "1000,2000,4000,8000,15000,30000")
  .split(",").map((s) => Number(s.trim())).filter((n) => Number.isFinite(n) && n >= 0);

// 插嘴纪律注入（09-21 受控实验结论）：GLM 系引擎被训练成"任务中途不理会人类发言"
// ——不是 harness 不投递（ZCode 对所有供应商一样把插嘴排队到下个请求注入），
// 是模型侧把插嘴当背景噪音。往系统提示里加一条纪律，模型立刻就改为"下一步先应一声"。
// 只对带 tools 的请求注入（那是 agentic harness 的特征：Codex/ZCode/CC 都带）；
// 纯聊天/SDK 直连不带 tools，不打扰。AUTOCLAW_STEER=0 可关。
const STEER_TEXT = process.env.AUTOCLAW_STEER_TEXT || [
  "# Interjection protocol (MANDATORY)",
  "While you work, the user may send new messages (\"interjections\"), which arrive as user messages between your tool calls.",
  "1. At your very next step after an interjection appears — BEFORE any further tool call — FIRST acknowledge it explicitly: restate the requirement in one short line, e.g. 「收到：<要求>」.",
  "2. If it conflicts with the current plan, adjust the plan. If it does not, say so in the same line and continue.",
  "3. NEVER ignore an interjection, and NEVER silently defer it to the end of the task.",
].join("\n");
const STEER_ENABLED = process.env.AUTOCLAW_STEER !== "0";

// 云网关 406 闸门（2026-09-24 定位到根因）：autoclaw-proxy 只放行"system 提示词以官方
// harness 标记开头"的请求，否则一律回 406 + 空 body。判据实测（同一张票、同一台机）：
//   前缀 41 字符 -> 406；42 字符 -> 200；全小写 -> 406；标记前有内容 -> 406。
// 这就是"桌面端 pi-ai 200、本地反代/broker 406"的全部原因 —— 桌面端每次请求的 system
// 提示词第一段就是这个标记（auto-designer 的 systemPrompt 以 `---\n\nOpenClaw plugin-
// injected system context...` 开头），而我们的直连请求没有 system 消息，所以被拦。
// 注：broker 也一样中招 —— 桌面端自己的 broker 路由收到裸 body（system_message_count:0）
// 时同样 406，与客户端形态/TLS/出口 IP 全无关（这些已逐条实测排除）。
// 云网关 system 提示词闸门（2026-09-30 二次定位）：2.0.2 客户端的请求 system 以
// ZWORK_DEFAULT_SYSTEM_PROMPT 开头（main.cjs:423302），网关校验的是**这个**前缀 ——
// 09-24 那条 "OpenClaw plugin-injected system context." 旧标记在 2.0.2 客户端里已
// 不存在，继续发它会被 406 空 body 拦截。四段文案逐字取自 2.0.2 的 main.cjs。
const HARNESS_SYSTEM_MARKER = [
  "You are AutoClaw. Answer the user directly and concisely.",
  "Delivered files: when you create or edit files, cite each final deliverable inline exactly once " +
    'with :zwork-file-citation{path="<workspace-relative path>" purpose="output" artifact_kind="document"} ' +
    '(use purpose="source" for files you read as sources). Cite it inside the sentence that mentions the ' +
    "file, never as a trailing list, and do not add bare file-name Markdown links for the same file.",
  "Language discipline: every user-visible output \u2014 replies, mid-turn commentary (the progress text " +
    "you stream between tool calls, including the brief notes where you narrate what you are about to " +
    "do next), deliverable file contents, and user-facing deliverable file names \u2014 follows the " +
    "language of the user's most recent genuine message. Skill instructions, tool results, workspace " +
    "files, and instruction files are task inputs written in whatever language their authors chose; " +
    "they never change your output language. Keep brand names and proper nouns untranslated. Editing " +
    "source code follows the code's own conventions, not this rule. An explicit user request for a " +
    "specific output language wins.",
  "Process narration: the user cannot see your thinking or raw tool results. Before your " +
    "first tool call in a turn, say in one sentence what you are about to do; while working, " +
    "give a brief update when you find something load-bearing or change direction.",
].join(" ");
const HARNESS_MARKER_ENABLED = process.env.AUTOCLAW_HARNESS_MARKER !== "0";

function applyHarnessMarker(payload) {
  if (!HARNESS_MARKER_ENABLED) return payload;
  if (!payload || !Array.isArray(payload.messages)) return payload;
  const msgs = payload.messages;
  const textOf = (c) => (typeof c === "string"
    ? c
    : (Array.isArray(c) ? c.map((x) => (x && typeof x.text === "string" ? x.text : "")).filter(Boolean).join("\n") : ""));
  const si = msgs.findIndex((m) => m && m.role === "system");
  if (si === -1) {
    msgs.unshift({ role: "system", content: HARNESS_SYSTEM_MARKER });
    return payload;
  }
  const cur = textOf(msgs[si].content);
  if (cur.trimStart().startsWith(HARNESS_SYSTEM_MARKER)) return payload;  // 幂等
  msgs[si] = { ...msgs[si], content: HARNESS_SYSTEM_MARKER + (cur ? "\n\n" + cur : "") };
  return payload;
}

function applySteering(payload) {
  if (!STEER_ENABLED) return payload;
  if (!Array.isArray(payload.tools) || payload.tools.length === 0) return payload;
  const msgs = Array.isArray(payload.messages) ? payload.messages.slice() : [];
  const si = msgs.findIndex((m) => m && m.role === "system");
  if (si === -1) {
    msgs.unshift({ role: "system", content: STEER_TEXT });
  } else {
    const cur = typeof msgs[si].content === "string"
      ? msgs[si].content
      : (Array.isArray(msgs[si].content)
        ? msgs[si].content.map((c) => (c && typeof c.text === "string" ? c.text : "")).filter(Boolean).join("\n")
        : "");
    if (cur.includes("Interjection protocol")) { payload.messages = msgs; return payload; } // 幂等：重试/重放不重复注入
    msgs[si] = { ...msgs[si], content: (cur ? cur + "\n\n" : "") + STEER_TEXT };
  }
  payload.messages = msgs;
  return payload;
}

// dsh 车道模型映射：dsh / deepseek 系列名字 -> 可用 route。
// 语义必须对上：名字带 deepseek 就送 tdpsk_deepseek-*，不能落到 zai_auto
// （zai_auto 是 Auto 选路，实测会挑到别的模型，调用方以为在跑 deepseek）。
// 注意 dradar 的 pi-ai 发的是映射后的官方 slug `deepseek-flash`，不是格子名，
// 两种名字都得接。AUTOCLAW_DSH_ROUTE 若设置则整体覆盖（兼容旧用法）。
const DSH_OVERRIDE = process.env.AUTOCLAW_DSH_ROUTE || "";
const DSH_ROUTE_FLASH = DSH_OVERRIDE || "tdpsk_deepseek-v4-flash-202605";
const DSH_ROUTE_PRO = DSH_OVERRIDE || "tdpsk_deepseek-v4-pro-202606";
const DSH_ALIASES = {
  "dsh-deepseek-v4.1-flash": DSH_ROUTE_FLASH,
  "deepseek-v4.1-flash": DSH_ROUTE_FLASH,
  "deepseek-flash": DSH_ROUTE_FLASH,
  "deepseek-chat": DSH_ROUTE_PRO,
  "deepseek-v4-pro": DSH_ROUTE_PRO,
};

// ---------------- 日志 ----------------
let lastToken = "";
function scrub(s) {
  if (!s) return "";
  let out = String(s);
  if (lastToken) out = out.split(lastToken).join("***");
  return out.replace(/(Bearer\s+)[A-Za-z0-9._\-]{8,}/g, "$1***");
}
function log(...a) {
  const line = `[${new Date().toISOString()}] ` + a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" ");
  try { process.stdout.write(line + "\n"); } catch { /* ignore */ }
  try {
    const st = fs.statSync(LOG_FILE);
    if (st.size > LOG_MAX) fs.renameSync(LOG_FILE, LOG_FILE.replace(/\.log$/, ".old.log"));
  } catch { /* 文件不存在 */ }
  try { fs.appendFileSync(LOG_FILE, line + "\n", "utf8"); } catch { /* ignore */ }
}

// ---------------- 凭据 ----------------
function readGatewayToken() {
  try { lastToken = fs.readFileSync(TOKEN_FILE, "utf8").trim(); } catch { lastToken = ""; }
  return lastToken;
}
function readJwtHeaders() {
  try {
    const j = JSON.parse(fs.readFileSync(HEADERS_FILE, "utf8"));
    const h = j.headers || j;
    return { auth: h["X-Authorization"] || h["Authorization"] || h["authorization"] || "", clientType: h["X-Client-Type"] || "pc" };
  } catch { return { auth: "", clientType: "pc" }; }
}

// ---------------- broker 发现 ----------------
// 注意：tasklist / netstat 都是 Windows 控制台程序，每次调用都会新建一个控制台
// （即便 windowsHide，也会产生 conhost 开销，密集调用时表现为屏幕闪烁 + CPU 抖动）。
// 因此下面两个扫描都加了短 TTL 缓存，broker 发现再加单飞（single-flight）：
// 并发请求只会触发一次真实扫描。
const PID_TTL_MS = 10_000;      // AutoClaw 进程列表缓存
const NETSTAT_TTL_MS = 30_000;  // 监听端口列表缓存
const pidCache = { pids: new Set(), at: 0 };
const netstatCache = { out: "", at: 0 };
let brokerInflight = null;

function autoclawPidsScan() {
  try {
    const out = execFileSync("tasklist", ["/FI", "IMAGENAME eq AutoClaw.exe", "/FO", "CSV"], {
      encoding: "utf8", timeout: 15_000, windowsHide: true,
    });
    const pids = new Set();
    for (const line of out.split(/\r?\n/)) {
      const m = /^"AutoClaw\.exe"\s*,\s*"(\d+)"/.exec(line);
      if (m) pids.add(m[1]);
    }
    return pids;
  } catch { return new Set(); }
}

function autoclawPids() {
  const now = Date.now();
  if (now - pidCache.at < PID_TTL_MS) return pidCache.pids;
  pidCache.pids = autoclawPidsScan();
  pidCache.at = now;
  return pidCache.pids;
}

function netstatTcp() {
  const now = Date.now();
  if (now - netstatCache.at < NETSTAT_TTL_MS) return netstatCache.out;
  let out = "";
  try {
    out = execFileSync("netstat", ["-ano", "-p", "TCP"], {
      encoding: "utf8", timeout: 20_000, maxBuffer: 32 * 1024 * 1024, windowsHide: true,
    });
  } catch { out = ""; }
  netstatCache.out = out;
  netstatCache.at = now;
  return out;
}

function candidatePorts() {
  const ordered = [];
  const push = (p) => { const n = Number(p); if (n && !ordered.includes(n)) ordered.push(n); };
  // AUTOCLAW_BROKER_PORT 可手动锁定 broker 端口；配合 AUTOCLAW_BROKER_PORT_ONLY=1 则不做自动发现
  if (process.env.AUTOCLAW_BROKER_PORT) push(process.env.AUTOCLAW_BROKER_PORT);
  if (process.env.AUTOCLAW_BROKER_PORT_ONLY === "1") return ordered;
  const pids = autoclawPids();
  if (pids.size) {
    try {
      const out = netstatTcp();
      for (const line of out.split(/\r?\n/)) {
        if (!line.includes("LISTENING")) continue;
        const p = line.trim().split(/\s+/);
        if (p.length < 5) continue;
        const local = p[1], pid = p[4];
        if (!pids.has(pid)) continue;
        const host = local.replace(/:\d+$/, "");
        if (host.startsWith("127.") || host === "[::1]" || host === "::1") push(local.split(":").pop());
      }
    } catch { /* ignore */ }
  }
  for (const p of [18432, 19654, 19723, 53699]) push(p);
  return ordered;
}

async function isBroker(port, timeoutMs = DISCOVERY_TIMEOUT_MS) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`http://127.0.0.1:${port}/internal/model-proxy/v1/chat/completions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      signal: ctl.signal,
    });
    const txt = await r.text();
    return txt.includes("autoclaw_model_broker_error");
  } catch { return false; } finally { clearTimeout(t); }
}

const brokerCache = { base: "", at: 0 };
async function getBrokerBase(force = false) {
  const now = Date.now();
  if (!force && brokerCache.base && now - brokerCache.at < BROKER_TTL_MS) return brokerCache.base;
  // 负缓存：刚确认过 AutoClaw 不在，别每次请求都去跑 tasklist/netstat
  if (!force && !brokerCache.base && now - brokerCache.at < BROKER_NEG_TTL_MS) return "";
  // 单飞：并发请求共用同一次发现，避免每个请求都去 scans（tasklist/netstat）
  if (brokerInflight) return brokerInflight;
  brokerInflight = (async () => {
    const now0 = Date.now();
    const running = autoclawPids().size > 0;
    // AutoClaw 未运行：只快速探几个历史端口（1.5s 超时），避免逐端口等待导致请求挂住
    const ports = running || process.env.AUTOCLAW_BROKER_PORT_ONLY === "1"
      ? candidatePorts()
      : [Number(process.env.AUTOCLAW_BROKER_PORT) || 0, 18432, 19654, 19723, 53699].filter(Boolean);
    const timeout = running ? DISCOVERY_TIMEOUT_MS : 1_500;

    for (const port of ports) {
      if (await isBroker(port, timeout)) {
        const base = `http://127.0.0.1:${port}/internal/model-proxy`;
        if (base !== brokerCache.base) log("broker discovered:", base);
        brokerCache.base = base; brokerCache.at = Date.now();
        return base;
      }
    }
    brokerCache.base = ""; brokerCache.at = Date.now();
    return "";
  })().finally(() => { brokerInflight = null; });
  return brokerInflight;
}

// ---------------- 云端直连上游（桌面端没开时的替代） ----------------
// 桌面端里那段 Model Broker 的真实上游就是这个地址（ac_main.js:93776 / 93793）。
// 2026-09-20 23:42 实测：拿档案里的 access token + 官方那一套头，云端直接回 200，
// 完全不需要本地 broker —— 所以"退出桌面端就断供"这件事从今天起不成立了。
// 选择顺序：broker 在就走 broker（桌面端用自己的会话，最省事），不在才走云端。
// ⚠ X-Version 必须四段式（1.18.5.851）。写三段式（1.18.5）网关会拒，
//   而且回的是误导性的 {"error":"Invalid token"}，极易被误判成"凭证坏了"。
const CLOUD_LANES = {
  oversea: "https://autoglm-api.autoglm.ai/autoclaw-proxy/proxy/autoclaw",
  cn: "https://autoglm-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw",
};
const CLOUD_BASE = process.env.AUTOCLAW_CLOUD_BASE
  || CLOUD_LANES[process.env.AUTOCLAW_CLOUD_LANE || "oversea"] || CLOUD_LANES.oversea;
const CLOUD_VERSION = process.env.AUTOCLAW_CLIENT_VERSION || "1.18.5.851";
// 官方客户端发的是 Electron app.getVersion()，即 package.json 的三段版本（2.0.2），
// 不是 exe FileVersion 的四段（2.0.2.189）。网关只认它自己发出去的形状，所以对齐。
const CLOUD_VERSION_3 = String(CLOUD_VERSION).split(".").slice(0, 3).join(".");

// ---------------- 国内官方线（AutoClaw 2.x official channel，PR#4 PandaKing2021 逆向） ----------------
// 账号宇宙与海外不互通：国内号 token 打海外网关 401，反之亦然。一个进程同时服务两条线：
// 账号条目带 lane 字段（oversea=默认/aswitch_cloud_pool.json；cn=下方 auth-compat 凭证）。
const CN_CLOUD_BASE = process.env.AUTOCLAW_CN_CLOUD_BASE
  || "https://autoglm-api.zhipuai.cn/autoclaw-proxy/proxy/autoclaw";
// 国内 2.x 网关 X-Version 要四段式（三段式报误导性 "Invalid token"，与海外 2.0.2 相反）
const CN_VERSION = process.env.AUTOCLAW_CN_CLIENT_VERSION || CLOUD_VERSION;
// 凭证源：bridge/watch_auth.py 把 2.x 客户端 DPAPI 凭证解密成 1.x 形状写到这，跟着客户端轮换自动刷新
const CN_POOL_FILE = process.env.AUTOCLAW_CN_POOL_FILE
  || path.join(os.homedir(), ".autoclaw-relay", "auth-compat", "auth.json");
// 2.x 网关 system 闸门要求与应用 persona 完全一致（2026-10-04 W1/W2/T3 三方对照实锤：
// persona 原样→200；persona 合并任何额外内容→406；无 system→406）
const CN_PERSONA_FILE = process.env.AUTOCLAW_CN_PERSONA_FILE
  || path.join(BASE_DIR, "..", "bridge", "persona.txt");
let CN_PERSONA_CACHE = null;

// 2026-09-29 23:06 起网关新启用了请求指纹校验：官方客户端（dist/main.cjs 的
// createZworkManagedRequestHeaders）每个请求都带 appId+时间戳+md5 签名三元组，
// 凭证放在 Authorization 而不是 X-Authorization，x_trace_id 发 "autoclaw-desktop"、
// X-Channel 跟随 channel.json（本机 official）。缺这些头时一律回 **406 空 body** ——
// 和"system 缺 harness 标记"的 406 完全同形，整条池子会被误判成"号未放行"白等。
const AUTOCLAW_APP_ID = process.env.AUTOCLAW_APP_ID || "100003";
const AUTOCLAW_APP_KEY = process.env.AUTOCLAW_APP_KEY || "38d2391985e2369a5fb8227d8e6cd5e5";
const AUTOCLAW_CHANNEL = process.env.AUTOCLAW_CHANNEL || "official";
// 官方 2.0.2 的模型请求走 openai SDK（openai@6.26.0，见 resources/app/dist/main.cjs），
// SDK 自动附加这组指纹头；2026-09-29 23:06 起网关把它们也纳入校验，缺了回 406 空 body。
// Runtime-Version = Electron 42.6.1 内置 Node（process.version），OS/Arch 按本机。
const SDK_VERSION = process.env.AUTOCLAW_SDK_VERSION || "6.26.0";
// X-Stainless-Runtime-Version 跟随本机 node（桌面端 2.0.2 内嵌 node v24.18.0，
// 我们跑在同代 node 上，process.versions.node 形状一致；不一致时用桌面端值兜底）
const SDK_NODE_VERSION = process.env.AUTOCLAW_SDK_NODE_VERSION || `v${process.versions.node}`;
const SDK_UA = `OpenAI/JS ${SDK_VERSION}`;

// 2026-09-30 逐字节抓包（2.0.2 桌面端成功请求）定稿的头集合。token 新鲜度另见
// freshAccessToken()：chat 端点还校验 access token 的**铸造时间**（分钟级新鲜度，
// JWT 里的 24h exp 不算数），旧票一律 406 空 body / 401 "Invalid token"。

function cloudCredential() {
  const { auth } = readJwtHeaders();
  if (!auth) return "";
  return /^Bearer\s/i.test(auth) ? auth : `Bearer ${auth}`;
}

// broker 与云端是同一套约定：X-Request-Model 用带前缀的全名。
// 2026-09-30 起云端 body.model 也用全名：2.0.2 客户端的 connection.model 就是目录
// 全名（zai_glm-5.3-flash 等），body.model 与 X-Request-Model 同值。
function cloudBodyModel(route) { return route; }

// [asw-2x] 国内加速网关在 TLS/ALPN 层区分客户端：undici fetch 的 ClientHello 被 406，
// 同机同时刻 raw https + 相同头/体则 200（2026-10-03 同分钟 A/B 实锤，PR#4）。
// 国内线改走 node:https 原生请求，接口对齐 fetch（status/headers.get/text/json/body 流）。
function httpsFetch(urlStr, init = {}) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(urlStr); } catch (e) { return reject(e); }
    if (u.protocol !== "https:") return globalThis.fetch(urlStr, init).then(resolve, reject);
    const headers = { ...(init.headers || {}) };
    let body = init.body;
    if (body != null && typeof body !== "string" && !(body instanceof Buffer) && !(body instanceof Uint8Array)) {
      body = Buffer.from(String(body));
    }
    if (body != null) headers["content-length"] = String(Buffer.byteLength(body));
    const req = https.request({
      host: u.hostname,
      port: u.port || 443,
      path: u.pathname + u.search,
      method: init.method || "GET",
      headers,
      servername: u.hostname,
    }, (res) => {
      const hmap = new Map();
      for (const [k, v] of Object.entries(res.headers)) hmap.set(k.toLowerCase(), Array.isArray(v) ? v.join(", ") : v);
      let closed = false;
      let textBuf = "";
      let ended = false;
      const stream = new ReadableStream({
        start(ctrl) {
          res.on("data", (c) => { textBuf += c.toString("utf8"); if (!closed) ctrl.enqueue(new Uint8Array(c)); });
          res.on("end", () => { ended = true; if (!closed) { closed = true; ctrl.close(); } });
          res.on("error", (e) => { if (!closed) { closed = true; ctrl.error(e); } });
          res.pause();
        },
        pull() { res.resume(); },
        cancel() { closed = true; res.destroy(); },
      });
      const resp = {
        ok: res.statusCode >= 200 && res.statusCode < 300,
        status: res.statusCode,
        statusText: res.statusMessage || "",
        headers: { get: (k) => hmap.get(String(k).toLowerCase()) ?? null },
        body: stream,
        text: () => new Promise((r2, j2) => {
          if (ended) return r2(textBuf);
          res.once("end", () => r2(textBuf));
          res.once("error", j2);
          res.resume();
        }),
      };
      resp.json = () => resp.text().then((s) => JSON.parse(s));
      resolve(resp);
    });
    req.on("error", reject);
    if (init.signal) {
      const sig = init.signal;
      if (sig.aborted) req.destroy(sig.reason || new Error("aborted"));
      else sig.addEventListener("abort", () => req.destroy(sig.reason || new Error("aborted")), { once: true });
    }
    if (body != null) req.write(body);
    req.end();
  });
}

// [asw-2x] 国内线请求体/系统闸门（2.x 网关 406 三要素，PR#4 对照实验定案）：
//  reasoning_effort 必须 "high"、max_completion_tokens 模型满额 307200、store:false；
//  system 必须与应用 persona **完全一致**——整体替换，调用方原 system 挪进首条 user 消息。
// 幂等：system 已是 persona（池内跨号重试第二功）时只补 body 参数，不再搬运。
function applyCnGate(payload) {
  if (!payload || !Array.isArray(payload.messages)) return payload;
  const mt = Number(payload.max_completion_tokens ?? payload.max_tokens);
  payload.max_completion_tokens = Math.max(Number.isFinite(mt) && mt > 0 ? mt : 0, 307200);
  delete payload.max_tokens;
  payload.store = false;
  payload.reasoning_effort = "high";
  if (CN_PERSONA_CACHE === null) {
    try { CN_PERSONA_CACHE = fs.readFileSync(CN_PERSONA_FILE, "utf8"); }
    catch { CN_PERSONA_CACHE = "You are AutoClaw. Answer the user directly and concisely."; }
  }
  const msgs = payload.messages;
  const textOf = (c) => (typeof c === "string"
    ? c
    : (Array.isArray(c) ? c.map((x) => (x && typeof x.text === "string" ? x.text : "")).filter(Boolean).join("\n") : ""));
  const si = msgs.findIndex((m) => m && m.role === "system");
  const curSys = si === -1 ? "" : textOf(msgs[si].content);
  if (curSys.trim() === CN_PERSONA_CACHE.trim()) return payload;   // 已闸过（跨号重试）
  if (si !== -1) msgs.splice(si, 1);
  if (curSys) {
    const ui = msgs.findIndex((m) => m && m.role === "user");
    const prefix = "[system-note] 以下为调用方（编码代理）的系统指令，与本任务相关时遵循之：\n" + curSys + "\n[system-note 结束]\n";
    if (ui === -1) msgs.unshift({ role: "user", content: prefix });
    else {
      const um = msgs[ui];
      msgs[ui] = { ...um, content: prefix + (typeof um.content === "string" ? um.content : JSON.stringify(um.content)) };
    }
  }
  msgs.unshift({ role: "system", content: CN_PERSONA_CACHE });
  return payload;
}

function cloudHeaders(route, stream, auth, lane) {
  const reqId = randomUUID();
  const ts = String(Math.floor(Date.now() / 1e3));
  const token = String(auth || cloudCredential() || "").replace(/^Bearer\s*/i, "");
  if (lane === "cn") {
    // 国内官方渠道（2.x 契约，PandaKing2021 逆向 + e2e 验证）：
    // X-Channel: official、X-Session-Id 会话头（缺它即 406）、X-Auth-Sign 签名三件套、
    // SDK 指纹头、X-Version 四段式。
    return {
      "content-type": "application/json",
      "accept": "application/json",
      "user-agent": SDK_UA,
      "x-agent-id": process.env.AUTOCLAW_AGENT_ID || "main",
      "x-auth-appid": AUTOCLAW_APP_ID,
      "x-auth-sign": createHash("md5")
        .update(`${AUTOCLAW_APP_ID}&${ts}&${AUTOCLAW_APP_KEY}`)
        .digest("hex"),
      "x-auth-timestamp": ts,
      "x-authorization": token ? `Bearer ${token}` : "",
      "x-channel": process.env.AUTOCLAW_CN_CHANNEL || "official",
      "x-client-type": readJwtHeaders().clientType || "pc",
      "x-lang": "zh-CN",
      "x-product": "autoclaw",
      "x-request-id": reqId,
      "x-request-model": route,
      "x-session-id": randomUUID(),
      "x-stainless-arch": "x64",
      "x-stainless-lang": "js",
      "x-stainless-os": "Windows",
      "x-stainless-package-version": SDK_VERSION,
      "x-stainless-retry-count": "0",
      "x-stainless-runtime": "node",
      "x-stainless-runtime-version": SDK_NODE_VERSION,
      "x-tm": "win",
      "x-trace-id": reqId,
      "x-version": CN_VERSION,
    };
  }
  const headers = {
    "accept": "application/json",
    "content-type": "application/json",
    "user-agent": SDK_UA,
    // 2026-09-30 抓包新发现：桌面端模型请求必带 x-agent-id（主 agent 固定 "main"），
    // 缺它即 406。
    "x-agent-id": process.env.AUTOCLAW_AGENT_ID || "main",
    "x-auth-appid": AUTOCLAW_APP_ID,
    "x-auth-sign": createHash("md5")
      .update(`${AUTOCLAW_APP_ID}&${ts}&${AUTOCLAW_APP_KEY}`)
      .digest("hex"),
    "x-auth-timestamp": ts,
    // 凭证只挂 x-authorization（桌面端 resolveAuthorization 显式置 Authorization: null，
    // 网关只从 x-authorization 读票；发成 authorization 报 "Invalid token"）。
    "x-authorization": token ? `Bearer ${token}` : "",
    "x-channel": AUTOCLAW_CHANNEL,
    "x-client-type": readJwtHeaders().clientType || "pc",
    "x-lang": "zh-CN",
    "x-product": "autoclaw",
    "x-request-id": reqId,
    "x-request-model": route,
    "x-session-id": randomUUID(),
    "x-stainless-arch": "x64",
    "x-stainless-lang": "js",
    "x-stainless-os": "Windows",
    "x-stainless-package-version": SDK_VERSION,
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "node",
    "x-stainless-runtime-version": SDK_NODE_VERSION,
    "x-tm": "win",
    "x-trace-id": reqId,
    "x-version": CLOUD_VERSION_3,
    "x_trace_id": "autoclaw-desktop",
    // ⚠ 不要加 "X-Harness-Type": "zcode"（2026-09-24 实测反证：带它 -> 406 空 body）。
    // ⚠ 不要发 X-Stainless-Timeout / authorization（桌面端不发/显式置空，多发即 406）。
  };
  if (!headers["x-authorization"]) delete headers["x-authorization"];
  return headers;
}

async function callCloud(route, payload, { stream }, acc, externalSignal) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), UPSTREAM_TIMEOUT_MS);
  // 客户端断开（externalSignal）或超时，任一触发都中止上游——断开不传播会白烧账号额度
  const onExternalAbort = () => ctl.abort();
  if (externalSignal) {
    if (externalSignal.aborted) ctl.abort();
    else externalSignal.addEventListener("abort", onExternalAbort, { once: true });
  }
  try {
    const lane = (acc && acc.lane) === "cn" ? "cn" : "oversea";
    // 国内线：persona 闸门在账号选定后应用（system 整体替换，handler 层预置的 harness
    // 标记随原 system 一起被替换掉，跨线重试时两边闸门各自成立）。海外线：标记已在
    // handler 层应用（幂等），broker/单凭证路径也依赖它，保持原位不动。
    let body = payload;
    if (lane === "cn") body = applyCnGate(payload);
    // 现刷票只用于海外线（PR#3）；国内票由 watch_auth 跟随 2.x 客户端轮换，保持新鲜
    // 2026-10-22 修正：CN 线也必须现刷。网关校验 access token 的铸造时间（分钟级），
// 静态票放几小时就 401 "Invalid token"（全池冷却）。refresh 对 CN 号 code=0（zhipuai.cn）。
const freshAuth = acc ? await freshAccessToken(acc) : null;
    const send = lane === "cn" ? httpsFetch : fetch;
    const base = lane === "cn" ? CN_CLOUD_BASE : CLOUD_BASE;
    return await send(`${base}/chat/completions`, {
      method: "POST",
      headers: cloudHeaders(route, stream, freshAuth || (acc && acc.auth), lane),
      body: JSON.stringify({ ...body, model: cloudBodyModel(route) }),
      signal: ctl.signal,
    });
  } finally {
    clearTimeout(t);
    if (externalSignal) externalSignal.removeEventListener("abort", onExternalAbort);
  }
}

// ---------------- chat 端点 token 新鲜度闸门（2026-09-30 定位） ----------------
// 2.0.2 网关对 /chat/completions 校验 access token 的**铸造时间**（分钟级；JWT exp
// 写 24h 但网关不认）。桌面端每次模型请求前都由 credentialLifecycle 现刷一票，所以
// 永远新鲜；relay 拿池子里的静态票（可能几小时前的）一律 406 空 body（x-authorization
// 挂旧票）或 401 "Invalid token"（authorization 挂旧票）。
// 对策：每次云端调用前先打 /userapi/v1/refresh 现刷（body: {refresh_token, source_id:
// "autoclaw", device_id}，与桌面端逐字一致），单飞 + 60s 复用窗防止并发重复刷。
// 刷新是**事件驱动**（仅在实际有模型请求时发生），不设任何定时器——红线 1。
const FRESH_TTL_MS = Number(process.env.AUTOCLAW_FRESH_TTL_MS || 60_000);
const freshCache = new Map();   // uid -> { token, at, inflight }
async function freshAccessToken(acc) {
  const uid = acc.uid || "?";
  const c = freshCache.get(uid);
  if (c && !c.inflight && Date.now() - c.at < FRESH_TTL_MS && c.token) return c.token;
  if (c && c.inflight) return c.inflight;
  const refreshToken = acc.refresh_token || "";
  const deviceId = acc.device_id || "";
  if (!refreshToken) return null;   // 老池子没这字段，退回静态票
  const entry = { token: c ? c.token : null, at: c ? c.at : 0, inflight: null };
  entry.inflight = (async () => {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 15_000);
      // CN 号的 refresh 必须打 zhipuai.cn（autoglm.ai 对 CN 凭证回 400000）
      const u = new URL(acc.lane === "cn" ? CN_CLOUD_BASE : CLOUD_BASE);
      const ts2 = String(Math.floor(Date.now() / 1e3));
      const r = await fetch(`${u.origin}/userapi/v1/refresh`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "accept": "*/*",
          "user-agent": "node",
          "x-auth-appid": AUTOCLAW_APP_ID,
          "x-auth-sign": createHash("md5")
            .update(`${AUTOCLAW_APP_ID}&${ts2}&${AUTOCLAW_APP_KEY}`)
            .digest("hex"),
          "x-auth-timestamp": ts2,
          "x-channel": AUTOCLAW_CHANNEL,
          "x-lang": "zh-CN",
          "x-product": "autoclaw",
          "x-tm": "win",
          "x-trace-id": randomUUID(),
          "x-version": CLOUD_VERSION_3,
          "authorization": /^Bearer\s/i.test(acc.auth || "") ? acc.auth : `Bearer ${acc.auth || ""}`,
        },
        body: JSON.stringify({ refresh_token: refreshToken, source_id: "autoclaw", device_id: deviceId }),
        signal: ctl.signal,
      });
      clearTimeout(t);
      if (!r.ok) { log(`fresh token: refresh http ${r.status} for ${uid}`); return null; }
      const j = await r.json();
      if (j.code !== 0 || !j.data || !j.data.access_token) {
        log(`fresh token: refresh biz fail for ${uid}: code=${j.code}`);
        return null;
      }
      const tok = String(j.data.access_token).replace(/^Bearer\s+/i, "");
      entry.token = tok;
      entry.at = Date.now();
      log(`fresh token: minted for ${uid} (len ${tok.length})`);
      return tok;
    } catch (e) {
      log(`fresh token: refresh error for ${uid}: ${e.message}`);
      return null;
    } finally {
      entry.inflight = null;
      freshCache.set(uid, entry);
    }
  })();
  freshCache.set(uid, entry);
  return entry.inflight;
}

// ---------------- 账号池：按请求选号（用完一个号的积分自动换下一个） ----------------
// 数据面在 A-SWITCH 那边：它每轮刷新把 {uid, name, auth, points, expiring} 原子写到
// aswitch_cloud_pool.json（明文 access token，和桌面端自己写的 request-headers.json 同级）。
// 这边只做三件事：排序选号、按错误类型给单个号上冷却、失败时换下一个号重试。
// 为什么不在这里刷新 refreshToken：签名与 DPAPI 存档都在 A-SWITCH 侧，node 只该拿现成的票。
const POOL_FILE = process.env.ASWITCH_POOL_FILE || path.join(STATE_DIR, "aswitch_cloud_pool.json");
const POOL_STATE_FILE = process.env.AUTOCLAW_POOL_STATE_FILE || path.join(BASE_DIR, "pool_state.json");
const POOL_MODE = (process.env.AUTOCLAW_POOL_MODE || "auto").toLowerCase();  // auto | prefer | off
const POOL_TRIES = Math.max(1, Number(process.env.AUTOCLAW_POOL_TRIES || 5));  // 一次请求最多换几个号
const POOL_MAX_ROUNDS = POOL_TRIES + 3;        // 换号 + 排队等待的总轮次上限（防死循环）
// 全池都在冷却时，最多替用户排队等这么久，等不到就快速失败让 ZCode 自己重试。
// 宁等 6 秒成功，不要 60 秒轰炸后仍然失败 —— 后者正是 02:40 那轮"越来越频繁"的成因。
const POOL_WAIT_BUDGET_MS = Math.max(0, Number(
  process.env.AUTOCLAW_POOL_WAIT_MS === undefined ? 6000 : process.env.AUTOCLAW_POOL_WAIT_MS));
// 池子已经换过号了，上层就别再按 6 轮 60 秒退避：那样一次请求能砸出 30 发上游调用。
const PEAK_DELAYS_WITH_POOL = PEAK_RETRY_DELAYS_MS.slice(0, 2);
const POOL_TOPK = 3;                    // 分数前 K 名里再挑最久没用的（把请求摊开）
// 池子文件的"新鲜度"闸门：A-SWITCH 每 10 秒刷一次，超过这个时长没动就是它没在跑，
// 里面的票大概率已经过期 → 退回单凭证那条路，别拿一堆死票去撞 401。
const POOL_STALE_MS = Number(process.env.AUTOCLAW_POOL_STALE_MS || 2 * 3600_000);
const COOLDOWN_MS = {
  exhausted: 0,         // 0 = 到下一个每日刷新点（额度按天发，提前放出来只会再撞一次）
  throttle: 20_000,     // 撞到付费墙但账上还有分：这是限流不是没钱，20 秒就放回来再试
  auth: 30 * 60_000,    // 票被轮换掉了 / 账号被冻：A-SWITCH 下一轮刷新会写好新票，届时提前解除
  transient: 60_000,    // 429/5xx：只躲一分钟，别把高峰当成没额度
  // 410004 = 账号级终态封禁（09-22 流水取证：业务面照常 200，只有推理面被切断，无自助解封）。
  // 之前它落到 auth 那一档 → 30 分钟后又被选号挑中 → 每个请求白撞一跳死号（实测 5 个死号
  // 反复进入候选）。封禁不会自愈，所以给一个长隔离窗，且 A-SWITCH 换票也不提前解除。
  banned: Number(process.env.AUTOCLAW_BANNED_COOLDOWN_MS || 12 * 3600_000),
  // 406 = 该号推理面尚未放行（09-23 取证：新注册号的票打 chat 一律 406 空响应体，
  // 同形请求老号能进到配额检查 402）。不是请求形状问题、不是 IP 问题（VPS 换出口同 406），
  // 是服务端对新号的放行延迟（某号 时间线吻合：09-21 注册、09-22 才可推理）。
  // 给一个中等冷却：本次请求换下一个号，且短期内别再反复撞它白烧配额。
  notprovisioned: Number(process.env.AUTOCLAW_NOTPROV_COOLDOWN_MS || 30 * 60_000),
};
// 家宽回源兜底（09-21 A+D 方案）：VPS 机房 IP 可能被上游按"号池模式"标记（410004 团灭），
// 而家宽 IP 同票能通。全池失败时，若配置了 AUTOCLAW_HOME_FALLBACK_URL（指向本机反代），
// 把请求转发到本机再回给用户——本机可达 = 站照常供血；本机也挂 = 如实报错，不静默装死。
// 安全：回源 URL 只在服务端环境变量里，永不回显；转发时替换 Authorization 为本机实例的 PROXY_TOKEN。
const HOME_FALLBACK_URL = process.env.AUTOCLAW_HOME_FALLBACK_URL || "";
const HOME_FALLBACK_TOKEN = process.env.AUTOCLAW_HOME_FALLBACK_TOKEN || "";
// 回源健康状态（内存态）：成功/失败都记住，失败连续 3 次后停 5 分钟再试（不把死链当活马医）
const homeFall = { ok: 0, fail: 0, cooldownUntil: 0, lastAt: 0, lastStatus: 0 };
const HOME_FALLBACK_COOLDOWN_MS = 5 * 60_000;

// ---------------- 单号限速（防 09-21 封号指纹在真实流量下复发） ----------------
// 封号证据：服务端积分流水显示被封号 15:19→15:43 **每分钟精确 20 次、4 模型固定顺序、
// 秒数固定在 03/04/05/07、连续 25 分钟 = 500 次**，然后同一分钟被批量封。
// 那次元凶是状态页探针（已改为零出站）。但真实用户并发同样能重现这种节奏，所以在池子层加三道闸：
//   1) 滑动窗口限速：单号每 60s 最多 N 次（默认 12，留足真人节奏空间，砍掉机器尾巴）
//   2) 抖动：同一号被连续挑中时插入随机间隔，抹平"秒级固定"特征
//   3) 模型多样性：同一号优先给"最久没用过的模型"，避免一个号长时间只被一个模型打
const POOL_RATE_PER_MIN = Math.max(0, Number(process.env.AUTOCLAW_POOL_RATE_PER_MIN === undefined
  ? 12 : process.env.AUTOCLAW_POOL_RATE_PER_MIN));   // 0 = 关闭限速
const POOL_RATE_WINDOW_MS = 60_000;
const POOL_JITTER_MIN_MS = Number(process.env.AUTOCLAW_POOL_JITTER_MIN_MS || 350);
const POOL_JITTER_MAX_MS = Number(process.env.AUTOCLAW_POOL_JITTER_MAX_MS || 1600);

// 全局并发上限（保命机制）：封号的死法 B 是"单号/单池被并发 pile-on 打死"
// （09-22 同一秒 9~10 条完全相同的 30 万字符请求）。上游一个账号同一时刻能吃多少并发有限，
// 超过就按"异常批量行为"标记。这里把整池对上游的并发卡死在一个安全值，从根上掐掉 pile-on。
// 默认 4 = 留足真人 agent 的长工具链（Grok/Codex 车道会开多条），又不给"秒级 10 连发"留门。
const POOL_MAX_CONCURRENCY = Math.max(1, Number(process.env.AUTOCLAW_POOL_MAX_CONCURRENCY === undefined
  ? 4 : process.env.AUTOCLAW_POOL_MAX_CONCURRENCY));   // 0 = 不限制
// 单请求输入硬上限（字符数）。09-22 的元凶是 510k~516k 字符的单体巨请求**反复重放**，
// 把单个号打成 pay-view 限流后轮转团封。防那种"机器批量行为"的真护栏是限速 12/min、
// 并发 4、日额度三道闸（仍在）；字符闸是误伤真实长上下文任务的钝器，2026-09-24 按用户
// 要求提到 1M 字符（≈500k token 窗口，覆盖 649k 字符的长会话还有余量）。0 = 不限制。
const MAX_INPUT_CHARS = Math.max(1024, Number(process.env.AUTOCLAW_MAX_INPUT_CHARS === undefined
  ? 1_000_000 : process.env.AUTOCLAW_MAX_INPUT_CHARS));   // 0 = 不限制
let inflightUpstream = 0;   // 当前真实打向上游的在途数（并发闸用）
// 并发满时**直接拒绝**（返回 false），不排队：排队会把洪峰串行化但拖慢正常请求，
// 而拒绝让超额请求立刻拿到 429（ZCode 重试/降级），在途的 <=N 路正常请求零延迟。
// 真正的"总吞吐"限制交给 12/min 滑窗限速（占位修复后真生效），两道闸互补。
function acquireUpstreamSlot() {
  if (inflightUpstream < POOL_MAX_CONCURRENCY) {
    inflightUpstream++;
    return Promise.resolve(true);
  }
  return Promise.resolve(false);
}
function releaseUpstreamSlot() {
  inflightUpstream = Math.max(0, inflightUpstream - 1);
}
const rateWin = new Map();          // uid -> [ts...]（升序）
const lastUse = new Map();          // uid -> 上次真实发出的时刻
const lastRouteUse = new Map();     // uid -> { route -> 时刻 }（模型多样性打分用）

// 单号每日调用上限（09-22 取证后加，09-22 深夜二次校准）。
// 初版 400 的依据是"死者一天冲到 750+"——但那是探针时代的量。现在占位修复后重看账本：
// 三个活号当天各 6000 条【全真人形】推理、1h 峰 2820~3240，全活着 → 日总量本身不是红线。
// cap 的真实职责只剩"防单号累计失控兜底"，太保守会误杀高强度自用（且 dailyWin 已持久化，
// 不再靠重启洗白放行）。按活号实证 6000/天不死取 ~5 倍余量 → 默认 1200。
// 注意：真正的死因 B 防线是 12/min 滑窗限速（占位修复后才真生效），不是这个日预算。
// 2026-09-24 用户定 AutoClaw 为反代主力：1200 太保守，高强度自用当天就顶死（实测两号 1193/1197）。
// 提到 6000（对齐注释里"活号当天 6000 条全真人形不死"的实证）。日预算只是兜底，
// 防封的真护栏仍是 12/min 限速 + 并发 4 + 输入 1M 字符闸，三道未动。0 = 不限。
const POOL_DAILY_CAP = Math.max(0, Number(process.env.AUTOCLAW_POOL_DAILY_CAP === undefined
  ? 6000 : process.env.AUTOCLAW_POOL_DAILY_CAP));   // 0 = 不限
const DAILY_WINDOW_MS = 24 * 60 * 60_000;
const dailyWin = new Map();         // uid -> [ts...]（24h 内真实发出时刻）
/** 该号今天的日预算还剩几个。 */
function dailyLeft(uid, now = Date.now()) {
  if (!POOL_DAILY_CAP) return Infinity;
  const arr = dailyWin.get(uid);
  if (!arr || !arr.length) return POOL_DAILY_CAP;
  const cutoff = now - DAILY_WINDOW_MS;
  while (arr.length && arr[0] <= cutoff) arr.shift();
  return Math.max(0, POOL_DAILY_CAP - arr.length);
}
/** 该号今天已经真实发出几次（全池超预算时按它做公平排序，把溢出落在最轻的号上）。 */
function dailyUsed(uid, now = Date.now()) {
  const arr = dailyWin.get(uid);
  if (!arr || !arr.length) return 0;
  const cutoff = now - DAILY_WINDOW_MS;
  while (arr.length && arr[0] <= cutoff) arr.shift();
  return arr.length;
}

/** 该号在本窗口内还剩几个配额。 */
function rateSlotsLeft(uid, now) {
  if (!POOL_RATE_PER_MIN) return Infinity;
  const arr = rateWin.get(uid);
  if (!arr || !arr.length) return POOL_RATE_PER_MIN;
  const cutoff = now - POOL_RATE_WINDOW_MS;
  while (arr.length && arr[0] <= cutoff) arr.shift();
  return POOL_RATE_PER_MIN - arr.length;
}

/** 该号要等到什么时候才有新配额（0 = 现在就有）。 */
function rateReadyInMs(uid, now) {
  if (!POOL_RATE_PER_MIN) return 0;
  const arr = rateWin.get(uid);
  if (!arr || !arr.length || arr.length < POOL_RATE_PER_MIN) return 0;
  const cutoff = now - POOL_RATE_WINDOW_MS;
  while (arr.length && arr[0] <= cutoff) arr.shift();
  if (arr.length < POOL_RATE_PER_MIN) return 0;
  return Math.max(0, arr[0] + POOL_RATE_WINDOW_MS - now);
}

// ⚠️ 并发绕过修复（2026-09-22 某号 死因取证）：原 rateRecord 在 callCloud 之后调用，
//   而思考模型单次 30~60s —— 这期间并发请求查 rateSlotsLeft 时这一次还没入账，全部放行，
//   实测单号 60s 峰 240（配置只有 12），某号 死前 720。限速闸被并发整个绕过。
//   修法：挑定号后、进 await 之前**同步占位**（rateReserve），使 check+占位 处于同一微任务，
//   并发请求看不到可钻的空隙；日预算仍在完成后提交（commitDaily），连接失败则释放占位。
function rateReserve(uid, now) {
  lastUse.set(uid, now);
  if (!POOL_RATE_PER_MIN) return false;
  const arr = rateWin.get(uid) || [];
  arr.push(now);
  const cutoff = now - POOL_RATE_WINDOW_MS;
  let i = 0; while (i < arr.length && arr[i] <= cutoff) i++;
  if (i) arr.splice(0, i);
  rateWin.set(uid, arr);
  return true;
}
/** 释放一次占位（连接层抛错、请求可能没到上游时用）。 */
function rateRelease(uid) {
  const arr = rateWin.get(uid);
  if (arr && arr.length) arr.pop();
}
/** 完成后提交日预算 + 模型多样性时间戳（含 4xx/5xx —— 那是上游真实见到的一次请求）。 */
function commitDaily(uid, now, route) {
  if (route) {
    let m = lastRouteUse.get(uid);
    if (!m) { m = new Map(); lastRouteUse.set(uid, m); }
    m.set(route, now);
  }
  if (POOL_DAILY_CAP) {
    const d = dailyWin.get(uid) || [];
    d.push(now);
    const cutoffD = now - DAILY_WINDOW_MS;
    let j = 0; while (j < d.length && d[j] <= cutoffD) j++;
    if (j) d.splice(0, j);
    dailyWin.set(uid, d);
    poolStateDirty = true;     // 日预算落盘：重启不再洗白（某号 死法之一是重启窗口 pile-on）
  }
}


// ---------------- lane 级故障切换（09-21 402 积分不足） ----------------
// 症状：ZCode 收到 `AutoClaw Broker 返回 402（已重试 2 次）: {"message":"积分不足,请充值"}`。
// 根因：broker（桌面端那条 lane）的账号打空后，QUOTA_HARD_RE 是英文/数字口径，
// **中文「积分不足」一条都不匹配** → 既没判成额度耗尽，也没换 lane，
// 只按普通失败把同一个死号重试 2 次，最后把 402 包成 502 抛给用户。
// 修法：broker 回额度类错误（402 / 中文积分不足 / 810000）时，把整条 broker lane 标记冷却，
// 当前请求**当场**改用账号池（池内按请求换号 = 自动换到另一个账号的积分），用户无感。
// 冷却结束后自动再试 broker —— 桌面端充值/次日刷新后无需人工干预即可回到最稳的那条路。
const BROKER_DEAD_MS = Math.max(0, Number(process.env.AUTOCLAW_BROKER_DEAD_MS === undefined
  ? 5 * 60_000 : process.env.AUTOCLAW_BROKER_DEAD_MS));
const BROKER_DEAD_RE = /积分不足|请充值|余额不足|insufficient|no\s+credit|out\s+of\s+credit|810000|quota|used\s*up/i;
const brokerLane = { deadUntil: 0, reason: "", deadCount: 0, switches: 0 };
function brokerLaneDead(now = Date.now()) { return now < brokerLane.deadUntil; }
function markBrokerDead(detail) {
  brokerLane.deadUntil = Date.now() + BROKER_DEAD_MS;
  brokerLane.reason = String(detail || "").replace(/\s+/g, " ").slice(0, 120);
  brokerLane.deadCount++;
  log(`lane: broker 额度打空（${brokerLane.reason}）→ 冷却 ${Math.round(BROKER_DEAD_MS / 1000)}s，本请求及后续改走账号池`);
}
/** broker 死了且池子可用时，返回切过去的上游描述；否则 null。 */
function laneSwitchTarget() {
  if (POOL_MODE === "off") return null;
  if (!poolCandidates().length) return null;
  return { kind: "cloud", base: CLOUD_BASE, pool: true };
}
/** 给 /health 用的 lane 快照。 */
function brokerLaneSnapshot() {
  const now = Date.now();
  return {
    dead: brokerLaneDead(now),
    until_s: brokerLane.deadUntil > now ? Math.round((brokerLane.deadUntil - now) / 1000) : 0,
    reason: brokerLane.reason,
    dead_count: brokerLane.deadCount,
    switches: brokerLane.switches,
  };
}

/** broker 返回额度类错误？是则标记 lane 冷却并告知调用方可以换 lane 重试。 */
function brokerHitQuota(up, detail) {
  if (!up || up.kind !== "broker") return false;
  if (!BROKER_DEAD_RE.test(detail)) return false;
  markBrokerDead(detail);
  return true;
}
// 只认"这个号确实没票了"的口径。宁可漏判（多撞一次）也别错判（把可用号锁到明天）。
const POOL_EXHAUST_RE = /积分不足|余额不足|额度.{0,6}(不足|已用尽|用完)|insufficient\s+(balance|quota|credit|points)|used\s+up|quota\s+exceeded|exceeded\s+your\s+current\s+quota|810000/i;
// 真机抓到的付费墙回包（09-21 02:04，HTTP 403）：{"action":{"kind":"pay-view"},"code":810002,...}
// 只认 pay-view 这个标记：810002 本身还用于"上游高峰"，按高峰走退避才对，不能当打空锁掉。
const POOL_PAYVIEW_RE = /pay-view/i;

const pool = { mtime: 0, accounts: [], clientVersion: "" };
// ⚠️ 票本身绝不进 pool_state.json：那个文件在工程目录里（可能被同步/被别的工具读到），
//    只需要知道"这张票还是不是冷却时那张"，所以存指纹。
const authFp = (auth) => createHash("sha256").update(String(auth || "")).digest("hex").slice(0, 12);
// 冷却分两个维度（09-21 18:0x 实测修正）：
//   byUid   —— 账号级故障：票失效(auth)、限流(throttle)、发不出去(transient)。这些与模型无关，全模型共享。
//   byRoute —— **模型级额度**：真机抓到同一个号同一时刻 GLM-5.3-Flash=200 / GLM-5.3=403 810000 /
//              DeepSeek-V4-Pro=200 —— 额度是按模型分的，一次满血模型的 810000 不该把该号上
//              所有模型锁到明天。这个 bug 让池子里唯一可用的号被一个不相关的模型锁死，
//              表现为"池子 7 个号全冷却、一个都接不了请求"。
const pstate = { byUid: {}, byRoute: {}, lastPick: {}, loaded: false };
let poolLastReject = { detail: "", at: 0 };   // 最近一次账号级拒绝的原文（全池等不起时用它回话）
// 410004 终态封禁集合：撞到一次就永久排除该号，不再自动复选（封禁不会自愈，12h 冷却
// 到了还会再撞，每次白烧一发请求；且反复撞死号本身是异常流量特征）。可人工从
// pool_state.json 的 bannedUids 数组摘除解封。
const bannedUids = new Set();

function loadPoolState() {
  if (pstate.loaded) return;
  pstate.loaded = true;
  try {
    const j = JSON.parse(fs.readFileSync(POOL_STATE_FILE, "utf8"));
    pstate.byUid = j.byUid || {};
    pstate.byRoute = j.byRoute || {};
    pstate.lastPick = j.lastPick || {};
    (j.bannedUids || []).forEach((u) => bannedUids.add(String(u)));
    // 日预算恢复（09-22）：以前 dailyWin 只在内存，每次部署/重启把 24h 预算洗白 ——
    // 某号 死于重启窗口 pile-on 的放大器。加载时按窗口裁掉过期时间戳。
    const now = Date.now(), cutoff = now - DAILY_WINDOW_MS;
    for (const [uid, arr] of Object.entries(j.dailyWin || {})) {
      if (Array.isArray(arr)) {
        const keep = arr.map(Number).filter((t) => t > cutoff);
        if (keep.length) dailyWin.set(String(uid), keep);
      }
    }
    // 迁移（09-21 18:0x）：旧版本把"额度打空"记在账号级（byUid），导致一次满血模型的
    // 810000 把该号上所有模型锁到明天 —— 实测把唯一可用号锁死，站点表现为"全部不可用"。
    // 旧记录无法反推是哪个模型打空的，且代价不对称（丢掉 = 白试一发；留着 = 白锁 21 小时），
    // 所以一律丢弃，让它们在新代码下按模型维度重新标记。
    let migrated = 0;
    for (const uid of Object.keys(pstate.byUid)) {
      if (pstate.byUid[uid] && pstate.byUid[uid].kind === "exhausted") { delete pstate.byUid[uid]; migrated++; }
    }
    if (migrated) {
      poolStateDirty = true;
      log(`pool_state 迁移：丢弃 ${migrated} 条账号级 exhausted 记录（改为按模型维度记录）`);
    }
  } catch { /* 首次运行或被删：从空开始，不影响选号 */ }
}
let poolStateDirty = false;
function savePoolState() {
  if (!poolStateDirty) return;
  poolStateDirty = false;
  try {
    fs.writeFileSync(POOL_STATE_FILE, JSON.stringify({
      at: Date.now(), byUid: pstate.byUid, byRoute: pstate.byRoute, lastPick: pstate.lastPick,
      dailyWin: Object.fromEntries(dailyWin), bannedUids: [...bannedUids],
    }));
  } catch (e) { log(`pool_state 写盘失败（不影响选号，只是重启后冷却清零）: ${e.message}`); }
}
setInterval(savePoolState, 5000).unref?.();

function readPool() {
  if (POOL_MODE === "off") return null;
  const now = Date.now();
  let st;
  try { st = fs.statSync(POOL_FILE); } catch { return null; }   // 没有池子文件：A-SWITCH 还没导过
  // 国内凭证文件独立轮换（watch_auth 随 2.x 客户端重写），mtime 也要进缓存键
  let cnMtime = 0;
  try { cnMtime = fs.statSync(CN_POOL_FILE).mtimeMs; } catch { /* 没有国内凭证源 */ }
  // 文件没动就不重复解析；动了立刻重读（A-SWITCH 每 10 秒原子改写一次，一次 stat 而已）
  if (st.mtimeMs === pool.mtime && cnMtime === pool.cnMtime) return pool.accounts.length ? pool : null;
  if (now - st.mtimeMs > POOL_STALE_MS) {
    // 海外池过期只影响海外号（A-SWITCH 没在跑）；国内号走独立凭证源，保留可用
    const cnOnly = (pool.accounts || []).filter((a) => a.lane === "cn");
    log(`海外账号池已过期 ${Math.round((now - st.mtimeMs) / 60_000)} 分钟没刷新 —— A-SWITCH 大概没在跑`
        + (cnOnly.length ? `，仅保留 ${cnOnly.length} 个国内号` : "，退回单凭证"));
    pool.mtime = st.mtimeMs;
    pool.cnMtime = cnMtime;
    pool.accounts = cnOnly;
    return pool.accounts.length ? pool : null;
  }
  try {
    const j = JSON.parse(fs.readFileSync(POOL_FILE, "utf8"));
    pool.accounts = Array.isArray(j.accounts) ? j.accounts : [];
    // 国内官方线账号并入统一池（PR#4）：watch_auth.py 把 2.x 客户端 DPAPI 凭证解密成
    // 1.x 形状写到 auth-compat/auth.json（跟随客户端轮换自动刷新，写完即生效）。
    // 该文件缺失 = 没装 2.x 客户端/没跑同步器，静默跳过，海外线不受影响。
    try {
      // 双源：auth-cn.json（国内版 2.x）→ lane=cn；auth-oversea.json（国际 2.x）→ lane=oversea。
      // 旧格式单文件 auth.json（无 lane 字段）按 cn 兼容。
      for (const f of ["auth-cn.json", "auth-oversea.json", "auth.json"]) {
      const cj = JSON.parse(fs.readFileSync(path.join(path.dirname(CN_POOL_FILE), f), "utf8"));
      const lane = cj.lane === "oversea" ? "oversea" : "cn";
      const uid = String(cj.userInfo?.user_id || cj.deviceId || lane + "-local");
      if (cj.token && uid && !uid.endsWith("-local")) {
        const existed = pool.accounts.find((a) => a.uid === uid);
        const entry = {
          uid, lane,
          name: `[CN]${cj.userInfo?.user_name || cj.userInfo?.nickname || uid.slice(0, 8)}`,
          auth: String(cj.token).toLowerCase().startsWith("bearer ") ? cj.token : `Bearer ${cj.token}`,
          refresh_token: cj.refreshToken || "",
          device_id: cj.deviceId || "",
          points: null, expiring: null, is_live: false,
        };
        if (existed) Object.assign(existed, entry);
        else pool.accounts.push(entry);
      }
      }
    } catch { /* 没有国内凭证源：纯海外池，正常 */ }
    pool.mtime = st.mtimeMs;
    pool.cnMtime = cnMtime;
    pool.clientVersion = j.client_version || "";
    // A-SWITCH 换了票（指纹变了）→ 上一轮因 401/403 上的冷却已经没有意义，立刻解除
    const fresh = new Set(pool.accounts.map((a) => `${a.uid}|${authFp(a.auth)}`));
    loadPoolState();
    for (const [uid, v] of Object.entries(pstate.byUid)) {
      if (v.kind === "auth" && !fresh.has(`${uid}|${v.fp}`)) delete pstate.byUid[uid];
    }
    poolStateDirty = true;
    return pool.accounts.length ? pool : null;
  } catch (e) {
    log(`账号池读不动（退回单凭证）：${scrub(e.message)}`);
    return null;
  }
}

function nextDailyResetMs() {
  const d = new Date();
  d.setHours(0, 5, 0, 0);                       // 00:05：给服务端刷新留点余量
  if (d.getTime() <= Date.now()) d.setDate(d.getDate() + 1);
  return d.getTime();
}

/** 三因子打分（借 wb2api 的思路，但按"额度当天过期、过期不补"这个前提改权重）：
 *  1) 即将过期的份额 ×10 —— 主力。今晚 23:59 蒸发的分必须先烧掉，留着等于没拿到；
 *  2) 总余额 —— 次级，避免挑到一个只剩几分的号上，一撞就空；
 *  3) 空闲分钟数 —— 摊平请求，别让一个号连续挨着撞限流。
 *  余额查不到（points=null）记 0 分：不惩罚它到永远不用，但也不拿它当首选。 */
function poolScore(a, now) {
  const expiring = Number(a.expiring) || 0;
  const points = Number(a.points) || 0;
  const idleMin = (now - (pstate.lastPick[a.uid] || 0)) / 60_000;
  return expiring * 10 + points + Math.min(idleMin, 120) * 0.5;
}

/** 冷却查询：账号级(byUid) 或 模型级(byRoute) 任一命中即冷却。
 *  模型级只对 exhausted 生效（额度按模型分）；auth/throttle/transient 都是账号级。 */
function poolCooldown(a, now, route) {
  const v = pstate.byUid[a.uid];
  if (v && (v.until || 0) > now) return v;
  const rv = route && pstate.byRoute[routeKey(a.uid, route)];
  if (rv && (rv.until || 0) > now) return rv;
  return null;
}

/** 模型级冷却的键：同一个号在不同模型上的额度是独立的。 */
function routeKey(uid, route) { return `${uid}|${route || ""}`; }

function poolCandidates(route) {
  loadPoolState();
  const p = readPool();
  if (!p) return [];
  const now = Date.now();
  const usable = [];
  const cooling = [];
  for (const a of p.accounts) {
    if (!a || !a.auth) continue;
    if (bannedUids.has(String(a.uid))) continue;   // 410004 终态封禁：永久排除（人工摘除 bannedUids 才恢复）
    if (a.access_expires_at && a.access_expires_at * 1000 <= now + 30_000) continue;
    // ⚠ 不要按 is_live 过滤（我 2026-09-24 一度这么改过，是错的，已回退）。
    // is_live 的语义是"A-SWITCH 活动目录里的那个号"（appdata_dir == Roaming\autoclaw），
    // 即**当前桌面端正在登录的号**，与"号是否可用"无关：
    //   某号 is_live=true（它是活动目录） / 某号等所有存档号 is_live=false。
    // 按它过滤会把池子里除当前登录号以外的号全挡掉 —— 而多号轮换正是池子的意义。
    // 真正该挡的是上游事实：账号级封禁（403 410004）和真没钱（810000 积分不足），
    // 那些由 callCloudPool 的 POOL_EXHAUST_RE / 401/403 分支按服务端响应分类处理。
    // 余额实测为 0 的号直接跳过（A-SWITCH 每轮刷新写真实 total_balance）。
    // 不跳的后果实测过（2026-09-24 22:39 事故）：池子"摊负载"逻辑会把请求优先给
    // "最久没用过的号"，0 分号因此被反复挑中 → 402 抛给用户 → 用户看到"积分不足"，
    // 而其实有积分的号（某号 10570 分）就在旁边没被轮到。
    if (a.points === 0 && !a.expiring) { cooling.push({ acc: a, until: 0, kind: "zero_points" }); continue; }
    const cd = poolCooldown(a, now, route);
    if (cd) { cooling.push({ acc: a, until: cd.until || 0 }); continue; }
    // 日预算耗尽：这个号今天已经扛够了，换别人 —— 压力摊到整个池子，
    // 而不是让一个号冲到风控阈值（760 次/天 就是 某号 的死亡线）。
    if (dailyLeft(a.uid, now) <= 0) {
      cooling.push({ acc: a, until: 0, kind: "daily_cap" });
      continue;
    }
    usable.push({ acc: a, score: poolScore(a, now) });
  }
  // 模型多样性微调：同一号如果最近一直只被某个模型打，把它的分数往下压，
  // 让"最久没用过的模型"优先挑它 —— 避免单号长时间只被一个模型打（风控看模型分布）。
  // 只在多个模型都有候选号时生效（否则没得选）。
  if (route && usable.length > 1) {
    const lastByRoute = new Map();
    for (const u of usable) {
      const m = lastRouteUse.get(u.acc.uid);
      const t = m && m.get(route);
      lastByRoute.set(u.acc.uid, t || 0);
    }
    // 把"最久没被这个模型用过的号"排前面（与分数并列时优先）
    usable.sort((x, y) => {
      const sx = x.score, sy = y.score;
      if (Math.abs(sx - sy) < 500) {
        const tx = lastByRoute.get(x.acc.uid) || 0;
        const ty = lastByRoute.get(y.acc.uid) || 0;
        return tx - ty;   // 小的 = 更久没用过
      }
      return sy - sx;
    });
  } else {
    usable.sort((x, y) => y.score - x.score);
  }
  // 分数前 K 名里挑"最久没被用"的那个：既保住优先级，又把负载摊开
  const top = usable.slice(0, POOL_TOPK);
  top.sort((x, y) => (pstate.lastPick[x.acc.uid] || 0) - (pstate.lastPick[y.acc.uid] || 0));
  if (top.length) return [...top, ...usable.slice(POOL_TOPK)];
  // 日预算耗尽 ≠ 冷却恢复中：日预算是"今天到此为止"，没有"等一会儿就有"可言，
  // 所以超预算的号绝不重新交出去（否则 cap 形同虚设 —— 实测兜底路径曾放行超预算号）。
  // 只剩超预算号时返回空列表：callCloudPool 拿不到候选 → 走家宽兜底/如实回错，站不崩。
  const waiting = cooling.filter((c) => c.kind !== "daily_cap");
  if (waiting.length) {
    waiting.sort((x, y) => x.until - y.until);
    return waiting.map((c) => ({ acc: c.acc, score: 0, onCooldown: true,
                                 readyInMs: Math.max(0, (c.until || 0) - now) }));
  }
  return [];
}

/** 给账号上冷却。scope="route" 时记到模型级（额度按模型分），否则记到账号级。 */
function poolMark(uid, kind, detail, auth, route) {
  if (!uid) return;
  loadPoolState();
  const now = Date.now();
  // 额度类（exhausted）按模型分：同一个号的 GLM-5.3 打空 ≠ GLM-5.3-Flash 打空。
  // 实测证据（09-21 18:0x）：同号同刻 GLM-5.3-Flash=200 / GLM-5.3=403 810000 / DeepSeek-V4-Pro=200。
  const scope = (kind === "exhausted" && route) ? "route" : "uid";
  const key = scope === "route" ? routeKey(uid, route) : uid;
  const bag = scope === "route" ? pstate.byRoute : pstate.byUid;
  const prev = bag[key];
  let until = kind === "exhausted" ? nextDailyResetMs() : now + COOLDOWN_MS[kind];
  // 限流类冷却不滑动：否则高频请求下每次撞墙都把窗口往后推，号就永远出不来了
  // （exhausted 到每日刷新点，本来就该锁住，不受这条影响）。
  if (kind !== "exhausted" && prev && prev.kind === kind && (prev.until || 0) > now) {
    until = prev.until;
  }
  bag[key] = {
    kind, until, at: now, fp: authFp(auth), route: scope === "route" ? route : undefined,
    hits: (prev && prev.kind === kind ? prev.hits || 0 : 0) + 1,
    reason: String(detail || "").slice(0, 200),
  };
  poolStateDirty = true;
  savePoolState();
}

/** 冷却解除：一次成功就把限流/票失效类的标记抹掉（exhausted 归每日刷新管，不抹）。 */
function poolClear(uid, route) {
  let changed = false;
  const v = pstate.byUid[uid];
  // exhausted 归每日刷新管、banned 归隔离窗管（封号不会因为别处成功而自愈，
  // 若被一次成功抹掉就会立刻重新进选号，白撞）。
  if (v && v.kind !== "exhausted" && v.kind !== "banned") { delete pstate.byUid[uid]; changed = true; }
  // 模型级额度锁：这个模型成功了就说明该模型的额度可用 → 解开它（别的模型各自的锁不受影响）
  const rk = routeKey(uid, route);
  if (pstate.byRoute[rk]) { delete pstate.byRoute[rk]; changed = true; }
  if (changed) poolStateDirty = true;
}

/** 云端这一发交给哪个号 —— **排队，不轰炸**。
 *  每一轮只试一个号：失败就给它上冷却，下一轮自然换人；全在冷却时只等"最快恢复的那个"，
 *  等不起（超过 POOL_WAIT_BUDGET_MS）就立刻把错误交回上层。
 *  为什么这么改：上一版一次请求连撞 5 个号、上层再退避 6 轮，等于把同一个 prompt 往限流上
 *  砸 30 发 —— 09-21 02:40 实测 20 分钟内 99 次限流标记 / 9 次彻底失败，越重试越频繁。 */
// ---------------- 探针识别（09-22 封号取证后补的真正防御） ----------------
// 证据（服务端 ledgers_std 流水，按 amount 分档统计）：
//   死者：某号 760 次调用**全部**是探针(amount=-1)、某号 752/752、
//         某号 744/776、某号 680/768 —— 探针占比 88%~100%
//   活者：某号 760 次里只有 136 次探针、624 次是真实调用（-21/-22/-53 分）→ 17.9%
//   结论：**不是"打了多少发"致死，是"只有探针、零真实用量"致死**。
//         一个号全是 1-token ping、没有任何真人对话，服务端就判定它是机器。
// 所以这里加一道闸：识别探针 → **本地直接回假响应，一个字节都不发给上游**。
// 这样状态页/健康检查拿到的仍是 200，但账号池的流水里再也不会出现 amount=-1。
const PROBE_DETECT = process.env.AUTOCLAW_PROBE_DETECT !== "0";
const PROBE_MAX_TOKENS = 1;                        // 探针只用 1 token 探活
const PROBE_TEXT_RE = /^\s*(ping|hi|hello|test|ok|hey|probe|健康检查|测试)\s*[.。!！?？]*\s*$/i;
const PROBE_TEXT_MAXLEN = 24;
/** 取最后一条 user 消息的文本（兼容 content 为数组的多模态格式）。 */
function lastUserText(payload) {
  const msgs = Array.isArray(payload && payload.messages) ? payload.messages : [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (!m || m.role !== "user") continue;
    const c = m.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) {
      for (const p of c) if (p && p.type === "text" && typeof p.text === "string") return p.text;
    }
  }
  return "";
}
/** 像不像探针：max_tokens<=1 且最后一条 user 消息是极短的打招呼/探活词。 */
function isProbeRequest(payload) {
  if (!PROBE_DETECT || !payload) return false;
  const mt = Number(payload.max_tokens ?? payload.max_completion_tokens);
  if (!(mt > 0 && mt <= PROBE_MAX_TOKENS)) return false;
  const t = lastUserText(payload);
  if (t.length > PROBE_TEXT_MAXLEN) return false;
  return PROBE_TEXT_RE.test(t);
}
let probeServed = 0, probeLastAt = 0;
/** 探针的本地假响应：造一个真正的 Response，形状与上游完全一致。
 *  不手搓对象 —— 下游读 upstream.headers.get(...) / .body / .text() 十几个地方，
 *  少一个字段就是 500（第一次手搓漏了 headers，探针整条路径直接崩）。 */
function fakeProbeResponse(payload) {
  const model = (payload && payload.model) || "GLM-5.3-Flash";
  const body = {
    id: `probe-local-${Date.now()}`, object: "chat.completion", created: Math.floor(Date.now() / 1000), model,
    choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: "ok" } }],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  };
  const text = JSON.stringify(body);
  return new Response(text, {
    status: 200,
    headers: { "Content-Type": "application/json", "Content-Length": String(Buffer.byteLength(text)) },
  });
}

// ---------------- 池等待期早发 SSE（2026-10-28） ----------------
// 为什么需要：DSH 侧 Node fetch 的 undici headersTimeout 默认 300s，而池等待预算
// 是 330s；全池 406 冷却时 relay 在选到号之前一个响应头都不写，客户端 300s 先断，
// relay 收到 close 又中止轮转 —— 冷却永远等不到（实测 08:50:29 → 08:55:28.993
// 正好 300.3s）。流式请求在真正开始等号前先回 200 + 周期性 SSE 注释心跳：
//   - 响应头立即到达 → headersTimeout 不再触发
//   - 每 10s 一个 ": keepalive" 块 → bodyTimeout / 客户端 idle watchdog 都不会误杀
// 仅聊天补全的流式请求启用；非流式仍保持原来的状态码语义。
const EARLY_SSE_ENABLED = process.env.AUTOCLAW_EARLY_SSE !== "0";
const EARLY_SSE_MIN_WAIT_MS = Math.max(0, Number(process.env.AUTOCLAW_EARLY_SSE_MIN_WAIT_MS || 5000));
const EARLY_SSE_HEARTBEAT_MS = Math.max(1000, Number(process.env.AUTOCLAW_EARLY_SSE_HEARTBEAT_MS || 10_000));
const SSE_HEADERS = {
  "Content-Type": "text/event-stream; charset=utf-8",
  "Cache-Control": "no-cache, no-transform",
  "Connection": "keep-alive",
  "X-Accel-Buffering": "no",
};
/** 池等待前先把 SSE 头发出去；已发过/已结束则返回 false（幂等）。 */
function beginEarlySse(res, kind) {
  if (!EARLY_SSE_ENABLED || !res || res.headersSent || res.writableEnded || res.destroyed) return false;
  if (res._earlySse) return false;
  res.writeHead(200, SSE_HEADERS);
  if (typeof res.flushHeaders === "function") res.flushHeaders();
  const timer = setInterval(() => {
    if (res.writableEnded || res.destroyed) { clearInterval(timer); return; }
    try { res.write(": keepalive\n\n"); } catch { clearInterval(timer); }
  }, EARLY_SSE_HEARTBEAT_MS);
  timer.unref?.();
  res._earlySse = { kind, timer, at: Date.now() };
  res.once("close", () => clearInterval(timer));
  log(`early-sse: 先回 200（kind=${kind}），等待期每 ${EARLY_SSE_HEARTBEAT_MS}ms 心跳`);
  return true;
}
function stopEarlySse(res) {
  if (!res || !res._earlySse) return false;
  clearInterval(res._earlySse.timer);
  res._earlySse = null;
  return true;
}
function earlySseFrame(kind, message, type = "server_error") {
  if (kind === "anthropic") return sse({ type: "error", error: { type: "api_error", message } });
  if (kind === "responses") return `event: error\ndata: ${JSON.stringify({ type: "error", message, code: type })}\n\n`;
  return `data: ${JSON.stringify({ error: { message, type, code: type } })}\n\n`;
}
/** 早发头之后不能再回 JSON 状态码：把错误改写成 SSE 错误帧并收流。 */
function failEarlySse(res, kind, message, type = "server_error") {
  if (!res || !res._earlySse) return false;
  stopEarlySse(res);
  try {
    res.write(earlySseFrame(kind, message, type));
    if (kind === "chat") res.write("data: [DONE]\n\n");
    res.end();
  } catch { /* 客户端已走 */ }
  return true;
}
function notifyPoolWait(opts, waitMs, why) {
  if (!opts || typeof opts.onPoolWait !== "function") return;
  if (waitMs < EARLY_SSE_MIN_WAIT_MS) return;
  try { opts.onPoolWait(waitMs, why); } catch { /* 通知失败不影响等待 */ }
}
// 池等待（冷却/限速/抖动）可以长达数分钟，必须跟着客户端断开一起结束：
// 裸 setTimeout 会让已断开的请求继续占着 in-flight 槽等满冷却。
// 返回 false = 被 abort 打断，true = 正常睡满。
function poolSleep(ms, opts) {
  const sig = opts && opts.externalSignal;
  if (!sig) return new Promise((x) => setTimeout(() => x(true), ms));
  if (sig.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    const done = (v) => {
      clearTimeout(t);
      sig.removeEventListener("abort", onAbort);
      resolve(v);
    };
    const onAbort = () => done(false);
    const t = setTimeout(() => done(true), ms);
    sig.addEventListener("abort", onAbort, { once: true });
  });
}
function clientGoneError() {
  const e = new Error("客户端已断开，停止池等待");
  e.name = "AbortError";
  return e;
}

async function callCloudPool(route, payload, opts) {
  // 探针就地消化，绝不打到上游 —— 账号池流水里从此不再有 amount=-1 的机器指纹。
  if (isProbeRequest(payload)) {
    probeServed++; probeLastAt = Date.now();
    log(`probe 就地消化（零上游）：model=${payload.model} 已省 ${probeServed} 次真实调用`);
    const r = fakeProbeResponse(payload);
    r._probeLocal = true;
    return r;
  }
  const cands0 = poolCandidates(route);
  if (!cands0.length) return callCloud(route, payload, opts);
  const deadline = Date.now() + POOL_WAIT_BUDGET_MS;
  const tried = new Set();
  let resp = null;
  for (let guard = 0; guard < POOL_MAX_ROUNDS; guard++) {
    const list = guard === 0 ? cands0 : poolCandidates(route);
    if (!list.length) break;
    const c = list.find((x) => !tried.has(x.acc.uid)) || list[0];
    if (!c) break;
    if (c.onCooldown && c.readyInMs > 0) {
      const left = deadline - Date.now();
      if (c.readyInMs + 40 > left) break;             // 等不到它恢复：别把请求拖死在这台机器上
      // 真要等超过 5s 才轮到号：流式请求先把 200/SSE 头发出去，否则客户端
      // 300s headersTimeout 会先断，relay 这边的等待等于白等。
      notifyPoolWait(opts, c.readyInMs, "cooldown");
      if (!(await poolSleep(c.readyInMs + 40, opts))) {
        log(`pool ${c.acc.name}: 冷却等待中客户端已断开，停止本次池轮转`);
        throw clientGoneError();
      }
      tried.delete(c.acc.uid);                        // 等完之后这个号值得再试一次
    }
    const acc = c.acc;
    // 单号滑动窗口限速：这个号 60s 内配额用完了，要么等窗口滑过去，要么跳过它换下一个号。
    // 跳过而不是死等 —— 池子里还有别的号时，把请求分给它们更稳（也破"固定顺序"指纹）。
    const slots = rateSlotsLeft(acc.uid, Date.now());
    if (slots <= 0) {
      const waitMs = rateReadyInMs(acc.uid, Date.now());
      const left = deadline - Date.now();
      if (waitMs + 40 <= left) {
        log(`pool ${acc.name} 限速窗口满：等 ${waitMs}ms 再试`);
        notifyPoolWait(opts, waitMs, "rate");
        if (!(await poolSleep(waitMs + 40, opts))) {
          log(`pool ${acc.name}: 限速等待中客户端已断开，停止本次池轮转`);
          throw clientGoneError();
        }
        tried.delete(acc.uid);
      } else {
        tried.add(acc.uid);   // 标记已试，让下一轮跳过它
        log(`pool ${acc.name} 限速窗口满且等不起 → 跳过`);
        continue;
      }
    }
    tried.add(acc.uid);
    pstate.lastPick[acc.uid] = Date.now();
    poolStateDirty = true;
    // 抖动判断读的是"上一个使用者"的时刻，必须在本次占位写 lastUse 之前抓快照，
    // 否则 rateReserve 刚写进去的 now 会被自己读到，导致每个请求（含首个）都白白抖一次。
    const lastAt = lastUse.get(acc.uid) || 0;
    // ⚠️ 占位必须在这里（进 await 之前的最后一个同步点）：rateSlotsLeft 的检查和这次占位
    //   之间没有任何 await，并发请求无法插进"查过但没记"的空隙 —— 修前限速就是被
    //   思考模型 30~60s 的 await 挂起彻底绕过的（实测单号 240~720 次/分，配置 12）。
    rateReserve(acc.uid, Date.now());
    // 抖动：同一号被连续挑中时，插入随机间隔，抹平"秒级固定"特征。
    // 只对"刚用过这个号"的请求生效；第一次挑它不抖（否则首请求无谓延迟）。
    if (lastAt && Date.now() - lastAt < 30_000) {
      const jitter = POOL_JITTER_MIN_MS + Math.random() * (POOL_JITTER_MAX_MS - POOL_JITTER_MIN_MS);
      if (!(await poolSleep(jitter, opts))) {
        rateRelease(acc.uid);   // 一发都没出去，别占这个号的配额
        log(`pool ${acc.name}: 抖动等待中客户端已断开，停止本次池轮转`);
        throw clientGoneError();
      }
    }
    try {
      resp = await callCloud(route, payload, opts, acc, opts && opts.externalSignal);
    } catch (e) {
      // 客户端主动断开（AbortError 样）不是账号故障：不该给这个号上冷却，直接静默退出
      if (opts && opts.externalSignal && opts.externalSignal.aborted) {
        log(`pool ${acc.name}: 客户端已断开，停止本次池轮转`);
        throw e;
      }
      // 连接层抛错 = 上游大概率没见到这次请求：释放占位，别冤枉这个号的配额
      rateRelease(acc.uid);
      poolMark(acc.uid, "transient", `fetch: ${e.message}`, acc.auth, route);
      log(`pool ${acc.name}: 发不出去 ${scrub(e.message)} → 换下一个号`);
      continue;
    }
    // 真实到达上游：提交日预算 + 模型多样性时间戳（限速配额已在发出前占过，不重复记）
    commitDaily(acc.uid, Date.now(), route);
    resp._poolUid = acc.uid; resp._poolName = acc.name;
    // 200 但 body 是业务错误（810001 付费墙等）→ 翻成真状态再判定，别当成功
    resp = await unpackUpstreamError(resp, Boolean(opts && opts.stream));
    if (resp.ok) { poolClear(acc.uid, route); log(`pool pick: ${acc.name}(${acc.lane || "oversea"}) ← ${route}`); return resp; }
    const detail = await readDetail(resp);
    poolLastReject = { detail, at: Date.now() };
    if (POOL_EXHAUST_RE.test(detail)) {
      poolMark(acc.uid, "exhausted", detail, acc.auth, route);
      log(`pool ${acc.name} 额度打空（${detail.slice(0, 80)}）→ 换下一个号`);
      continue;
    }
    if (POOL_PAYVIEW_RE.test(detail)) {
      // pay-view 一律只当限流：真机见过 某号(21330 分)/某号(28009 分) 大把分也被回这个，
      // 而"查不到余额"更不该把它升级成锁到明天 —— 判错的代价不对称：
      // 误判限流 = 白等 20 秒；误判打空 = 白锁 21 小时。真打空有 810000/积分不足那条正则去认。
      poolMark(acc.uid, "throttle", detail, acc.auth, route);
      log(`pool ${acc.name} 被限流（pay-view，账上 ${acc.points == null ? "余额未知" : acc.points + " 分"}）→ 换下一个号`);
      continue;
    }
    if (resp.status === 401 || resp.status === 403) {
      // 410004 = 账号级终态封禁（推理面被切断，钱包/签到面照常）。它和"票被轮换掉"不是一回事：
      // 票会随 A-SWITCH 刷新换新（届时自动解除冷却），封禁不会。混在一起会让每个请求重撞死号。
      const isBan = /410004/.test(detail) || /been banned/i.test(detail);
      if (isBan) {
        // 终态封禁：写进永久黑名单，永不自动复选（冷却到了还会再撞，白烧请求且是异常流量特征）
        bannedUids.add(String(acc.uid));
        poolStateDirty = true;
        log(`pool ${acc.name} 终态封禁(410004) → 永久拉黑，不再选号（人工从 pool_state.json 的 bannedUids 摘除可解封）`);
        continue;
      }
      poolMark(acc.uid, "auth", detail, acc.auth, route);
      log(`pool ${acc.name} 票失效/账号受限 ${resp.status} → 换下一个号`);
      continue;
    }
    if (resp.status === 429 || resp.status >= 500) {
      poolMark(acc.uid, "transient", detail, acc.auth, route);
      continue;
    }
    if (resp.status === 406) {
      // 该号推理面未放行（新注册号常见）→ 冷却并换下一个号，绝不把空 406 直接抛给用户。
      poolMark(acc.uid, "notprovisioned", detail || "406 not-provisioned", acc.auth, route);
      log(`pool ${acc.name} 推理面未放行(406) → 冷却 ${Math.round(COOLDOWN_MS.notprovisioned / 60000)} 分钟并换下一个号`);
      continue;
    }
    return resp;                      // 别的 4xx 是请求本身的问题，换号也没用
  }
  if (!resp) {
    // 一次都没发出去（全池在冷却且都等不起）。这里**必须**回一个真响应：
    // 回 null 会让上层在 `upstream.ok` 上抛 TypeError、被 catch 吞掉，最后变成误导性的 502
    // —— 离线验收抓到过一次，所以这条写死在这里。
    // 全池都是"推理面未放行(406)"时别把它说成高峰限流：那是新号还没被服务端放行，等或换号才是解。
    const allNotProv = Object.values(pstate.byUid).length > 0
      && Object.entries(pstate.byUid).every(([uid, v]) => v && v.kind === "notprovisioned" && (v.until || 0) > Date.now());
    const body = (Date.now() - poolLastReject.at < 60_000 && poolLastReject.detail)
      || (allNotProv
        ? '{"error":{"message":"account pool not provisioned yet (406: newly registered accounts need upstream approval)"}}'
        : '{"error":{"message":"account pool throttled (810002 high demand)"}}');
    resp = new Response(body, { status: 429, headers: { "content-type": "application/json" } });
    resp._detail = body;
    log(`pool: 全池冷却中且等不起 ${POOL_WAIT_BUDGET_MS}ms → 429（不轰炸上游）`);
  }
  // 家宽回源兜底：池子没打通（429 全冷却 / 全部 410004=403 / 5xx）时试一次本机反代。
  // 400/401 等请求本身有问题的 4xx 不兜（换出口也一样死）；200 早 return 了到不了这。
  const hfEligible = resp && (resp.status === 429 || resp.status === 403 || resp.status >= 500);
  if (hfEligible) {
    const hf = await callHomeFallback(route, payload, opts);
    if (hf) return hf;
  }
  return resp;
}

/** 经家宽出口转发一次请求（池子被机房 IP 连坐时的活路）。失败返回 null，绝不掩盖真实错误。 */
async function callHomeFallback(route, payload, opts) {
  if (!HOME_FALLBACK_URL) return null;
  const now = Date.now();
  if (now < homeFall.cooldownUntil) return null;
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), UPSTREAM_TIMEOUT_MS);
  if (opts && opts.externalSignal) {
    if (opts.externalSignal.aborted) ctl.abort();
    else opts.externalSignal.addEventListener("abort", () => ctl.abort(), { once: true });
  }
  try {
    const headers = { ...cloudHeaders(route, Boolean(payload && payload.stream), null) };
    delete headers["X-Authorization"];                       // 本机有自己的凭证体系
    if (HOME_FALLBACK_TOKEN) headers.Authorization = `Bearer ${HOME_FALLBACK_TOKEN}`;
    const r = await fetch(`${HOME_FALLBACK_URL.replace(/\/$/, "")}/v1/chat/completions`, {
      method: "POST",
      headers,
      body: JSON.stringify({ ...payload, model: publicModelName(route) }),
      signal: ctl.signal,
    });
    homeFall.lastAt = now; homeFall.lastStatus = r.status;
    if (r.ok || (r.status !== 429 && r.status !== 403 && r.status < 500)) {
      homeFall.ok++; homeFall.fail = 0;
      log(`home-fallback: ${r.status}（经家宽出口）route=${route}`);
      return r;
    }
    homeFall.fail++;
    if (homeFall.fail >= 3) {
      homeFall.cooldownUntil = Date.now() + HOME_FALLBACK_COOLDOWN_MS;
      log(`home-fallback: 连续 ${homeFall.fail} 次失败 → 冷却 5 分钟`);
    }
    return null;
  } catch (e) {
    homeFall.fail++;
    homeFall.lastAt = now;
    if (homeFall.fail >= 3) homeFall.cooldownUntil = Date.now() + HOME_FALLBACK_COOLDOWN_MS;
    log(`home-fallback: 不可达（${scrub(e.message)}）fail=${homeFall.fail}`);
    return null;
  } finally {
    clearTimeout(t);
  }
}

/** 内部 route（带前缀）→ 对外友好名（本机反代的 normalizeRoute 按名字解析）。 */
function publicModelName(route) {
  const m = (modelsCache.list || []).find((x) => x.id === route);
  return (m && m.name) || route.replace(/^[a-z]+_/, "");
}

/** 给 /health 和日志用的池子快照（不含任何凭证）。 */
function poolSnapshot() {
  const p = readPool();
  if (!p) return null;
  const now = Date.now();
  const liveUids = new Set(p.accounts.map((a) => String(a.uid)));
  return {
    file: POOL_FILE, accounts: p.accounts.length,
    picked: Object.entries(pstate.lastPick)
      .filter(([uid]) => liveUids.has(String(uid)))
      .sort((a, b) => b[1] - a[1]).slice(0, 3)
      .map(([uid, at]) => ({ uid: String(uid).slice(0, 8), s: Math.round((now - at) / 1000) })),
    // 两个维度都报：账号级（票失效/限流/发不出去）与模型级（额度按模型分）。
    // scope 字段让调用方一眼分清这条冷却会不会连坐该号的其它模型。
    cooling: [
      ...[...bannedUids].map((uid) => ({ uid: String(uid).slice(0, 8), scope: "uid",
                                         kind: "banned-permanent", until_s: 0,
                                         reason: "410004 终态封禁（永久拉黑，人工摘除 bannedUids 解封）" })),
      ...Object.entries(pstate.byUid)
        .filter(([, v]) => (v.until || 0) > now)
        .map(([uid, v]) => ({ uid: String(uid).slice(0, 8), scope: "uid", kind: v.kind,
                              until_s: Math.round(((v.until || 0) - now) / 1000),
                              reason: (v.reason || "").slice(0, 80) })),
      ...Object.entries(pstate.byRoute)
        .filter(([, v]) => (v.until || 0) > now)
        .map(([k, v]) => { const [uid, route] = String(k).split("|");
          return { uid: uid.slice(0, 8), scope: "route", route, kind: v.kind,
                   until_s: Math.round(((v.until || 0) - now) / 1000),
                   reason: (v.reason || "").slice(0, 80) }; }),
    ],
  };
}

/** 把已经读过的错误体挂在响应上，供上层分类用（同一个 body 不能读两次）。 */
async function readDetail(resp, max = 500) {
  if (!resp) return "";
  if (resp._detail != null) return resp._detail.slice(0, max);
  let s = "";
  try { s = await resp.text(); } catch { s = ""; }
  resp._detail = s;
  return s.slice(0, max);
}

/** HTTP 200 但 body 是网关的业务错误体（既不是 SSE 也不是正常 chat.completion）。
 *
 *  2026-10-21 实测：账号被降级到 pay-view 时 chat 端点回 **200** +
 *  `{"action":{"kind":"pay-view"},"code":810001,"message":"...upgrade..."}`。
 *  旧代码只看 resp.ok 就当成功，把它交给 SSE 解析器，用户看到的是毫无信息量的
 *  "parse response failed"，真实原因（付费墙/高峰限流）被完全掩盖。
 *  这里翻成带真实状态码的合成响应，走后面的正常错误分类（换号 / 冷却）。
 */
async function unpackUpstreamError(resp, isStream = false) {
  if (!resp || !resp.ok) return resp;
  // 响应体是一次性可读流：这里读过，调用方就读不到了。
  // 旧版无条件 await resp.text() 后又把原 resp 返回给上层，上层再 json()/读 SSE 就抛
  // "Body is unusable: Body has already been read" / "ReadableStream is locked"（实测约半数请求失败）。
  // 改法：流式直接跳过，非流式用 clone() 读，绝不动原 body。
  if (isStream) return resp;
  if (String(resp.headers.get("content-type") || "").toLowerCase().includes("text/event-stream")) return resp;
  let text;
  try { text = await resp.clone().text(); } catch { return resp; }
  if (!text.trimStart().startsWith("{")) return resp;          // SSE：data: {...}
  let j;
  try { j = JSON.parse(text); } catch { return resp; }
  if (Array.isArray(j.choices) || j.object === "chat.completion") return resp;  // 正常非流式
  const code = Number(j.code ?? j.error?.code ?? 0);
  if (!code) return resp;
  // 810000 额度耗尽 / 810001 付费墙 / 810002 高峰限流 —— 都按"这个号暂时不能用"处理
  const busy = code === 810000 || code === 810001 || code === 810002;
  log(`upstream 200 with business error code=${code} -> synth ${busy ? 429 : 400}: ${String(j.message || "").slice(0, 80)}`);
  const synth = new Response(text, {
    status: busy ? 429 : 400,
    headers: { "content-type": resp.headers.get("content-type") || "application/json" },
  });
  synth._detail = text;
  synth._poolUid = resp._poolUid;
  synth._poolName = resp._poolName;
  return synth;
}

/** 这一轮请求走哪个上游。
 *  默认（auto）：**云端账号池优先**（2026-09-24 反转，原先是 broker 优先）。
 *  为什么反转：broker lane 结构性走不通 —— 它把请求转给桌面端进程内的 Model Broker，
 *  而 broker 再发上游时不带 system 提示词（日志实测 system_message_count:0），
 *  恰好踩中 406 闸门（上游要求 system 以 harness 标记开头），所以 broker 历史 110/110 全 406。
 *  云端直连这条路在补上 harness 标记后实测 200（见 applyHarnessMarker）。
 *  AUTOCLAW_POOL_MODE=broker：需要时退回"broker 优先"的老行为（调试/对比用）。
 *  AUTOCLAW_POOL_MODE=off：只用单凭证直连，不用账号池。 */
async function getUpstream(force = false) {
  // broker 优先只在显式要求时启用；默认跳过它（那条路必 406，见上方注释）
  if (POOL_MODE === "broker") {
    // broker 因额度打空被标记冷却时，仍要能切到账号池（否则强制 broker 模式下会卡死）
    if (brokerLaneDead()) {
      const t = laneSwitchTarget();
      if (t) return t;
    }
    if (process.env.AUTOCLAW_DISABLE_BROKER !== "1") {
      const base = await getBrokerBase(force);
      if (base) return { kind: "broker", base };
    }
  }
  if (POOL_MODE !== "off" && poolCandidates().length) {
    return { kind: "cloud", base: CLOUD_BASE, pool: true };
  }
  return cloudCredential() ? { kind: "cloud", base: CLOUD_BASE } : null;
}

async function callUpstream(up, route, payload, opts) {
  // 探针拦截放在这里而不是 callCloudPool 里：broker lane（桌面端）是本机的默认首选路，
  // 探针从那条路走照样会打到上游账号 —— 封号流水不分 lane。统一在唯一入口拦。
  if (isProbeRequest(payload)) {
    probeServed++; probeLastAt = Date.now();
    log(`probe 就地消化（零上游，lane=${up && up.kind}）：model=${payload && payload.model} 已省 ${probeServed} 次真实调用`);
    const r = fakeProbeResponse(payload);
    r._probeLocal = true;
    return r;
  }
  return up.kind === "cloud"
    ? (up.pool ? callCloudPool(route, payload, opts) : callCloud(route, payload, opts, null, opts && opts.externalSignal))
    : callBroker(up.base, route, payload, opts, opts && opts.externalSignal);
}

// ---------------- 模型清单 / 路由映射 ----------------
const modelsCache = { list: FALLBACK_ROUTES, at: 0, ok: false };
// ⚠ 2026-09-25 定案：model-config 轮询**默认彻底关闭**（AUTOCLAW_FETCH_MODELS=1 才启用）。
// 证据链：该端点对本机 token 永久 401（死端点），而 getModels 被启动/health/请求各处调用，
// 失败重拉形成固定节奏出站 —— 09-24 全天 1546 发 401（30s 一发、全天无休），
// 且 readJwtHeaders 只读 request-headers.json（= 某号 单一 token），
// 等于把 1500+ 发/天的无效凭证请求全部集中挂在一个新号头上。
// 某号 09-25 被 410004 终态封禁（最后推理扣费 09-24 22:57，封禁窗口与该节奏完全吻合，
// 而同池从未被此节奏打过的某号安好）——死法 A 机器指纹，与 provision_probe 同类，
// 正是用户 09-24 警告的"无意义出站导致封号"，当时只停了探针漏了这条。
// FALLBACK_ROUTES 的 6 个模型一直够用，动态拉取本就无增益。
const FETCH_MODELS = process.env.AUTOCLAW_FETCH_MODELS === "1";
const MODELS_NEG_TTL_MS = Number(process.env.AUTOCLAW_MODELS_NEG_TTL_MS || 6 * 3600_000);
async function getModels(force = false) {
  if (!FETCH_MODELS) return modelsCache.list;   // 默认零出站：只用 fallback 清单
  if (!force && modelsCache.ok && Date.now() - modelsCache.at < MODELS_TTL_MS) return modelsCache.list;
  if (!force && !modelsCache.ok && modelsCache.at && Date.now() - modelsCache.at < MODELS_NEG_TTL_MS) return modelsCache.list;
  try {
    const { auth, clientType } = readJwtHeaders();
    if (!auth) throw new Error("no jwt");
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 8_000);
    const r = await fetch(MODEL_CONFIG_URL, {
      headers: { "X-Authorization": auth, "X-Product": "autoclaw", "X-Client-Type": clientType || "pc" },
      signal: ctl.signal,
    });
    clearTimeout(t);
    if (!r.ok) throw new Error(`http ${r.status}`);
    const j = await r.json();
    const list = (j.models || []).map((m) => ({
      id: m.id, name: m.name || m.id,
      contextWindow: m.contextWindow || 1048576,
      maxTokens: m.maxTokens || 131072,
    }));
    if (list.length) { modelsCache.list = list; modelsCache.ok = true; modelsCache.at = Date.now(); return list; }
    throw new Error("empty list");
  } catch (e) {
    modelsCache.at = Date.now(); modelsCache.ok = false;
    log("model-config fetch failed:", scrub(e.message), "-> using fallback");
    return modelsCache.list;
  }
}

function routeCandidates() {
  return modelsCache.list.map((m) => m.id);
}

/** ZCode/Anthropic 侧模型名 -> AutoClaw route id（宽容匹配） */
function normalizeRoute(model) {
  const raw = (model || "").trim();
  if (!raw) return DEFAULT_ROUTE;
  // dsh 车道显式别名优先（dsh-deepseek-v4.1-flash 等 -> DSH_ROUTE）
  if (DSH_ALIASES[raw]) return DSH_ALIASES[raw];
  const known = routeCandidates();
  // 1) 精确命中（含大小写不敏感）
  for (const id of known) if (id === raw) return id;
  const lowRaw = raw.toLowerCase();
  for (const id of known) if (id.toLowerCase() === lowRaw) return id;
  // 2) 去掉厂商前缀后再试：anthropic/xxx、autoclaw/xxx、zai/xxx
  const bare = lowRaw.replace(/^[a-z0-9_\-]+[:/]/, "");
  for (const id of known) if (id.toLowerCase() === bare) return id;
  // 3) 按名字匹配（"Auto" / "GLM-5.3" / "DeepSeek-V4-Pro"）
  for (const m of modelsCache.list) {
    if ((m.name || "").toLowerCase() === lowRaw) return m.id;
  }
  // 4) 显式映射表：必须在关键词正则之前，否则 "glm-5.3-flash" 会被 /glm-5.3/ 抢走
  if (EXPLICIT_ROUTES[lowRaw]) return pick(EXPLICIT_ROUTES[lowRaw]);
  if (EXPLICIT_ROUTES[bare]) return pick(EXPLICIT_ROUTES[bare]);
  // 5) 关键词规则（先匹配带 flash 后缀的，避免被裸 glm-5.3 截胡）
  const s = `${lowRaw} ${bare}`;
  if (/glm[\s_\-.]?5[\s_\-.]?3[\s_\-.]?flash/.test(s)) return pick("zai_glm-5.3-flash");
  if (/glm[\s_\-.]?5[\s_\-.]?3/.test(s) || /glm[\s_\-.]?4[\s_\-.]?/.test(s)) return pick("zaicoding_glm-5.3");
  if (/deepseek.*flash/.test(s)) return pick("tdpsk_deepseek-v4-flash-202605");
  if (/deepseek/.test(s)) return pick("tdpsk_deepseek-v4-pro-202606");
  if (/auto[\s_\-.]?fast/.test(s)) return pick("zai_auto-fast");
  if (/\bauto\b/.test(s)) return pick("zai_auto");
  if (/glm|zai/.test(s)) return pick(DEFAULT_ROUTE);
  // 6) 原样透传（假定调用方给的已是 route id）
  return raw;

  function pick(want) {
    return known.find((id) => id === want) || want;
  }
}

// ---------------- Anthropic -> OpenAI ----------------
// ---------------- 翻译层清洗 ----------------
// JSON Schema 里部分元字段会让上游解析异常，统一剥掉（$defs/$ref/anyOf 实测可用，保留）
const SCHEMA_DROP_KEYS = new Set(["$schema", "$id", "$anchor", "$comment", "definitions"]);
function sanitizeSchema(node, depth = 0) {
  if (depth > 12 || node == null || typeof node !== "object") return node;
  if (Array.isArray(node)) return node.map((x) => sanitizeSchema(x, depth + 1));
  const out = {};
  for (const [k, v] of Object.entries(node)) {
    if (SCHEMA_DROP_KEYS.has(k)) continue;
    out[k] = sanitizeSchema(v, depth + 1);
  }
  return out;
}

/** 请求指纹：出问题时不用翻 dump 也能看出请求规模与形状 */
function reqDigest(body, payload) {
  const msgs = Array.isArray(body.messages) ? body.messages : [];
  let chars = 0;
  const kinds = new Set();
  for (const m of msgs) {
    const c = m && m.content;
    if (typeof c === "string") chars += c.length;
    else if (Array.isArray(c)) for (const b of c) {
      if (b && b.type) kinds.add(b.type);
      const t = b && (b.text || b.content);
      if (typeof t === "string") chars += t.length;
    }
  }
  let sys = 0;
  if (typeof body.system === "string") sys = body.system.length;
  else if (Array.isArray(body.system)) sys = body.system.reduce((n, b) => n + (typeof b === "string" ? b.length : (b && b.text ? b.text.length : 0)), 0);
  const tools = Array.isArray(body.tools) ? body.tools.length : 0;
  const parts = [
    `msgs=${msgs.length}`,
    `chars=${chars}`,
    `sys=${sys}`,
    `tools=${tools}`,
    `max_tokens=${body.max_completion_tokens ?? body.max_tokens}`,
    `thinking=${body.thinking ? body.thinking.budget_tokens || "on" : "off"}`,
    `stream=${body.stream ? 1 : 0}`,
    `stop=${Array.isArray(body.stop_sequences) ? body.stop_sequences.length : 0}`,
  ];
  if (kinds.size) parts.push(`blocks=[${[...kinds].join(",")}]`);
  if (payload && Array.isArray(payload.messages)) parts.push(`out_msgs=${payload.messages.length}`);
  return parts.join(" ");
}

function anthropicToolsToOpenai(tools) {
  const out = [];
  for (const t of tools || []) {
    if (t && typeof t === "object" && t.name) {
      out.push({
        type: "function",
        function: {
          name: t.name,
          description: t.description || "",
          parameters: sanitizeSchema(t.input_schema || { type: "object", properties: {} }),
        },
      });
    }
  }
  return out;
}

function anthropicImageToOpenai(src) {
  if (!src || typeof src !== "object") return null;
  if (src.type === "base64" && src.data) {
    return { type: "image_url", image_url: { url: `data:${src.media_type || "image/png"};base64,${src.data}` } };
  }
  if (src.type === "url" && src.url) return { type: "image_url", image_url: { url: src.url } };
  return null;
}

// 图片能力**按 route 判定**，不能全局开关。
// ⚠ 2026-09-24 实测修正（用纯红 PNG 问"什么颜色"逐路由打上游验证）：
//   zai_auto                        text+image  ← 200 答"红色"（上游 model=deepseek/deepseek-v4-flash-vision-exp）
//   zai_auto-fast                   text+image  ← 200（官方 catalog 原本就标了 image）
//   zaicoding_glm-5.3               text only   ← 200 但模型自述 "image unsupported"（glm-5.3 coding 版无视觉）
//   tdpsk_deepseek-v4-flash-202605  text+image  ← 200 答"红色"（**反代此前把它当纯文本是错的**，
//                                                    用户看到的"v4.1 没有识图能力"就是这行白名单漏了它）
//   tdpsk_deepseek-v4-pro-202606    text only   ← 200 但模型自述 "Cannot see image"
//   zai_glm-5.3-flash               text+image  ← 200 答"红色"
// 给纯文本 route 发图：上游不会报错，但模型自己承认看不见图（占位文本会让它瞎答），
// 所以宁可在这里按实测矩阵挡掉，别让用户拿到"我不具备识图能力"这种自相矛盾的回答。
// （旧注释说"给纯文本 route 发图会 403 pay-view"：那是当时对 810002 的误归因，已证伪。）
const IMAGE_CAPABLE_ROUTES = new Set([
  "zai_auto", "zai_auto-fast", "zai_glm-5.3-flash",
  "tdpsk_deepseek-v4-flash-202605",
]);

// 进程级兜底开关：AUTOCLAW_PASS_IMAGES=1 强制全部透传，=0 强制全部占位。
const PASS_IMAGES_ENV = process.env.AUTOCLAW_PASS_IMAGES;

function routeSupportsImages(route) {
  if (PASS_IMAGES_ENV === "1") return true;
  if (PASS_IMAGES_ENV === "0") return false;
  return IMAGE_CAPABLE_ROUTES.has(route);
}

function imagePlaceholder(src) {
  const media = (src && (src.media_type || src.mediaType)) || "image";
  return `[image omitted: current model is text-only (${media})]`;
}

/** Anthropic 内容（字符串或块数组）-> OpenAI content（字符串或 multimodal 数组） */
function convertContent(content, { forUser = false, route = "" } = {}) {
  const passImages = routeSupportsImages(route);
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  const parts = [];
  let text = "";
  for (const b of content) {
    if (!b || typeof b !== "object") continue;
    switch (b.type) {
      case "text":
        text += b.text || "";
        break;
      case "image": {
        if (passImages) {
          const img = anthropicImageToOpenai(b.source);
          if (img) parts.push(img);
        } else {
          // 纯文本上游：换成占位文本，避免上游解析失败触发降级换模型
          text += imagePlaceholder(b.source);
        }
        break;
      }
      default: {
        // 未知块不静默丢弃造成信息缺失：能取到文本就并入，document 退化为占位
        if (typeof b.text === "string") { text += b.text; break; }
        if (b.type === "document") {
          const src = b.source;
          const t = src && (src.text || src.data ? `[document:${b.title || src.media_type || "attachment"}]` : "");
          if (t) text += t;
        }
        break; // thinking / redacted_thinking / tool_use 在 user 侧不转发
      }
    }
  }
  if (forUser && parts.length) {
    const arr = [];
    if (text) arr.push({ type: "text", text });
    arr.push(...parts);
    return arr;
  }
  return text;
}

function anthropicToOpenai(body, route = "") {
  const msgs = [];

  // system
  let system = body.system;
  if (Array.isArray(system)) {
    system = system
      .map((b) => (typeof b === "string" ? b : b && b.type === "text" ? b.text || "" : ""))
      .filter(Boolean)
      .join("\n");
  }
  if (typeof system === "string" && system.trim()) msgs.push({ role: "system", content: system });

  for (const m of body.messages || []) {
    const role = m.role;
    const content = m.content;

    if (role === "user") {
      if (Array.isArray(content)) {
        const trBlocks = content.filter((b) => b && b.type === "tool_result");
        const rest = content.filter((b) => b && b.type !== "tool_result");
        if (trBlocks.length) {
          for (const tr of trBlocks) {
            let inner = tr.content;
            if (Array.isArray(inner)) inner = inner.map((x) => (typeof x === "string" ? x : x && x.text)).filter(Boolean).join("\n");
            if (inner === undefined || inner === null) inner = "";
            msgs.push({
              role: "tool",
              tool_call_id: tr.tool_use_id || "",
              content: typeof inner === "string" ? inner : JSON.stringify(inner),
            });
          }
        }
        const txt = convertContent(rest, { forUser: true, route });
        if (typeof txt === "string" ? txt.trim() : (txt && txt.length)) {
          msgs.push({ role: "user", content: txt });
        } else if (!trBlocks.length) {
          msgs.push({ role: "user", content: "" });
        }
      } else {
        msgs.push({ role: "user", content: content == null ? "" : content });
      }
      continue;
    }

    if (role === "assistant") {
      let text = "";
      const toolUses = [];
      if (typeof content === "string") {
        text = content;
      } else if (Array.isArray(content)) {
        for (const b of content) {
          if (!b || typeof b !== "object") continue;
          if (b.type === "text") text += b.text || "";
          else if (b.type === "tool_use") toolUses.push(b);
          // thinking / redacted_thinking 丢弃（OpenAI 侧无对应）
        }
      }
      // 空 assistant 消息（只有 thinking 块或纯空）对上游无意义且易致解析异常，直接跳过
      if (!text && !toolUses.length) continue;
      const am = { role: "assistant", content: text || null };
      if (toolUses.length) {
        am.tool_calls = toolUses.map((t) => ({
          id: t.id || `call_${randomUUID().replace(/-/g, "").slice(0, 24)}`,
          type: "function",
          function: { name: t.name || "", arguments: JSON.stringify(t.input == null ? {} : t.input) },
        }));
      }
      msgs.push(am);
      continue;
    }
    // 其它 role 忽略
  }

  const out = { model: null, messages: msgs, max_completion_tokens: 4096 };

  let maxTokens = Number(body.max_tokens);
  if (!Number.isFinite(maxTokens) || maxTokens <= 0) maxTokens = 4096;
  if (body.thinking && body.thinking.type === "enabled" && Number(body.thinking.budget_tokens) > 0) {
    // 推理预算也要占用输出额度
    maxTokens = Math.max(maxTokens, Number(body.thinking.budget_tokens) + 2048);
  }
  // GLM 通道对 max_tokens 有硬上限：实测 131072 任意上下文都通过，163840 起
  // 一律 500 "parse response failed"（与大上下文叠加时更早触发）。ZCode 会按
  // 供应商 limit.output 发 307200，超限后必然失败并被降级到 DeepSeek，等于
  // 用户选 GLM 却拿到别的模型。这里钳到实测安全值，保证 GLM 路由始终可用。
  // AUTOCLAW_MAX_OUTPUT_TOKENS 可覆盖（0 = 不钳制）。
  if (MAX_OUTPUT_TOKENS > 0 && maxTokens > MAX_OUTPUT_TOKENS) {
    maxTokens = MAX_OUTPUT_TOKENS;
  }
  // 2026-09-30 起 2.0.2 网关对 body 里的 `max_tokens` 字段（OpenAI 已废弃参数）
  // 一律回 406 空 body —— 桌面端只发 `max_completion_tokens` 所以无恙，而这里
  // 是 ZCode 流量的必经翻译层。字段名照桌面端改，语义不变。
  out.max_completion_tokens = maxTokens;

  if (body.temperature != null) out.temperature = body.temperature;
  if (body.top_p != null) out.top_p = body.top_p;
  if (Array.isArray(body.stop_sequences) && body.stop_sequences.length) out.stop = body.stop_sequences;

  const tools = anthropicToolsToOpenai(body.tools);
  if (tools.length) {
    out.tools = tools;
    const tc = body.tool_choice;
    if (tc) {
      if (tc.type === "auto") out.tool_choice = "auto";
      else if (tc.type === "any") out.tool_choice = "required";
      else if (tc.type === "tool") out.tool_choice = { type: "function", function: { name: tc.name || "" } };
      else if (tc.type === "none") out.tool_choice = "none";
    }
  }
  return out;
}

// ---------------- OpenAI -> Anthropic ----------------
const STOP_REASON_MAP = { stop: "end_turn", length: "max_tokens", tool_calls: "tool_use", content_filter: "end_turn", function_call: "tool_use" };

function msgId() { return `msg_${randomUUID().replace(/-/g, "").slice(0, 24)}`; }
function toolId() { return `toolu_${randomUUID().replace(/-/g, "").slice(0, 24)}`; }

function openaiToAnthropic(resp, modelName) {
  const choice = (resp.choices || [{}])[0] || {};
  const message = choice.message || {};
  const content = [];
  const reasoning = message.reasoning_content || message.reasoning;
  if (reasoning) content.push({ type: "thinking", thinking: reasoning, signature: "" });

  let text = message.content;
  if (Array.isArray(text)) text = text.map((b) => (typeof b === "string" ? b : b && b.text) || "").join("");
  if (text) content.push({ type: "text", text });

  for (const tc of message.tool_calls || []) {
    const fn = tc.function || {};
    let args;
    try { args = JSON.parse(fn.arguments || "{}"); } catch { args = { _raw: fn.arguments || "" }; }
    content.push({ type: "tool_use", id: tc.id || toolId(), name: fn.name || "", input: args });
  }
  if (!content.length) content.push({ type: "text", text: "" });

  const usage = resp.usage || {};
  return {
    id: msgId(),
    type: "message",
    role: "assistant",
    // 回显客户端请求的模型名，便于客户端按名字匹配（上游返回的是 broker 内部名）
    model: modelName || resp.model,
    content,
    stop_reason: STOP_REASON_MAP[choice.finish_reason] || "end_turn",
    stop_sequence: null,
    usage: {
      input_tokens: usage.prompt_tokens || 0,
      output_tokens: usage.completion_tokens || 0,
    },
  };
}

// ---------------- 流式：OpenAI SSE -> Anthropic SSE ----------------
function sse(obj) { return `data: ${JSON.stringify(obj)}\n\n`; }

async function* streamAnthropic(upstream, modelName) {
  const id = msgId();
  yield sse({
    type: "message_start",
    message: {
      id, type: "message", role: "assistant", model: modelName,
      content: [], stop_reason: null, stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: 0 },
    },
  });

  const blocks = new Map();   // index -> state
  let openIdx = null;
  let nextIndex = 0;
  let finish = null;
  const usage = { input_tokens: 0, output_tokens: 0 };
  let sawUsage = false;

  function* closeOpen() {
    if (openIdx !== null) { yield sse({ type: "content_block_stop", index: openIdx }); openIdx = null; }
  }
  function* findOrOpen(kind, match, make) {
    for (const [i, st] of blocks) if (st.kind === kind && (match ? match(st) : true)) { return i; }
    yield* closeOpen();
    const idx = nextIndex++;
    blocks.set(idx, make());
    openIdx = idx;
    yield sse({ type: "content_block_start", index: idx, content_block: blocks.get(idx).block });
    return idx;
  }

  const decoder = new TextDecoder();
  let buf = "";
  try {
    for await (const chunk of upstream.body) {
      buf += decoder.decode(chunk, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        if (payload === "[DONE]") { buf = ""; break; }
        let c;
        try { c = JSON.parse(payload); } catch { continue; }
        if (c.usage) {
          usage.input_tokens = c.usage.prompt_tokens || usage.input_tokens;
          usage.output_tokens = c.usage.completion_tokens || usage.output_tokens;
          sawUsage = true;
        }
        const ch = (c.choices || [])[0];
        if (!ch) continue;
        if (ch.finish_reason) finish = ch.finish_reason;
        const d = ch.delta || {};

        const reasoning = d.reasoning_content != null ? d.reasoning_content : d.reasoning;
        if (reasoning) {
          const idx = yield* findOrOpen("thinking", null, () => ({ kind: "thinking", block: { type: "thinking", thinking: "", signature: "" } }));
          yield sse({ type: "content_block_delta", index: idx, delta: { type: "thinking_delta", thinking: reasoning } });
        }
        if (typeof d.content === "string" && d.content) {
          const idx = yield* findOrOpen("text", null, () => ({ kind: "text", block: { type: "text", text: "" } }));
          yield sse({ type: "content_block_delta", index: idx, delta: { type: "text_delta", text: d.content } });
        }
        for (const tc of d.tool_calls || []) {
          const oi = tc.index;
          const idx = yield* findOrOpen(
            "tool_use",
            (st) => (oi == null ? true : st.oi === oi),
            () => {
              const tid = tc.id || toolId();
              const name = (tc.function && tc.function.name) || "";
              return { kind: "tool_use", oi, id: tid, name, json: "", block: { type: "tool_use", id: tid, name, input: {} } };
            },
          );
          const frag = (tc.function && tc.function.arguments) || "";
          if (frag) {
            const st = blocks.get(idx);
            st.json += frag;
            yield sse({ type: "content_block_delta", index: idx, delta: { type: "input_json_delta", partial_json: frag } });
          }
        }
      }
    }
  } finally {
    upstream.body && upstream.body.cancel && upstream.body.cancel().catch(() => {});
  }

  // 收尾：JSON 未闭合则补一个空对象
  if (openIdx !== null) {
    const st = blocks.get(openIdx);
    if (st && st.kind === "tool_use" && st.json) {
      try { JSON.parse(st.json); } catch { /* 片段未闭合，尽力而为 */ }
    }
    yield* closeOpen();
  }

  yield sse({
    type: "message_delta",
    delta: { stop_reason: STOP_REASON_MAP[finish] || "end_turn", stop_sequence: null },
    usage: { input_tokens: usage.input_tokens, output_tokens: usage.output_tokens },
  });
  yield sse({ type: "message_stop" });
  yield "data: [DONE]\n\n";
  if (!sawUsage) log("warn: upstream stream had no usage chunk");
}

// ---------------- HTTP 工具 ----------------
function sendJson(res, code, obj) {
  // 池等待前已经发过 200/event-stream：状态码改不了，JSON 也必须改写成 SSE 帧。
  if (res._earlySse) {
    const kind = res._earlySse.kind || "chat";
    const err = obj && obj.error;
    if (err) {
      const msg = String(err.message || "relay error");
      failEarlySse(res, kind, msg, err.type || "server_error");
      log(`early-sse: 原 HTTP ${code} 改写为 SSE 错误帧: ${msg.slice(0, 160)}`);
      return;
    }
    stopEarlySse(res);
    try {
      res.write(`data: ${JSON.stringify(obj)}\n\n`);
      if (kind === "chat") res.write("data: [DONE]\n\n");
      res.end();
    } catch { /* 客户端已走 */ }
    log(`early-sse: 原 HTTP ${code} JSON 改写为单帧 SSE`);
    return;
  }
  const body = Buffer.from(JSON.stringify(obj), "utf8");
  res.writeHead(code, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": body.length,
    "Cache-Control": "no-store",
  });
  res.end(body);
}
function anthropicError(res, code, message, type = "api_error") {
  sendJson(res, code, { type: "error", error: { type, message } });
}
function openaiError(res, code, message, type = "server_error") {
  sendJson(res, code, { error: { message, type, code: String(code) } });
}

// 单请求输入字符数硬上限校验（保命机制：09-22 的元凶是 51万字符单体巨请求反复重放）。
// 返回 null 表示通过；否则返回一条可直接发给客户端的错误信息。
function checkInputSize(body, isAnthropic) {
  if (!MAX_INPUT_CHARS) return null;
  let chars = 0;
  const msgs = (body && body.messages) || [];
  for (const m of msgs) {
    const c = m.content;
    if (typeof c === "string") chars += c.length;
    else if (Array.isArray(c)) {
      for (const b of c) {
        if (!b || typeof b !== "object") continue;
        if (typeof b.text === "string") chars += b.text.length;
        else if (typeof b.input === "string") chars += b.input.length;       // tool_result 内文本
        else if (typeof b.content === "string") chars += b.content.length;
      }
    }
  }
  if (typeof body?.system === "string") chars += body.system.length;
  if (chars > MAX_INPUT_CHARS) {
    const k = Math.round(MAX_INPUT_CHARS / 1000);
    const got = Math.round(chars / 1000);
    const msg = `单请求输入过长（约 ${got}k 字符，上限 ${k}k）。请拆分上下文/分批附文件，不要把超长历史一次性塞进单条消息——这既会触发上游批量行为风控，也会打空账号额度。`;
    return isAnthropic
      ? { code: 413, type: "request_too_large", message: msg }
      : { code: 413, type: "invalid_request_error", message: msg };
  }
  return null;
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY) { reject(new Error("body too large")); req.destroy(); return; }
      chunks.push(c);
    });
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      if (!raw.trim()) return resolve({});
      try { resolve(JSON.parse(raw)); } catch (e) { reject(new Error("invalid JSON body")); }
    });
    req.on("error", reject);
  });
}

async function callBroker(base, route, payload, { stream }, externalSignal) {
  const token = readGatewayToken();
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), UPSTREAM_TIMEOUT_MS);
  const onExternalAbort = () => ctl.abort();
  if (externalSignal) {
    if (externalSignal.aborted) ctl.abort();
    else externalSignal.addEventListener("abort", onExternalAbort, { once: true });
  }
  try {
    return await fetch(`${base}/v1/chat/completions`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${token}`,
        "Content-Type": "application/json",
        "Accept": stream ? "text/event-stream" : "application/json",
        "x-autoclaw-model-route": route,
      },
      body: JSON.stringify(payload),
      signal: ctl.signal,
    });
  } finally {
    clearTimeout(t);
    if (externalSignal) externalSignal.removeEventListener("abort", onExternalAbort);
  }
}

// ---------------- 请求处理 ----------------
async function handleMessages(req, res, body) {
  let up = await getUpstream();
  if (!up) {
    up = await getUpstream(true);
    if (!up) {
      return anthropicError(res, 503,
        "AutoClaw 未运行，且本机没有可用凭证（request-headers.json 里没有 X-Authorization）。"
        + "请先启动一次 AutoClaw 桌面端登录，之后即使退出也能直连云端。");
    }
  }

  const requested = body.model || DEFAULT_ROUTE;
  const route = normalizeRoute(requested);
  const isStream = Boolean(body.stream);

  const sizeErr = checkInputSize(body, true);
  if (sizeErr) return anthropicError(res, sizeErr.code, sizeErr.message, sizeErr.type);

  let payload;
  try { payload = anthropicToOpenai(body, route); }
  catch (e) { return anthropicError(res, 400, `请求转换失败: ${scrub(e.message)}`, "invalid_request_error"); }
  payload.model = route;
  payload.stream = isStream;
  applySteering(payload);   // 带 tools 的 agentic 请求注入插嘴纪律（幂等）
  applyHarnessMarker(payload);  // 云通道 406 闸门：system 提示词必须以 harness 标记开头（幂等）
  if (isStream) payload.stream_options = { include_usage: true };  // 流式也要 usage（message_delta 里回给客户端）

  const started = Date.now();
  const digest = reqDigest(body, payload);

  // 上游偶发 500（如 GLM 通道 "parse response failed"）：重试 + 降级到备用路由
  const plan = [route];
  if (RETRY_ON_UPSTREAM_ERROR) plan.push(route);
  const fb = FALLBACK_ROUTE && FALLBACK_ROUTE !== route ? FALLBACK_ROUTE : "";
  if (fb) plan.push(fb);

  let upstream = null, usedRoute = route, lastStatus = 0, lastDetail = "";
  // 瞬态高峰限流：同模型重试（PEAK_RETRY_DELAYS_MS），不换模型、不换反代
  let peakTries = 0;
  for (let i = 0; i < plan.length; i++) {
    const r = plan[i];
    const p = { ...payload, model: r };
    // 并发闸：整池对上游的在途数卡在 POOL_MAX_CONCURRENCY，杜绝 pile-on 秒级 10 连发
    if (POOL_MAX_CONCURRENCY) {
      const ok = await acquireUpstreamSlot();
      if (!ok) {
        return anthropicError(res, 429, `并发已满（上限 ${POOL_MAX_CONCURRENCY}），请稍后重试或降低并发。`, "rate_limit_error");
      }
    }
    try {
      upstream = await callUpstream(up, r, p, { stream: isStream, externalSignal: res._reqCtrl && res._reqCtrl.signal });
    } catch (e) {
      if (POOL_MAX_CONCURRENCY) releaseUpstreamSlot();
      // 客户端已断开：后面的重试只会再打一轮空（含 freshAccessToken 的现刷），直接收工
      if (res._reqCtrl && res._reqCtrl.signal.aborted) return;
      const again = await getUpstream(true);
      if (again) up = again;
      log(`POST /v1/messages model=${requested} route=${r} attempt#${i + 1} upstream error: ${scrub(e.message)} | ${digest}`);
      upstream = null;
      continue;
    }
    if (POOL_MAX_CONCURRENCY) releaseUpstreamSlot();
    if (upstream.ok) { usedRoute = r; break; }

    const detail = await readDetail(upstream);
    lastStatus = upstream.status; lastDetail = scrub(detail);
    // broker lane 额度打空（402 积分不足 / 810000 / quota）：当场换到账号池，用户无感。
    // 关键：这里**不能**走下面的 QUOTA_HARD_RE 分支 —— 那是"池子也换过了，回终态 400"，
    // 对 broker 而言池子还没试过，直接失败就是把可用的号浪费掉（09-21 402 事故就是这个）。
    if (brokerHitQuota(up, detail)) {
      const t = laneSwitchTarget();
      if (t) {
        up = t;
        brokerLane.switches++;
        log(`lane: 本请求改用账号池（${poolCandidates().length} 个号可选）| ${digest}`);
        i--;                      // 不推进 plan 游标，用同一个 route 在新 lane 上重试
        continue;
      }
      return anthropicError(res, 402, `模型 ${requested} 当前不可用：${lastDetail.slice(0, 200)}（broker 额度打空且账号池无可用号）`);
    }
    if (upstream.status === 401 || upstream.status === 403) {
      const again = await getUpstream(true);
      if (again) up = again;
    }
    log(`POST /v1/messages model=${requested} route=${r} attempt#${i + 1}/${plan.length} upstream ${upstream.status}: ${lastDetail} | ${digest}`);
    upstream = null;

    // 真·额度耗尽（810000/quota/余额类）：**不降级不重试**，直接返回 400 终态错误。
    if (QUOTA_HARD_RE.test(detail)) {
      log(`quota error on ${r}: 不降级，直接返回错误 | ${digest}`);
      return anthropicError(res, 400,
        `模型 ${requested} 当前不可用：${lastDetail.slice(0, 200)}`
        + (up.pool ? `（账号池已换过 ${POOL_TRIES} 个号）` : ""));
    }

    // 瞬态高峰限流（810002 + high demand 文案）：同模型退避重试；耗尽后回 429
    //（ZCode 视为可稍后重试），绝不返回会让用户以为模型永久挂了的 400。
    if (TRANSIENT_PEAK_RE.test(detail) && !QUOTA_HARD_RE.test(detail)) {
      const pd = up.pool ? PEAK_DELAYS_WITH_POOL : PEAK_RETRY_DELAYS_MS;
      if (peakTries < pd.length) {
        const d = pd[peakTries++];
        log(`transient peak on ${r}: retry in ${d}ms (${peakTries}/${pd.length}) | ${digest}`);
        await new Promise((x) => setTimeout(x, d));
        i--; // 重试同一路由（不推进 plan 游标）
        continue;
      }
      log(`transient peak on ${r}: retries exhausted -> 429 | ${digest}`);
      return anthropicError(res, 429,
        `模型 ${requested} 上游高峰限流（810002 high demand），已重试 ${peakTries} 次未恢复，请稍后自动重试`);
    }

    if (i < plan.length - 1) await new Promise((x) => setTimeout(x, 300));
  }

  if (!upstream) {
    try {
      const dumpDir = path.join(BASE_DIR, "dumps");
      fs.mkdirSync(dumpDir, { recursive: true });
      const ts = new Date().toISOString().replace(/[:.]/g, "-");
      fs.writeFileSync(path.join(dumpDir, `failed-${ts}.inbound.json`), JSON.stringify(body, null, 1));
      fs.writeFileSync(path.join(dumpDir, `failed-${ts}.outbound.json`), JSON.stringify(payload, null, 1));
      fs.writeFileSync(path.join(dumpDir, `failed-${ts}.upstream.txt`), `${lastStatus} ${lastDetail}\n${digest}`);
      log(`dumped failed request -> dumps/failed-${ts}.*`);
    } catch (e2) { log("dump write failed: " + scrub(e2.message)); }
    return anthropicError(res, lastStatus === 401 ? 401 : 502,
      `${up.kind === "cloud" ? "AutoClaw 云端上游" : "AutoClaw Broker"} 返回 ${lastStatus || "连接失败"}（已重试 ${plan.length} 次）: ${lastDetail || "无响应体"}`);
  }
  if (usedRoute !== route) log(`degraded: ${route} -> ${usedRoute} | ${digest}`);

    // 客户端随时可能中断（ZCode 取消请求、切模型等）：吞掉 socket 错误，避免进程崩溃
    req.on("error", (e) => log("req socket error:", scrub(e.message)));
    res.on("error", (e) => log("res socket error:", scrub(e.message)));

    if (isStream) {
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
      });
      try {
        for await (const out of streamAnthropic(upstream, requested)) {
          if (res.writableEnded || res.destroyed) break;
          try { res.write(out); } catch (e) { log("stream write failed:", scrub(e.message)); break; }
        }
      } catch (e) {
        log(`stream error model=${requested}: ${scrub(e.message)}`);
        try { res.write(sse({ type: "error", error: { type: "api_error", message: scrub(e.message) } })); } catch { /* ignore */ }
      }
      try { res.end(); } catch { /* ignore */ }
    log(`POST /v1/messages(stream) model=${requested} -> ${usedRoute} ${Date.now() - started}ms | ${digest}`);
    return;
  }

  let data;
  try { data = await upstream.json(); }
  catch (e) { return anthropicError(res, 502, `上游返回非 JSON: ${scrub(e.message)}`); }
  const result = openaiToAnthropic(data, requested);
  sendJson(res, 200, result);
  log(`POST /v1/messages model=${requested} -> ${usedRoute} ${Date.now() - started}ms stop=${result.stop_reason} in=${result.usage.input_tokens} out=${result.usage.output_tokens} | ${digest}`);
}

// ============================================================
// OpenAI Responses API 兼容层（Codex CLI / Codex 系 harness 接入）
// ------------------------------------------------------------
// 背景：Codex 的 config.toml 里 wire_api 只支持 "responses"（官方文档：
// "responses is the only supported value"），无法直接用 chat 端点。
// 本层把入站 Responses 请求（input / tools / stream）翻译成 chat completions
// 发给上游，再把结果翻译回 Responses 事件流。
//
// 取舍（最小可用版）：
//   - 不持久化 previous_response_id：Codex 每轮带完整 input 历史，只做格式翻译
//   - 支持：文本消息、instructions、function tools、function_call 回传、流式/非流式
//   - 不支持：内置工具（web_search 等）、图像输入（降级为文本占位）
// ============================================================

/** Responses input[] -> chat messages[] */
function responsesInputToMessages(body) {
  const msgs = [];
  const instr = typeof body.instructions === "string" ? body.instructions.trim() : "";
  if (instr) msgs.push({ role: "system", content: instr });

  const input = body.input;
  if (typeof input === "string") {
    msgs.push({ role: "user", content: input });
    return msgs;
  }
  if (!Array.isArray(input)) return msgs;

  for (const item of input) {
    if (!item || typeof item !== "object") continue;
    const t = item.type || (item.role ? "message" : "");
    if (t === "message") {
      const role = item.role || "user";
      const parts = Array.isArray(item.content)
        ? item.content
        : [{ type: "input_text", text: String(item.content ?? "") }];
      const texts = [];
      for (const p of parts) {
        if (!p) continue;
        if (p.type === "input_text" || p.type === "output_text" || p.type === "text") texts.push(p.text || "");
        else if (p.type === "input_image") texts.push("[image omitted: upstream is text-only]");
        else if (p.type === "refusal") texts.push(p.refusal || "");
      }
      if (texts.length) msgs.push({ role, content: texts.join("\n") });
    } else if (t === "function_call") {
      msgs.push({
        role: "assistant",
        content: null,
        tool_calls: [{
          id: item.call_id || item.id,
          type: "function",
          function: { name: item.name, arguments: item.arguments || "{}" },
        }],
      });
    } else if (t === "function_call_output") {
      msgs.push({
        role: "tool",
        tool_call_id: item.call_id || item.id,
        content: typeof item.output === "string" ? item.output : JSON.stringify(item.output ?? ""),
      });
    } else if (t === "reasoning") {
      const sum = Array.isArray(item.summary) ? item.summary.map((s) => s.text || "").join("\n") : "";
      if (sum) msgs.push({ role: "assistant", content: sum });
    }
  }
  if (!msgs.some((m) => m.role !== "system" && m.role !== "developer")) {
    msgs.push({ role: "user", content: "." });
  }
  return msgs;
}

/** Responses tools[] -> chat tools[] */
function responsesToolsToChat(tools) {
  if (!Array.isArray(tools)) return undefined;
  const out = [];
  for (const t of tools) {
    if (!t || t.type !== "function") continue;
    out.push({
      type: "function",
      function: {
        name: t.name,
        description: t.description || "",
        parameters: t.parameters || { type: "object", properties: {} },
      },
    });
  }
  return out.length ? out : undefined;
}

/** Responses 请求 -> chat 请求体 */
function responsesToChatRequest(body) {
  const req = {
    model: body.model,
    messages: responsesInputToMessages(body),
    stream: Boolean(body.stream),
  };
  const tools = responsesToolsToChat(body.tools);
  if (tools) req.tools = tools;
  if (body.tool_choice != null) {
    const tc = body.tool_choice;
    req.tool_choice = (tc === "auto" || tc === "none" || tc === "required")
      ? tc
      : (tc && tc.type === "function" ? { type: "function", function: { name: tc.name } } : "auto");
  }
  if (typeof body.temperature === "number") req.temperature = body.temperature;
  if (typeof body.top_p === "number") req.top_p = body.top_p;
  // 与 /v1/messages (anthropicToOpenai) 对齐：输出上限钳到 GLM 通道实测安全值，
  // 否则 Codex 按供应商 limit.output 发的 307200 会裸传上游 → 必 500 且等于白给上限。
  let maxTokens = Number(body.max_output_tokens ?? body.max_tokens);
  if (!Number.isFinite(maxTokens) || maxTokens <= 0) maxTokens = 4096;
  if (MAX_OUTPUT_TOKENS > 0 && maxTokens > MAX_OUTPUT_TOKENS) maxTokens = MAX_OUTPUT_TOKENS;
  // 2.0.2 网关拒收 max_tokens 字段（406），云上只认 max_completion_tokens
  req.max_completion_tokens = maxTokens;
  if (typeof body.parallel_tool_calls === "boolean") req.parallel_tool_calls = body.parallel_tool_calls;
  return req;
}

/** 生成 responses 的 response 对象骨架 */
function responseSkeleton(id, model, status, extra = {}) {
  return {
    id, object: "response", created_at: Math.floor(Date.now() / 1000),
    status, model, output: [], parallel_tool_calls: true,
    tool_choice: "auto", tools: [], error: null, incomplete_details: null,
    instructions: null, metadata: {}, temperature: 1, top_p: 1, max_output_tokens: null,
    previous_response_id: null, reasoning: null, usage: null, user: null,
    ...extra,
  };
}

/** chat 响应 -> responses 响应体 */
function chatResponseToResponses(chat, id, model) {
  const r = responseSkeleton(id, model, "completed");
  const choice = (chat.choices && chat.choices[0]) || {};
  const msg = choice.message || {};
  const out = [];
  const reasoning = msg.reasoning_content || "";
  if (reasoning) {
    out.push({
      id: "rs_" + id.slice(5), type: "reasoning", status: "completed",
      summary: [{ type: "summary_text", text: String(reasoning) }],
    });
  }
  if (msg.content) {
    out.push({
      id: "msg_" + id.slice(5), type: "message", role: "assistant", status: "completed",
      content: [{ type: "output_text", text: String(msg.content), annotations: [] }],
    });
  }
  for (const tc of msg.tool_calls || []) {
    out.push({
      id: "fc_" + String(tc.id || "").replace(/^call_/, ""), type: "function_call",
      status: "completed", call_id: tc.id,
      name: tc.function && tc.function.name,
      arguments: (tc.function && tc.function.arguments) || "{}",
    });
  }
  r.output = out;
  if (chat.usage) {
    r.usage = {
      input_tokens: chat.usage.prompt_tokens || 0,
      input_tokens_details: { cached_tokens: (chat.usage.prompt_tokens_details || {}).cached_tokens || 0 },
      output_tokens: chat.usage.completion_tokens || 0,
      output_tokens_details: { reasoning_tokens: (chat.usage.completion_tokens_details || {}).reasoning_tokens || 0 },
      total_tokens: chat.usage.total_tokens || 0,
    };
  }
  if (choice.finish_reason === "length") {
    r.status = "incomplete";
    r.incomplete_details = { reason: "max_output_tokens" };
  }
  return r;
}

/** 把聚合后的 chat 结果写成 responses SSE 事件流 */
function streamChatAsResponses(chatBody, res, id, model) {
  const out = (s) => { try { res.write(`event: ${s.type}\ndata: ${JSON.stringify(s)}\n\n`); } catch { /* client gone */ } };
  const skel = responseSkeleton(id, model, "in_progress");
  let seq = 0;
  out({ type: "response.created", sequence_number: seq++, response: skel });
  out({ type: "response.in_progress", sequence_number: seq++, response: skel });

  const mid = "msg_" + id.slice(5);
  if (chatBody.content) {
    out({
      type: "response.output_item.added", sequence_number: seq++, output_index: 0,
      item: { id: mid, type: "message", role: "assistant", status: "in_progress", content: [] },
    });
    out({
      type: "response.content_part.added", sequence_number: seq++,
      item_id: mid, output_index: 0, content_index: 0,
      part: { type: "output_text", text: "", annotations: [] },
    });
    const chunkSize = 120;
    for (let i = 0; i < chatBody.content.length; i += chunkSize) {
      out({
        type: "response.output_text.delta", sequence_number: seq++,
        item_id: mid, output_index: 0, content_index: 0,
        delta: chatBody.content.slice(i, i + chunkSize),
      });
    }
    out({
      type: "response.output_text.done", sequence_number: seq++,
      item_id: mid, output_index: 0, content_index: 0, text: chatBody.content,
    });
    out({
      type: "response.content_part.done", sequence_number: seq++,
      item_id: mid, output_index: 0, content_index: 0,
      part: { type: "output_text", text: chatBody.content, annotations: [] },
    });
    out({
      type: "response.output_item.done", sequence_number: seq++, output_index: 0,
      item: {
        id: mid, type: "message", role: "assistant", status: "completed",
        content: [{ type: "output_text", text: chatBody.content, annotations: [] }],
      },
    });
  }
  const final = chatResponseToResponses(
    { choices: [{ message: chatBody, finish_reason: chatBody.finish_reason }], usage: chatBody.usage },
    id, model);
  final.status = "completed";
  out({ type: "response.completed", sequence_number: seq++, response: final });
  try { res.write("data: [DONE]\n\n"); res.end(); } catch { /* ignore */ }
}

/** 把上游 chat SSE 聚合为完整结果（翻译层需要完整文本才能生成 responses 事件） */
async function accumulateChatStream(upstream) {
  const acc = { content: "", reasoning: "", tool_calls: {}, usage: null, finish_reason: null };
  const decoder = new TextDecoder();
  let buf = "";
  for await (const raw of upstream.body) {
    buf += decoder.decode(raw, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop() || "";
    for (const line of lines) {
      const s = line.trim();
      if (!s.startsWith("data:")) continue;
      const payload = s.slice(5).trim();
      if (!payload || payload === "[DONE]") continue;
      let j;
      try { j = JSON.parse(payload); } catch { continue; }
      if (j.usage) acc.usage = j.usage;
      const ch = (j.choices && j.choices[0]) || {};
      if (ch.finish_reason) acc.finish_reason = ch.finish_reason;
      const d = ch.delta || {};
      if (d.reasoning_content) acc.reasoning += d.reasoning_content;
      if (d.content) acc.content += d.content;
      for (const tc of d.tool_calls || []) {
        const idx = tc.index ?? 0;
        if (!acc.tool_calls[idx]) acc.tool_calls[idx] = { id: tc.id || `call_${idx}`, name: "", arguments: "" };
        if (tc.id) acc.tool_calls[idx].id = tc.id;
        if (tc.function && tc.function.name) acc.tool_calls[idx].name += tc.function.name;
        if (tc.function && tc.function.arguments) acc.tool_calls[idx].arguments += tc.function.arguments;
      }
    }
  }
  const tool_calls = Object.values(acc.tool_calls).map((t) => ({
    id: t.id, type: "function", function: { name: t.name, arguments: t.arguments || "{}" },
  }));
  return {
    content: acc.content,
    reasoning_content: acc.reasoning,
    tool_calls: tool_calls.length ? tool_calls : undefined,
    finish_reason: acc.finish_reason,
    usage: acc.usage,
  };
}

async function handleResponses(req, res, body) {
  const id = "resp_" + randomUUID().replace(/-/g, "").slice(0, 24);
  const model = body.model || DEFAULT_ROUTE;
  const isStream = Boolean(body.stream);

  const sizeErr = checkInputSize(body, false);
  if (sizeErr) return openaiError(res, sizeErr.code, sizeErr.message, sizeErr.type);

  let chatReq;
  try { chatReq = responsesToChatRequest(body); }
  catch (e) {
    return openaiError(res, 400, `responses->chat 转换失败: ${scrub(e.message)}`, "invalid_request_error");
  }
  applySteering(chatReq);   // 带 tools 的 agentic 请求注入插嘴纪律（幂等）
  applyHarnessMarker(chatReq);  // 云通道 406 闸门：system 提示词必须以 harness 标记开头（幂等）

  let up = await getUpstream();
  if (!up) {
    up = await getUpstream(true);
    if (!up) {
      return openaiError(res, 503,
        "AutoClaw 未运行且本机没有可用凭证，请先启动一次 AutoClaw 桌面端登录。");
    }
  }

  const route = normalizeRoute(model);
  const plan = [route];
  if (RETRY_ON_UPSTREAM_ERROR) plan.push(route);
  const fb = FALLBACK_ROUTE && FALLBACK_ROUTE !== route ? FALLBACK_ROUTE : "";
  if (fb) plan.push(fb);

  let upstream = null, lastStatus = 0, lastDetail = "";
  let peakTries = 0;
  for (let i = 0; i < plan.length; i++) {
    const r = plan[i];
    const p = { ...chatReq, model: r, stream: true, stream_options: { include_usage: true } };
    if (POOL_MAX_CONCURRENCY) {
      const ok = await acquireUpstreamSlot();
      if (!ok) {
        return openaiError(res, 429, `并发已满（上限 ${POOL_MAX_CONCURRENCY}），请稍后重试或降低并发。`, "rate_limit_error");
      }
    }
    try { upstream = await callUpstream(up, r, p, { stream: true, externalSignal: res._reqCtrl && res._reqCtrl.signal }); }
    catch (e) {
      if (POOL_MAX_CONCURRENCY) releaseUpstreamSlot();
      if (res._reqCtrl && res._reqCtrl.signal.aborted) return;
      await getUpstream(true);
      log(`POST /v1/responses model=${model} route=${r} attempt#${i + 1} upstream error: ${scrub(e.message)}`);
      upstream = null;
      continue;
    }
    if (POOL_MAX_CONCURRENCY) releaseUpstreamSlot();
    if (upstream.ok) break;
    let detail = "";
    try { detail = (await upstream.text()).slice(0, 500); } catch { /* ignore */ }
    lastStatus = upstream.status;
    lastDetail = scrub(detail);
    // broker 额度打空 → 当场换账号池（同 handleMessages 的 402 修法）
    if (brokerHitQuota(up, detail)) {
      const t = laneSwitchTarget();
      if (t) {
        up = t; brokerLane.switches++;
        log(`lane: responses 本请求改用账号池（${poolCandidates().length} 个号可选）`);
        i--; continue;
      }
      return openaiError(res, 402, `模型 ${model} 当前不可用：${lastDetail.slice(0, 200)}（broker 额度打空且账号池无可用号）`);
    }
    log(`POST /v1/responses model=${model} route=${r} attempt#${i + 1}/${plan.length} upstream ${upstream.status}: ${lastDetail}`);
    upstream = null;
    if (QUOTA_HARD_RE.test(detail)) {
      return openaiError(res, 400, `模型 ${model} 当前不可用：${lastDetail.slice(0, 200)}`, "invalid_request_error");
    }
    if (TRANSIENT_PEAK_RE.test(detail) && !QUOTA_HARD_RE.test(detail)) {
      if (peakTries < PEAK_RETRY_DELAYS_MS.length) {
        const d = PEAK_RETRY_DELAYS_MS[peakTries++];
        log(`responses: transient peak on ${r}: retry in ${d}ms (${peakTries}/${PEAK_RETRY_DELAYS_MS.length})`);
        await new Promise((x) => setTimeout(x, d));
        i--;
        continue;
      }
      return openaiError(res, 429, `模型 ${model} 上游高峰限流，已重试 ${peakTries} 次未恢复`);
    }
    if (i < plan.length - 1) await new Promise((x) => setTimeout(x, 300));
  }
  if (!upstream) {
    return openaiError(res, lastStatus === 401 ? 401 : 502,
      `AutoClaw 上游返回 ${lastStatus || "连接失败"}: ${lastDetail || "无响应体"}`);
  }

  req.on("error", (e) => log("req socket error:", scrub(e.message)));
  res.on("error", (e) => log("res socket error:", scrub(e.message)));

  try {
    const chatBody = await accumulateChatStream(upstream);
    if (isStream) {
      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
      });
      streamChatAsResponses(chatBody, res, id, model);
      log(`POST /v1/responses(stream) model=${model} -> ${route} ok`);
    } else {
      const resp = chatResponseToResponses(
        { choices: [{ message: chatBody, finish_reason: chatBody.finish_reason }], usage: chatBody.usage },
        id, model);
      log(`POST /v1/responses model=${model} -> ${route} completed`);
      sendJson(res, 200, resp);
    }
  } catch (e) {
    log("responses translate error:", scrub(e.message));
    if (!res.headersSent) return openaiError(res, 502, `responses 转换失败: ${scrub(e.message)}`);
    try { res.end(); } catch { /* ignore */ }
  }
}

async function handleChatCompletions(req, res, body) {
  let up = await getUpstream();
  if (!up) {
    up = await getUpstream(true);
    if (!up) return openaiError(res, 503,
      "AutoClaw 未运行且本机没有可用凭证（request-headers.json 缺 X-Authorization），请先启动一次 AutoClaw 桌面端登录。");
  }
  const requested = body.model || DEFAULT_ROUTE;
  const route = normalizeRoute(requested);
  const isStream = Boolean(body.stream);
  const sizeErr = checkInputSize(body, false);
  if (sizeErr) return openaiError(res, sizeErr.code, sizeErr.message, sizeErr.type);
  const payload = { ...body, model: route };
  delete payload.stream_options;
  // 2.0.2 网关拒收 max_tokens 字段（406 空 body）：透传路径把客户端的 max_tokens
  // 改写成 max_completion_tokens（桌面端同款字段），语义相同。
  if (payload.max_tokens != null) {
    if (payload.max_completion_tokens == null) payload.max_completion_tokens = payload.max_tokens;
    delete payload.max_tokens;
  }
  applySteering(payload);   // 带 tools 的 agentic 请求注入插嘴纪律（幂等）
  applyHarnessMarker(payload);  // 云通道 406 闸门：system 提示词必须以 harness 标记开头（幂等）
  // 用量可视化：流式请求向上游要 usage（include_usage）——new-api 靠它记账，
  // OpenAI SDK 用户也能在 stream 上拿到最终 token 数；透传路径只观察不重写帧。
  if (isStream) payload.stream_options = { include_usage: true };

  const started = Date.now();

  // 重试 + 降级（与 Anthropic 路径一致），应对上游偶发 5xx
  const plan = [route];
  if (RETRY_ON_UPSTREAM_ERROR) plan.push(route);
  const fb = FALLBACK_ROUTE && FALLBACK_ROUTE !== route ? FALLBACK_ROUTE : "";
  if (fb) plan.push(fb);

  let upstream = null, usedRoute = route, lastStatus = 0, lastDetail = "";
  let peakTries = 0;
  for (let i = 0; i < plan.length; i++) {
    const r = plan[i];
    const p = { ...payload, model: r };
    if (POOL_MAX_CONCURRENCY) {
      const ok = await acquireUpstreamSlot();
      if (!ok) {
        return openaiError(res, 429, `并发已满（上限 ${POOL_MAX_CONCURRENCY}），请稍后重试或降低并发。`, "rate_limit_error");
      }
    }
    try {
      upstream = await callUpstream(up, r, p, {
        stream: isStream,
        externalSignal: res._reqCtrl && res._reqCtrl.signal,
        // 池等待超过 5s 时先把 SSE 头发出去（仅流式）：DSH 的 300s headersTimeout 不再先断
        onPoolWait: isStream ? () => beginEarlySse(res, "chat") : null,
      });
    } catch (e) {
      if (POOL_MAX_CONCURRENCY) releaseUpstreamSlot();
      if (res._reqCtrl && res._reqCtrl.signal.aborted) return;
      const again = await getUpstream(true);
      if (again) up = again;
      log(`POST /v1/chat/completions model=${requested} route=${r} attempt#${i + 1} upstream error: ${scrub(e.message)}`);
      upstream = null;
      continue;
    }
    if (POOL_MAX_CONCURRENCY) releaseUpstreamSlot();
    if (upstream.ok) { usedRoute = r; break; }
    const detail = await readDetail(upstream);
    lastStatus = upstream.status; lastDetail = scrub(detail);
    // broker 额度打空 → 当场换账号池（同 handleMessages 的 402 修法）
    if (brokerHitQuota(up, detail)) {
      const t = laneSwitchTarget();
      if (t) {
        up = t; brokerLane.switches++;
        log(`lane: chat 本请求改用账号池（${poolCandidates().length} 个号可选）`);
        i--; continue;
      }
      return openaiError(res, 402, `模型 ${requested} 当前不可用：${lastDetail.slice(0, 200)}（broker 额度打空且账号池无可用号）`);
    }
    if (upstream.status === 401 || upstream.status === 403) {
      const again = await getUpstream(true);
      if (again) up = again;
    }
    log(`POST /v1/chat/completions model=${requested} route=${r} attempt#${i + 1}/${plan.length} upstream ${upstream.status}: ${lastDetail}`);
    upstream = null;
    if (QUOTA_HARD_RE.test(detail)) {
      log(`quota error on ${r}: 直接返回错误`);
      return openaiError(res, 400, `模型 ${requested} 当前不可用：${lastDetail.slice(0, 200)}`
        + (up.pool ? `（账号池已换过 ${POOL_TRIES} 个号）` : ""));
    }
    if (TRANSIENT_PEAK_RE.test(detail) && !QUOTA_HARD_RE.test(detail)) {
      const pd = up.pool ? PEAK_DELAYS_WITH_POOL : PEAK_RETRY_DELAYS_MS;
      if (peakTries < pd.length) {
        const d = pd[peakTries++];
        log(`transient peak on ${r}: retry in ${d}ms (${peakTries}/${pd.length})`);
        await new Promise((x) => setTimeout(x, d));
        i--;
        continue;
      }
      log(`transient peak on ${r}: retries exhausted -> 429`);
      return openaiError(res, 429, `模型 ${requested} 上游高峰限流（810002 high demand），已重试 ${peakTries} 次未恢复`);
    }
    if (i < plan.length - 1) await new Promise((x) => setTimeout(x, 300));
  }

  if (!upstream) {
    return openaiError(res, lastStatus === 401 ? 401 : 502,
      `${up.kind === "cloud" ? "AutoClaw 云端上游" : "AutoClaw Broker"} 返回 ${lastStatus || "连接失败"}（已重试 ${plan.length} 次）: ${lastDetail || "无响应体"}`);
  }
  if (usedRoute !== route) log(`degraded: ${route} -> ${usedRoute} model=${requested}`);

  req.on("error", (e) => log("req socket error:", scrub(e.message)));
  res.on("error", (e) => log("res socket error:", scrub(e.message)));

  const ctype = upstream.headers.get("content-type") || "application/json";
  if (isStream && ctype.includes("event-stream")) {
    // 池等待期可能已经发过 200/SSE 头：这里只停心跳，绝不能再写一次头。
    stopEarlySse(res);
    if (!res.headersSent) res.writeHead(200, SSE_HEADERS);
    // 原样透传（绝不对 SSE 重拆行——帧一旦变形就是掐流），只顺带扫一眼有没有 usage 块：
    // 有 = new-api 能按流式记账；没有 = 打 warn（说明上游不认 include_usage，要去查上游）。
    let sawUsage = false;
    try {
      for await (const chunk of upstream.body) {
        if (res.writableEnded || res.destroyed) break;
        if (!sawUsage && chunk.toString("utf8").includes('"usage"')) sawUsage = true;
        try { res.write(chunk); } catch (e) { log("passthrough write failed:", scrub(e.message)); break; }
      }
    } catch (e) { log("passthrough stream error:", scrub(e.message)); }
    if (!sawUsage) log(`warn: chat stream model=${requested} 无 usage 块（上游不认 include_usage？）——流式用量记账会缺`);
    try { res.end(); } catch { /* ignore */ }
    log(`POST /v1/chat/completions(stream) model=${requested} -> ${usedRoute} ${Date.now() - started}ms`);
    return;
  }

  // 已经早发过 200/event-stream，但上游没按流式回（JSON 200）：不能再 writeHead 改状态，
  // 把 JSON 包成单帧 SSE + [DONE]，客户端拿到的仍是合法流。
  if (isStream && res._earlySse) {
    const text = await upstream.text();
    stopEarlySse(res);
    if (!res.headersSent) res.writeHead(200, SSE_HEADERS);
    try {
      res.write(`data: ${text}\n\n`);
      res.write("data: [DONE]\n\n");
      res.end();
    } catch { /* 客户端已走 */ }
    log(`POST /v1/chat/completions(stream) model=${requested} -> ${usedRoute} JSON→SSE 单帧 ${Date.now() - started}ms`);
    return;
  }

  const text = await upstream.text();
  const buf = Buffer.from(text, "utf8");
  res.writeHead(upstream.status, { "Content-Type": ctype, "Content-Length": buf.length });
  res.end(buf);
  log(`POST /v1/chat/completions model=${requested} -> ${usedRoute} ${upstream.status} ${Date.now() - started}ms`);
  if (upstream.status === 401 || upstream.status === 403) getBrokerBase(true);
}

// ---------------- 服务器 ----------------
// ---------------- 热替换：排空在途请求再退出 ----------------
// 为什么要有这个：09-21 02:50 我直接 taskkill 换代码，把用户两个正在流的会话一起掐断，
// 两边同时开始重连。排空 = 不收新请求、等在途的流跑完再退，watchdog 30 秒内拉起新实例。
let draining = false;
let inflight = 0;
const DRAIN_MAX_MS = Number(process.env.AUTOCLAW_DRAIN_MAX_MS || 90_000);

function maybeFinishDrain() {
  if (!draining || inflight > 0) return;
  log("drain: 在途请求已跑完 → 退出，等 watchdog 拉起新实例");
  setTimeout(() => process.exit(0), 150);
}

function startDrain() {
  draining = true;
  log(`drain: 开始排空（在途 ${inflight} 条），新请求改回 503；最多等 ${DRAIN_MAX_MS / 1000}s`);
  // 空载时必须自己收尾：maybeFinishDrain 只在"某条请求跑完"时被调用，
  // 没有在途请求时永远没人触发 → 空闲服务白等满 DRAIN_MAX_MS=90s 才退，
  // 等于每次热替换凭空多断 90 秒（09-21 15:2x 实测，Qoder CN 会话抓出来的）。
  // 400ms 是留给 /admin/drain 自己的回包先写出去。
  setTimeout(maybeFinishDrain, 400);
  setTimeout(() => {
    log(`drain: 超时仍有 ${inflight} 条在途 → 强制退出`);
    process.exit(0);
  }, DRAIN_MAX_MS).unref?.();
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "127.0.0.1"}`);
  const pathname = url.pathname.replace(/\/+$/, "") || "/";

  // 来源控制：loopback 始终放行；非 loopback 且在非环回监听时强制 Bearer 校验
  const ip = (req.socket.remoteAddress || "").replace("::ffff:", "");
  const isLoopback = ip === "127.0.0.1" || ip === "::1" || ip === "::ffff:127.0.0.1";
  if (!isLoopback && REQUIRE_AUTH) {
    const auth = req.headers["authorization"] || "";
    if (auth !== `Bearer ${PROXY_TOKEN}`) {
      return sendJson(res, 401, { error: { message: "unauthorized: invalid or missing proxy token" } });
    }
  }

  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
      "Access-Control-Allow-Headers": "*",
      "Access-Control-Max-Age": "86400",
    });
    return res.end();
  }

  try {
    // 业务请求 TLS 桥（2026-09-25）：上游 WAF 启用 TLS 指纹白名单后，python/curl 的
    // 握手在 TLS 阶段被 RST（WinError 10054 / SSL EOF），只有本进程的 undici 指纹能过。
    // A-SWITCH GUI 后端(python) 的业务请求经此出站：POST /fwd {method,path,headers,body}
    // → {status, body}。仅限 loopback（上面已放行 loopback，非 loopback 需 token）。
    // 注意：与推理无关，纯转发；失败原样回传 status=0 让调用方自行降级。
    if (req.method === "POST" && pathname === "/fwd") {
      let raw = "";
      await new Promise((ok) => { req.on("data", (c) => { raw += c; }); req.on("end", ok); req.on("error", ok); });
      let fwd;
      try { fwd = JSON.parse(raw); } catch { return sendJson(res, 400, { error: { message: "bad fwd json" } }); }
      const { method: fm, path: fp, headers: fh, body: fb } = fwd || {};
      if (!fp || typeof fp !== "string" || !fp.startsWith("/")) {
        return sendJson(res, 400, { error: { message: "fwd.path must start with /" } });
      }
      // 2026-10-22：CN lane 身份接口必须打 zhipuai.cn（autoglm.ai 对 CN 凭证回「用户未登录」），
      // 允许调用方按账号 lane 传 base，但只在两个已知上游域的白名单里，不透传任意目标。
      const FWD_BASES = ["https://autoglm-api.autoglm.ai", "https://autoglm-api.zhipuai.cn"];
      const fwdBase = FWD_BASES.includes(fwd.base) ? fwd.base : FWD_BASES[0];
      try {
        // 连接层间歇失败（2026-09-25 实测：node 对业务路径约 1/3 成功率）必须重试，
        // 否则 GUI 后端（python→桥）拿不到数据。指数退避 0.8s/1.6s/3.2s。
        let lastErr = "";
        for (let attempt = 0; attempt < 4; attempt++) {
          try {
            const r2 = await fetch(`${fwdBase}${fp}`, {
              method: fm || "GET",
              headers: fh || {},
              body: fb === undefined || fb === null ? undefined : (typeof fb === "string" ? fb : JSON.stringify(fb)),
              signal: AbortSignal.timeout(30_000),
            });
            const text = await r2.text();
            // 5xx/429 视为可重试的瞬态；其余状态码原样返回（业务错误重试无意义）
            if (r2.status >= 500 || r2.status === 429) {
              lastErr = `http ${r2.status}`;
              if (attempt < 3) { await new Promise((x) => setTimeout(x, 800 * 2 ** attempt)); continue; }
            }
            return sendJson(res, 200, { status: r2.status, body: text });
          } catch (e) {
            lastErr = scrub(e.message);
            if (attempt < 3) { await new Promise((x) => setTimeout(x, 800 * 2 ** attempt)); continue; }
          }
        }
        return sendJson(res, 200, { status: 0, body: "bridge-fetch-err after retries: " + lastErr });
      } catch (e) {
        return sendJson(res, 200, { status: 0, body: "bridge-fetch-err: " + scrub(e.message) });
      }
    }
    if (req.method === "GET") {
      if (pathname === "/health" || pathname === "/" || pathname === "/healthz") {
        const up = await getUpstream();
        const pids = autoclawPids();
        const broker = up && up.kind === "broker" ? up.base : null;
        const pl = poolSnapshot();
        return sendJson(res, up ? 200 : 503, {
          status: up ? (draining ? "draining" : "ok") : "no_upstream",
          service: "autoclaw-glm-endpoint",
          host: HOST, port: PORT,
          draining, inflight,
          // 热替换脚本靠它分辨"这是新起来的实例"还是"排空中的旧实例"
          uptime_s: Math.round(process.uptime()),
          // upstream="cloud" 才是"不用开桌面端"的证据；broker 只是顺带
          upstream: up ? up.kind : null,
          // pool=true 表示这一发是按请求从账号池里选号的（用完一个号自动换下一个）
          pool_lane: Boolean(up && up.pool),
          broker,
          cloud: up && up.kind === "cloud" ? CLOUD_BASE : null,
          pool: pl,
          // lane 级故障切换状态：broker 额度打空后这里会显示 dead + 剩余冷却秒数
          broker_lane: brokerLaneSnapshot(),
          home_fallback: HOME_FALLBACK_URL
            ? { configured: true, ok: homeFall.ok, fail: homeFall.fail,
                cooling_s: Math.max(0, Math.round((homeFall.cooldownUntil - Date.now()) / 1000)),
                last_status: homeFall.lastStatus }
            : { configured: false },
          // 探针拦截计数：probe_served 只增不落零 = 探针再没打到任何真实账号
          probe_guard: { enabled: PROBE_DETECT, served: probeServed, last_at: probeLastAt },
          daily_cap: {
            limit: POOL_DAILY_CAP,
            per_account: Object.fromEntries(
              (pool.accounts || []).map((a) => [String(a.uid).slice(0, 8), dailyLeft(a.uid)]),
            ),
          },
          autoclawRunning: pids.size > 0,
          autoclawProcesses: pids.size,
          defaultRoute: DEFAULT_ROUTE,
          models: (await getModels()).map((m) => m.id),
          aliases: ALIASES,
        });
      }
      if (pathname === "/v1/models" || pathname === "/models") {
        const list = await getModels();
        const now = Math.floor(Date.now() / 1000);
        const data = [
          ...list.map((m) => ({
            id: m.id, object: "model", created: now, owned_by: "autoclaw",
            display_name: m.name, context_window: m.contextWindow, max_tokens: m.maxTokens,
          })),
          ...ALIASES.map((a) => ({
            id: a, object: "model", created: now, owned_by: "autoclaw",
            display_name: a, alias_of: normalizeRoute(a),
          })),
        ];
        return sendJson(res, 200, { object: "list", data });
      }
      if (pathname === "/routes") {
        return sendJson(res, 200, { defaultRoute: DEFAULT_ROUTE, routes: await getModels() });
      }
      return sendJson(res, 404, { error: { message: `unknown path ${pathname}` } });
    }

    if (req.method === "POST" && pathname === "/admin/drain") {
      if (!isLoopback) {
        return sendJson(res, 403, { error: { message: "drain 只允许本机调用" } });
      }
      if (!draining) startDrain();
      return sendJson(res, 200, { draining: true, inflight, uptime_s: Math.round(process.uptime()) });
    }

    if (req.method === "POST") {
      let body;
      try { body = await readBody(req); }
      catch (e) { return sendJson(res, 400, { error: { message: scrub(e.message) } }); }

      // 请求级中止源：客户端断开（Esc/超时/插嘴 cancel）→ 传播到上游 fetch，止损账号额度
      const reqCtrl = new AbortController();
      res.on("close", () => { if (!res.writableEnded) reqCtrl.abort(); });
      res._reqCtrl = reqCtrl;

      const isModel = pathname.endsWith("/messages") || pathname.endsWith("/completions") || pathname.endsWith("/responses");
      if (isModel && draining) {
        // 热替换期间不收新请求：让调用方立刻拿到 503 去重试，比排队 90 秒再断流好
        const msg = `端点正在热替换（在途 ${inflight} 条跑完就退出），请 30 秒后重试`;
        return pathname.endsWith("/messages")
          ? anthropicError(res, 503, msg, "overloaded_error")
          : openaiError(res, 503, msg, "server_error");
      }
      if (isModel) {
        let counted = true;
        const done = () => { if (!counted) return; counted = false; inflight--; maybeFinishDrain(); };
        res.on("finish", done);
        res.on("close", done);
        inflight++;
      }

      if (pathname === "/v1/messages" || pathname === "/messages" || pathname.endsWith("/messages")) {
        return await handleMessages(req, res, body);
      }
      if (pathname === "/v1/chat/completions" || pathname === "/chat/completions" || pathname.endsWith("/chat/completions")) {
        return await handleChatCompletions(req, res, body);
      }
      if (pathname === "/v1/responses" || pathname === "/responses") {
        return await handleResponses(req, res, body);
      }
      return sendJson(res, 404, { error: { message: `unknown path ${pathname}` } });
    }

    sendJson(res, 405, { error: { message: `method ${req.method} not allowed` } });
  } catch (e) {
    log("HANDLER ERROR:", scrub(e && e.stack ? e.stack : String(e)));
    if (!res.headersSent) sendJson(res, 500, { error: { message: scrub(e && e.message ? e.message : String(e)) } });
    else try { res.end(); } catch { /* ignore */ }
  }
});

// 常驻服务：任何未捕获异常/拒绝都只记录，不退出
process.on("uncaughtException", (e) => log("UNCAUGHT:", scrub(e && e.stack ? e.stack : String(e))));
process.on("unhandledRejection", (e) => log("UNHANDLED REJECTION", scrub(e && e.stack ? e.stack : String(e))));
process.on("exit", (code) => { try { log(`process exit code=${code}`); } catch { /* ignore */ } });
// 退出前落盘池状态（冷却/日预算）：kill/热替换不再丢 5s 节流窗内的记账
for (const sig of ["SIGTERM", "SIGINT"]) {
  process.on(sig, () => { try { poolStateDirty = true; savePoolState(); } catch { /* ignore */ } process.exit(0); });
}

server.on("error", (e) => {
  if (e.code === "EADDRINUSE") {
    process.stdout.write(`\n[FATAL] 端口 ${PORT} 已被占用。请先结束占用该端口的进程，或用 AUTOCLAW_PORT 指定其它端口。\n`);
    process.exit(1);
  }
  log("SERVER ERROR:", scrub(e.message));
});

server.listen(PORT, HOST, async () => {
  log("=".repeat(60));
  log(`AutoClaw GLM endpoint  http://${HOST}:${PORT}`);
  log(`  POST /v1/messages            (Anthropic, 主)`);
  log(`  POST /v1/chat/completions    (OpenAI 直通)`);
  log(`  POST /v1/responses           (OpenAI Responses, Codex 兼容)`);
  log(`  GET  /v1/models  GET /health`);
  const pids = autoclawPids();
  log(`AutoClaw 进程: ${pids.size ? [...pids].join(",") : "未运行"}`);
  const base = await getBrokerBase(true);
  log(`Broker: ${base || "NOT FOUND —— 请启动 AutoClaw"}`);
  const models = await getModels(true);
  log(`模型(${models.length}): ${models.map((m) => m.id).join(", ")}`);
  log("=".repeat(60));
});
