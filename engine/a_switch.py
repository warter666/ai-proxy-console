#!/usr/bin/env python3
"""A-SWITCH —— AutoClaw 多账号管理与批量签到领积分工具。

能力：
  list    列出所有已检测到的 AutoClaw 账号（含积分/任务状态）
  claim   批量领取所有可领任务（每日签到 400 分、灵感中心 200 分等）
  auto    只领取"可领"的（幂等，可反复跑/定时跑）
  activities  尝试领取服务端下发的新人/活动奖励
  login   打开官方登录窗口，登录后把账号加入账号库（独立 profile，不动主实例）
  add     从指定路径导入账号凭证（另一份 auth.json）
  export  导出当前账号凭证备份
  pool    把可用凭证导给本机反代的账号池（按请求选号，用完一个号自动换下一个）

关键实现要点（逆向 AutoClaw 1.17.8 得到，勿随意改）：
  - 认证头必须含：X-Auth-Appid/X-Auth-TimeStamp/X-Auth-Sign(md5)
    + **小写** authorization（大写 Authorization 会被网关拒！）
  - 签名：md5(f"{APP_ID}&{秒级时间戳}&{APP_KEY}")
  - token 存储：auth.json 的 token 字段是 Chromium os_crypt 加密
    （v10 + AES-256-GCM，密钥在 Local State 的 os_crypt.encrypted_key，DPAPI 包裹）

用法：
  python a_switch.py list
  python a_switch.py claim
  python a_switch.py auto
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------- AutoClaw 常量（逆向自 app.asar 1.17.8） ----------------
APP_ID = "100003"
RELAY_PORT = int(os.environ.get("AUTOCLAW_RELAY_PORT") or 18766)  # 本地反代（一键反代部署）
APP_KEY = "38d2391985e2369a5fb8227d8e6cd5e5"
BASE = "https://autoglm-api.autoglm.ai"
BASE_CN = "https://autoglm-api.zhipuai.cn"    # 国内线身份域：CN lane 凭证只在 zhipuai.cn 有效（2026-10-22 实测）
TASK_LIST = "/autoclaw-proxy/proxy/autoclaw-task-list"
TASK_COMPLETE = "/autoclaw-proxy/proxy/autoclaw-task-complete"
NEWBIE_GRANT_POINTS = 10000                     # 新人注册奖励的目标额度
NEW_ACCOUNT_DIAG_FILE = "new_account_diagnose.json"   # 新号取证落盘（脱敏，不入库）
CLAIM_BLOCK_FILE = "claim_blocked.json"               # 当天领取台账：被拒的 + 已到账的
CLAIM_BLOCK_RETRY_AFTER = 3600                        # 被拒任务的滑动冷却（秒）

DEFAULT_STATE_DIR = Path(os.environ.get("AUTOCLAW_AUTH_DIR")
                         or (Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
                             / "autoclaw"))
ACCOUNTS_DIR = Path(os.environ.get("ASWITCH_DIR")
                    or (Path(__file__).resolve().parent / "accounts"))


# ---------------- 日志 ----------------
def _force_utf8_streams():
    """无控制台 exe（console=False）里 stdout 按系统 ANSI(GBK) 编码，
    日志中一个 ✓/✗/⚠ 就抛 UnicodeEncodeError，会把切换、领取整条链路炸掉。
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_streams()


def safe_print(*a, **kw):
    """打印永不抛异常：日志失败绝不能变成业务失败。"""
    try:
        print(*a, **kw)
        return
    except Exception:
        pass
    try:  # reconfigure 不可用时（流被替换/为 None）退到字节写入
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write((" ".join(str(x) for x in a) + "\n").encode("utf-8", "replace"))
            buf.flush()
    except Exception:
        pass


def log(*a):
    safe_print(" ".join([time.strftime("[%H:%M:%S]")] + [str(x) for x in a]), flush=True)


def _fmt_ms(ms) -> str:
    """毫秒时间戳 -> "MM-DD HH:mm"（活动窗口显示用）。"""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ms) / 1000))
    except Exception:
        return "-"


# ---------------- DPAPI + AES-GCM 解密（Chromium os_crypt） ----------------
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData failed")
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def _os_crypt_key(appdata_dir: Path) -> bytes:
    local_state = json.loads((Path(appdata_dir) / "Local State").read_text(encoding="utf-8"))
    wrapped = base64.b64decode(local_state["os_crypt"]["encrypted_key"])
    return _dpapi_unprotect(wrapped[5:])


def decrypt_chromium_value(value: str, appdata_dir: Path) -> str:
    """解密 auth.json 里的 enc:v10... 值。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = value[4:] if value.startswith("enc:") else value
    raw = re.sub(r"\s+", "", raw)
    blob = base64.b64decode(raw)
    assert blob[:3] == b"v10", f"unknown prefix {blob[:3]!r}"
    key = _os_crypt_key(appdata_dir)
    return AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8")


def encrypt_chromium_value(plaintext: str, appdata_dir: Path) -> str:
    """加密成 AutoClaw 认得的 enc:v10... 格式（轮换后的 token 要写回存档）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _os_crypt_key(appdata_dir)
    nonce = os.urandom(12)
    blob = b"v10" + nonce + AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return "enc:" + base64.b64encode(blob).decode("ascii")


# ---------------- 身份文件成套读写 ----------------
# AutoClaw 把登录身份同时写在三个文件里（见 ac_main.js saveAuthTokens）：
#   auth.json        token + refreshToken + userInfo + deviceId（主源）
#   token-cache.json token + refreshToken（auth-state 缺 token 时的回退源）
#   user-cache.json  userId + userName + deviceId（缺 userInfo 时的回退源）
# 三者的读函数在主文件缺失/解析失败时都会从同名 .backup 复活旧内容。
# 只换 auth.json = 残留的两个缓存 + 三个 .backup 仍是上一个账号，
# App 启动后 0.1 秒的 startup-refresh 就会从缓存回退源把旧账号写回来。
IDENTITY_FILES = ("auth.json", "token-cache.json", "user-cache.json")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.aswitch-{os.getpid()}-{_now_ms()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def build_identity_bundle(src_dir: Path) -> dict:
    """按账号目录里那份 auth.json，产出"成套"的身份文件内容 {相对路径: bytes}。

    token-cache / user-cache 从 auth.json 反推重建（而不是沿用目录里的旧缓存），
    保证四个来源指向同一个账号；.backup 影子文件一并写成同样的内容，
    这样即使 App 走 backup 复活路径，复活的也是目标账号。
    """
    src_dir = Path(src_dir)
    auth_raw = (src_dir / "auth.json").read_bytes()
    auth = json.loads(auth_raw.decode("utf-8", "replace"))
    ui = auth.get("userInfo") or {}
    bundle = {"auth.json": auth_raw, "auth.json.backup": auth_raw}

    tc = json.dumps({"token": auth.get("token"), "refreshToken": auth.get("refreshToken"),
                     "updatedAt": _now_ms()}, ensure_ascii=False, indent=2).encode("utf-8")
    bundle["token-cache.json"] = tc
    bundle["token-cache.json.backup"] = tc

    uc: dict = {}
    src_uc = src_dir / "user-cache.json"
    if src_uc.is_file():
        try:
            base = json.loads(src_uc.read_text(encoding="utf-8"))
            if isinstance(base, dict):
                # 只继承与身份无关的行为字段，身份三要素一律按目标账号重建
                uc = {k: v for k, v in base.items()
                      if k in ("optimizeData", "firstLogin", "webFirstLogin")}
        except Exception:
            uc = {}
    if ui.get("user_id"):
        uc["userId"] = ui["user_id"]
    if auth.get("deviceId"):
        uc["deviceId"] = auth["deviceId"]
    name = ui.get("user_name") or ""
    if name:
        try:
            uc["userName"] = encrypt_chromium_value(name, src_dir)
        except Exception as e:
            log(f"user-cache 昵称重新加密失败（App 会从远端补齐）：{type(e).__name__}")
    phone = ui.get("user_phone") or ""
    if phone:
        try:
            uc["phone"] = encrypt_chromium_value(phone, src_dir)
        except Exception:
            pass
    uc["updatedAt"] = _now_ms()
    ucb = json.dumps(uc, ensure_ascii=False, indent=2).encode("utf-8")
    bundle["user-cache.json"] = ucb
    bundle["user-cache.json.backup"] = ucb

    for f in ("Local State", "channel.json"):
        p = src_dir / f
        if p.is_file():
            bundle[f] = p.read_bytes()
    return bundle


def write_identity_set(live_dir: Path, bundle: dict, keep_rollback: bool = True) -> Path | None:
    """把整套身份原子写进活动目录；返回存放原文件的回滚目录（None=没备份）。

    回滚目录不能建在活动目录里：它存的是被换掉账号的完整身份（auth.json /
    device.json / user-cache），留在客户端活动目录内会被当成自身状态读取上报，
    风控会把"用过封禁号的设备指纹"算到当前账号头上。改写到工作区
    accounts/_rollback_archive/（活动目录之外）。
    """
    live_dir = Path(live_dir)
    rb = ACCOUNTS_DIR / "_rollback_archive" / f"aswitch-rollback-{time.strftime('%Y%m%d-%H%M%S')}"
    backed = False
    if keep_rollback:
        for rel in bundle:
            cur = live_dir / rel
            if cur.is_file():
                rb.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(cur, rb / rel.replace("/", "__"))
                    backed = True
                except Exception:
                    pass
    for rel, data in bundle.items():
        _atomic_write(live_dir / rel, data)
    return rb if backed else None


def verify_identity_set(live_dir: Path, bundle: dict, expect_uid: str) -> tuple:
    """回读校验：内容逐字节一致 + auth.json 能解出目标账号的 token。"""
    live_dir = Path(live_dir)
    for rel, want in bundle.items():
        cur = live_dir / rel
        if not cur.is_file():
            return False, f"{rel} 缺失"
        if cur.read_bytes() != want:
            return False, f"{rel} 内容被改写"
    acc = load_account(live_dir)
    if not acc:
        return False, "auth.json 解析/解密失败"
    if str(acc.user_id) != str(expect_uid):
        return False, f"当前登录身份是 {acc.user_id}"
    if not acc.token:
        return False, "token 解不出来"
    return True, ""


def auth_headers(token: str = "", channel: str = "zai") -> dict:
    """服务端认的公共头集合。未登录态必须整个省略 authorization（空 Bearer 会被拒）。"""
    ts = str(int(time.time()))
    sign = hashlib.md5(f"{APP_ID}&{ts}&{APP_KEY}".encode()).hexdigest()
    h = {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "X-Version": detect_autoclaw_version(),  # 跟随本机 AutoClaw 实际版本
        "X-Tm": "win",
        "X-Product": "autoclaw",
        "X-Auth-Appid": APP_ID,
        "X-Auth-TimeStamp": ts,
        "X-Auth-Sign": sign,
        "X-Lang": "zh-CN",
        "X-Channel": channel,
        "X-Trace-Id": str(uuid.uuid4()),
    }
    if token:
        # ⚠️ 必须小写：大写 Authorization 会被网关 401
        h["authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    return h


# ---------------- 账号抽象 ----------------
class Account:
    """一个 AutoClaw 账号（含凭证与来源）。"""

    def __init__(self, name: str, appdata_dir: Path, auth_json: dict, token: str,
                 device_id: str = "", channel: str = "zai",
                 refresh_token: str = "", is_live: bool = False):
        self.name = name
        self.appdata_dir = Path(appdata_dir)
        self.auth_json = auth_json
        self.token = token
        self.refresh_token = refresh_token
        self.is_live = is_live          # 活动目录里的账号：它的 token 由 App 自己续期
        self.auth_expired = False       # 刷新也救不回来 → 需要重新登录
        self.device_id = device_id or auth_json.get("deviceId", "")
        self.channel = channel
        self.lane = _read_lane(appdata_dir)   # 决定身份接口打哪个域名（cn/oversea）
        ui = auth_json.get("userInfo") or {}
        self.user_id = ui.get("user_id") or ui.get("id")
        self.nickname = ui.get("user_name") or ui.get("nickname") or name
        self.email = ui.get("email") or ""

    def access_expires_at(self) -> int:
        return int(_jwt_payload(self.token).get("exp") or 0)

    def jwt_uid(self) -> str:
        """access token 里声明的数字 user_id —— 才是"这个 token 属于谁"的铁证。

        auth.json 的 userInfo 是明文，写谁像谁；token 里的 claim 由服务端签发，
        校验切换是否真的成功只能看它（App 若回退到 token-cache，昵称可能还是目标，
        token 却已经是别人的）。
        """
        return str(_jwt_payload(self.token).get("user_id") or "")

    def headers(self) -> dict:
        return auth_headers(self.token, self.channel)

    def _http(self, method: str, path: str, body=None, timeout=40, extra_headers=None):
        headers = self.headers()
        if extra_headers:
            headers.update(extra_headers)
        # 2026-10-22 实锤：CN lane 凭证打国际域 /userapi/v1/refresh 回 400000「用户未登录」，
        # 同一枚 token 打国内域 code=0。身份接口必须按 lane 分流，否则国内号永远"需要重新登录"。
        base = BASE_CN if getattr(self, "lane", "oversea") == "cn" else BASE
        req = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return e.code, e.read(600).decode("utf-8", "replace")
        except Exception as e:
            # 2026-09-25：上游 WAF 对非 node 指纹的 TLS 握手间歇性 RST（python/curl 几乎全挂，
            # node undici 重试能过）。直连失败（st=0 连接层错误）自动改走本地反代的业务桥
            # /fwd（loopback 明文进、node TLS 出，桥内自带 4 次重试）——这是 GUI 存活的保命路。
            # 桥也不通才返回原直连错误。
            st2, d2 = self._http_via_bridge(method, path, headers, body, max(timeout, 90), base)
            if st2 is not None:
                return st2, d2
            return 0, f"{type(e).__name__}: {e}"

    @staticmethod
    def _http_via_bridge(method: str, path: str, headers: dict, body, timeout: float, base: str = ""):
        """经本地反代 /fwd 转发业务请求。桥不可用返回 (None, None)。"""
        try:
            payload = {"method": method, "path": path, "headers": headers,
                       "body": body if isinstance(body, str) else body, "base": base}
            req = urllib.request.Request(
                "http://127.0.0.1:18766/fwd",
                data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            st = data.get("status", 0)
            raw = data.get("body", "")
            try:
                return st, json.loads(raw)
            except Exception:
                return st, raw
        except Exception:
            return None, None

    def call(self, method: str, path: str, body=None, timeout=40, extra_headers=None):
        """带续期的请求：库存快照的 access token 只有 24h，过期前自助刷新。

        活动目录里的账号不刷新（App 每小时自己续期并回写文件，我们插手会把
        它的 refreshToken 打旧，反而把 App 顶下线）。
        """
        if not self.is_live and self.refresh_token and not self.auth_expired:
            exp = self.access_expires_at()
            if exp and exp - time.time() < 300:
                self.refresh_access_token()
        st, d = self._http(method, path, body, timeout, extra_headers)
        if st == 401 and not self.is_live and not self.auth_expired and self.refresh_token:
            if self.refresh_access_token():
                st, d = self._http(method, path, body, timeout, extra_headers)
        return st, d

    def refresh_access_token(self) -> bool:
        """用 refreshToken 换新的 access/refresh（服务端会轮换）。"""
        if not self.refresh_token:
            self.auth_expired = True
            return False
        body = {"source_id": "autoclaw", "device_id": self.device_id,
                "refresh_token": self.refresh_token}
        st, d = self._http("POST", "/userapi/v1/refresh", body)
        if isinstance(d, dict) and d.get("code") == 400002:
            # 与 App 一致：签名校验失败时降级到 agent-refresh
            st, d = self._http("POST", "/userapi/v1/agent-refresh", body)
        if not (st == 200 and isinstance(d, dict) and d.get("code") == 0 and d.get("data")):
            msg = d.get("msg") if isinstance(d, dict) else str(d)
            log(f"{self.nickname} 续期失败：{str(msg)[:120]}（需要重新登录）")
            self.auth_expired = True
            return False
        data = d["data"]
        self.token = data.get("access_token") or ""
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        self.auth_expired = False
        ok = self.persist_rotated_tokens()
        log(f"{self.nickname} 已续期" + ("" if ok else "（存档回写失败，本次会话内有效）"))
        return bool(self.token)

    def persist_rotated_tokens(self) -> bool:
        """把轮换后的凭证写回账号存档（enc: 形式），否则下次读取又是旧的。"""
        try:
            src = self.appdata_dir / "auth.json"
            obj = json.loads(src.read_text(encoding="utf-8"))
            # v2 vault 导入的存档可能没有 Local State（os_crypt 密钥）。没有它
            # enc:v10 加密不了，硬写只会 FileNotFoundError，轮换后的 refreshToken
            # 仅本次进程有效，下次续期必死。退回明文回写（与导入脚本的产物一致）；
            # token-cache / user-cache 依赖同一把密钥，这里一并跳过。
            encrypted = (self.appdata_dir / "Local State").is_file()
            if encrypted:
                obj["token"] = encrypt_chromium_value(self.token, self.appdata_dir)
                obj["refreshToken"] = encrypt_chromium_value(self.refresh_token, self.appdata_dir)
            else:
                obj["token"] = self.token
                obj["refreshToken"] = self.refresh_token
            obj["updatedAt"] = _now_ms()
            raw = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            _atomic_write(src, raw)
            if encrypted:
                write_identity_set(self.appdata_dir, build_identity_bundle(self.appdata_dir),
                                   keep_rollback=False)
            return True
        except Exception as e:
            log(f"凭证回写存档失败：{type(e).__name__}: {e}")
            return False

    def task_list(self):
        st, d = self.call("GET", TASK_LIST)
        if st == 200 and isinstance(d, dict):
            return d.get("data") or []
        return []

    def claim(self, task_id: str):
        return self.call("POST", TASK_COMPLETE, {"task_id": task_id})

    @staticmethod
    def reject_reason(st: int, d) -> str:
        """这次领取失败是不是"今天再撞多少次都是同一个结果"。

        是 → 返回服务端给的原因文字；否 → 空串（留着重试）。
        HTTP 0（超时/断网/DNS）、401（凭证，refresh 可能稍后成功）、429/502/503/504
        一律当瞬时故障，绝不记账 —— 把它们拉黑会让当天本该拿到的分白白丢掉。
        """
        if st in (0, 401, 429, 502, 503, 504) or st < 400:
            return ""
        if isinstance(d, (bytes, bytearray)):
            d = d.decode("utf-8", "replace")
        if isinstance(d, str):
            # 非 2xx 的响应体走的是 HTTPError 分支，到这里还是一串原文
            try:
                d = json.loads(d)
            except Exception:
                pass
        if isinstance(d, dict):
            dd = d.get("data") or {}
            if dd.get("success") or dd.get("already_completed"):
                return ""
            return str(d.get("message") or d.get("error") or d.get("msg")
                       or json.dumps(dd, ensure_ascii=False))[:160]
        return (str(d or "").strip() or f"HTTP {st}")[:160]

    # ---------------- 当天的领取台账（两类：被服务端拒的 / 已经到账的） ----------------
    # 为什么要两类：
    #  · 被拒的（`task user_id required` 这种确定性 500）：再撞一万次也是同一句，别撞；
    #  · 已到账的：**task-list 的 status 是滞后的**（09-21 01:18 实测某个号签到 +400
    #    已进流水，task-list 里 `daily_signin` 仍写 incomplete），只信它就永远显示"有分可领"。
    # 两类都只活到当天结束，且被拒的那类还有滑动冷却（见 CLAIM_BLOCK_RETRY_AFTER）。
    def _block_file(self) -> Path:
        return self.appdata_dir / CLAIM_BLOCK_FILE

    def _ledger(self) -> dict:
        """读台账并按"今天 + 冷却期"过滤；旧格式/读不懂一律当空（宁可多试一次，不可锁死）。"""
        today = time.strftime("%Y-%m-%d")
        now = time.time()
        try:
            raw = json.loads(self._block_file().read_text(encoding="utf-8"))
        except Exception:
            return {"blocked": {}, "done": {}}
        if not isinstance(raw, dict) or "blocked" not in raw:
            return {"blocked": {}, "done": {}}     # 旧格式：过期即弃，不做兼容
        out = {"blocked": {}, "done": {}}
        for kind in ("blocked", "done"):
            for k, v in (raw.get(kind) or {}).items():
                if not isinstance(v, dict) or v.get("day") != today:
                    continue
                if kind == "blocked":
                    try:
                        if now - float(v.get("at") or 0) >= CLAIM_BLOCK_RETRY_AFTER:
                            continue      # 冷却到了 → 允许再撞一次（服务端随时可能修好）
                    except (TypeError, ValueError):
                        continue
                out[kind][k] = v
        return out

    def _write_ledger(self, led: dict) -> None:
        try:
            self._block_file().write_text(json.dumps(led, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
        except Exception as e:
            log(f"写领取台账失败（不影响领取）：{type(e).__name__}: {e}")

    def blocked_tasks(self) -> dict:
        return self._ledger()["blocked"]

    def claimed_today(self) -> dict:
        return self._ledger()["done"]

    def note_claimed(self, task_id: str, points: int = 0) -> None:
        if not task_id:
            return
        led = self._ledger()
        led["done"][task_id] = {"day": time.strftime("%Y-%m-%d"), "at": time.time(),
                                "points": int(points or 0)}
        self._write_ledger(led)

    def note_blocked(self, task_id: str, reason: str) -> None:
        if not task_id:
            return
        led = self._ledger()
        cur = led["blocked"]
        ent = cur.get(task_id) or {"day": time.strftime("%Y-%m-%d"), "tries": 0}
        ent.update({"day": time.strftime("%Y-%m-%d"), "at": time.time(),
                    "tries": int(ent.get("tries") or 0) + 1,
                    "reason": str(reason or "")[:160]})
        cur[task_id] = ent
        led["blocked"] = cur
        self._write_ledger(led)

    def clear_blocked(self, task_id: str) -> None:
        led = self._ledger()
        if led["blocked"].pop(task_id, None) is not None:
            self._write_ledger(led)

    def clear_all_blocked(self) -> int:
        """手动点「一键领取」时先解除冷却：用户明确要求再试一次，就别拿旧判决挡路。"""
        led = self._ledger()
        n = len(led["blocked"])
        if n:
            led["blocked"] = {}
            self._write_ledger(led)
        return n

    # ---------------- 灵感中心（每日 200，走独立接口） ----------------
    # 注意：灵感奖励**不是** task-complete，那个接口只会回 already_completed。
    # 正确链路：inspiration-center 取案例 id → inspiration-task-complete 领奖。
    def inspiration_center(self) -> list:
        st, d = self.call("POST", "/agentdr/v1/assistant/inspiration-center",
                          {"source_id": "autoclaw", "device_id": self.device_id})
        if st == 200 and isinstance(d, dict) and d.get("code") == 0:
            data = d.get("data") or {}
            return data.get("list") or []
        return []

    def claim_inspiration(self, inspiration_id: str):
        return self.call("POST", "/agentdr/v1/assistant/inspiration-task-complete",
                         {"inspiration_id": inspiration_id})

    def claim_inspiration_daily(self) -> tuple:
        """领今日灵感中心奖励。返回 (获得积分, 状态)。

        状态：claimed=本次领到 / already=今日已领过 / unavailable=没有可领条目 /
        fail=接口拒绝。接口幂等，探测时直接调用即得真实资格，绝不虚报。
        """
        items = self.inspiration_center()
        if not items:
            return 0, "unavailable"
        for item in items[:5]:
            iid = (item or {}).get("inspiration_id")
            if not iid:
                continue
            st, d = self.claim_inspiration(iid)
            if isinstance(d, dict):
                code = d.get("code")
                if code == 0:
                    got = ((d.get("data") or {}).get("points")) or 200
                    return got, "claimed"
                if code == 560118:  # 今日已完成
                    return 0, "already"
        return 0, "fail"

    # ---------------- 新人任务 / 活动奖励 ----------------
    def newbie_tasks(self, task_category: str = "") -> dict:
        params = {
            "biz_app_id": "autoclaw",
            "client_version": detect_autoclaw_version(),
            "os": "win",
            # 官方客户端永远带上这个空参数（makeNewbieParams 的展开结果），照抄
            "task_category": task_category,
        }
        path = "/agent-assetmgr/api/v1/identity-tasks?" + urllib.parse.urlencode(params)
        st, d = self.call("GET", path)
        return d if st == 200 and isinstance(d, dict) else {}

    def complete_newbie_task(self, params: dict):
        body = {
            "source_id": "autoclaw",
            "device_id": self.device_id,
            "biz_app_id": "autoclaw",
            "client_version": detect_autoclaw_version(),
            "os": "win",
        }
        body.update(params or {})
        return self.call("POST", "/agent-assetmgr/api/v1/identity-tasks/complete", body)

    def claim_newbie_tasks(self) -> list[dict]:
        """领取服务端实际下发的新人任务；服务端没下发时不虚报 10000 分。"""
        data = self.newbie_tasks().get("data") or {}
        tasks = data.get("tasks") or []
        results = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            done = task.get("completed") is True or task.get("status") in {"completed", "done"}
            params = {}
            for key in ("task_category", "task_id", "id", "category"):
                if task.get(key) is not None and task.get(key) != "":
                    params[key] = task[key]
            if done or not params:
                continue
            st, d = self.complete_newbie_task(params)
            results.append({"task": params, "http": st, "response": d})
        return results

    def newbie_guide_token(self):
        token = self.token if self.token.lower().startswith("bearer ") else f"Bearer {self.token}"
        return self.call(
            "POST", "/autoclaw-proxy/proxy/autoclaw-newbie-guide/token", {},
            extra_headers={"X-Authorization": token},
        )

    def promotion_config(self) -> dict:
        st, d = self.call("GET", "/autoclaw-proxy/proxy/autoclaw-promotion-config")
        return d if st == 200 and isinstance(d, dict) else {}

    def grant_promotion_reward(self, modal_id: str, reward_type: str):
        token = self.token if self.token.lower().startswith("bearer ") else f"Bearer {self.token}"
        return self.call(
            "POST", "/autoclaw-proxy/proxy/autoclaw-promotion-reward",
            {"modal_id": modal_id, "reward_type": reward_type},
            extra_headers={"X-Authorization": token},
        )

    def claim_active_promotions(self) -> list[dict]:
        """尝试当前时间窗内的活动领奖（只试 btnType=default 的奖励型弹窗；
        link 型是外部活动/公告，领不了）。保留服务端原始结果供 UI 解释。"""
        data = self.promotion_config().get("data") or {}
        now_ms = int(time.time() * 1000)
        results = []
        for modal in data.get("modals") or []:
            if not isinstance(modal, dict):
                continue
            modal_id = modal.get("modal_id") or modal.get("id")
            if not modal_id:
                continue
            if str(modal.get("btnType") or "").lower() != "default":
                continue  # link 型 = 外部活动/公告，API 领不了，跳过不制造噪音
            start = modal.get("startTime") or 0
            end = modal.get("endTime") or 0
            if start and now_ms < start or end and now_ms > end:
                continue
            for reward_type in ("sendReward", "timingReward"):
                st, d = self.grant_promotion_reward(str(modal_id), reward_type)
                results.append({
                    "modal_id": modal_id,
                    "name": modal.get("name") or modal_id,
                    "reward_type": reward_type,
                    "http": st,
                    "response": d,
                })
        return results

    # ---------------- 邀请裂变（大号取码 / 新号绑定，绑定并查询计 800 分/号） ----------------
    def invite_status(self) -> dict:
        st, d = self.call("GET", "/agent-assetmgr/api/v1/fission-v2/status?" +
                          urllib.parse.urlencode({"activity_id": "autoclaw_fission",
                                                  "business": "autoclaw"}))
        if st == 200 and isinstance(d, dict) and d.get("code") == 0:
            return (d.get("data") or {}).get("status") or {}
        return {}

    def my_invite_code(self) -> str:
        return str((self.invite_status().get("share") or {}).get("invite_code") or "")

    def bind_invite_code(self, invite_code: str) -> tuple:
        """把本账号绑到邀请码下（withWebInfo 语义：source_id + device_id）。

        `business` 必须带上：与 invite_status 同一套活动标识，缺了服务端回 400001。
        """
        body = {"activity_id": "autoclaw_fission", "business": "autoclaw",
                "invite_code": invite_code,
                "source_id": "autoclaw", "device_id": self.device_id}
        return self.call("POST", "/agent-assetmgr/api/v1/fission-v2/bind", body)

    def online_activity_info(self) -> dict:
        st, d = self.call("GET", "/agent-assetmgr/api/v1/fission-v2/online-activity-info?" +
                          urllib.parse.urlencode({"business": "autoclaw"}))
        if st == 200 and isinstance(d, dict) and d.get("code") == 0:
            return d.get("data") or {}
        return {}

    def invite_rule(self) -> dict:
        """服务端自己声明的计奖规则（金额/上限/老号补绑上限），拿来当解释依据。"""
        act = (self.invite_status() or {}).get("activity") or {}
        return {k: act.get(k) for k in (
            "bind_inviter_reward_amount", "bind_inviter_reward_limit",
            "existing_user_bind_inviter_reward_limit", "bind_invitee_reward_amount",
            "bind_inviter_reward_event", "status")} if act else {}

    # ---------------- 积分查询 ----------------
    def points(self) -> dict:
        """查询积分总览。返回 {total, wallets:[...], expiring, expiring_text}。"""
        out = {"total": None, "wallets": [], "expiring": None, "expiring_text": ""}
        st, d = self.call("GET", "/agent-assetmgr/api/v2/wallets")
        if st == 200 and isinstance(d, dict) and d.get("code") == 0:
            data = d.get("data") or {}
            out["total"] = data.get("total_balance")
            out["wallets"] = [
                {"type": w.get("public_wallet_type"), "name": w.get("display_name"),
                 "balance": w.get("balance"), "display": w.get("display")}
                for w in (data.get("wallets") or [])
            ]
        st, d = self.call("GET", "/agent-assetmgr/api/v1/points/expiring")
        if st == 200 and isinstance(d, dict) and d.get("code") == 0:
            data = d.get("data") or {}
            out["expiring"] = data.get("expiring_points")
            out["expiring_text"] = data.get("expiring_points_text") or ""
        return out

    def ledger(self, limit: int = 15) -> list:
        """最近消费/获得明细。"""
        st, d = self.call("GET", "/agent-assetmgr/api/v1/ledgers_std")
        if st != 200 or not isinstance(d, dict) or d.get("code") != 0:
            return []
        entries = (d.get("data") or {}).get("entries") or []
        out = []
        for e in entries[:limit]:
            out.append({
                "amount": e.get("amount"),
                "type": e.get("mutation_type"),
                "desc": e.get("description") or "",
                "time": e.get("created_at") or e.get("time") or "",
            })
        return out

    # ---------------- 实时资格探测（学 Z·SWITCH：不写死活动规律，
    # 一切以服务端实时返回为准） ----------------
    def probe_all(self) -> dict:
        """探测该账号当前全部可领资格。

        返回 {
          claimable: [{kind, id, title, points}],   # 现在就能尝试领的
          upcoming:  [{id, name, start_text, end_text}],  # 时间窗未到的活动预告
          newbie_issued: bool,                       # 服务端是否下发了新人任务
        }
        """
        claimable, upcoming = [], []
        # 0) 当天台账：被拒的不再算"可领"（但要说明为什么），已到账的也不算（task-list 会滞后）
        blocked_today = self.blocked_tasks()
        done_today = self.claimed_today()
        blocked = [{"id": k, "title": k, "reason": (v or {}).get("reason") or "",
                    "tries": (v or {}).get("tries") or 0, "points": 0}
                   for k, v in sorted(blocked_today.items())]
        # 1) 每日任务（task-list 是权威状态源）
        raw_tasks = self.task_list()
        for t in raw_tasks:
            if not isinstance(t, dict):
                continue
            tid = t.get("task_id")
            if not (t.get("client_triggered") and t.get("status") != "completed"):
                continue
            if tid in done_today:
                continue          # 已经进过流水了，服务端状态还没翻，别再报"可领"
            if tid in blocked_today:
                for b in blocked:
                    if b["id"] == tid:
                        b["title"] = t.get("title") or tid
                        b["points"] = t.get("reward_points", 0)
                continue
            claimable.append({"kind": "daily", "id": tid,
                              "title": t.get("title") or tid,
                              "points": t.get("reward_points", 0)})
        # 2) 灵感中心（每日 200；探测即调用幂等领取接口 → 真实资格，不虚报）
        got_insp, insp_state = self.claim_inspiration_daily()
        if insp_state in ("claimed", "already"):
            note = f"已领 +{got_insp}" if insp_state == "claimed" else "今日已领"
            claimable.append({"kind": "inspiration_done", "id": "daily_inspiration_center",
                              "title": f"灵感中心（{note}）", "points": 0})
        # 3) 新人任务（服务端下发才有，绝不虚报）
        nb = self.newbie_tasks().get("data") or {}
        tasks = nb.get("tasks") or []
        newbie_issued = bool(tasks)
        # 服务端同时给了三个能说明现状的字段；它们的语义官方并未文档化，
        # 所以这里只如实转述字段值，绝不替服务端猜一个原因。
        if newbie_issued:
            newbie_hint = ""
        elif not nb:
            newbie_hint = "接口无返回（凭证可能已失效）"
        elif nb.get("all_tasks_completed") is True:
            newbie_hint = "全部新人任务已完成"
        else:
            newbie_hint = ("未下发（is_bound=%s, identity=%s）"
                           % (json.dumps(nb.get("is_bound")),
                              json.dumps(nb.get("identity"))))
        for task in tasks:
            if not isinstance(task, dict):
                continue
            done = task.get("completed") is True or task.get("status") in {"completed", "done"}
            tid = (task.get("task_category") or task.get("task_id")
                   or task.get("id") or task.get("category"))
            if done or not tid:
                continue
            claimable.append({"kind": "newbie", "id": str(tid),
                              "title": f"新人任务 {tid}",
                              "points": task.get("reward_points") or task.get("points") or 0})
        # 4) 活动窗口（promotion-config；按服务端给的 btnType 分类：
        #    default=弹窗内可领奖励；link=外部活动/公告（表单、关注等），API 无从领取；
        #    "Log in to claim" 类在窗口期内登录自动到账）
        data = self.promotion_config().get("data") or {}
        now_ms = int(time.time() * 1000)
        login_type_active = False
        for m in data.get("modals") or []:
            if not isinstance(m, dict):
                continue
            mid = m.get("modal_id") or m.get("id")
            if not mid:
                continue
            name = m.get("name") or mid
            start, end = m.get("startTime") or 0, m.get("endTime") or 0
            window = (not start or now_ms >= start) and (not end or now_ms <= end)
            btn = str(m.get("btnType") or "").lower()
            content = str(m.get("modalContent") or "")
            is_login_type = bool(re.search(r"log\s*in|登录", content, re.I))
            if btn == "default":
                # 奖励型弹窗：探测即真实尝试（服务端判定资格），实报结果
                if window:
                    if is_login_type:
                        login_type_active = True  # 窗口期内登录自动到账 → 打卡有用
                    got_p = 0
                    for rt in ("sendReward", "timingReward"):
                        try:
                            _st, _d = self.grant_promotion_reward(str(mid), rt)
                            if isinstance(_d, dict):
                                got_p += ((_d.get("data") or {}).get("points")) or 0
                        except Exception:
                            pass
                    claimable.append({"kind": "promotion", "id": str(mid),
                                      "title": (f"{name}（已到账 +{got_p}）" if got_p
                                                else (f"{name}（登录自动到账）" if is_login_type
                                                      else f"{name}（暂无可领）")),
                                      "points": got_p})
                elif start and now_ms < start:
                    upcoming.append({"id": str(mid), "name": name,
                                     "start_text": _fmt_ms(start), "end_text": _fmt_ms(end)})
            else:
                # link 型：外部参与/公告。窗内展示；未开始列预告；已结束不展示
                if window:
                    # 09-28 实证（Weekly_Token_Rush_0928_0929）：link 型里也有"Log in to
                    # claim"的登录到账活动——隔离实例打卡一次，20,000 credits 当即进流水。
                    # 文案里带 Log in / 登录的 link 型活动同样应触发自动打卡。
                    if is_login_type:
                        login_type_active = True
                    claimed = "Claimed" in str(m.get("btnText") or "")
                    claimable.append({"kind": "promo_link", "id": str(mid), "title": name,
                                      "points": 0,
                                      "claimed": claimed,
                                      "title_text": (f"{name}（已领）" if claimed else
                                                     (f"{name}（登录即领，去「活动打卡」）"
                                                      if is_login_type else name)),
                                      "start_text": _fmt_ms(start) if start else "",
                                      "end_text": _fmt_ms(end) if end else ""})
                elif start and now_ms < start:
                    upcoming.append({"id": str(mid), "name": name,
                                     "start_text": _fmt_ms(start), "end_text": _fmt_ms(end)})
                # else: 已结束 → 跳过
        return {"claimable": claimable, "upcoming": upcoming, "blocked": blocked,
                "newbie_issued": newbie_issued, "newbie_hint": newbie_hint,
                "tasks": raw_tasks,
                "login_type_active": login_type_active}

    def __repr__(self):
        return f"<Account {self.nickname} uid={self.user_id}>"


# ---------------- 账号发现 ----------------
def _read_channel(appdata_dir: Path) -> str:
    p = appdata_dir / "channel.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("channel", "zai")
    except Exception:
        return "zai"


def _read_lane(appdata_dir: Path) -> str:
    """账号线路：cn=国内域(zhipuai.cn)，oversea=国际域(autoglm.ai)。缺省 oversea。"""
    p = Path(appdata_dir) / "a_switch_lane.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("lane", "oversea") or "oversea"
    except Exception:
        return "oversea"


def load_account(appdata_dir: Path, name: str | None = None) -> Account | None:
    """从某份 auth.json 所在目录加载账号。"""
    appdata_dir = Path(appdata_dir)
    auth_path = appdata_dir / "auth.json"
    if not auth_path.is_file():
        return None
    try:
        raw = auth_path.read_text(encoding="utf-8")
        d = json.loads(raw)

        def _secret(field):
            m = re.search(r'"%s":\s*"enc:([^"]+)"' % field, raw)
            if m:
                try:
                    return decrypt_chromium_value("enc:" + m.group(1), appdata_dir)
                except Exception:
                    return ""
            return d.get(field) or ""

        token = _secret("token")
        if not token:
            return None
        return Account(name or appdata_dir.name, appdata_dir, d, token,
                       channel=_read_channel(appdata_dir),
                       refresh_token=_secret("refreshToken"),
                       is_live=appdata_dir.resolve() == Path(DEFAULT_STATE_DIR).resolve())
    except Exception as e:
        log(f"load_account({appdata_dir}) failed: {type(e).__name__}: {e}")
        return None


def discover_accounts() -> list[Account]:
    """发现本机所有 AutoClaw 账号（含 ASWITCH_DIR 里导入的）。"""
    found: list[Account] = []
    seen_uids = set()

    candidates = [DEFAULT_STATE_DIR]
    # 环境变量/多实例目录
    for extra in os.environ.get("AUTOCLAW_EXTRA_DIRS", "").split(os.pathsep):
        if extra.strip():
            candidates.append(Path(extra.strip()))
    # ASWITCH_DIR 下导入的账号（每个子目录一份 auth.json + Local State）
    if ACCOUNTS_DIR.is_dir():
        for sub in sorted(ACCOUNTS_DIR.iterdir()):
            if sub.is_dir():
                candidates.append(sub)

    for c in candidates:
        acc = load_account(c)
        if not acc:
            continue
        key = str(acc.user_id or acc.name)
        if key in seen_uids:
            continue
        seen_uids.add(key)
        found.append(acc)
    return found


# ---------------- 命令 ----------------
def cmd_list(accounts: list[Account]):
    if not accounts:
        log("未发现任何 AutoClaw 账号。请先运行 AutoClaw 桌面端登录，或用 add 命令导入凭证。")
        return
    print()
    print(f"{'账号':<16} {'user_id':<10} {'积分任务':<10} {'可领':<6} 邮箱")
    print("-" * 78)
    for acc in accounts:
        tasks = acc.task_list()
        if not tasks:
            print(f"{acc.nickname:<16} {str(acc.user_id):<10} {'读取失败':<10} {'-':<6} {acc.email}")
            continue
        skip = set(acc.blocked_tasks()) | set(acc.claimed_today())
        claimable = [t for t in tasks if t.get("client_triggered")
                     and t.get("status") != "completed" and t.get("task_id") not in skip]
        total = sum(t.get("reward_points", 0) for t in claimable)
        print(f"{acc.nickname:<16} {str(acc.user_id):<10} {len(tasks):<10} "
              f"{len(claimable)}({total}分){'':<1} {acc.email}")
        for t in tasks:
            st_ = t.get("status")
            mark = "★" if st_ != "completed" and t.get("client_triggered") else " "
            print(f"    {mark} {t.get('task_id'):<26} +{t.get('reward_points',0):<5} {st_}")
    for c in deviceid_conflicts():
        print(f"  ⚠ 设备身份 {c['device_id']} 被 {len(c['uids'])} 个账号共用："
              f"{'、'.join(u[:8] for u in c['uids'])}"
              f" —— 服务端把它们看成一台机器，新人资格只算一次")
    print()


def cmd_pool(accounts: list[Account]) -> int:
    """把可用凭证导给反代账号池（headless 手动刷一次；GUI 每轮刷新本来就会做）。"""
    entries = []
    for acc in accounts:
        try:
            pts = acc.points() or {}
        except Exception as e:
            log(f"{acc.nickname} 余额查询失败，仍进池：{type(e).__name__}: {e}")
            pts = {}
        entries.append({"account": acc, "points": pts.get("total"),
                        "expiring": pts.get("expiring")})
    r = export_cloud_pool(entries)
    log(("已导出 " if r.get("ok") else "未导出：")
        + f"{r.get('wrote', 0)} 个号 → {r.get('path')}"
        + (f"（{r['error']}）" if r.get("error") else ""))
    for name, p in (r.get("points") or {}).items():
        log(f"   {name:<16} {p}")
    return 0 if r.get("ok") else 1


def cmd_claim(accounts: list[Account], only_available=True):
    total_gained = 0
    for acc in accounts:
        log(f"=== {acc.nickname} (uid={acc.user_id}) ===")
        # 灵感中心每日 200 走独立接口，和任务列表无关，单独领
        insp, insp_state = acc.claim_inspiration_daily()
        if insp:
            total_gained += insp
            log(f"  ✓ {'灵感中心':<26} +{insp} 分")
        elif insp_state == "already":
            log(f"  - {'灵感中心':<26} 今日已领")
        for nr in acc.claim_newbie_tasks():
            resp = nr.get("response")
            data = resp.get("data") if isinstance(resp, dict) else {}
            got = (data or {}).get("reward_points") or (data or {}).get("points") or 0
            if got:
                total_gained += got
            log(f"  {'✓' if got else '?'} 新人任务 {nr.get('task')}：{('+' + str(got) + ' 分') if got else str(resp)[:120]}")
        tasks = acc.task_list()
        if not tasks:
            log("  任务列表读取失败，跳过")
            continue
        for t in tasks:
            tid = t.get("task_id")
            status = t.get("status")
            if not t.get("client_triggered"):
                continue  # 非客户端触发（邀请好友等）跳过
            if only_available and status == "completed":
                continue
            if tid in acc.claimed_today():
                log(f"  - {tid:<26} 今日已到账（流水为准，task-list 状态滞后）")
                continue
            if tid in acc.blocked_tasks():
                log(f"  ✗ {tid:<26} 今日不再重试（服务端已拒）")
                continue
            st, d = acc.claim(tid)
            if isinstance(d, dict):
                dd = d.get("data") or {}
                got = dd.get("reward_points") or 0
                if dd.get("already_completed"):
                    acc.clear_blocked(tid)
                    acc.note_claimed(tid)
                    log(f"  - {tid:<26} 今日已领")
                elif dd.get("success"):
                    total_gained += got
                    acc.clear_blocked(tid)
                    acc.note_claimed(tid, got)
                    log(f"  ✓ {tid:<26} 领取成功 +{got} 分")
                else:
                    reason = acc.reject_reason(st, d)
                    if reason:
                        acc.note_blocked(tid, reason)
                    log(f"  ? {tid:<26} HTTP {st} "
                        f"{reason or json.dumps(dd, ensure_ascii=False)[:80]}"
                        + ("（今日不再重试）" if reason else ""))
            else:
                reason = acc.reject_reason(st, d)
                if reason:
                    acc.note_blocked(tid, reason)
                log(f"  ! {tid:<26} HTTP {st} {str(d)[:90]}"
                    + ("（今日不再重试）" if reason else ""))
    log(f"本次共领取 {total_gained} 积分")
    return total_gained


def cmd_activities(accounts: list[Account]) -> int:
    """领取新人/活动奖励；需证明或邀请的活动由服务端拒绝，不伪造完成。"""
    for acc in accounts:
        log(f"=== {acc.nickname} (uid={acc.user_id}) ===")
        newbie = acc.claim_newbie_tasks()
        if not newbie:
            log("  - 服务端未下发可领取新人任务")
        for r in newbie:
            log(f"  新人任务 {r.get('task')}: {str(r.get('response'))[:180]}")
        promos = acc.claim_active_promotions()
        if not promos:
            log("  - 当前没有活动窗口")
        for r in promos:
            log(f"  活动 {r.get('name')} / {r.get('reward_type')}: HTTP {r.get('http')} {str(r.get('response'))[:180]}")
    return 0


def cmd_add(src_dir: str):
    """从另一份 AutoClaw 数据目录导入账号。"""
    src = Path(src_dir)
    acc = load_account(src, name=src.name)
    if not acc:
        log(f"无法从 {src} 读取账号（需要 auth.json + Local State）")
        return 1
    dest = ACCOUNTS_DIR / (str(acc.user_id) or src.name)
    dest.mkdir(parents=True, exist_ok=True)
    for f in ("auth.json", "Local State", "channel.json"):
        p = src / f
        if p.is_file():
            (dest / f).write_bytes(p.read_bytes())
    # 设备身份也一起带走：新注册账号的 deviceId 就存这里
    ident = src / "identity" / "device.json"
    if ident.is_file():
        (dest / "identity").mkdir(parents=True, exist_ok=True)
        (dest / "identity" / "device.json").write_bytes(ident.read_bytes())
    log(f"已导入账号 {acc.nickname} (uid={acc.user_id}) -> {dest}")
    return 0


def cmd_export(out_dir: str):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for acc in discover_accounts():
        d = out / str(acc.user_id)
        d.mkdir(parents=True, exist_ok=True)
        for f in ("auth.json", "Local State", "channel.json"):
            p = acc.appdata_dir / f
            if p.is_file():
                (d / f).write_bytes(p.read_bytes())
        n += 1
    log(f"已导出 {n} 个账号 -> {out}")
    return 0


# ---------------- 账号切换（仿 Z·SWITCH） ----------------
def _autoclaw_roots():
    """AutoClaw 可能的安装根目录：环境变量 > 盘符扫描 > 常见路径。"""
    roots = []
    env = os.environ.get("AUTOCLAW_HOME")
    if env:
        roots.append(Path(env))
    for drive in ("C:", "D:", "E:", "F:"):
        roots.append(Path(drive + "\\") / "AutoClaw")          # Path("C:")/"x" 是盘符相对路径（无分隔符），必须补 \n        roots.append(Path(drive + "\\") / "Program Files" / "AutoClaw")
    roots.append(Path.home() / "AppData" / "Local" / "AutoClaw")
    return roots

AUTOCLAW_EXE_CANDIDATES = [r / "AutoClaw.exe" for r in _autoclaw_roots()] \
                        + [r / "AutoClaw2.exe" for r in _autoclaw_roots()]


def _running_autoclaw_exes():
    """从正在运行的进程里找 AutoClaw*.exe 的真实路径。

    客户端可能装在目录扫描覆盖不到的地方（本机就在 D:\\APP\\code\\autoclaw\\AutoClaw2\\），
    而运行中的进程路径是权威来源，且不做任何网络请求。
    """
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process AutoClaw* -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    return [Path(p) for p in (l.strip() for l in out.splitlines()) if p.lower().endswith(".exe")]


def find_autoclaw_exe() -> Path | None:
    for p in _running_autoclaw_exes():
        if p.is_file():
            return p
    for p in AUTOCLAW_EXE_CANDIDATES:
        if p.is_file():
            return p
    return None


_AUTOCLAW_VERSION_CACHE: str | None = None


def detect_autoclaw_version() -> str:
    """读 AutoClaw.exe 的 FileVersion，**原样保留完整段数**。

    官方客户端发的是 Electron `app.getVersion()`（形如 `1.18.5.851`）。
    实测：`/autoclaw-proxy` 网关对 `X-Version: 1.18.5` 回 **401 "Invalid token"**，
    对 `1.18.5.851` 回 200 —— 它校验版本，但把失败报成凭证错误，极具误导性。
    所以这里绝不截断成三段；AutoClaw 升级后自动跟随，读不到时用
    AUTOCLAW_VERSION_OVERRIDE 兜底。
    """
    global _AUTOCLAW_VERSION_CACHE
    if _AUTOCLAW_VERSION_CACHE:
        return _AUTOCLAW_VERSION_CACHE
    ver = (os.environ.get("AUTOCLAW_VERSION_OVERRIDE") or "").strip()
    if not ver:
        exe = find_autoclaw_exe()
        if exe and exe.is_file():
            try:
                ver = _read_exe_file_version(exe)
            except Exception as e:
                log(f"读取 AutoClaw 版本失败：{type(e).__name__}: {e}")
    if not ver:
        ver = "1.17.8"  # 兜底：已知可用的老版本号，服务端仍然接受
    m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", ver)
    _AUTOCLAW_VERSION_CACHE = m.group(1) if m else ver
    log(f"AutoClaw 版本自适应：{_AUTOCLAW_VERSION_CACHE}")
    return _AUTOCLAW_VERSION_CACHE


def _read_exe_file_version(exe: Path) -> str:
    """用 version.dll 读 exe 的 FileVersion 字符串（免 PowerShell，静默快速）。"""
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(exe), None)
    if not size:
        return ""
    data = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(exe), 0, size, data):
        return ""
    val = ctypes.c_void_p()
    ln = ctypes.c_uint()
    # 常见语言代码页组合；Win10+ 的 Electron 应用基本都在 040904b0
    for sub in (r"\StringFileInfo\040904b0\FileVersion",
                r"\StringFileInfo\040904e4\FileVersion",
                r"\StringFileInfo\080404b0\FileVersion"):
        if ctypes.windll.version.VerQueryValueW(data, sub, ctypes.byref(val), ctypes.byref(ln)) and ln.value:
            return ctypes.wstring_at(val, ln.value).strip()
    return ""


def autoclaw_running() -> bool:
    import subprocess
    try:
        # 2.x 客户端进程名是 AutoClaw2.exe，两个镜像都要查
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AutoClaw.exe", "/FO", "CSV"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        out2 = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AutoClaw2.exe", "/FO", "CSV"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return (b"AutoClaw.exe" in (out or b"")
                or b"AutoClaw2.exe" in (out2 or b""))
    except Exception:
        return False


def kill_autoclaw(timeout=10) -> bool:
    """关闭主实例 AutoClaw（切换账号重启用）。

    红线：绝不无差别 taskkill /IM。若 A-SWITCH 的隔离登录窗口恰好开着，
    那些进程在 _ISOLATED_PIDS 里，必须放过；只杀主实例。
    """
    if not autoclaw_running():
        return True
    pids = _autoclaw_pids()
    if not pids:
        # tasklist 说还在但枚举不到：刚退出中或枚举抖动，绝不能当成"已关闭"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not autoclaw_running():
                return True
            pids = _autoclaw_pids()
            if pids:
                break
            time.sleep(0.5)
    if not pids:
        log("枚举不到 AutoClaw 进程但任务列表仍有该镜像，无法确认已关闭")
        return False
    targets = pids - _ISOLATED_PIDS
    if not targets:
        log("只剩隔离登录实例进程，已跳过关闭（不误杀登录窗口）")
        return False
    ppids = _autoclaw_ppids()
    roots = [p for p in targets if ppids.get(p) not in targets] or list(targets)
    if not _kill_pid_tree(roots):
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not (_autoclaw_pids() - _ISOLATED_PIDS):
            return True
        time.sleep(0.5)
    return not (_autoclaw_pids() - _ISOLATED_PIDS)


def launch_autoclaw() -> tuple[bool, str]:
    """启动 AutoClaw（脱离父进程）。

    AutoClaw.exe 带提升清单，直接 Popen 会 WinError 740；和隔离登录一样
    经 cmd /c start 让 shell 去创建。
    """
    import subprocess
    exe = find_autoclaw_exe()
    if not exe:
        return False, "找不到 AutoClaw.exe"
    try:
        subprocess.Popen(f'cmd /c start "" "{exe}"',
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         close_fds=True)
        return True, str(exe)
    except Exception as e:
        return False, str(e)


# ---------------- 登录添加账号（独立 profile + 官方登录流程） ----------------
# 原理（见交接文档 3.5）：用 --user-data-dir 起一个隔离实例，用户在官方窗口里
# 正常登录，凭证就落在我们控制的目录里，随后按 cmd_add 的方式入库。
# 之所以不逆向 OAuth API：zai-oauth-url 端点被服务端门禁（631002）封死，客户端无解。
#
# ⚠️ 安全红线：绝不能 taskkill /IM AutoClaw.exe。
#    用户平时常驻 4~10 个进程，杀错会影响正常使用。只按 --user-data-dir 特征
#    结束本次自己启动的那一组进程。pid 也拿不到（cmd start 会脱离父子关系），
#    所以统一用命令行特征来定位。

def _jwt_payload(token: str) -> dict:
    """从 JWT 里解出 payload（auth.json 还没写入 userInfo 时的兜底，取 user_id/邮箱）。"""
    tok = token.split()[-1] if token.lower().startswith("bearer ") else token
    parts = tok.split(".")
    if len(parts) < 2:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8", "replace"))
    except Exception:
        return {}


_ISOLATED_PIDS: set = set()  # 本工具启动的隔离登录实例 PID（登录窗口进程树）


def _autoclaw_pids() -> set:
    """所有 AutoClaw*.exe（1.x AutoClaw.exe + 2.x AutoClaw2.exe）的 PID。

    ⚠ AutoClaw 以管理员运行，非管理员查询 CIM 时 CommandLine 是空的
    （这正是 1.18.x 上"启动后探测不到进程"的根因），但 tasklist/CIM 的
    PID 永远可见——所以一切定位改用 PID 差集，不再匹配命令行。
    """
    import subprocess
    pids = set()
    try:
        for image in ("AutoClaw.exe", "AutoClaw2.exe"):
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV"],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in (out or b"").decode("gbk", "replace").splitlines():
                parts = [x.strip('"') for x in line.split('","')]
                if len(parts) >= 2 and parts[0] == image:
                    try:
                        pids.add(int(parts[1]))
                    except ValueError:
                        pass
        return pids
    except Exception as e:
        log(f"枚举 AutoClaw 进程失败：{type(e).__name__}: {e}")
        return set()


def _autoclaw_ppids() -> dict:
    """{pid: ppid}。提权进程的 ParentProcessId 同样可见，用于找进程树根。"""
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='AutoClaw.exe'\" | "
             "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)\" }"],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        m = {}
        for line in (out.stdout or "").splitlines():
            if "\t" not in line:
                continue
            pid_s, ppid_s = line.split("\t", 1)
            try:
                m[int(pid_s.strip())] = int(ppid_s.strip())
            except ValueError:
                continue
        return m
    except Exception:
        return {}


def _kill_pid_tree(pids, timeout_each: int = 15) -> bool:
    """提权杀进程树：先常规 taskkill，拒绝访问时经 UAC 提权重试。

    pids 里只传"根"，/T 会带走整棵子树。以管理员身份运行本工具时
    （打包 exe 默认提权）不会触发 UAC 弹窗。
    """
    import subprocess
    pids = [p for p in pids if p]
    if not pids:
        return True
    args = []
    for p in pids:
        args += ["/PID", str(p)]
    r = subprocess.run(["taskkill", "/F", "/T"] + args,
                       capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode == 0:
        return True
    err = (r.stderr or b"").decode("gbk", "replace")
    if "拒绝访问" in err:
        log("普通权限杀不掉（目标以管理员运行），走 UAC 提权重试…")
        pids_str = ",".join(f"'{p}'" for p in pids)
        ps = (f"Start-Process taskkill -ArgumentList '/F','/T',{pids_str}"
              " -Verb RunAs -Wait")
        try:
            r2 = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return r2.returncode == 0
        except Exception as e:
            log(f"提权杀进程失败：{type(e).__name__}: {e}")
            return False
    return False


def _prepare_isolated_identity(profile_dir: Path) -> Path:
    """给临时 profile 预置独立 Ed25519 设备身份。

    AutoClaw 新 profile 找不到 identity/device.json 时，会回退到系统凭据
    和固定的 ~/.openclaw-autoclaw 身份；那会让两个账号共享 deviceId，
    服务端把它们当成同一设备，正是之前权益串号的根因。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    identity_dir = Path(profile_dir) / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    public_pem = public.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    raw_public = public.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    identity = {
        "version": 1,
        "deviceId": hashlib.sha256(raw_public).hexdigest(),
        "publicKeyPem": public_pem,
        "privateKeyPem": private_pem,
        "createdAtMs": int(time.time() * 1000),
    }
    path = identity_dir / "device.json"
    path.write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# ---------------- 无桌面端注册 / 登录（纯 HTTP，不启动 AutoClaw） ----------------
# 逆向结论（1.18.5 bundle）：AutoClaw 没有独立 register 接口——
# /userapi/v1/agent-login 对未注册手机号即完成注册（响应里的 first_login 为真）。
# 请求体明文手机号 + 6 位数字验证码，客户端侧不签名、无验证码控件，
# 唯一绕不开的外部依赖是"收到那条短信"，bundle 里没有测试码也没有后门。
SEND_CODE_PATH = "/userapi/v1/agent-send-code"
LOGIN_PATH = "/userapi/v1/agent-login"      # ⚠️ 不带结尾斜杠：带斜杠服务端 307 后直接断连
ACCOUNT_META = "a_switch_account.json"      # 手机号 → 档案目录的反查索引（App 自己没这信息）
CODE_TTL_SECONDS = 300


def mask_phone(phone) -> str:
    p = re.sub(r"\D", "", str(phone or ""))
    if not p:
        return ""
    return f"{p[:3]}****{p[-4:]}" if len(p) >= 7 else "***"


def _safe_json(raw: bytes) -> dict:
    try:
        d = json.loads((raw or b"").decode("utf-8", "replace"))
    except Exception:
        return {"code": -1, "msg": (raw or b"")[:200].decode("utf-8", "replace").strip() or "非 JSON 响应",
                "data": None}
    return d if isinstance(d, dict) else {"code": -1, "msg": str(d)[:200], "data": None}


def _api_request(method: str, path: str, body=None, token: str = "",
                 channel: str = "zai", timeout: int = 25, base: str = BASE) -> tuple:
    """注册/登录阶段还没有 Account 可加载，用这套等价的公共头直连。

    2026-09-26：上游对本机出口间歇性 RST 非 node 指纹的 TLS（python 全挂）。
    直连失败（st=0）改走本地反代 /fwd 桥（node 出站 + 4 次重试）——人机验证配置、
    发验证码、登录这些注册链路全靠它，断了就是 GUI 上"Captcha instance timed out"。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=auth_headers(token, channel))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _safe_json(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, _safe_json(e.read(600))
        except Exception:
            return e.code, {"code": -1, "msg": f"HTTP {e.code}", "data": None}
    except Exception as e:
        st2, d2 = _api_via_bridge(method, path, auth_headers(token, channel), body,
                                  max(timeout, 90), base)
        if st2 is not None:
            return st2, d2
        return 0, {"code": -1, "msg": f"{type(e).__name__}: {e}", "data": None}


def _api_via_bridge(method: str, path: str, headers: dict, body, timeout: float, base: str = ""):
    """经本地反代 /fwd 转发（loopback HTTP -> node TLS 出站）。桥不可用返回 (None, None)。"""
    try:
        payload = {"method": method, "path": path, "headers": headers, "body": body, "base": base}
        req = urllib.request.Request(
            f"http://127.0.0.1:{RELAY_PORT}/fwd",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        st = data.get("status", 0)
        raw = data.get("body", "")
        try:
            return st, _safe_json(raw.encode("utf-8") if isinstance(raw, str) else raw)
        except Exception:
            return st, raw
    except Exception:
        return None, None


def _dpapi_protect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), "AutoClaw os_crypt", None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptProtectData failed")
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def mint_local_state(appdata_dir: Path) -> None:
    """给这个档案目录现造一把 os_crypt 主密钥（DPAPI 包裹，与真 App 同格式）。

    有了它，headless 注册出来的目录和桌面端登录出来的目录完全同构：
    既能被 load_account 解密，也能被 build_identity_bundle 成套写进桌面端切换。
    """
    appdata_dir = Path(appdata_dir)
    ls = appdata_dir / "Local State"
    if ls.is_file():
        try:
            _os_crypt_key(appdata_dir)     # 已有且本机能解 → 不动它
            return
        except Exception:
            pass
    wrapped = base64.b64encode(b"DPAPI" + _dpapi_protect(os.urandom(32))).decode("ascii")
    _atomic_write(ls, json.dumps({"os_crypt": {"audit_enabled": True,
                                               "encrypted_key": wrapped}},
                                 ensure_ascii=False, indent=2).encode("utf-8"))


def _pending_path(phone: str) -> Path:
    """发码与换码之间必须共用同一个 deviceId，所以身份先落在这。"""
    return ACCOUNTS_DIR.parent / "_pending" / hashlib.sha256(phone.encode()).hexdigest()[:16]


def account_phone(appdata_dir: Path) -> str:
    """档案 sidecar 里记的手机号；桌面端导入的老档案没有这一项。"""
    try:
        meta = json.loads((Path(appdata_dir) / ACCOUNT_META).read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(meta.get("phone") or "")


def _archive_of_phone(phone: str) -> Path | None:
    if not ACCOUNTS_DIR.is_dir():
        return None
    for sub in sorted(ACCOUNTS_DIR.iterdir()):
        meta = sub / ACCOUNT_META
        if not meta.is_file():
            continue
        try:
            if str(json.loads(meta.read_text(encoding="utf-8")).get("phone")) == phone:
                return sub
        except Exception:
            continue
    return None


def begin_headless_add(phone: str, channel: str = "zai") -> dict:
    """预铸设备身份 + 发送短信验证码（短信只能人读，所以注册拆成两步）。"""
    phone = re.sub(r"\D", "", str(phone or ""))
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        return {"ok": False, "error": "手机号格式不正确（需要 11 位中国大陆手机号）"}
    known = _archive_of_phone(phone)
    pend = _pending_path(phone)
    pend.mkdir(parents=True, exist_ok=True)
    ident_file = known / "identity" / "device.json" if known else None
    if ident_file and ident_file.is_file():
        # 老号重新登录：必须沿用原 deviceId，否则服务端按"换设备"处理，权益会串
        _atomic_write(pend / "identity" / "device.json", ident_file.read_bytes())
    else:
        _prepare_isolated_identity(pend)
    device_id = json.loads((pend / "identity" / "device.json")
                           .read_text(encoding="utf-8"))["deviceId"]
    st, d = _api_request("POST", SEND_CODE_PATH,
                         {"source_id": "autoclaw", "device_id": device_id, "phone": phone},
                         channel=channel)
    ok = st == 200 and d.get("code") == 0 and d.get("data") is not False
    if not ok:
        shutil.rmtree(pend, ignore_errors=True)
        return {"ok": False, "phone": mask_phone(phone), "code": d.get("code"),
                "error": d.get("msg") or f"HTTP {st}"}
    _atomic_write(pend / "pending.json", json.dumps(
        {"phone": phone, "channel": channel, "device_id": device_id, "sent_at": _now_ms(),
         "reused_identity": bool(ident_file and ident_file.is_file())},
        ensure_ascii=False, indent=2).encode("utf-8"))
    log(f"验证码已发往 {mask_phone(phone)}（deviceId={device_id[:12]}，"
        f"{'沿用已有设备身份' if ident_file else '新铸设备身份'}）")
    return {"ok": True, "phone": mask_phone(phone), "device_id": device_id[:12],
            "reused_identity": bool(ident_file and ident_file.is_file()),
            "expires_in": CODE_TTL_SECONDS, "error": ""}


def deviceid_owner(device_id: str, exclude_uid: str = "") -> str:
    """返回已占用该 deviceId 的**其它**账号 uid（没有则返回 ""）。

    学 Z-SWITCH 的 taken() 检查：两个档案绝不能共用一份设备身份 ——
    服务端把它们看成同一台机器，多号就退化成一个号，新人资格直接归零。
    活动目录（桌面端正在用的那份）也一起扫：它可能还没同步进档案。
    """
    if not device_id:
        return ""
    roots = []
    if ACCOUNTS_DIR.is_dir():
        roots += [sub for sub in sorted(ACCOUNTS_DIR.iterdir()) if sub.is_dir()]
    if DEFAULT_STATE_DIR.is_dir():
        roots.append(DEFAULT_STATE_DIR)
    for sub in roots:
        if sub.name == str(exclude_uid):
            continue
        # 身份文件是第一手记录，但早期从桌面端 profile 直接导入的号只有 auth.json
        # 里那行明文 deviceId —— 只看前者会漏掉"共用桌面端固定身份"这一整类撞车
        for rel, key in (("identity/device.json", "deviceId"), ("auth.json", "deviceId")):
            p = sub / rel
            try:
                if p.is_file() and json.loads(p.read_text(encoding="utf-8")).get(key) == device_id:
                    return sub.name
            except Exception:
                continue
    return ""


def deviceid_conflicts() -> list[dict]:
    """列出共用同一份设备身份的档案（一台"设备"只有一次新人资格）。

    早期从桌面端 profile 直接导入的账号全都带同一个 deviceId —— 那是在
    按号铸身份之前留下的，服务端会把它们看成一台机器上的多个号。
    """
    by: dict[str, list] = {}
    if not ACCOUNTS_DIR.is_dir():
        return []
    for sub in sorted(ACCOUNTS_DIR.iterdir()):
        p = sub / "identity" / "device.json"
        if not (sub.is_dir() and p.is_file()):
            continue
        try:
            dev = str(json.loads(p.read_text(encoding="utf-8")).get("deviceId") or "")
        except Exception:
            continue
        if dev:
            by.setdefault(dev, []).append(sub.name)
    return [{"device_id": d[:12], "uids": u} for d, u in by.items() if len(u) > 1]


def mask_invite_code(code: str) -> str:
    c = str(code or "").strip()
    if not c:
        return "(空)"
    return f"{c[:2]}***{c[-1]}" if len(c) >= 4 else "***"


def find_inviter(invite_code: str):
    """在本机已入库账号里找这个邀请码的主人（拿它自己的计数当证据）。"""
    target = str(invite_code or "").strip()
    if not target:
        return None
    for a in discover_accounts():
        try:
            if str(a.my_invite_code()).strip() == target:
                return a
        except Exception:
            continue
    return None


def _invite_stats(acc) -> tuple:
    """返回 (是否读到, stats)。

    读不到必须如实返回 False：以前失败时返回 {}，差值就按 0 算，
    于是"基线没读到 + 邀请人早就计过的奖"会被当成**本次新增计奖**报成功。
    """
    try:
        s = (acc.invite_status() or {}).get("stats")
    except Exception:
        s = None
    return (isinstance(s, dict) and bool(s)), (s or {})


def _bind_state(acc) -> tuple:
    """绑没绑上以受邀号自己的 bind.status 为准，不看接口返回码。"""
    try:
        b = (acc.invite_status() or {}).get("bind") or {}
    except Exception:
        return False, "读不到绑定状态"
    st = str(b.get("status") or "unknown")
    return st == "bound", st


def bind_invite_with_proof(acc, invite_code: str, say=None, attempts: int = 3) -> dict:
    """绑邀请码 —— **第一件事就是绑**，然后才去取证。

    顺序为什么是这样：服务端接受绑定的窗口极短（实测甲号注册后 +4s 绑成功，
    乙号 +40s 就被回 400001），而取证要先在本机逐号打网络请求找邀请人（实测 3.5s）——
    放在前面就是把奖励等没。何况计奖本来要等受邀号**真实使用之后**才结算
    （任务标题就叫「邀请好友使用」；实测只签到、只领灵感都不算），
    所以绑这一瞬间的"前后差值"几乎没有信息量，一律不在这里宣称已计奖，
    只把受邀号自己的 bind.status 和邀请人当前计数如实摆出来，
    事后用「复核奖励」看差值才是对的。

    另外两条实测过的"补一下就计奖"猜测都不成立：绑完再调一次 task-list 没用；
    把 `activity.bind_inviter_reward_event`（值 `bind_and_query`）当事件名上报给
    `/userapi/v1/user/web-event/report` 会被回 400001 —— 那个接口只收客户端自己的事件。
    """
    note = say or (lambda m: None)
    code = str(invite_code or "").strip()
    out = {"invite_code": mask_invite_code(code), "bound": False, "http": None,
           "server_code": None, "msg": "", "inviter": "", "rule": {},
           "before": {}, "after": {}, "invited_delta": None, "reward_delta": None,
           "baseline_ok": False, "bind_status": "", "attempts": 0}

    # 1) 先绑，并以受邀号自己的状态判定是否真的生效
    for i in range(max(1, attempts)):
        out["attempts"] = i + 1
        try:
            st, d = acc.bind_invite_code(code)
        except Exception as e:
            st, d = None, f"{type(e).__name__}: {e}"
        out["http"] = st
        if isinstance(d, dict):
            out["server_code"] = d.get("code")
            out["msg"] = str(d.get("msg") or "")[:120]
        else:
            out["msg"] = str(d)[:120]
        ok, bstate = _bind_state(acc)
        out["bound"], out["bind_status"] = ok, bstate
        if ok or bstate not in ("unbound", "unknown"):
            break                      # 生效了，或服务端明确给了别的状态，都别再重试
        if i + 1 < attempts:
            note(f"绑定未生效（{out['msg'] or bstate}），第 {i + 2} 次重试…")
            time.sleep(1.5 * (i + 1))
    note(f"邀请码 {out['invite_code']} → "
         + (f"已绑定（受邀号 bind.status={out['bind_status']}）" if out["bound"]
            else f"未绑上：{out['msg'] or out['bind_status'] or out['server_code']}"))
    if not out["bound"]:
        note("未绑定就不可能有邀请奖励；该号已错过注册时的绑定窗口，"
             "可在它上面点「补绑未绑定账号」再试（老号补绑服务端按规则不计奖）")
        return out

    # 2) 绑上了才去取证：邀请人是谁、它现在收到多少
    inviter = find_inviter(code)
    if not inviter:
        note("本机找不到该邀请码的主人，无法核对奖励归属")
        return out
    out["inviter"] = f"{inviter.nickname}/{inviter.jwt_uid()}"
    out["rule"] = inviter.invite_rule()
    after_ok, out["after"] = _invite_stats(inviter)
    out["baseline_ok"] = after_ok
    if not after_ok:
        note(f"邀请人 {out['inviter']}：计数没读到，稍后用「复核奖励」再看")
        return out
    st = out["after"]
    note(f"邀请人 {out['inviter']}：当前邀请数 {st.get('invited_count')}、"
         f"已计奖 {st.get('rewarded_bind_count')}、奖励合计 {st.get('inviter_reward_total')}"
         f"（规则：新号 {out['rule'].get('bind_inviter_reward_amount')} 分/人、"
         f"上限 {out['rule'].get('bind_inviter_reward_limit')}；"
         "要这个新号真实使用之后才结算，稍后用「复核奖励」核对）")
    return out


def invite_reward_check(say=None) -> list:
    """只读复核：本机每个号的「绑给了谁 + 作为邀请人收到多少」现状。

    好友注册奖励要等受邀号真实使用（甚至可能延迟结算），当场看不到属正常。
    这里提供一个不看接口返回值、只看邀请人自己计数和流水的复核入口。
    """
    note = say or (lambda m: None)
    rows = []
    for a in discover_accounts():
        s = a.invite_status() or {}
        b = s.get("bind") or {}
        st = s.get("stats") or {}
        lines = []
        try:
            for e in a.ledger(30):
                desc = str(e.get("desc") or "")
                if any(w in desc.lower() for w in ("invite", "fission", "referral", "friend")) \
                        or any(w in desc for w in ("邀请", "好友")):
                    lines.append(e)
        except Exception:
            pass
        row = {"name": a.nickname, "jwt_uid": a.jwt_uid(),
               "bound_to": str(b.get("inviter_id") or ""),
               "bind_status": str(b.get("status") or ""),
               "invited_count": st.get("invited_count"),
               "existing_user_invited_count": st.get("existing_user_invited_count"),
               "rewarded_bind_count": st.get("rewarded_bind_count"),
               "inviter_reward_total": st.get("inviter_reward_total"),
               "reward_lines": lines}
        rows.append(row)
        note(f"  {a.nickname}/{a.jwt_uid()}：绑给 {row['bound_to'] or '—'}"
             f"（{row['bind_status'] or '—'}），邀请数 {row['invited_count']}，"
             f"已计奖 {row['rewarded_bind_count']}，奖励合计 {row['inviter_reward_total']}"
             + (f"，流水 {len(lines)} 条" if lines else ""))
    return rows


def diagnose_new_account(acc: Account, say=None, first_login: bool | None = None) -> dict:
    """新号一次性全量取证：一次注册就问清服务端怎么对待这份新身份。

    可用来注册的账号名额有限，绝不能靠"再注册一个来试试"排错，所以这里把
    每一条独立取一遍并落盘（脱敏），一次就拿到全部真相。
    """
    note = say or (lambda m: None)
    out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "uid": str(acc.user_id or ""),
           "jwt_uid": acc.jwt_uid(), "nickname": acc.nickname,
           "device_id": (acc.device_id or "")[:12], "shared_device": "", "errors": []}

    def grab(label, fn):
        """一段失败不许带走其它段 —— 少一个字段都比少一整包强。"""
        try:
            return fn()
        except Exception as e:
            out["errors"].append(f"{label}: {type(e).__name__}: {e}")
            return None

    out["shared_device"] = grab("deviceid_owner",
                                lambda: deviceid_owner(acc.device_id,
                                                       exclude_uid=str(acc.user_id))) or ""

    nb = grab("identity-tasks", acc.newbie_tasks) or {}
    nd = nb.get("data") if isinstance(nb, dict) else None
    nd = nd if isinstance(nd, dict) else {}
    ident = nd.get("identity")
    out["identity_tasks"] = {
        "code": nb.get("code") if isinstance(nb, dict) else None,
        "all_tasks_completed": nd.get("all_tasks_completed"),
        "is_bound": nd.get("is_bound"),
        # 只留形状，不留内容：里面可能是个人信息
        "identity": ("null" if ident is None
                     else f"{type(ident).__name__}({len(ident)})"
                     if isinstance(ident, (dict, list)) else str(type(ident).__name__)),
        "tasks": [{"id": t.get("task_category") or t.get("task_id") or t.get("id"),
                   "points": t.get("reward_points") or t.get("points"),
                   "state": ("completed" if t.get("completed") is True
                             else t.get("status") or "todo")}
                  for t in (nd.get("tasks") or []) if isinstance(t, dict)],
    }

    bal = grab("wallet", acc.points) or {}
    out["wallet"] = {"total": bal.get("total"),
                     "buckets": [{"name": w.get("name"), "balance": w.get("balance")}
                                 for w in (bal.get("wallets") or [])]}

    led = grab("ledger", acc.ledger) or []
    credits = [{"amount": e.get("amount"), "desc": e.get("desc", "")}
               for e in led if e.get("type") == "credit"]
    out["credits"] = credits

    gt = grab("newbie-guide-token", acc.newbie_guide_token)
    if isinstance(gt, tuple):
        gst, gd = gt
        # 服务端把 token 放在响应顶层（不是 data 里），两种位置都认
        blob = gd if isinstance(gd, dict) else {}
        tok = str(blob.get("token") or (blob.get("data") or {}).get("token") or "")
        # 只记"有没有、多长"，token 本身绝不落盘、绝不打日志
        out["guide_token"] = {"http": gst, "code": blob.get("code"),
                              "present": bool(tok), "length": len(tok)}
    else:
        out["guide_token"] = {"http": None, "present": False, "length": 0}

    amounts = [int(c.get("amount") or 0) for c in credits]
    tasks = out["identity_tasks"]["tasks"]
    total = int(out["wallet"]["total"] or 0)
    out["first_login"] = first_login
    if any(a >= NEWBIE_GRANT_POINTS for a in amounts):
        out["verdict"] = f"新人 {NEWBIE_GRANT_POINTS} 分已到账（积分流水里看得见）"
    elif total >= NEWBIE_GRANT_POINTS:
        out["verdict"] = f"当前余额 {total} 已 ≥ {NEWBIE_GRANT_POINTS}，按余额判定为已到账"
    elif tasks:
        out["verdict"] = f"服务端下发了 {len(tasks)} 个新人任务，还没领（点「一键领取」）"
    elif first_login is False:
        out["verdict"] = "老号重新登录：服务端本来就不会再发新人任务"
    else:
        out["verdict"] = (f"新号但服务端既没下发新人任务、也没有 ≥{NEWBIE_GRANT_POINTS} 的到账流水"
                          + ("（设备身份与已有账号相同，这是直接原因）"
                             if out["shared_device"] else " —— 这份新身份没被当成新设备"))

    try:
        (acc.appdata_dir / NEW_ACCOUNT_DIAG_FILE).write_bytes(
            json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception as e:
        out["errors"].append(f"写取证文件失败: {type(e).__name__}")

    note(f"新号取证 余额={total} 新人任务={len(tasks)} "
         f"最大单笔到账={max(amounts) if amounts else 0} "
         f"设备身份{'与 ' + out['shared_device'][:8] + ' 相同！' if out['shared_device'] else '独立'}")
    note("结论：" + out["verdict"])
    if out["errors"]:
        note("取证中的缺项：" + "；".join(out["errors"][:4]))
    return out


def _archive_login_result(token: str, refresh: str, device_id: str, channel: str,
                          data: dict, say, fallback_name: str = "",
                          login_via: str = "phone-code", pend_dir: Path | None = None,
                          invite_code: str = "", phone: str = "") -> dict:
    """把一对登录凭证落成一个账号档案目录（短信注册与 Z.ai OAuth 共用）。

    写失败或回读校验不过就整目录回滚，绝不留下半新半旧的档案——那种档案正是
    §5.1 里"切过去看着成功、其实顶回旧号"的源头。
    """
    # 绑定必须是"拿到 token 后的第一个网络动作"：服务端接受邀请绑定的窗口只有几秒
    # （实测 +3s 成功、+40s 就被回 400001），而下面拉资料 + 写档案 + 回读校验
    # 一跑就是 1~2s。所以这里用一对还没落盘的凭证直接绑，档案阶段只负责存结果。
    bound = None
    code = str(invite_code or "").strip()
    if code:
        pending_acc = Account(fallback_name or "新号", pend_dir or ACCOUNTS_DIR,
                              {"userInfo": {"user_id": str(data.get("user_id") or "")}},
                              token, device_id=device_id, channel=channel,
                              refresh_token=refresh)
        say("绑定邀请关系…")
        try:
            bound = bind_invite_with_proof(pending_acc, code, say=say)
        except Exception as e:
            bound = {"bound": False, "msg": f"{type(e).__name__}: {e}",
                     "invite_code": mask_invite_code(code)}
            log(f"绑定邀请码异常：{type(e).__name__}: {e}")

    say("拉取账号资料…")
    st2, pd = _api_request("POST", "/userapi/v1/user-profile",
                           {"source_id": "autoclaw", "device_id": device_id},
                           token=token, channel=channel)
    ui = pd.get("data") if (st2 == 200 and pd.get("code") == 0
                            and isinstance(pd.get("data"), dict)) else None
    if not ui:
        ui = {"user_id": str(data.get("user_id") or ""),
              "user_name": data.get("user_name") or "", "user_phone": ""}
    ui.setdefault("user_id", str(data.get("user_id") or ""))
    uid = str(ui.get("user_id") or data.get("user_id") or "")
    if not uid:
        return {"ok": False, "error": "拿不到 user_id，未入档"}
    clash = deviceid_owner(device_id, exclude_uid=uid)
    if clash:
        return {"ok": False,
                "error": f"这份设备身份已被账号 {clash[:8]} 占用，拒绝入档："
                         f"两个号共用一台「设备」会被服务端当成同一个新号，"
                         f"新人资格只会算在先到那个头上"}
    nickname = (ui.get("user_name") or ui.get("name") or fallback_name or f"账号{uid[:8]}")
    first_login = bool(data.get("first_login") or data.get("web_first_login"))
    email = str(ui.get("user_email") or ui.get("email") or "")

    dest = ACCOUNTS_DIR / uid
    dest.mkdir(parents=True, exist_ok=True)
    keep = {}
    for f in ("auth.json", "channel.json", "Local State"):
        p = dest / f
        if p.is_file():
            keep[f] = p.read_bytes()

    def _restore():
        for f in ("auth.json", "channel.json", "Local State"):
            if f in keep:
                _atomic_write(dest / f, keep[f])
            else:
                (dest / f).unlink(missing_ok=True)

    say(f"写入账号档案 {uid[:8]}…")
    ident_src = (pend_dir / "identity" / "device.json") if pend_dir else None
    dst_ident = dest / "identity" / "device.json"
    try:
        if ident_src and ident_src.is_file() and not dst_ident.is_file():
            _atomic_write(dst_ident, ident_src.read_bytes())
        mint_local_state(dest)
        auth = {"deviceId": device_id, "updatedAt": _now_ms(),
                "token": encrypt_chromium_value(token, dest),
                "refreshToken": encrypt_chromium_value(refresh, dest),
                "userInfo": ui}
        _atomic_write(dest / "auth.json",
                      json.dumps(auth, ensure_ascii=False, indent=2).encode("utf-8"))
        _atomic_write(dest / "channel.json",
                      json.dumps({"channel": channel}, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception as e:
        _restore()
        return {"ok": False, "error": f"写入档案失败：{type(e).__name__}: {e}"}

    acc = load_account(dest)
    if not acc or str(acc.user_id) != uid or not acc.token or not acc.refresh_token:
        _restore()
        return {"ok": False, "error": "档案回读校验未通过，已回滚到写入前的状态"}
    _atomic_write(dest / ACCOUNT_META, json.dumps(
        {"phone": phone or str(ui.get("user_phone") or ""), "email": email,
         "added_at": _now_ms(), "source": "headless", "login_via": login_via,
         "first_login": first_login, "jwt_uid": acc.jwt_uid()},
        ensure_ascii=False, indent=2).encode("utf-8"))
    if pend_dir and pend_dir.is_dir():
        shutil.rmtree(pend_dir, ignore_errors=True)

    log(f"已{'注册' if first_login else '登录'}并入档 {nickname}（uid={uid[:8]} "
        f"jwt_uid={acc.jwt_uid()} deviceId={device_id[:12]} 方式={login_via}）")
    diag = diagnose_new_account(acc, say=say, first_login=first_login)
    say("（新人到账可能有几秒延迟，稍后点「刷新」可再看一次余额）")
    archived_phone = phone or str(ui.get("user_phone") or "")
    return {"ok": True, "uid": uid, "nickname": nickname,
            "phone": mask_phone(archived_phone), "email": email,
            "first_login": first_login, "new_account": first_login,
            "jwt_uid": acc.jwt_uid(), "device_id": device_id[:12],
            "dir": str(dest), "invite_bound": bound,
            "diagnosis": diag, "error": ""}


def finish_headless_add(phone: str, code: str, invite_code: str = "",
                        on_progress=None) -> dict:
    """验证码换凭证 → 合成档案目录。全程不碰活动目录，也不启动桌面端。"""
    say = on_progress or (lambda m: None)
    phone = re.sub(r"\D", "", str(phone or ""))
    code = re.sub(r"\D", "", str(code or ""))
    if not re.fullmatch(r"\d{6}", code):
        return {"ok": False, "error": "验证码是 6 位数字"}
    pend = _pending_path(phone)
    try:
        meta = json.loads((pend / "pending.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    device_id = str(meta.get("device_id") or "")
    if not device_id:
        return {"ok": False, "error": "没有待完成的验证：先点\"发送验证码\"再回来填码"}
    age = (_now_ms() - int(meta.get("sent_at") or 0)) / 1000
    if age > CODE_TTL_SECONDS:
        say(f"验证码已发出 {int(age / 60)} 分钟，服务端通常已过期，失败就重发一次")
    channel = str(meta.get("channel") or "zai")
    say("正在用验证码换登录凭证…")
    st, d = _api_request("POST", LOGIN_PATH,
                         {"source_id": "autoclaw", "device_id": device_id,
                          "phone": phone, "code": int(code)}, channel=channel)
    data = d.get("data")
    if not (st == 200 and d.get("code") == 0 and isinstance(data, dict)):
        # 码错不删 pending：TTL 内允许重填，省一次短信
        return {"ok": False, "code": d.get("code"),
                "error": d.get("msg") or f"HTTP {st}",
                "retry_seconds_left": max(0, int(CODE_TTL_SECONDS - age))}
    token = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    if not token or not refresh:
        return {"ok": False, "error": "登录响应缺 access/refresh token，未入档"}

    return _archive_login_result(
        token, refresh, device_id, channel, data, say,
        fallback_name=mask_phone(phone), login_via="phone-code",
        pend_dir=pend, invite_code=invite_code, phone=phone)


# ---------------- Z.ai / Google OAuth 登录（免桌面端、且完全不需要短信） ----------------
ZAI_OAUTH_URL_PATH = "/userapi/overseasv1/zai-oauth-url"
ZAI_OAUTH_LOGIN_PATH = "/userapi/overseasv1/zai-oauth-login"
GOOGLE_OAUTH_URL_PATH = "/userapi/overseasv1/google-oauth-url"
GOOGLE_OAUTH_LOGIN_PATH = "/userapi/overseasv1/google-oauth-login"
# 桌面端用的就是这一端口（ac_main.js: CALLBACK_PORT=8989 / REDIRECT_URI）
OAUTH_CALLBACK_PORT = int(os.environ.get("ASWITCH_OAUTH_PORT") or 8989)
OAUTH_REDIRECT_URI = f"http://127.0.0.1:{OAUTH_CALLBACK_PORT}/oauth/callback"
OAUTH_WAIT_SECONDS = 240

# ---------------- 内置授权窗（零端口）：官方人机验证 → 官方授权页 → 截回跳读 code ----------------
# 字段名/顺序全部照官方 bundle：
#   ac_main.js:40144 buildOverseaOAuthCaptchaRequestParams → {ali_captcha_verify_param: verifyParam}
#   ac_main.js:41063 getOverseaZaiOAuthUrl                → withWebInfo({source_id, navigate_uri, 票据})
#   ac_main.js:98582 getZaiCallbackUri                    → http://localhost:{ALL_PORTS[0]}/auth/callback-zai
#   renderer:97340 loadAliyunCaptchaScript                → 先挂 window.AliyunCaptchaConfig={region,prefix} 再加载脚本
OVERSEA_CAPTCHA_CONFIG_PATH = "/userapi/overseasv1/oauth-captcha-config"
ZAI_CALLBACK_URI = "http://localhost:18432/auth/callback-zai"
GOOGLE_CALLBACK_URI = "http://localhost:18432/auth/callback-google"
ALIYUN_CAPTCHA_SCRIPT_URL = "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"
CAPTCHA_VERIFY_TIMEOUT = 120          # 官方 VERIFY_TIMEOUT_MS = 12e4
# 国际域是账号真正所在的主机；国内域是同服务的另一入口（06:05 实测它能进业务层回 631000）
OAUTH_BASES = (("国际域 autoglm.ai", BASE),
               ("国内域 zhipuai.cn", "https://autoglm-api.zhipuai.cn"))

# 2026-09-20 02:30 真人实测定论（用户亲手过完官方滑块，票据确实交到了服务端）：
# 国际域仍然 HTTP 405 + 阿里云边缘拦截页，国内域仍然业务码 631000。
# 已排除的两条猜测：① 缺票据；② 没带服务器自己下发的 acw_tc cookie
# （_t_cookie_conformance.py 实测带与不带结果完全一致）。
# 请求体与官方 renderer 的 getOverseaZaiOAuthUrl 逐字段一致
# （ac_main.js:40749 withWebInfo / 41063 组装 / 40193 签名头），
# 剩下的唯一差异是客户端指纹（Electron UA / TLS 指纹）——那是边缘防护在区分的东西，
# 伪装它属于绕过，A-SWITCH 不做。所以这条通道对本程序是关的，不是没修好。
INAPP_OAUTH_VERDICT = ("内置授权窗这条路已被服务端关掉：2026-09-20 02:30 真人过完官方验证后，"
                       "国际域仍回 HTTP 405（阿里云边缘拦截页）、国内域仍回 631000，"
                       "带不带服务器下发的 cookie 都一样。请改用「桌面端登录添加」或「手机号内置添加」。")


def oversea_oauth_captcha_config(channel: str = "zai", base: str = BASE) -> dict:
    """读官方海外 OAuth 的人机验证配置：{enabled, supplier, region, prefix, scene_id}。

    这一步不需要登录态（实测未鉴权 HTTP 200），拿到的三元组就是官方验证控件的初始化参数。
    """
    st, d = _api_request("POST", OVERSEA_CAPTCHA_CONFIG_PATH, {},
                         channel=channel, base=base, timeout=20)
    out = {"ok": False, "http": st, "enabled": False, "supplier": "",
           "region": "", "prefix": "", "scene_id": "", "error": ""}
    data = d.get("data") if isinstance(d, dict) else None
    if not (st == 200 and isinstance(data, dict) and d.get("code") == 0):
        out["error"] = (str(d.get("msg") or "")[:160] if isinstance(d, dict)
                        else str(d)[:160]) or f"HTTP {st}"
        return out
    out.update({"ok": True,
                "enabled": bool(data.get("enabled")),
                "supplier": str(data.get("captcha_supplier") or "").strip().lower(),
                "region": str(data.get("region") or "").strip().lower(),
                "prefix": str(data.get("prefix") or "").strip(),
                "scene_id": str(data.get("scene_id") or "").strip()})
    if out["enabled"] and out["supplier"] not in ("aliyun", "shumei"):
        out["error"] = f"未知验证供应商：{out['supplier'] or '（空）'}"
    if out["enabled"] and out["supplier"] == "aliyun":
        if out["region"] not in ("cn", "sgp", "ga") or not out["prefix"] or not out["scene_id"]:
            out["error"] = "验证配置缺字段（region/prefix/scene_id）"
    return out


def request_oversea_oauth_url(vendor: str = "zai", device_id: str = "", ticket: str = "",
                              navigate_uri: str = "", base: str = BASE,
                              channel: str = "zai") -> dict:
    """带着真人点出来的验证票据，向服务端申请授权链接。

    票据为空时官方客户端也会被回 631000（06:05 实测），所以这里允许空票——
    调用方需要的就是"服务端到底怎么说"这件事本身。
    """
    url_path = ZAI_OAUTH_URL_PATH if vendor == "zai" else GOOGLE_OAUTH_URL_PATH
    body = {"source_id": "autoclaw", "device_id": device_id,
            "navigate_uri": navigate_uri or (ZAI_CALLBACK_URI if vendor == "zai"
                                             else GOOGLE_CALLBACK_URI)}
    if ticket:
        body["ali_captcha_verify_param"] = str(ticket)
    st, d = _api_request("POST", url_path, body, channel=channel, base=base, timeout=20)
    out = {"ok": False, "http": st, "server_code": None, "msg": "", "url": "",
           "raw_keys": [], "base": base, "navigate_uri": body["navigate_uri"]}
    if isinstance(d, dict):
        out["server_code"] = d.get("code")
        out["msg"] = str(d.get("msg") or "")[:200]
        out["raw_keys"] = sorted(str(k) for k in d)[:10]
        u, why = _extract_oauth_url(d)
        out["url"], out["msg"] = u, (out["msg"] or why)
        out["ok"] = bool(u)
    else:
        out["msg"] = str(d)[:200]
    return out


_CAPTCHA_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%(title)s</title>
<style>
 body{font:14px/1.9 "Microsoft YaHei",sans-serif;background:#101014;color:#eaeaea;
      margin:0;padding:26px 30px}
 h2{font-size:17px;margin:0 0 10px}
 #status{margin:16px 0;padding:12px 14px;background:#1b1b22;border-left:3px solid #6b8afd;
         border-radius:4px;min-height:1.4em;word-break:break-all}
 button.go{font:inherit;padding:10px 22px;border:0;border-radius:6px;background:#3d5afe;
           color:#fff;cursor:pointer}
 small{color:#8f8f9a;display:block;margin-top:18px}
</style></head>
<body>
<h2>%(title)s</h2>
<p>%(lead)s</p>
<div id="status">正在加载官方验证控件…</div>
<button id="aliyun-captcha-trigger" class="go" type="button">开始人机验证</button>
<div id="aliyun-captcha-element"></div>
<small>这一步由阿里云官方验证控件完成：必须由你本人拖动，A-SWITCH 不代点、也不绕过它。
点开后 A-SWITCH 只做一件事——把你通过后拿到的票据原样交给 AutoClaw 服务端，
并把服务端的答复如实显示在这里。</small>
<script>
window.AliyunCaptchaConfig = {region: %(region)s, prefix: %(prefix)s};
</script>
<script src="%(script)s" onerror="document.getElementById('status').textContent=
  '验证控件脚本加载失败（网络被断/域名被拦），请检查网络后重试'"></script>
<script>
var statusEl = document.getElementById('status');
function setStatus(t){
  statusEl.textContent = t;
  // 把页面状态回传给本机：超时到底是"没人拖"还是"脚本没加载起来、根本没东西可拖"，
  // 只看窗口是分辨不出来的，下一次实验必须能自己说清。
  try { window.pywebview.api.captcha_note(t); } catch (e) {}
}
function ready(){
  if (typeof initAliyunCaptcha !== 'function') {
    setStatus('验证控件未就绪：官方脚本没给出 initAliyunCaptcha'); return;
  }
  initAliyunCaptcha({
    SceneId: %(scene)s, mode: 'popup',
    element: '#aliyun-captcha-element', button: '#aliyun-captcha-trigger',
    slideStyle: {width: 360, height: 40}, language: 'cn',
    getInstance: function (inst) { window.__aswCap = inst; setStatus('验证控件已就绪，请点「开始人机验证」'); },
    onError: function (e) { setStatus('验证控件出错：' + ((e && (e.msg || e.code)) || e)); },
    onBizResultCallback: function () {},
    captchaVerifyCallback: function (param) { return handOff(param); }
  });
}
async function handOff(param){
  if (typeof param !== 'string' || !param) { setStatus('验证控件回传为空'); 
                                             return {captchaResult: false, bizResult: false}; }
  setStatus('已拿到票据，正在交给 AutoClaw 服务端…');
  try {
    var r = await window.pywebview.api.oauth_ticket(param);
    if (r && r.ok) { setStatus('服务端已放行，正在打开授权页…'); 
                     return {captchaResult: true, bizResult: true}; }
    setStatus('服务端没接受这次验证：' + ((r && (r.msg || r.reason)) || '未知原因'));
    return {captchaResult: true, bizResult: false};
  } catch (e) {
    setStatus('本机回传失败：' + e);
    return {captchaResult: false, bizResult: false};
  }
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', function(){ setTimeout(ready, 300); });
} else { setTimeout(ready, 300); }
</script>
</body></html>
"""


def render_captcha_page(cfg: dict, title: str = "A-SWITCH 授权验证",
                        lead: str = "") -> str:
    """生成挂官方阿里云验证控件的本地页面（region/prefix 先挂全局、再加载脚本，顺序照官方）。"""
    return _CAPTCHA_PAGE % {
        "title": _html_escape(title),
        "lead": _html_escape(lead or "AutoClaw 的海外授权链接要求先过一次人机验证。"),
        "region": json.dumps(str(cfg.get("region") or "ga")),
        "prefix": json.dumps(str(cfg.get("prefix") or "")),
        "scene": json.dumps(str(cfg.get("scene_id") or "")),
        "script": _html_escape(ALIYUN_CAPTCHA_SCRIPT_URL),
    }


def _html_escape(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))



class _ExclusiveHTTPServer(ThreadingHTTPServer):
    """Windows 下带 SO_REUSEADDR 会让"端口已被占用"这件事静默通过，
    回跳被别的进程（比如正在跑的 AutoClaw 桌面端）接走，我们只能干等到超时。
    """
    allow_reuse_address = False


class OAuthCallbackCatch:
    """在 127.0.0.1:8989 上接住授权页回跳里的 code/state。"""

    def __init__(self):
        self.got = threading.Event()
        self.code = ""
        self.state = ""
        self.err = ""
        self.httpd = None
        self.th = None

    @staticmethod
    def _port_busy() -> bool:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", OAUTH_CALLBACK_PORT), 0.6):
                return True
        except OSError:
            return False

    def start(self) -> str:
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                outer.code = (q.get("code") or [""])[0]
                outer.state = (q.get("state") or [""])[0]
                outer.err = (q.get("error") or [""])[0]
                page = (
                    "<html><head><meta charset='utf-8'><title>A-SWITCH</title></head>"
                    "<body style='font:16px/1.8 sans-serif;background:#0a0a0c;color:#e8e8ea;"
                    "padding:48px'>"
                    + ("<b>授权成功</b>，可以关掉这个页面回到 A-SWITCH。" if outer.code
                       else f"<b>授权没完成</b>：{outer.err or '回跳里没有 code'}")
                    + "</body></html>").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                try:
                    self.wfile.write(page)
                except Exception:
                    pass
                outer.got.set()

        if self._port_busy():
            return (f"接不住授权回跳：本机 {OAUTH_CALLBACK_PORT} 端口上已经有服务在听"
                    "（通常是 AutoClaw 桌面端正在用同一个回调端口），请先完全退出桌面端再试")
        try:
            self.httpd = _ExclusiveHTTPServer(("127.0.0.1", OAUTH_CALLBACK_PORT), H)
        except OSError as e:
            return (f"接不住授权回跳：本机 {OAUTH_CALLBACK_PORT} 端口不可用"
                    f"（{e}；通常是 AutoClaw 桌面端正在用同一个回调端口，请先退出桌面端）")
        self.th = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()
        return ""

    def wait(self, seconds):
        return self.got.wait(seconds)

    def close(self):
        for fn in (getattr(self.httpd, "shutdown", None),
                   getattr(self.httpd, "server_close", None)):
            try:
                if fn:
                    fn()
            except Exception:
                pass


def _extract_oauth_url(resp) -> tuple:
    """从 zai-oauth-url / google-oauth-url 响应里取授权链接，返回 (url, 错误)。"""
    data = resp.get("data") if isinstance(resp, dict) else None
    cands = [data] if isinstance(data, str) else (
        list(data.values()) if isinstance(data, dict) else [])
    for v in cands:
        if isinstance(v, str) and v.startswith("http"):
            return v, ""
    if isinstance(data, dict):
        for k in ("url", "oauth_url", "authorize_url", "auth_url", "login_url", "redirect_url"):
            v = data.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v, ""
        return "", ("授权链接字段名变了，请把响应键名报给开发者："
                    + ",".join(sorted(str(k) for k in data)[:10]))
    return "", "服务端没返回授权链接"


def oauth_add(invite_code: str = "", vendor: str = "zai", on_progress=None,
              open_browser: bool = True, timeout: int = OAUTH_WAIT_SECONDS,
              channel: str = "zai", on_url=None) -> dict:
    """用 Z.ai（或 Google）账号登录/注册 AutoClaw：不开桌面端、不发短信。

    与短信通道共用同一套入档逻辑，所以新人身份、独立 deviceId、回读校验、
    失败回滚这些保证完全一致；差别只在"凭证是怎么拿到的"。
    """
    say = on_progress or (lambda m: None)
    if vendor not in ("zai", "google"):
        return {"ok": False, "error": "vendor 只支持 zai / google"}
    url_path = ZAI_OAUTH_URL_PATH if vendor == "zai" else GOOGLE_OAUTH_URL_PATH
    login_path = ZAI_OAUTH_LOGIN_PATH if vendor == "zai" else GOOGLE_OAUTH_LOGIN_PATH
    pend = ACCOUNTS_DIR.parent / "_pending" / f"oauth-{vendor}-{int(time.time())}"
    pend.mkdir(parents=True, exist_ok=True)
    _prepare_isolated_identity(pend)
    device_id = json.loads((pend / "identity" / "device.json")
                           .read_text(encoding="utf-8"))["deviceId"]
    catch = OAuthCallbackCatch()
    err = catch.start()
    if err:
        shutil.rmtree(pend, ignore_errors=True)
        return {"ok": False, "error": err}
    try:
        say(f"向服务端申请 {'Z.ai' if vendor == 'zai' else 'Google'} 授权链接…")
        st, d = _api_request("POST", url_path,
                             {"source_id": "autoclaw", "device_id": device_id,
                              "navigate_uri": OAUTH_REDIRECT_URI}, channel=channel)
        auth_url, why = _extract_oauth_url(d)
        if not auth_url:
            blob = str(d.get("msg") or "")
            if "405" in blob or "<!doctype" in blob.lower() or "punish" in blob.lower():
                why = ("服务端边缘防护拒绝了这个路径（HTTP 405 拦截页）："
                       "Z.ai/Google 授权的人机验证只在官方登录窗口里可用，"
                       "请改用「桌面端登录添加」，在那个窗口里点 Z.ai/Google")
            elif d.get("code") == 631000:
                why = ("服务端返回 631000（登录失败）：该接口要求官方客户端带验证码票据，"
                       "免桌面端这条路走不通，请改用「桌面端登录添加」")
            return {"ok": False, "error": why or f"HTTP {st}"}
        if on_url:
            try:
                on_url(auth_url)
            except Exception:
                pass
        if open_browser:
            try:
                webbrowser.open(auth_url)
                say("已在默认浏览器打开授权页，请在页面里完成授权…")
            except Exception as e:
                say(f"浏览器没能自动打开（{type(e).__name__}），请点界面上的授权链接手动打开")
        else:
            say("请把授权链接发到浏览器里打开，然后完成授权…")
        if not catch.wait(max(30, int(timeout))):
            return {"ok": False, "url": auth_url,
                    "error": f"等不到授权回跳（{timeout} 秒内没有请求打到 "
                             f"127.0.0.1:{OAUTH_CALLBACK_PORT}）"}
        if not catch.code:
            return {"ok": False, "url": auth_url,
                    "error": f"授权没通过：{catch.err or '回跳里没有 code'}"}
        say("已拿到 code，正在换登录凭证…")
        st2, d2 = _api_request("POST", login_path,
                               {"source_id": "autoclaw", "device_id": device_id,
                                "code": catch.code, "state": catch.state,
                                "navigate_uri": OAUTH_REDIRECT_URI}, channel=channel)
        data = d2.get("data") if isinstance(d2, dict) else None
        if not (st2 == 200 and isinstance(data, dict) and d2.get("code") == 0):
            return {"ok": False, "url": auth_url, "code": d2.get("code"),
                    "error": d2.get("msg") or f"HTTP {st2}"}
        token = data.get("access_token") or ""
        refresh = data.get("refresh_token") or ""
        if not token or not refresh:
            return {"ok": False, "url": auth_url,
                    "error": "登录响应缺 access/refresh token，未入档"}
        out = _archive_login_result(token, refresh, device_id, channel, data, say,
                                    login_via=f"oauth-{vendor}", pend_dir=pend,
                                    invite_code=invite_code)
        out["url"] = auth_url
        return out
    finally:
        catch.close()
        shutil.rmtree(pend, ignore_errors=True)


def oauth_add_inapp(invite_code: str = "", vendor: str = "zai", channel: str = "zai",
                    on_progress=None, authorize=None, force: bool = False) -> dict:
    """免桌面端、**零端口**地加号：授权环节由 GUI 提供的窗口完成。

    与 oauth_add 的区别就是"code 是怎么拿到的"：
      oauth_add       —— 在 8989 上监听回跳（要抢端口，桌面端在跑就抢不到）
      oauth_add_inapp —— 授权页开在我们自己的 WebView 里，在 NavigationStarting
                         那一跳上截下回跳 URL 读 code 并取消导航。不监听任何端口，
                         也不需要用户退出 AutoClaw 桌面端。
    拿到 code 之后（换凭证、入档、绑定、取证、回滚）与 oauth_add 完全同一套。

    authorize(vendor=..., device_id=..., channel=..., say=...) ->
        {'ok': True, 'code': str, 'state': str, 'url': str}
        {'ok': False, 'error': str, 'url': str}
    """
    say = on_progress or (lambda m: None)
    if vendor not in ("zai", "google"):
        return {"ok": False, "error": "vendor 只支持 zai / google"}
    if not callable(authorize):
        return {"ok": False, "error": "没有可用的授权窗口（GUI 未提供 authorize 回调）"}
    if not force:
        # 定论之后不许再消耗一次真人验证；要复验请显式 force（探针脚本走这条）。
        return {"ok": False, "error": INAPP_OAUTH_VERDICT, "verdict": True}
    login_path = ZAI_OAUTH_LOGIN_PATH if vendor == "zai" else GOOGLE_OAUTH_LOGIN_PATH
    callback_uri = ZAI_CALLBACK_URI if vendor == "zai" else GOOGLE_CALLBACK_URI
    pend = ACCOUNTS_DIR.parent / "_pending" / f"inapp-{vendor}-{int(time.time())}"
    pend.mkdir(parents=True, exist_ok=True)
    _prepare_isolated_identity(pend)
    device_id = json.loads((pend / "identity" / "device.json")
                           .read_text(encoding="utf-8"))["deviceId"]
    try:
        say("请在弹出的窗口里完成人机验证与授权…")
        try:
            got = authorize(vendor=vendor, device_id=device_id, channel=channel, say=say) or {}
        except Exception as e:
            return {"ok": False, "error": f"授权窗口异常：{type(e).__name__}: {e}"}
        url = str(got.get("url") or "")
        if not got.get("ok") or not got.get("code"):
            return {"ok": False, "url": url,
                    "error": str(got.get("error") or "授权没完成（窗口里没有拿到 code）")}
        say("已拿到 code，正在换登录凭证…")
        st, d = _api_request("POST", login_path,
                             {"source_id": "autoclaw", "device_id": device_id,
                              "code": str(got["code"]), "state": str(got.get("state") or ""),
                              "navigate_uri": callback_uri}, channel=channel)
        data = d.get("data") if isinstance(d, dict) else None
        if not (st == 200 and isinstance(data, dict) and d.get("code") == 0):
            return {"ok": False, "url": url, "code": d.get("code") if isinstance(d, dict) else None,
                    "error": (d.get("msg") if isinstance(d, dict) else str(d)) or f"HTTP {st}"}
        token = data.get("access_token") or ""
        refresh = data.get("refresh_token") or ""
        if not token or not refresh:
            return {"ok": False, "url": url,
                    "error": "登录响应缺 access/refresh token，未入档"}
        out = _archive_login_result(token, refresh, device_id, channel, data, say,
                                    login_via=f"inapp-{vendor}", pend_dir=pend,
                                    invite_code=invite_code)
        out["url"] = url
        return out
    finally:
        shutil.rmtree(pend, ignore_errors=True)


def _launch_isolated_profile(profile_dir: Path) -> set:
    """用 cmd /c start 启动隔离 profile 的 AutoClaw，返回新出现的进程 PID 集合。

    必须经 cmd start：AutoClaw.exe 带提升清单，直接 CreateProcess 会
    WinError 740（请求的操作需要提升）；让 shell 去创建可绕过。
    cmd start 破坏了父子关系拿不到 PID，且提权进程的命令行对非管理员
    不可见——因此用"启动前后 PID 差集"来识别本次实例的整组进程。
    """
    import subprocess
    exe = find_autoclaw_exe()
    if not exe:
        return set()
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    # 预置独立 Ed25519 身份（仅当目录里还没有时）：
    # 登录添加 = 全新身份；活动打卡 = 保留账号自己的身份（调用方已拷入）
    if not (profile_dir / "identity" / "device.json").is_file():
        _prepare_isolated_identity(profile_dir)
    # AutoClaw 的 gateway/openclaw 状态取自 os.homedir()，不随 userData 自动隔离。
    # 给子进程一个独立 home，并禁用系统 Credential Manager 回退，避免再次污染主账号。
    isolated_home = profile_dir / "_home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    # 字符串形式传命令，避免 list2cmdline 把整段再包一层引号
    cmd = f'cmd /c start "" "{exe}" "--user-data-dir={profile_dir}"'
    # AutoClaw 自身在 app ready 前会优先读取这个变量并 setPath("userData")。
    # 同时传命令行参数，确保 Electron 与应用层看到的是同一个临时目录；
    # 只依赖其中一层时，升级后的客户端可能把 auth.json 落回主 profile。
    env = os.environ.copy()
    env["ELECTRON_USER_DATA_DIR"] = str(profile_dir)
    env["AUTOCLAW_E2E_USER_DATA_DIR"] = str(profile_dir)
    env["AUTOCLAW_DEVICE_IDENTITY_CREDENTIAL_STORE"] = "disabled"
    env["OPENCLAW_STATE_DIR"] = str(isolated_home / ".openclaw-autoclaw")
    env["USERPROFILE"] = str(isolated_home)
    env["HOME"] = str(isolated_home)
    # 2.x 客户端 pinUserDataDirectory() 在 single-instance lock 前调
    # app.getPath("appData")（Windows 上直接读 APPDATA 环境变量）；
    # 只改 USERPROFILE/HOME 会让 APPDATA 与 USERPROFILE 指向分裂，
    # Electron 早期启动即抛 "Failed to get 'appData' path"。三件套一起重定向：
    iso_appdata = isolated_home / "AppData" / "Roaming"
    iso_local = isolated_home / "AppData" / "Local"
    iso_appdata.mkdir(parents=True, exist_ok=True)
    iso_local.mkdir(parents=True, exist_ok=True)
    env["APPDATA"] = str(iso_appdata)
    env["LOCALAPPDATA"] = str(iso_local)
    before = _autoclaw_pids()
    try:
        subprocess.Popen(cmd, env=env,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         close_fds=True)
    except Exception as e:
        log(f"启动隔离实例失败：{type(e).__name__}: {e}")
        return set()

    deadline = time.time() + 40
    while time.time() < deadline:
        new = _autoclaw_pids() - before
        if new:
            _ISOLATED_PIDS.update(new)
            return new
        time.sleep(0.6)
    # 40s 内没等到 PID 差集 ≠ 实例没起来（冷启动/杀软延迟可能超过 deadline）。
    # 按临时 profile 特征兜底扫一次：只要有用这个 profile 目录的实例在跑，
    # 一样登记为隔离实例，否则调用方按"启动失败"跳过 → 进程泄漏成多余窗口
    #（2026-10-07 实测泄漏：两个 sweep 实例挂了 8 小时才被用户发现）。
    leaked = _pids_with_userdata(profile_dir)
    if leaked:
        _ISOLATED_PIDS.update(leaked)
        log(f"启动确认超时，但发现 {len(leaked)} 个持有该 profile 的实例，已登记隔离")
        return leaked
    return set()


def _pids_with_userdata(profile_dir: Path) -> set:
    """按 --user-data-dir=<profile_dir> 特征找实例 PID（兜底泄漏清理用）。

    ⚠ 提权进程的 CommandLine 对非管理员不可见（空串），此时返回空集——
    兜底只在能看见命令行的场景生效；打包版 A-SWITCH 以管理员运行，可见。
    """
    import subprocess
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name LIKE 'AutoClaw%'\" "
             "| Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        import json as _json
        data = _json.loads((out or b"{}").decode("utf-8", "replace") or "{}")
        items = data.get("ProcessId") and [data] or data if isinstance(data, list) else [data] if data else []
        needle = str(profile_dir).lower()
        hits = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            cl = str(it.get("CommandLine") or "")
            if needle in cl.lower():
                try:
                    hits.add(int(it.get("ProcessId")))
                except (TypeError, ValueError):
                    pass
        return hits
    except Exception:
        return set()


def _kill_isolated(pids=None, wait: int = 12) -> None:
    """只结束本工具启动的隔离登录实例进程树，绝不碰用户其它 AutoClaw。

    pids 缺省时用 _ISOLATED_PIDS 记录（隔离实例以管理员运行，普通权限
    杀不掉时会走 UAC 提权重试；打包版以管理员运行则无感）。
    """
    pids = set(pids if pids is not None else _ISOLATED_PIDS) & _autoclaw_pids()
    if not pids:
        _ISOLATED_PIDS.clear()
        return
    # 只杀根（父进程不在我们集合里的），/T 连子进程整棵带走
    ppids = _autoclaw_ppids()
    roots = [p for p in pids if ppids.get(p) not in pids] or list(pids)
    _kill_pid_tree(roots)
    deadline = time.time() + wait
    while time.time() < deadline:
        if not (pids & _autoclaw_pids()):
            break
        time.sleep(0.5)
    _ISOLATED_PIDS.difference_update(pids)
    log(f"已关闭临时登录实例（{len(pids)} 个进程）")


def _inflight_beat(profile_dir) -> None:
    """往本次临时 profile 里打一个在途心跳，供换包脚本判断"有没有操作正在跑"。

    不能靠"有没有 AutoClaw 进程"判断：用户常驻的主实例一直在跑，那样永远换不了包；
    也不能靠"有没有 aswitch-login-* 目录"判断：被强杀留下的孤儿目录会永久卡死换包。
    AutoClaw.exe 带提升清单，非管理员看不到它的命令行，所以只能自己留心跳 ——
    进程一死，心跳自然过期。
    """
    try:
        (Path(profile_dir) / ".inflight").write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass


def _login_candidates(profile_dir: Path) -> list[Path]:
    """隔离登录可能把凭证落到哪些目录（按优先级）。

    为什么不只看 --user-data-dir 那份：AutoClaw 的网关状态目录不随 userData 走，
    实测（09-23 第三次加号）客户端在网页登录完成后**自己重启并丢了 --user-data-dir**，
    于是 auth.json 落到主 profile；只轮询 tmp 的那份就永远等不到 → 超时 → finally 把
    tmp 连凭证一起删掉，登录成功却被销毁。所以三处都要盯：
      1) tmp 本身（隔离成功，最常见）
      2) 隔离 home 下的 .openclaw-autoclaw（OPENCLAW_STATE_DIR 生效时）
      3) 主 profile（隔离逃逸时的落点，必须抢救，否则被回滚销毁）
    """
    profile_dir = Path(profile_dir)
    out = [profile_dir, profile_dir / "_home" / ".openclaw-autoclaw",
           Path(DEFAULT_STATE_DIR)]
    seen, uniq = set(), []
    for p in out:
        try:
            k = str(p.resolve())
        except Exception:
            k = str(p)
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def _main_identity(snap: dict) -> tuple:
    """快照里主账号的 (uid, jti)，用于判断"某份 token 是不是新号"。"""
    return (snap.get("uid"), snap.get("jti"))


def _token_identity(appdata_dir: Path) -> tuple | None:
    """就地解密某目录的 auth.json，返回 (uid, jti)；读不出返回 None。

    uid 一律以 **userInfo.user_id**（32 位 hex，= acc.user_id，也是 accounts/ 目录名、
    快照 uid 的口径）为准 —— JWT payload 里的 user_id 是另一套数字 ID（如 151132），
    两者不同源，混用会让"主号刷新"比对不上被误判成新登录。JWT 的数字 ID 只在 userInfo
    还没落盘时兜底，且比对主要靠 jti（邮箱，两边一致）。
    """
    try:
        acc = load_account(appdata_dir)
        if not acc:
            return None
        payload = _jwt_payload(acc.token)
        ui = (acc.auth_json or {}).get("userInfo") or {}
        uid = str(acc.user_id or ui.get("user_id") or payload.get("user_id") or "")
        jti = payload.get("jti") or payload.get("email") or ui.get("user_name")
        return (uid or None, jti or None)
    except Exception:
        return None


# 「桌面端登录添加」的取消开关：GUI 的「取消」按钮 set 它，_wait_for_login 每圈检查。
# 用 Event 而不是布尔：跨线程可见 + 无竞态；login_and_add_account 进入时 clear 复位。
LOGIN_CANCEL = threading.Event()


def _wait_for_login(profile_dir: Path, timeout: int = 300, on_progress=None,
                    main_snap: dict | None = None,
                    honor_cancel: bool = True) -> dict | None:
    """轮询**所有候选落点**，直到出现"非空且不是主账号"的 token。

    返回 {'auth_json', 'token', 'source_dir'}；source_dir 就是入库时要拷贝文件的来源目录。
    主 profile 那份必须比对身份：桌面端自己会定时刷新 token（还是某号），
    不比对就会把"主号刷新"误判成"新号登录成功"，把老号当新号入库。
    """
    profile_dir = Path(profile_dir)
    cands = _login_candidates(profile_dir)
    main_uid, main_jti = _main_identity(main_snap or {})
    main_auth = Path(DEFAULT_STATE_DIR) / "auth.json"
    flow_start = time.time()
    handover_reported = False
    deadline = time.time() + timeout
    last_report = time.time()
    cancelled = False
    while time.time() < deadline:
        if honor_cancel and LOGIN_CANCEL.is_set():
            cancelled = True
            break
        _inflight_beat(profile_dir)      # 心跳：标记本登录目录"进行中"，防孤儿清理误删
        # 实时监听兑换失败（630014 审核拒绝 / 631001 域名风控 / ...）：后端拒绝后 token
        # 永远不会落盘，继续等只是白等 300 秒——日志里出现 failed 立即退出，报错里带真因。
        _authlog = profile_dir / "_home" / ".openclaw-autoclaw" / "logs" / "autoclaw-auth.log"
        if _authlog.is_file():
            try:
                _txt = _authlog.read_text(encoding="utf-8", errors="replace")
                if "oauth-login-zai.failed" in _txt or "oauth-login.failed" in _txt:
                    cancelled = True
                    break
            except Exception:
                pass
        for d in cands:
            auth_path = d / "auth.json"
            if not auth_path.is_file():
                continue
            try:
                dd = json.loads(auth_path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            tok = dd.get("token") or ""
            if not (isinstance(tok, str) and tok.strip()):
                continue
            ident = _token_identity(d)
            got_uid, got_jti = ident or (None, None)
            # 隔离目录里出现的就是本次登录的号（主号不会往 tmp 写）；
            # 主 profile 那份必须"身份与快照不同"才算新号。
            if str(d.resolve()) == str(Path(DEFAULT_STATE_DIR).resolve()):
                if main_uid and got_uid and str(got_uid) == str(main_uid):
                    continue
                if main_jti and got_jti and str(got_jti) == str(main_jti):
                    continue
            return {"auth_json": dd, "token": tok, "source_dir": d,
                    "escaped_isolation": str(d.resolve()) != str(profile_dir.resolve())}
        now = time.time()
        if on_progress and now - last_report >= 15:
            on_progress(f"等待登录中…（请在登录窗口完成登录，剩余 {int(deadline - now)}s）")
            last_report = now
        time.sleep(1.5)
    if cancelled:
        if on_progress:
            on_progress("已取消：正在关闭登录窗口并重启桌面端…")
        return None
    return None


def _wait_for_userinfo(profile_dir: Path, timeout: int = 10,
                       source_dir: Path | None = None) -> dict:
    """token 出现后，再给 userInfo 一点落盘时间（决定入库目录名和昵称）。

    必须盯 source_dir：隔离逃逸时 userInfo 落在主 profile，只读 tmp 会读到空壳，
    于是 uid 解析失败 → 报"未能解析出账号 ID"。
    """
    dirs = [Path(source_dir)] if source_dir else []
    dirs += _login_candidates(profile_dir)
    auth_path = profile_dir / "auth.json"
    deadline = time.time() + timeout
    best = {}
    while time.time() < deadline:
        for d in dirs:
            p = Path(d) / "auth.json"
            try:
                dd = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if (dd.get("userInfo") or {}).get("user_id"):
                return dd
            if dd.get("userInfo") and not best:
                best = dd
        time.sleep(1)
    return best


def _snapshot_main_session() -> dict:
    """记录主 profile 的登录身份，用于登录流程结束后检测"主账号是否被顶掉"。

    AutoClaw 的网关状态（~/.openclaw-autoclaw）不随 --user-data-dir 隔离，
    隔离实例登录时可能把主 profile 的 auth.json 一起换掉（实测发生过），
    所以必须留底，事后能判断并回滚。
    """
    snap = {"uid": None, "jti": None, "auth": None, "local_state": None}
    try:
        auth = DEFAULT_STATE_DIR / "auth.json"
        if auth.is_file():
            snap["auth"] = auth.read_bytes()
            try:
                raw_obj = json.loads(snap["auth"].decode("utf-8", "replace"))
                snap["uid"] = (raw_obj.get("userInfo") or {}).get("user_id")
                m = re.search(r'"token":\s*"enc:([^"]+)"', snap["auth"].decode("utf-8", "replace"))
                if m:
                    token = decrypt_chromium_value("enc:" + m.group(1), DEFAULT_STATE_DIR)
                    payload = _jwt_payload(token)
                    snap["jti"] = payload.get("jti") or payload.get("email")
            except Exception:
                snap["uid"] = None
        ls = DEFAULT_STATE_DIR / "Local State"
        if ls.is_file():
            snap["local_state"] = ls.read_bytes()
    except Exception:
        pass
    return snap


def _restore_main_session_if_switched(snap: dict) -> bool:
    """若登录流程把主账号换掉了，回滚主 profile 的 auth.json + Local State。

    只认账号级变化（userInfo.user_id 不同）；单纯的 token 自动刷新不动。
    """
    if not snap.get("auth"):
        return False
    try:
        auth = DEFAULT_STATE_DIR / "auth.json"
        cur_uid = None
        cur_jti = None
        if auth.is_file():
            try:
                raw_text = auth.read_text(encoding="utf-8")
                cur_uid = (json.loads(raw_text).get("userInfo") or {}).get("user_id")
                m = re.search(r'"token":\s*"enc:([^"]+)"', raw_text)
                if m:
                    token = decrypt_chromium_value("enc:" + m.group(1), DEFAULT_STATE_DIR)
                    payload = _jwt_payload(token)
                    cur_jti = payload.get("jti") or payload.get("email")
            except Exception:
                cur_uid = None
        switched = bool(snap.get("jti") and cur_jti and cur_jti != snap["jti"])
        if not switched and snap.get("uid") and cur_uid and cur_uid != snap["uid"]:
            switched = True
        if switched:
            auth.write_bytes(snap["auth"])
            if snap.get("local_state"):
                (DEFAULT_STATE_DIR / "Local State").write_bytes(snap["local_state"])
            log(f"⚠ 检测到主账号被登录流程顶掉（{snap.get('jti') or snap.get('uid')} -> {cur_jti or cur_uid}），已自动回滚原登录")
            return True
    except Exception as e:
        log(f"回滚主账号失败：{type(e).__name__}: {e}")
    return False


TOKEN_SERVER_PORTS = (18432, 19654, 19723, 53699)   # 官方 TokenServer 的固定端口集


def token_ports_in_use(ports=TOKEN_SERVER_PORTS) -> list:
    """返回当前正在监听的官方回调端口。

    AutoClaw 一个实例会把这四个端口**全部**占住，而账号级 Z.ai/Google 回跳是
    http://localhost:{primaryPort}/auth/callback-zai。只要主实例还活着，
    隔离实例就一个端口都抢不到 → 浏览器里的登录回调必然落进主账号。
    """
    busy = []
    for p in ports:
        try:
            with socket.create_connection(("127.0.0.1", p), 0.4):
                busy.append(p)
        except Exception:
            continue
    return busy


RELAY_HEALTH_URL = "http://127.0.0.1:18766/health"   # 本机反代（server.mjs）自报状态


def wait_token_ports_free(timeout: float = 25.0) -> tuple:
    """等官方回调端口全部释放。

    进程退出与内核回收 listen socket 之间有一点延迟，所以"杀完立刻抢"会假失败。
    """
    deadline = time.time() + timeout
    busy = token_ports_in_use()
    while busy and time.time() < deadline:
        time.sleep(0.5)
        busy = token_ports_in_use()
    return (not busy), busy


def relay_upstream_wait(timeout: float = 120.0) -> dict:
    """等反代的上游回来：桌面端进程里的 Model Broker，或者（桌面端不在时）云端直连。

    反代自己活着不等于有算力：server.mjs 只有在拿得到上游时才报 status="ok"，
    所以恢复判据只看它，不看进程是否存在。
    """
    import urllib.request
    t0 = time.time()
    deadline = t0 + timeout
    last: dict = {}
    while True:
        try:
            with urllib.request.urlopen(RELAY_HEALTH_URL, timeout=5) as r:
                last = json.loads(r.read().decode("utf-8", "replace"))
            if last.get("status") == "ok":
                return {"ok": True, "upstream": str(last.get("upstream") or "broker"),
                        "broker": str(last.get("broker") or ""),
                        "models": len(last.get("models") or []),
                        "waited_s": round(time.time() - t0, 1)}
        except Exception as e:
            last = {"error": f"{type(e).__name__}: {e}"}
        if time.time() >= deadline:
            return {"ok": False, "broker": "", "models": 0,
                    "waited_s": round(time.time() - t0, 1),
                    "last": {k: str(v)[:80] for k, v in list(last.items())[:4]}}
        time.sleep(2.0)


# ---------------- 反代的账号池（"用完一个号自动切下一个"的数据面） ----------------
# 为什么池子在 A-SWITCH 这边组、不在 server.mjs 里组：
# DPAPI 存档、X-Auth-Sign 签名、refreshToken 轮换全在这里，node 侧只该拿现成的 access token。
GATEWAY_STATE_DIR = Path(os.environ.get("OPENCLAW_STATE_DIR")
                         or (Path.home() / ".openclaw-autoclaw"))
CLOUD_POOL_FILE = "aswitch_cloud_pool.json"
POOL_BANLIST_FILE = "aswitch_pool_banned.json"


def _pool_banned_set() -> set:
    """池子黑名单：确认已封(410004)的 uid，导出时跳过，防止 GUI 每轮刷新把死号复活。
    文件不存在返回空集。每行/每项可以是完整 uid 或 uid 前 8 位。"""
    p = GATEWAY_STATE_DIR / POOL_BANLIST_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    out = set()
    for x in (data.get("banned") or []):
        s = str(x).strip()
        if s:
            out.add(s)
    return out


# 暖号闸门：新号导入后必须"暖号"（破纯机器画像）+ 过隔离期，才允许进池。
# 否则 keeper/GUI 一导入就把号导出 → 被探针抢先烧成纯机器号 → 当天封（某号 教训）。
# 状态文件放在每个账号档案目录：accounts/<uid>/warm_state.json = {"warmed": bool, ...}
WARM_STATE_FILE = "warm_state.json"
# 导入后多少秒内视为"新号、仍需暖号"（隔离窗）。0 = 仅靠 warmed 标记（不限时）。
WARM_GRACE_S = int(os.environ.get("ASWITCH_WARM_GRACE_S", "0"))


def _account_warm_state(acc) -> dict:
    """读账号档案的暖号状态；文件缺失/损坏 → 视为未暖（保守：挡在池外）。"""
    try:
        p = Path(getattr(acc, "appdata_dir", "") or "") / WARM_STATE_FILE
        if not p.is_file():
            return {"warmed": False, "warmed_at": 0}
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"warmed": False, "warmed_at": 0}


def _account_warmed(acc) -> bool:
    st = _account_warm_state(acc)
    if st.get("warmed") is True:
        return True
    # 无 warm_state.json = 改造前已存在的存量活号（新号入库时会被 login_and_add_account
    # 显式写 warmed=false）。存量号无文件即视为已暖、直接放行，避免把唯一活号挡在池外。
    # 一次性迁移见 pool_keeper 首次运行批量补标 warmed=true。
    p = Path(getattr(acc, "appdata_dir", "") or "") / WARM_STATE_FILE
    if not p.is_file():
        return True
    return False


def account_mark_warmed(acc, human_shape_ratio: float = 0.0) -> None:
    """暖号达标后调用：写 warmed=true。"""
    try:
        p = Path(getattr(acc, "appdata_dir", "") or "") / WARM_STATE_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"warmed": True, "warmed_at": int(time.time()),
                "human_shape_ratio": human_shape_ratio}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:
        log(f"写暖号状态失败 {acc.nickname if hasattr(acc, 'nickname') else ''}: {e}")


def real_inference_count(acc, pages: int = 3) -> int:
    """该号真实推理调用条数（服务端流水，不可被本地标记改写）。

    判"是不是存量活号"用服务端事实而非本地 warm_state.json：新导入的号推理流水为 0，
    哪怕本地被误标 warmed=true 也能识别出来。拉取失败返回 -1（调用方按未知处理）。
    """
    try:
        n = 0
        for page in range(1, pages + 1):
            st, d = _api_request("GET", f"/agent-assetmgr/api/v1/ledgers_std?page={page}&page_size=100",
                                 token=acc.token, channel=getattr(acc, "channel", "zai"))
            if st != 200 or not isinstance(d, dict):
                return -1 if page == 1 else n
            es = (d.get("data") or {}).get("entries") or []
            for e in es:
                if "model usage" in str(e.get("description") or "").lower():
                    n += 1
            if len(es) < 100:
                break
        return n
    except Exception:
        return -1


def pool_needs_probe(acc) -> bool:
    """该号是否属于"待放行探测"对象：黑名单外、且服务端零真实推理流水。

    用流水而非本地 warmed 标记做判据 —— 标记可能被 GUI/迁移逻辑改写，流水不会。
    """
    if str(acc.user_id) in _pool_banned_set():
        return False
    return real_inference_count(acc) == 0


def pool_ban_add(uid: str) -> dict:
    """把一个 uid 加入池子黑名单（幂等）。"""
    p = GATEWAY_STATE_DIR / POOL_BANLIST_FILE
    uid = str(uid).strip()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"banned": []}
    cur = data.get("banned") or []
    if uid not in [str(x).strip() for x in cur]:
        cur.append(uid)
    data["banned"] = cur
    data["at"] = int(time.time())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return {"ok": True, "count": len(cur), "path": str(p)}



def export_cloud_pool(entries: list) -> dict:
    """把可用凭证写给反代，让它**按请求**选号：一个号的积分打空了就换下一个。

    entries: [{"account": Account, "points": int|None, "expiring": int|None}]
    余额只是排序用的提示，不是闸门 —— 查余额失败（points=None）的号照样进池，
    否则一次接口抖动就会把可用算力踢掉，那是帮倒忙。
    ⚠️ 明文 access token 落盘，和桌面端自己写的 request-headers.json 同级同风险，
    所以只写 ~/.openclaw-autoclaw，绝不写进仓库目录。
    """
    accs = []
    banned = _pool_banned_set()
    skipped_warm = []
    for e in entries:
        a = e.get("account")
        tok = getattr(a, "token", "") or ""
        if not a or not tok or getattr(a, "auth_expired", False):
            continue
        exp = a.access_expires_at()
        if exp and exp <= time.time():
            continue                      # 已经过期的 token 不塞给反代让它撞 401
        _uid = str(a.user_id or a.appdata_dir.name)
        if _uid in banned or _uid[:8] in banned:
            continue                      # 池子黑名单：已封号不再导出，防止 GUI 刷新复活
        # 暖号闸门：新号（无 warmed 标记）未暖号前挡在池外，防导入即导出被探针烧死。
        # 存量活号（档案早于 2026-09-23）无状态文件也放行，由一次性标 warmed=true 覆盖。
        if not _account_warmed(a):
            skipped_warm.append((_uid[:8], getattr(a, "nickname", "") or _uid[:8]))
            continue
        accs.append({
            "uid": str(a.user_id or a.appdata_dir.name),
            "name": a.nickname,
            "auth": tok if tok.lower().startswith("bearer ") else f"Bearer {tok}",
            # 2026-09-30 起 2.0.2 网关对 chat 端点校验 token 新鲜度（分钟级，尽管 JWT
            # 写 24h），relay 必须在每次模型请求前现刷一票；refresh_token+device_id
            # 是刷票的原料，与 access token 同级明文落盘（同 request-headers.json 风险）。
            "refresh_token": getattr(a, "refresh_token", "") or "",
            "device_id": getattr(a, "device_id", "") or "",
            "access_expires_at": exp,
            "points": e.get("points"),
            "expiring": e.get("expiring"),
            "is_live": bool(a.is_live),
        })
    out = {"version": 1, "at": int(time.time()),
           "client_version": detect_autoclaw_version(),
           "accounts": accs}
    path = GATEWAY_STATE_DIR / CLOUD_POOL_FILE
    if not accs:
        # 全被暖号闸门挡住也别覆盖上一次的池子（否则会把唯一活号也清掉）
        if skipped_warm and not banned:
            return {"ok": False, "wrote": 0, "path": str(path),
                    "error": f"全部号未暖号（{len(skipped_warm)} 个），池子不覆盖上一次", "skipped_warm": skipped_warm}
        return {"ok": False, "wrote": 0, "path": str(path),
                "error": "没有可用凭证（全部过期或需要重新登录），池子不覆盖上一次的"}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)             # 原子换：反代随时读到的都是完整文件
    except OSError as ex:
        return {"ok": False, "wrote": len(accs), "path": str(path),
                "error": f"{type(ex).__name__}: {ex}"}
    return {"ok": True, "wrote": len(accs), "path": str(path),
            "points": {a["name"]: a["points"] for a in accs},
            "skipped_warm": skipped_warm}


def _kill_stray_login_instances(known_before: set | None = None) -> None:
    """登录流程 finally 兜底清理：杀掉所有残留的 AutoClaw 登录实例（含野进程）。

    登录流程开始时主实例已被 kill_autoclaw 关掉，所以此刻跑着的 AutoClaw.exe 都是本次登录启动的。
    隔离实例若触发官方客户端的 self-relaunch（网页登录完成后 Electron 自己重启），relaunch 出来的
    野进程会丢掉 --user-data-dir 和注入的隔离 env，A-SWITCH 只认启动时记录的 PID（_ISOLATED_PIDS）
    因此杀不到它 → 它一直占着官方回调端口 → 下次加号被 token_ports_in_use() 挡成"AutoClaw 正在运行
    请先退出"，用户看到的就是"加号爆了、再点也开不了、还莫名其妙多一个窗口"。这里按 PID 差集把
    "本次登录前不存在的"进程整棵杀掉（_kill_pid_tree 幂等，重复杀无害）。
    """
    known_before = set(known_before or set())
    stray = _autoclaw_pids() - known_before
    if not stray:
        return
    ppids = _autoclaw_ppids()
    roots = [p for p in stray if ppids.get(p) not in stray] or list(stray)
    _kill_pid_tree(roots)
    log(f"已兜底清理残留登录实例（{len(stray)} 个进程），避免占住官方回调端口")


def _cleanup_orphan_login_dirs() -> None:
    """登录开始前清掉历史遗留的 aswitch-login-* 孤儿目录。

    这些目录来自"加号流程被强杀（GUI 关闭/崩溃）导致 finally 没跑"：里面只剩空壳
    （auth.json 无 token，因为登录根本没完成或凭证被 relaunch 野进程带走了），且可能仍有残留
    AutoClaw 进程占着端口。只删 auth.json 不含 token 的空壳，绝不动有效登录态（含 token 的）。
    """
    try:
        troot = Path(tempfile.gettempdir())
        for d in sorted(troot.glob("aswitch-login-*")):
            try:
                ap = d / "auth.json"
                has_token = False
                if ap.is_file():
                    try:
                        obj = json.loads(ap.read_text(encoding="utf-8", errors="replace"))
                        has_token = bool((obj.get("token") or "").strip())
                    except Exception:
                        has_token = False
                if not has_token:
                    shutil.rmtree(d, ignore_errors=True)
                    log(f"已清理历史孤儿登录目录 {d.name}")
            except Exception:
                continue
    except Exception:
        pass


def login_and_add_account(timeout: int = 300, on_progress=None) -> dict:
    """登录并添加账号：起隔离实例 → 等官方登录 → 取凭证 → 入库 → 清理。

    ⚠ 隔离并不彻底：`~/.openclaw-autoclaw` 网关状态不随 --user-data-dir 走，
    登录可能连带改掉主 profile 的 auth.json。因此全程留底、结束后回滚，
    保证"只添加账号、不动现有登录"。

    返回 {'ok': bool, 'uid': str|None, 'name': str|None, 'error': str|None}
    on_progress: callable(str)，用于向 GUI 上报进度文本。
    """
    def report(msg):
        log(msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    if not find_autoclaw_exe():
        return {"ok": False, "uid": None, "name": None, "error": "找不到 AutoClaw.exe"}
    busy = token_ports_in_use()
    if busy:
        # 端口被占 = 主实例还在跑 = 隔离实例一个回调端口都拿不到。
        # 这时候绝不能开窗口让用户去登录：回调只会落进主账号，白废一次注册机会。
        return {"ok": False, "uid": None, "name": None,
                "error": f"检测到 AutoClaw 正在运行（官方回调端口 {busy} 都被占着）。"
                         f"请先完全退出 AutoClaw 再点「桌面端登录添加」——"
                         f"否则登录回跳只会被主实例接走，新号会登到当前账号上，白浪费一次注册机会"}
    # 进入本流程前先清掉历史遗留的孤儿登录目录（被强杀导致 finally 没跑、残留空壳占端口）；
    # 再记录"当前所有 AutoClaw 进程"快照，供 finally 兜底清理用——登录里 self-relaunch 的野进程
    # 不在 _ISOLATED_PIDS 里，但一定不在本快照之前存在，靠差集就能精准杀掉。
    _cleanup_orphan_login_dirs()
    before_all = _autoclaw_pids()
    main_snap = _snapshot_main_session()
    launched_pids: set = set()

    tmp = (Path(os.environ.get("TEMP") or r"C:\Windows\Temp")
           / f"aswitch-login-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
    LOGIN_CANCEL.clear()          # 新一次登录：复位上一次的取消标记
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        # channel.json 决定连哪个 realm（zai/海外），跟随主 profile 免得登录到别的区
        ch = DEFAULT_STATE_DIR / "channel.json"
        if ch.is_file():
            shutil.copy2(ch, tmp / "channel.json")

        report("正在启动登录窗口…")
        launched_pids = _launch_isolated_profile(tmp)
        if not launched_pids:
            return {"ok": False, "uid": None, "name": None,
                    "error": "登录窗口启动失败，请重试（若反复失败可能是权限问题）"}
        report("已打开登录窗口，请在窗口中完成登录")

        got = _wait_for_login(tmp, timeout=timeout, on_progress=report, main_snap=main_snap)
        if not got and not LOGIN_CANCEL.is_set():
            # 超时不等于没登录：用户可能刚在浏览器里填完验证码/人机码，凭证还在路上。
            # 必须在**销毁之前**（finally 会回滚主 profile + rmtree tmp）再复查一轮，
            # 否则就像 09-23 第三次加号那样：登录其实成功了，却被自己的清理逻辑销毁。
            report("等待超时，销毁前最后复查各落点（60 秒）…")
            got = _wait_for_login(tmp, timeout=60, main_snap=main_snap)
        if not got and LOGIN_CANCEL.is_set():
            # 取消 ≠ 丢弃：用户可能点取消前刚在窗口里完成登录，凭证常晚到几秒才落盘。
            # 销毁前快扫一遍（honor_cancel=False 才能真的扫），扫到就走正常入库，别烧掉一次成功的登录。
            report("取消前快扫迟到凭证（5 秒）…")
            got = _wait_for_login(tmp, timeout=5, main_snap=main_snap, honor_cancel=False)
        if not got:
            oauth_err = _extract_oauth_failure(tmp)
            if LOGIN_CANCEL.is_set():
                return {"ok": False, "uid": None, "name": None, "cancelled": True,
                        "error": "已取消登录添加（登录窗口已关闭，桌面端正在重启）"}
            return {"ok": False, "uid": None, "name": None,
                    "error": "未检测到登录：请确认在**弹出的登录窗口**里完成登录（不是平时那个"
                             " AutoClaw 主窗口），且登录期间主 AutoClaw 保持完全退出。"
                             "注意：浏览器当前登录的是哪个账号，回跳交回的就是哪个——"
                             "若那已是要添加的号本身，请先在浏览器退出登录换目标账号再重试"
                             + (f"［客户端兑换失败：{oauth_err}］" if oauth_err else "")}
        # 凭证实际所在目录：隔离成功=tmp；隔离逃逸=主 profile（09-23 实测第三次加号踩中）。
        # 后续所有读取/拷贝都必须走 src，否则读的是空壳 tmp。
        src = Path(got.get("source_dir") or tmp)
        if got.get("escaped_isolation"):
            report(f"⚠ 客户端把登录态写到了隔离目录之外（{src}），已从该处抢救凭证")

        # 邀请码绑定必须是"拿到 token 后的第一个动作"：
        # 实测甲号在注册后 +4s 绑成功，乙号在 +40s 就被服务端回 400001，
        # 窗口极短 —— 先等 userInfo 再绑就等于把奖励等没了。
        acc = load_account(src)
        if not acc:
            return {"ok": False, "uid": None, "name": None,
                    "error": "登录成功但读取凭证失败，请重试"}
        code = (load_settings().get("invite_code") or "").strip()
        invite_proof = None
        if code:
            try:
                invite_proof = bind_invite_with_proof(acc, code, say=report)
            except Exception as e:
                invite_proof = {"bound": False, "msg": f"{type(e).__name__}: {e}",
                                "invite_code": mask_invite_code(code)}
                report(f"- 邀请码绑定异常：{type(e).__name__}: {e}")

        report("已检测到登录，正在保存账号…")
        info = _wait_for_userinfo(tmp, source_dir=src)
        acc = load_account(src) or acc

        uid = acc.user_id
        name = acc.nickname
        if not uid or not (info.get("userInfo") or {}).get("user_id"):
            # auth.json 的 userInfo 可能还没落盘，退而从 JWT 里取
            j = _jwt_payload(got.get("token") or "")
            uid = uid or j.get("user_id")
            name = (name if acc.user_id else None) or j.get("jti") or name
        if not uid:
            return {"ok": False, "uid": None, "name": None,
                    "error": "未能解析出账号 ID，请重试"}

        dest = ACCOUNTS_DIR / str(uid)
        # 官方窗口不告诉我们 first_login；"档案是第一次建"是能拿到的最接近事实的替代
        is_new_archive = not dest.is_dir()
        # 身份文件也优先看实际落点：隔离逃逸时客户端用的是它自己那份 deviceId。
        ident = src / "identity" / "device.json"
        if not ident.is_file():
            ident = tmp / "identity" / "device.json"
        preset_dev = ""
        if ident.is_file():
            try:
                preset_dev = str(json.loads(ident.read_text(encoding="utf-8"))
                                 .get("deviceId") or "")
            except Exception:
                preset_dev = ""
        # 判定必须落在真正用于签名那份身份上：auth.json 里的 deviceId 才是桌面端
        # 实际用的，预置的被它忽略时预置值就成了假证据
        shared_device = deviceid_owner(acc.device_id or preset_dev, exclude_uid=str(uid))
        # 设备身份一旦与已入库的号重合，服务端就把两个号当同一台机器，新人资格只算
        # 先到那个 —— 这时候入档等于把登录机会变成废号，必须当场拒掉而不是提醒完收下。
        # 所以整个判定做在 mkdir 之前：还没有任何东西需要回滚。
        if shared_device:
            report(f"✗ 设备身份与已入库的 {shared_device[:8]} 相同，已拒绝入档："
                   f"这两个号在服务端是同一台机器，新人资格只会算一个")
            if got.get("escaped_isolation"):
                report("  ↳ 本次是客户端把登录态写回了主 profile（隔离逃逸）导致的共用设备，"
                       "不是你的新号问题。请**完全退出 AutoClaw（含托盘图标）**后重试，"
                       "让隔离窗口用上它自己的独立身份")
            return {"ok": False, "uid": str(uid), "name": str(name or uid),
                    "error": f"设备身份不独立：与账号 {shared_device[:8]} 用的是同一份 "
                             f"deviceId，已拒绝入档。请完全退出 AutoClaw 后重试"
                             f"（重开会另铸一份独立身份）",
                    "shared_device": shared_device}
        if (acc.device_id and preset_dev and acc.device_id != preset_dev):
            report("⚠ 桌面端没按我们预置的身份签名（auth.json 与身份文件的 deviceId 不一致），"
                   "两份都已入库，但新人资格要看服务端认哪个")
        # 首次入档才建目录：重复登录一个已有档案时不能因为这次失败把老档案删掉
        if is_new_archive:
            dest.mkdir(parents=True, exist_ok=True)
        # 凭证三件套必须**整组同源**：auth.json 的 token 是 DPAPI+AES-GCM 加密，
        # 解密密钥在同目录的 Local State 里 —— 混用 src 的 auth.json 和 tmp 的
        # Local State（或反之）会得到一份解不开的档案，入库即废号。
        # 所以先定"这一组从哪个目录取"（以 auth.json 的实际落点为准），再整组拷。
        cred_src = src if (Path(src) / "auth.json").is_file() else tmp
        for f in ("auth.json", "Local State", "channel.json"):
            p = Path(cred_src) / f
            if p.is_file():
                (dest / f).write_bytes(p.read_bytes())
        # 新号默认标未暖号：暖号闸门会把它挡在池外，直到暖号脚本跑完破"纯机器"画像。
        # 存量活号（非 is_new_archive）不写这个文件 → _account_warmed 按档案 mtime 兜底放行。
        if is_new_archive:
            try:
                (dest / WARM_STATE_FILE).write_text(
                    json.dumps({"warmed": False, "warmed_at": 0, "human_shape_ratio": 0,
                                "imported_at": int(time.time())}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                report("⚠ 新号已入库但**未暖号**：暖号闸门会先挡在池外，"
                       "跑暖号脚本破纯机器画像、过隔离期后自动进池，避免重蹈注册当天被烧死的覆辙")
            except Exception as e:
                report(f"- 写暖号状态失败（不影响入库）：{type(e).__name__}: {e}")
        # 隔离 profile 里的独立设备身份随账号入库（新人资格与它绑定）
        if ident.is_file() and not (dest / "identity" / "device.json").is_file():
            (dest / "identity").mkdir(parents=True, exist_ok=True)
            (dest / "identity" / "device.json").write_bytes(ident.read_bytes())
        report(f"已添加账号 {name or uid}（uid={uid}）")
        # 取证要跑在入库后的档案上：tmp 会在 finally 里删掉，写进去就等于没写
        diag = diagnose_new_account(load_account(dest) or acc, say=report,
                                    first_login=is_new_archive)
        if invite_proof:
            diag["invite"] = invite_proof
        return {"ok": True, "uid": str(uid), "name": str(name or uid), "error": None,
                "shared_device": shared_device, "diagnosis": diag,
                "invite_bound": invite_proof}
    except Exception as e:
        return {"ok": False, "uid": None, "name": None,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        # 无论成败都只关自己启动的那组实例，再清掉临时目录
        _kill_isolated(launched_pids)
        # 兜底：杀掉所有"本次登录前不存在"的 AutoClaw 进程（含 self-relaunch 的野进程）。
        # 主实例已在进入本流程前被 kill_autoclaw 关掉，所以此刻剩的都是登录实例，差集精准。
        _kill_stray_login_instances(before_all)
        _restore_main_session_if_switched(main_snap)
        # 日志留底：隔离实例的 auth 日志是兑换失败的唯一证据，销毁前抢救
        _preserve_login_logs(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_oauth_failure(tmp: Path) -> str:
    """从隔离实例的 auth 日志里捞出最后一次 OAuth 兑换失败的真实原因。

    客户端兑换走后端（overseaZaiLogin），失败码（如 631001）只写在
    tmp/_home/.openclaw-autoclaw/logs/autoclaw-auth.log —— 不捞出来，
    "未检测到登录"就永远查不出为什么（日志随 tmp 销毁）。
    """
    logf = Path(tmp) / "_home" / ".openclaw-autoclaw" / "logs" / "autoclaw-auth.log"
    try:
        if not logf.is_file():
            return ""
        last_err = ""
        for line in logf.read_text(encoding="utf-8", errors="replace").splitlines():
            if "failed" not in line.lower():
                continue
            try:
                j = json.loads(line)
                d = j.get("data") or {}
                if d.get("code") or d.get("msg"):
                    last_err = f"{d.get('code', '')} {str(d.get('msg', ''))[:120]}".strip()
                    if str(d.get("code")) == "631001":
                        last_err += ("（z.ai 后端拒绝此登录，与本地旧状态无关；"
                                     "同日 unive.fun 邮箱号批量被封——换自有域名邮箱的账号，"
                                     "如 szpuedu.dpdns.org）")
            except Exception:
                last_err = line[-160:]
        return last_err
    except Exception:
        return ""


def _preserve_login_logs(tmp: Path) -> str:
    """把隔离实例的日志留底到 accounts/_login_logs/（否则随 tmp 销毁，死无对证）。"""
    try:
        logdir = Path(tmp) / "_home" / ".openclaw-autoclaw" / "logs"
        if not logdir.is_dir():
            return ""
        keep = ACCOUNTS_DIR / "_login_logs" / f"login-{time.strftime('%Y%m%d-%H%M%S')}"
        keep.mkdir(parents=True, exist_ok=True)
        for f in logdir.iterdir():
            if f.is_file():
                (keep / f.name).write_bytes(f.read_bytes())
        return str(keep)
    except Exception:
        return ""


def _late_salvage(say=None) -> dict | None:
    """登录环节失败后的最后抢救：复查各落点有没有"晚到的新号凭证"。

    为什么需要：客户端经常在网页登录完成后才把凭证写回主 profile，比轮询窗口晚几秒；
    而调用方紧接着就重启桌面端，那份新号会话会被主账号顶掉，彻底救不回来。

    判据不依赖登录前快照（那是内层局部变量）：主 profile 里的 uid **若已在 accounts/
    入库**，说明它本来就是我们的号（多半是桌面端定时刷新 token），不是新登录，不抢救；
    **未入库**才是新号，抢救入库。这个判据自洽且不会误伤主号。
    """
    def _report(m):
        if say:
            try:
                say(m)
            except Exception:
                pass

    known = {p.name for p in ACCOUNTS_DIR.iterdir() if p.is_dir()} if ACCOUNTS_DIR.is_dir() else set()
    # 内层 finally 已把本次 tmp 删掉，所以这里扫：主 profile（隔离逃逸的真实落点）
    # + TEMP 下残留的 aswitch-login-*（被强杀/未清理的孤儿目录）。
    dirs: list[Path] = [Path(DEFAULT_STATE_DIR)]
    try:
        troot = Path(tempfile.gettempdir())
        dirs += sorted(troot.glob("aswitch-login-*"), reverse=True)[:3]
    except Exception:
        pass
    for d in dirs:
        ap = Path(d) / "auth.json"
        if not ap.is_file():
            continue
        ident = _token_identity(Path(d))
        if not ident or not ident[0]:
            continue
        uid = str(ident[0])
        if uid in known:
            continue                       # 已入库 = 主号刷新，不是新登录
        acc = load_account(Path(d))
        if not acc:
            continue
        try:
            shared = deviceid_owner(acc.device_id, exclude_uid=uid)
        except Exception:
            shared = None
        if shared:
            _report(f"✗ 迟到凭证与已入库的 {str(shared)[:8]} 共用设备身份，拒绝入库"
                    "（隔离逃逸所致，请完全退出 AutoClaw 含托盘后重试）")
            return None
        dest = ACCOUNTS_DIR / uid
        is_new = not dest.is_dir()
        dest.mkdir(parents=True, exist_ok=True)
        for f in ("auth.json", "Local State", "channel.json"):
            p = Path(d) / f
            if p.is_file():
                (dest / f).write_bytes(p.read_bytes())
        if is_new:
            try:
                (dest / WARM_STATE_FILE).write_text(
                    json.dumps({"warmed": False, "warmed_at": 0, "human_shape_ratio": 0,
                                "imported_at": int(time.time()), "salvaged": True},
                               ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:
                pass
        _report(f"✓ 抢救到迟到凭证并入库：{acc.nickname or uid}（uid={uid[:8]}，未暖号，交给探针放行）")
        return {"uid": uid, "name": acc.nickname or uid}
    return None


def login_add_with_yield(timeout: int = 300, on_progress=None) -> dict:
    """自动让位加号：替用户完成"退出桌面端→官方窗口登录→重启桌面端→确认算力恢复"。

    为什么必须这么做而不是"内置开个登录页"：官方 TokenServer 硬编码把四个回调端口
    全占住、抢不到就抛，所以"隔离实例走官方登录"与"主实例继续跑"在同一台机上不能并存。
    这里不绕任何防护，只是把"退出→登录→重启→回查反代"这一串做完，并把结果如实交回去。

    关于断供：反代的桌面端 broker 上游确实随主实例退出而消失，但 2026-09-20 起
    server.mjs 在拿不到 broker 时会用本机凭证直连云端，所以只要反代活着、凭证没过期，
    登录那几分钟算力其实不断 —— 恢复结论以 /health 的 upstream 字段为准，不猜。
    """
    say = on_progress or (lambda m: None)
    t0 = time.time()

    def _relay_note(y: dict) -> str:
        if not y.get("relaunched"):
            return (f"桌面端没能自动重启（{y.get('launch_error') or '未知原因'}），"
                    "反代上游不会自己恢复，请手动启动 AutoClaw")
        rly = y.get("relay") or {}
        if rly.get("ok"):
            via = ("云端直连（不依赖桌面端）" if rly.get("upstream") == "cloud"
                   else f"broker {rly.get('broker')}")
            return (f"断供 {y.get('outage_s')} 秒，反代上游已恢复"
                    f"（{via}，{rly.get('models')} 个模型可路由）")
        return (f"桌面端已重启，但反代上游 {rly.get('waited_s')} 秒内没回到 ok，"
                "算力仍然是断的 —— 请手动确认 AutoClaw 与 server.mjs")

    if not autoclaw_running():
        say("桌面端本来就没在跑，直接开官方登录窗口")
        r = dict(login_and_add_account(timeout=timeout, on_progress=say))
        r["yield"] = {"killed": False, "relaunched": False, "outage_s": 0,
                      "relay": relay_upstream_wait(timeout=20)}
        r["yield_note"] = ("桌面端本来没在跑，本次没有让位；"
                           + ("反代上游正常" if r["yield"]["relay"]["ok"]
                              else "反代没有可用通路（既没 broker，也没有能直连云端的凭证）"))
        return r

    say("正在让主实例 AutoClaw 退出（登录结束后会自动重启并校验反代上游）…")
    if not kill_autoclaw():
        # 没能确认主实例退出：绝不能继续（回调会落进错误的账号），也不该再启动一个实例
        return {"ok": False,
                "error": "没能确认主实例已退出（可能只剩隔离登录实例在跑）。"
                         "为不误伤登录窗口，也没有再启动桌面端：AutoClaw 保持原状",
                "yield": {"killed": False, "relaunched": False}}

    freed, busy = wait_token_ports_free()
    if not freed:
        say(f"端口 {busy} 仍被占用，先恢复桌面端再想办法")
        launched, info = launch_autoclaw()
        y = {"killed": True, "relaunched": bool(launched),
             "launch_error": "" if launched else str(info),
             "outage_s": round(time.time() - t0, 1),
             "relay": relay_upstream_wait(timeout=90)}
        return {"ok": False,
                "error": f"官方回调端口 {busy} 没释放，登录窗口不开（开了回调必然落进错误的账号）；"
                         + ("已把桌面端重启回去。" if launched else "桌面端重启失败，请手动启动 AutoClaw。"),
                "yield": y, "yield_note": _relay_note(y)}

    try:
        r = dict(login_and_add_account(timeout=timeout, on_progress=say))
    except Exception as e:
        r = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    if not r.get("ok"):
        # 登录窗口失败时**先别重启桌面端**：客户端常在网页登录完成后才把凭证写回主
        # profile（比轮询窗口晚几秒），一重启就会把那份新号会话顶掉、彻底救不回来。
        # 所以先复查各落点，出现"非主账号"的 token 就补入库。
        try:
            late = _late_salvage(say)
        except Exception as e:
            late = None
            say(f"- 迟到凭证复查异常：{type(e).__name__}: {e}")
        if late:
            r = {"ok": True, "uid": str(late.get("uid")), "name": str(late.get("name")),
                 "error": None, "salvaged": True}
            say(f"✓ 已从迟到落点抢救并入库：{late.get('name')}（uid={str(late.get('uid'))[:8]}）")

    say("登录环节结束，正在重启桌面端并等反代上游回来…")
    launched, info = launch_autoclaw()
    y = {"killed": True, "relaunched": bool(launched),
         "launch_error": "" if launched else str(info),
         "outage_s": round(time.time() - t0, 1),
         "relay": relay_upstream_wait()}
    r["yield"] = y
    r["yield_note"] = _relay_note(y)
    say(r["yield_note"])
    return r


def activity_login_sweep(accounts=None, hold_seconds: int = 80,
                         on_progress=None, skip_uid: str = "") -> list[dict]:
    """活动窗口期"登录打卡"：把每个入库账号依次装进隔离 profile 拉起一次
    AutoClaw，让服务端记录该账号的登录活跃。

    "Log in to claim" 类活动（如 Token Rush 的 500M tokens）在窗口期内
    登录即自动到账，无需点任何按钮——多账号就靠这个函数逐号打卡，
    全程不碰主实例的登录（隔离 env：--user-data-dir + OPENCLAW_STATE_DIR
    + USERPROFILE + 禁用系统凭据回退，前面登录流程已验证不串号）。
    """
    def report(msg):
        log(msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    if accounts is None:
        accounts = discover_accounts()
    results = []
    for i, acc in enumerate(accounts, 1):
        uid = str(acc.user_id or "")
        if skip_uid and uid == skip_uid:
            continue
        report(f"〔活动打卡〕({i}) {acc.nickname}：启动隔离实例…")
        tmp = (Path(os.environ.get("TEMP") or r"C:\Windows\Temp")
               / f"aswitch-sweep-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}")
        pids: set = set()
        try:
            tmp.mkdir(parents=True, exist_ok=True)
            for f in ("auth.json", "Local State", "channel.json"):
                p = acc.appdata_dir / f
                if p.is_file():
                    (tmp / f).write_bytes(p.read_bytes())
            ident = acc.appdata_dir / "identity" / "device.json"
            if ident.is_file():
                (tmp / "identity").mkdir(parents=True, exist_ok=True)
                (tmp / "identity" / "device.json").write_bytes(ident.read_bytes())
            pids = _launch_isolated_profile(tmp)
            if not pids:
                results.append({"uid": uid, "name": acc.nickname,
                                "ok": False, "error": "启动失败"})
                continue
            report(f"〔活动打卡〕{acc.nickname}：已登录态运行 {hold_seconds}s…")
            _hold_until = time.time() + hold_seconds
            while time.time() < _hold_until:
                _inflight_beat(tmp)
                time.sleep(min(3.0, max(0.2, _hold_until - time.time())))
            # 核验：隔离 profile 的 auth.json 应仍是该账号的登录态
            verified = None
            try:
                d = json.loads((tmp / "auth.json").read_text(encoding="utf-8"))
                cur_uid = str((d.get("userInfo") or {}).get("user_id") or "")
                tok = d.get("token") or ""
                if not cur_uid and tok:
                    m = re.search(r'"token":\s*"enc:([^"]+)"',
                                  (tmp / "auth.json").read_text(encoding="utf-8"))
                    if m:
                        j = _jwt_payload(decrypt_chromium_value(
                            "enc:" + m.group(1), tmp))
                        cur_uid = str(j.get("user_id") or "")
                verified = (cur_uid == uid) if cur_uid else None
            except Exception:
                verified = None
            results.append({"uid": uid, "name": acc.nickname, "ok": True,
                            "verified": verified})
        except Exception as e:
            results.append({"uid": uid, "name": acc.nickname,
                            "ok": False, "error": f"{type(e).__name__}: {e}"})
        finally:
            _kill_isolated(pids)   # 先确认进程没了，再收凭证（否则可能读到写一半的 auth.json）
            # 隔离实例一启动就强制续期，服务端轮换后的新 pair 只在这个 tmp 里；
            # 不收编就删目录 = 该账号存档里的 refreshToken 立刻变成用过的死凭证。
            fresh = load_account(tmp)
            if fresh and str(fresh.user_id or "") == uid:
                try:
                    sync_live_to_archive(fresh, tmp)
                except Exception as e:
                    report(f"〔活动打卡〕{acc.nickname}：新凭证回写存档失败（{type(e).__name__}），"
                           f"下次切换可能需要重新登录")
            shutil.rmtree(tmp, ignore_errors=True)
    ok_n = sum(1 for r in results if r.get("ok"))
    report(f"〔活动打卡〕完成：{ok_n}/{len(results)} 个账号打卡成功")
    return results


def live_identity_uid() -> str:
    """活动目录当前登录的 user_id（读不到返回空串）。"""
    acc = load_account(DEFAULT_STATE_DIR)
    return str(acc.user_id) if acc else ""


def sync_live_to_archive(live_acc: Account, src_dir: Path | None = None) -> str:
    """把一份"新鲜"凭证（活动目录或某个隔离实例的 profile）回写到该账号的存档。

    服务端每次刷新都轮换 refresh token，且 App 启动即强制续期：谁最后跑过，
    谁目录里那份才是新的。不回写，存档里那份迟早是死凭证，切回去直接登录失效。
    """
    if not live_acc or not live_acc.user_id:
        return ""
    src = Path(src_dir or live_acc.appdata_dir)
    dest = ACCOUNTS_DIR / str(live_acc.user_id)
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for rel, data in build_identity_bundle(src).items():
        _atomic_write(dest / rel, data)
        copied.append(rel)
    src_ident = src / "identity" / "device.json"
    dst_ident = dest / "identity" / "device.json"
    if src_ident.is_file() and (src == DEFAULT_STATE_DIR or not dst_ident.is_file()):
        # 活动目录里那把是 App 正在用的，权威；隔离实例现铸的那把只在存档
        # 本来没有身份时收编——已经确立过的设备指纹绝不静默替换（服务端按设备判新老）。
        _atomic_write(dst_ident, src_ident.read_bytes())
        copied.append("identity/device.json")
    log(f"已把 {src.name} 的新鲜凭证回写存档 {live_acc.nickname}（{len(copied)} 个文件）")
    return ", ".join(copied)


def switch_account(target: Account, on_progress=None, watch_seconds: int = 30) -> dict:
    """把 target 账号成套地装进 AutoClaw 活动目录并重启。

    时序（缺一步就会"弹回上一个账号"）：
      0. 当前账号的新鲜凭证先回写它自己的存档
      1. 关闭 AutoClaw 并验证进程真的没了（残留进程会用它内存里的旧 token 回写）
      2. 一次性写全套身份：auth.json + token-cache + user-cache + 三个 .backup
         影子 + Local State + channel.json（.backup 不写 = App 从备份复活旧账号）
      3. 回读校验（逐字节 + 能解密出目标 token），不一致就重试
      4. 启动，再观察 watch_seconds 秒确认身份没有回跳
    """
    steps = []
    result = {"ok": False, "steps": steps, "error": None, "switched_to": target.nickname,
              "already_active": False, "verified": False, "reverted_to": None,
              "rollback_dir": None}

    def report(msg):
        steps.append(msg)
        log(msg)
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    live_dir = DEFAULT_STATE_DIR
    if not (live_dir / "auth.json").is_file():
        result["error"] = f"活动目录没有 auth.json：{live_dir}"
        return result

    try:
        same_dir = target.appdata_dir.resolve() == live_dir.resolve()
    except Exception:
        same_dir = False
    if same_dir:
        result.update({"ok": True, "already_active": True})
        report(f"{target.nickname} 本来就是当前登录账号，未做任何改动")
        return result

    # 0. 先保全当前账号（它的 refreshToken 可能刚被 App 轮换过）
    live_acc = load_account(live_dir)
    if live_acc and live_acc.user_id:
        report(f"当前账号：{live_acc.nickname}，先把新鲜凭证存回它自己的存档…")
        try:
            sync_live_to_archive(live_acc)
            report("✓ 当前账号凭证已回写存档")
        except Exception as e:
            report(f"! 回写存档失败（不影响本次切换）：{type(e).__name__}: {e}")
    else:
        report("- 活动目录当前没有可解析的登录身份")

    # 1. 关掉 AutoClaw，并且要验证它真的死了
    if autoclaw_running():
        report("关闭 AutoClaw…")
        if not kill_autoclaw(timeout=20):
            result["error"] = "AutoClaw 没能确认退出（残留进程会用它内存里的旧 token 回写身份），已放弃切换"
            report("✗ " + result["error"])
            return result
        report("✓ AutoClaw 已确认退出")
    else:
        report("- AutoClaw 未在运行")
    time.sleep(0.6)  # 等最后一次的磁盘写入落定

    # 1.5 目标账号的凭证必须先"验活"：App 每次启动都强制刷新，服务端会轮换
    # refreshToken，存档里那份很可能已经被用过。把死凭证写进活动目录，App 要么
    # 回退去读别的凭证源（=弹回别的账号），要么直接判死刑强制登出。
    if not target.is_live:
        report(f"校验 {target.nickname} 的存档凭证是否还有效（必要时续期并回写存档）…")
        if not target.refresh_access_token():
            result["error"] = (f"{target.nickname} 的登录凭证已失效（存档里的 refreshToken "
                               "已被服务端轮换掉），需要重新登录该账号后再切换；本次未改动活动目录")
            report("✗ " + result["error"])
            return result
        report(f"✓ {target.nickname} 凭证有效，jwt_uid={target.jwt_uid()}")

    # 2. 成套写入目标身份
    try:
        bundle = build_identity_bundle(target.appdata_dir)
    except Exception as e:
        result["error"] = f"读取 {target.nickname} 的凭证失败：{type(e).__name__}: {e}"
        report("✗ " + result["error"])
        return result
    if not (target.appdata_dir / "Local State").is_file():
        report("! 该账号存档缺 Local State，沿用活动目录现有的解密密钥（可能解不开 token）")
    tgt_ident = target.appdata_dir / "identity" / "device.json"
    if tgt_ident.is_file():
        bundle["identity/device.json"] = tgt_ident.read_bytes()
        report(f"- 目标账号自带设备身份；活动目录原身份已留底（{len(bundle)} 个文件成套写入）")
    else:
        report("- 目标账号存档没有设备身份，保留活动目录现有身份（删掉会掉回系统共享身份）")
    try:
        rb = write_identity_set(live_dir, bundle)
        result["rollback_dir"] = str(rb) if rb else None
        report(f"✓ 已成套写入 {len(bundle)} 个身份文件"
               + (f"（原文件留底：{rb.name}）" if rb else ""))
    except Exception as e:
        result["error"] = f"写入凭证失败：{type(e).__name__}: {e}"
        report("✗ " + result["error"])
        return result

    # 3. 回读校验：App 侧任何一处回退源没对齐，都会在我们眼皮底下把身份改掉
    for attempt in range(3):
        ok_v, why = verify_identity_set(live_dir, bundle, target.user_id)
        if ok_v:
            result["verified"] = True
            report(f"✓ 回读校验通过：{target.nickname} 的凭证已完整落地（第 {attempt + 1} 次）")
            break
        report(f"! 回读不一致（{why}），重写入…")
        time.sleep(0.15)
        try:
            write_identity_set(live_dir, bundle, keep_rollback=False)
        except Exception as e:
            result["error"] = f"重新写入失败：{e}"
            return result
    if not result["verified"]:
        result["error"] = "凭证无法稳定落地，疑似有进程在抢写活动目录，已中止（AutoClaw 未启动）"
        report("✗ " + result["error"])
        return result

    # 4. 启动并观察是否回跳
    ok, detail = launch_autoclaw()
    if not ok:
        result["error"] = detail
        report("✗ 启动失败: " + detail)
        return result
    report("✓ 已启动 AutoClaw，观察登录身份是否稳住…")
    want_jwt = target.jwt_uid()   # 服务端签发的数字 uid，明文 userInfo 骗得过它骗不过
    deadline = time.time() + max(0, watch_seconds)
    while time.time() < deadline:
        cur = load_account(live_dir)
        uid = str(cur.user_id) if cur else ""
        jwt_now = cur.jwt_uid() if cur else ""
        bad_uid = uid and uid != str(target.user_id)
        bad_jwt = bool(want_jwt) and bool(jwt_now) and jwt_now != want_jwt
        if bad_uid or bad_jwt:
            result["reverted_to"] = f"{cur.nickname if cur else ''}({uid})"
            which = ("明文 userInfo 被改掉" if bad_uid else
                     "userInfo 看着对，但 token 已是别人的（refreshToken 走了 token-cache 回退）")
            report(f"✗ 身份被改回 {result['reverted_to']} —— {which}")
            result["error"] = "切换后身份回跳，已如实报告（原文件在 " \
                              f"{result['rollback_dir']}，可手动还原）"
            return result
        time.sleep(1.0)
    log(f"切换到 {target.nickname}：成功（观察 {watch_seconds}s 身份未回跳）")
    report(f"✓ 观察 {watch_seconds}s：登录身份稳定为 {target.nickname}")
    result["ok"] = True
    return result


def cmd_switch(target_uid: str):
    """CLI: 切换到指定 uid 的账号。"""
    accs = discover_accounts()
    tgt = next((a for a in accs if str(a.user_id) == str(target_uid)
                or a.nickname == target_uid), None)
    if not tgt:
        log(f"找不到账号 {target_uid}；可选：{[a.nickname for a in accs]}")
        return 1
    r = switch_account(tgt)
    for s in r["steps"]:
        log("  " + s)
    log("切换完成" if r["ok"] else f"切换失败：{r.get('error')}")
    return 0 if r["ok"] else 1


SETTINGS_PATH = ACCOUNTS_DIR / "aswitch_settings.json"


def load_settings() -> dict:
    """GUI 设置（自动领取开关等）。默认值 = 出厂状态。"""
    d = {"auto_claim": False,   # ⚠ 默认关：自动领取有风控风险，让用户显式打开
         "auto_sweep": False,   # ⚠ 默认关：活动登录打卡会拉起窗口，同样显式打开
         "invite_code": ""}     # 大号邀请码：登录添加新号时自动绑定（大号 800 分/号）
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            d.update(raw)
    except Exception:
        pass
    return d


def save_settings(d: dict) -> None:
    try:
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    except Exception as e:
        log(f"保存设置失败：{type(e).__name__}: {e}")


def cmd_probe(accounts: list):
    """CLI: 实时探测所有账号的全部可领资格。"""
    for acc in accounts:
        p = acc.probe_all()
        print(f"\n=== {acc.nickname} (uid={acc.user_id}) ===")
        if not p["claimable"]:
            print("  当前没有可领资格")
        for c in p["claimable"]:
            tag = {"daily": "每日", "inspiration": "灵感", "newbie": "新人",
                   "promotion": "活动", "promo_link": "外部活动",
                   "promo_soon": "预告"}.get(c["kind"], c["kind"])
            pts = f" +{c['points']}" if c.get("points") else ""
            print(f"  [{tag}] {c['title']}{pts}")
        if not p["newbie_issued"]:
            print("  (服务端未下发新人任务)")
        for u in p["upcoming"]:
            print(f"  [预告] {u['name']}  {u['start_text']} ~ {u['end_text']}")


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "list").lower()
    def _log_diag(r: dict):
        diag = r.pop("diagnosis", None) if isinstance(r, dict) else None
        if isinstance(diag, dict):
            log(f"新号取证：{diag.get('verdict')}"
                f"（全文见 {Path(str(r.get('dir') or '.')) / NEW_ACCOUNT_DIAG_FILE}）")
        return r

    if cmd == "login":
        r = login_and_add_account(timeout=int(argv[2]) if len(argv) > 2 else 300)
        log(f"登录添加结果：{_log_diag(r)}")
        return 0 if r.get("ok") else 1
    if cmd in ("signup", "add-phone"):
        # 免桌面端注册/登录，两步走：验证码只能人读。手机号是唯一绕不开的外部依赖。
        phone, code = (argv[2] if len(argv) > 2 else ""), (argv[3] if len(argv) > 3 else "")
        if not code:
            r = begin_headless_add(phone)
            log(f"发码结果：{r}")
            return 0 if r.get("ok") else 1
        inv = (argv[4] if len(argv) > 4 else load_settings().get("invite_code") or "")
        r = finish_headless_add(phone, code, invite_code=inv, on_progress=log)
        log(f"入档结果：{_log_diag(r)}")
        return 0 if r.get("ok") else 1
    if cmd in ("zai-login", "google-login", "oauth-add"):
        vendor = "google" if cmd == "google-login" else "zai"
        inv = (argv[2] if len(argv) > 2 else load_settings().get("invite_code") or "")
        r = oauth_add(invite_code=str(inv), vendor=vendor, on_progress=log)
        log(f"OAuth 登录结果：{ {k: v for k, v in _log_diag(r).items() if k != 'url'} }")
        if r.get("url") and not r.get("ok"):
            log(f"  授权链接：{r['url']}")
        return 0 if r.get("ok") else 1
    accounts = discover_accounts()
    if cmd == "list":
        cmd_list(accounts)
    elif cmd == "pool":
        return cmd_pool(accounts)
    elif cmd == "probe":
        cmd_probe(accounts)
    elif cmd == "claim":
        cmd_claim(accounts, only_available=False)
    elif cmd == "auto":
        cmd_claim(accounts, only_available=True)
    elif cmd == "activities":
        return cmd_activities(accounts)
    elif cmd == "bind":
        code = (argv[2] if len(argv) > 2 else load_settings().get("invite_code") or "").strip()
        if not code:
            log("用法: a_switch.py bind <邀请码>")
            return 2
        for acc in accounts:
            s = acc.invite_status()
            b = s.get("bind") or {}
            if (s.get("share") or {}).get("invite_code") == code:
                log(f"  {acc.nickname}: 邀请码本人，跳过")
                continue
            if b.get("status") != "unbound" or not b.get("can_bind"):
                log(f"  {acc.nickname}: 已绑定或不可绑定，跳过")
                continue
            st, d = acc.bind_invite_code(code)
            ok = isinstance(d, dict) and d.get("code") == 0
            log(f"  {acc.nickname}: "
                + (f"✓ 已绑定 {mask_invite_code(code)}" if ok else str(d)[:120]))
        # 服务端把话说得很死：existing_user_bind_inviter_reward_limit = 0
        # —— 已注册的老号补绑，邀请人一分钱奖励都拿不到，只有新号计奖
        log("提示：老号补绑不计邀请奖励（服务端 existing_user 计奖上限=0）；"
            "绑在刚注册的新号上服务端也只会记关系，800 分要等受邀号真实使用后才结算，"
            "用 `invite-check` 复核")
        return 0
    elif cmd == "invite-check":
        return 0 if invite_reward_check(say=log) else 1
    elif cmd == "add":
        if len(argv) < 3:
            log("用法: a_switch.py add <AutoClaw数据目录>")
            return 2
        return cmd_add(argv[2])
    elif cmd == "export":
        return cmd_export(argv[2] if len(argv) > 2 else r"D:\autoclaw-switch\export")
    elif cmd == "switch":
        if len(argv) < 3:
            log("用法: a_switch.py switch <uid或昵称>")
            return 2
        return cmd_switch(argv[2])
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


# ================= 一键反代（relay 部署 + ZCode 注册 + 暖号 + 验证） =================
# 目标："点一下按钮，ZCode 里就能用 AutoClaw 的积分"。
# 链路：AutoClaw 登录态 → relay(本地 node 反代) → ZCode(anthropic-messages provider)。
# 防封三原则写死在 relay 里：零固定节奏出站、三闸（限速/并发/输入）、新号暖号闸门。

RELAY_PORT = int(os.environ.get("AUTOCLAW_RELAY_PORT") or 18766)
RELAY_TOKEN = "autoclaw-local"
ZCODE_PROVIDER_ID = "autoclaw-glm-provider"
ZCODE_PROVIDER_NAME = "AutoClaw"
ZCODE_BASE_URL = f"http://127.0.0.1:{RELAY_PORT}"
# (ZCode 显示名, route, 支持视觉, contextWindow) —— 视觉矩阵来自逐路由实测
ZCODE_MODELS = [
    ("GLM-5.3",             "zaicoding_glm-5.3",              False, 500000),
    # 2026-09-29 起本机账号的模型目录里 tdpsk_deepseek-* 两条被上游移除
    # （runtime/model-catalog/remote-snapshot.json 只剩 4 个模型），请求回 400 非法模型。
    # 权益恢复后把下面两行加回来即可：
    # ("Deepseek-V4.1-Flash", "tdpsk_deepseek-v4-flash-202605", True,  500000),
    # ("DeepSeek-V4-Pro",     "tdpsk_deepseek-v4-pro-202606",   False, 500000),
    ("GLM-5.3-Flash",       "zai_glm-5.3-flash",              True,  500000),
    ("Auto",                "zai_auto",                       True,  500000),
    ("Auto-Fast",           "zai_auto-fast",                  True,  500000),
]

# WorkBuddy 网关（本地 OpenAI 兼容反代，腾讯 copilot 权益）：与 AutoClaw 同源"网关 + token"
# 模式——DSH/ZCode 只认本地地址与占位 token，真实 WorkBuddy 信封密钥仅在代理侧
# （wb-key-payload.json，由 workbuddy-proxy 从进程内存提取）读取，绝不进本仓库或客户端配置。
WB_PORT = int(os.environ.get("WORKBUDDY_PROXY_PORT") or 8787)
WB_TOKEN = os.environ.get("WORKBUDDY_LOCAL_TOKEN") or "wb-local"  # 本地占位值，非真实凭据
WB_PROVIDER_ID = "workbuddy-provider"
WB_PROVIDER_NAME = "WorkBuddy"
WB_BASE_URL = f"http://127.0.0.1:{WB_PORT}/v1"
# (真实模型 id, 上下文窗口) —— 取自 workbuddy-proxy /v1/models。
# ZCode 会把 modelId 原样发给 /v1/chat/completions，显示名（如 "GLM-5.3-Flash"）
# 会被代理拒成 model_not_found，所以这里只能用 id。
WB_MODELS = [
    ("deepseek-v4.1-flash", 1000000),
    ("glm-5.3",             1000000),
    ("glm-5.3-flash",       1000000),
    ("kimi-k2.8-preview",   1000000),
    ("kimi-k3-1",           1000000),
    ("deepseek-v4-pro",     1000000),
    ("glm-5.2",             1000000),
    ("glm-5.1",             200000),
    ("glm-5v-turbo",        200000),
    ("kimi-k2.7",           256000),
    ("kimi-k2.6",           256000),
    ("minimax-m3",          512000),
    ("hy3-b",               192000),
    ("hy3-x",               192000),
    ("hy4-preview-f",       1000000),
]

# Qoder 网关（vendored 社区网关 qoder/qoder_proxy.py，COSY 签名）：同"网关 + 占位 token"
# 模式。账号池落在 ~/.qoder-relay/accounts（从本机已登录的 Qoder 客户端导入），
# 绝不进仓库；ZCode/DSH 只认 127.0.0.1:8790 与占位 token。
QODER_PORT = int(os.environ.get("QODER_PROXY_PORT") or 8790)
QODER_TOKEN = os.environ.get("QODER_LOCAL_TOKEN") or "qoder-local"  # 本地占位值，非真实凭据
QODER_PROVIDER_ID = "qoder-openai-provider"
QODER_PROVIDER_NAME = "Qoder CN"
QODER_BASE_URL = f"http://127.0.0.1:{QODER_PORT}/v1"
QODER_PANEL_PASSWORD = os.environ.get("QODER_PANEL_PASSWORD") or "admin"
# /v1/models 不可达时的兜底目录（2026-10-25 实测 CN 区；窗口取 max_input_tokens）。
# 正常路径永远以网关实时目录为准，这里只是冷启动失败时的占位。
QODER_FALLBACK_MODELS = [
    ("auto",                200000),
    ("qwen3.8-max",         180000),
    ("qwen3.8-flash",       180000),
    ("glm-5.3",             180000),
    ("glm-5.3-flash",       1000000),
    ("deepseek-v4-pro",      96000),
    ("deepseek-flash",      180000),
    ("kimi-k3",             180000),
    ("kimi-k2.8-preview",   180000),
    ("minimax-m2.7",        180000),
]


def find_node_exe():
    """AutoClaw 自带的 node 优先（版本可控），退回系统 PATH。"""
    exe = find_autoclaw_exe()
    if exe:
        p = exe.parent / "resources" / "node" / "node.exe"
        if p.is_file():
            return p
    return shutil.which("node")


def relay_home() -> Path:
    p = Path.home() / ".autoclaw-relay"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _relay_source_dir() -> Path:
    """源码目录（开发态）或 PyInstaller 解包目录（frozen）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "relay"
    return Path(__file__).resolve().parent / "relay"


def deploy_relay_files() -> Path:
    src_dir = _relay_source_dir()
    home = relay_home()
    shutil.copy2(src_dir / "server.mjs", home / "server.mjs")
    return home / "server.mjs"


def write_single_credential() -> bool:
    """单凭证模式：把当前登录态 token 写 request-headers.json（relay 的兜底凭证源）。
    多号用户走账号池导出（export_cloud_pool），两者不冲突。"""
    try:
        accs = discover_accounts()
        acc = next((a for a in accs if a.is_live and a.token), None) \
            or next((a for a in accs if a.token), None)
        if not acc:
            return False
        tok = acc.token if str(acc.token).lower().startswith("bearer ") else f"Bearer {acc.token}"
        hdr = {"headers": {"X-Authorization": tok, "X-Client-Type": "pc"}}
        (Path(GATEWAY_STATE_DIR) / "request-headers.json").write_text(
            json.dumps(hdr, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def relay_start() -> dict:
    import subprocess
    node = find_node_exe()
    if not node:
        return {"ok": False, "error": "找不到 node.exe（AutoClaw 安装目录或系统 PATH）"}
    server = deploy_relay_files()
    write_single_credential()
    logf = open(relay_home() / "relay.log", "ab")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    # X-Version 必须跟随本机真实客户端（2.0.2 起上游对旧版本号回 406 空响应体，
    # 和缺 harness 标记的表现完全一样，极易误判成"账号未放行"）。relay 启动时
    # 读 AUTOCLAW_CLIENT_VERSION，默认 1.18.5.851 已经过时。
    ver = detect_autoclaw_version()
    if ver:
        os.environ["AUTOCLAW_CLIENT_VERSION"] = ver
    subprocess.Popen([str(node), str(server)], cwd=str(relay_home()),
                     stdout=logf, stderr=subprocess.STDOUT,
                     creationflags=flags)
    for _ in range(20):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{RELAY_PORT}/health", timeout=3) as r:
                if r.status == 200:
                    return {"ok": True}
        except Exception:
            continue
    return {"ok": False, "error": "relay 启动后 10 秒内未就绪，看 ~/.autoclaw-relay/relay.log"}


def relay_is_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{RELAY_PORT}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def zcode_register_provider() -> dict:
    """向 ZCode 注册 AutoClaw 供应商。三处必须同写（缺 providerOrder 会"显示但不可用"）：
    providerOrder / providerRules / modelConfigRules.providerModelRules。幂等。"""
    cfg_path = Path.home() / ".zcode" / "v2" / "provider_config.json"
    if not cfg_path.is_file():
        return {"ok": False, "error": f"未找到 ZCode 配置：{cfg_path}（确认已安装并运行过 ZCode）"}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    conf = cfg.setdefault("config", {})
    backup = cfg_path.with_suffix(".json.bak-autoclaw")
    if not backup.exists():
        backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")

    rule = {"providerId": ZCODE_PROVIDER_ID, "providerName": ZCODE_PROVIDER_NAME, "enabled": True,
            "config": {"group": "standard-personal",
                       "access": {"type": "api-key", "apiKey": RELAY_TOKEN},
                       "api": {"type": "anthropic-messages", "baseUrl": ZCODE_BASE_URL},
                       "personalModelIds": [m[0] for m in ZCODE_MODELS],
                       "modelOrder": [m[0] for m in ZCODE_MODELS]}}
    rules = conf.setdefault("providerConfigRules", {}).setdefault("providerRules", [])
    rules[:] = [r for r in rules if r.get("providerId") != ZCODE_PROVIDER_ID]
    rules.append(rule)
    order = conf.setdefault("providerOrder", [])
    if ZCODE_PROVIDER_ID not in order:
        order.append(ZCODE_PROVIDER_ID)
    mcr = conf.setdefault("modelConfigRules", {}).setdefault("providerModelRules", [])
    mcr[:] = [r for r in mcr if r.get("providerId") != ZCODE_PROVIDER_ID]
    for display, _route, image, ctx in ZCODE_MODELS:
        mcr.append({"providerId": ZCODE_PROVIDER_ID, "modelId": display,
                    "config": {"enabled": True,
                               "properties": {"contextWindow": ctx,
                                              "inputFormat": {"supportsText": True, "supportsImage": image,
                                                              "supportsVideo": False, "supportsAudio": False,
                                                              "supportsPdf": False},
                                              "outputFormat": {"supportsText": True}},
                               # ZCode 选模型时默认挂 $max 思考档位；不声明 values 会在
                               # 会话恢复时校验失败（sessionModelUnavailable）被回退成默认供应商
                               "optionSpecs": {"reasoningLevel": {"values": ["low", "high", "max"]}}}})
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "models": [m[0] for m in ZCODE_MODELS]}


def zcode_register_workbuddy() -> dict:
    """向 ZCode 注册 WorkBuddy 网关注册商（OpenAI 兼容，端口见 WB_PORT）。

    与 AutoClaw 共用同一套"三处同写"逻辑，仅 api 类型改为 openai-chat-completions，
    baseUrl 指向本地代理的 /v1。token 用本地占位值 WB_TOKEN，真实 WorkBuddy 信封密钥
    仅在代理侧读取，不进入本仓库。幂等。
    """
    cfg_path = Path.home() / ".zcode" / "v2" / "provider_config.json"
    if not cfg_path.is_file():
        return {"ok": False, "error": f"未找到 ZCode 配置：{cfg_path}（确认已安装并运行过 ZCode）"}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    conf = cfg.setdefault("config", {})
    backup = cfg_path.with_suffix(".json.bak-workbuddy")
    if not backup.exists():
        backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")

    rule = {"providerId": WB_PROVIDER_ID, "providerName": WB_PROVIDER_NAME, "enabled": True,
            "config": {"group": "standard-personal",
                       "access": {"type": "api-key", "apiKey": WB_TOKEN},
                       "api": {"type": "openai-chat-completions", "baseUrl": WB_BASE_URL},
                       "personalModelIds": [m[0] for m in WB_MODELS],
                       "modelOrder": [m[0] for m in WB_MODELS]}}
    rules = conf.setdefault("providerConfigRules", {}).setdefault("providerRules", [])
    rules[:] = [r for r in rules if r.get("providerId") != WB_PROVIDER_ID]
    rules.append(rule)
    order = conf.setdefault("providerOrder", [])
    if WB_PROVIDER_ID not in order:
        order.append(WB_PROVIDER_ID)
    mcr = conf.setdefault("modelConfigRules", {}).setdefault("providerModelRules", [])
    mcr[:] = [r for r in mcr if r.get("providerId") != WB_PROVIDER_ID]
    for mid, ctx in WB_MODELS:
        mcr.append({"providerId": WB_PROVIDER_ID, "modelId": mid,
                    "config": {"enabled": True,
                               "properties": {"contextWindow": ctx,
                                              "inputFormat": {"supportsText": True, "supportsImage": True,
                                                              "supportsVideo": False, "supportsAudio": False,
                                                              "supportsPdf": False},
                                              "outputFormat": {"supportsText": True}},
                               "optionSpecs": {"reasoningLevel": {"values": ["low", "high", "max"]}}}})
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "models": [m[0] for m in WB_MODELS]}


def relay_smoke_test() -> dict:
    """一发真实推理：确认"积分真的进了 ZCode 可用状态"。"""
    body = {"model": "GLM-5.3-Flash", "max_tokens": 16, "stream": False,
            "messages": [{"role": "user", "content": "reply READY"}]}
    req = urllib.request.Request(f"http://127.0.0.1:{RELAY_PORT}/v1/messages",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "x-api-key": RELAY_TOKEN,
                                          "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    blocks = d.get("content") or []
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text:
        # 思考模型小 max_tokens 时 output 全是 thinking：有 content 块+usage 就算通
        u = d.get("usage") or {}
        text = f"model={d.get('model')} out={u.get('output_tokens')}t"
    return {"ok": r.status == 200 and bool(blocks), "reply": text[:60]}


def workbuddy_smoke_test() -> dict:
    """一发真实推理：确认 WorkBuddy 网关（OpenAI 协议）真的可用。"""
    body = {"model": "glm-5.3-flash", "max_tokens": 16, "stream": False,
            "messages": [{"role": "user", "content": "reply READY"}]}
    req = urllib.request.Request(f"http://127.0.0.1:{WB_PORT}/v1/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {WB_TOKEN}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    choices = d.get("choices") or []
    text = "".join(c.get("message", {}).get("content", "") for c in choices)
    if not text:
        u = d.get("usage") or {}
        text = f"model={d.get('model')} out={u.get('completion_tokens')}t"
    return {"ok": r.status == 200 and bool(choices), "reply": text[:60]}


def qoder_home() -> Path:
    p = Path.home() / ".qoder-relay"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _qoder_source_dir() -> Path:
    """源码目录（开发态）或 PyInstaller 解包目录（frozen）。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")) / "qoder"
    return Path(__file__).resolve().parent / "qoder"


def _qoder_get(path, timeout=5, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{QODER_PORT}{path}",
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


def _qoder_post(path, body, timeout=60, headers=None):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(f"http://127.0.0.1:{QODER_PORT}{path}",
                                 data=json.dumps(body).encode(), method="POST", headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


def qoder_health() -> dict:
    try:
        _s, d = _qoder_get("/health", 5)
        return d if isinstance(d, dict) and d.get("service") == "qoder-proxy" else {}
    except Exception:
        return {}


def qoder_is_up() -> bool:
    return bool(qoder_health().get("ok"))


def qoder_start() -> dict:
    """启动 Qoder 网关（Python 长驻进程，detached）。已在跑则直接返回。"""
    import subprocess
    if qoder_is_up():
        return {"ok": True, "already": True}
    script = _qoder_source_dir() / "qoder_proxy.py"
    if not script.is_file():
        return {"ok": False, "error": f"未找到 Qoder 网关脚本：{script}"}
    if getattr(sys, "frozen", False):
        py = shutil.which("python") or shutil.which("python3")
    else:
        py = sys.executable or shutil.which("python")
    if not py:
        return {"ok": False, "error": "找不到 Python 3.9+（Qoder 网关需要 Python 运行时）"}
    home = qoder_home()
    logf = open(home / "gateway.log", "ab")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen([str(py), str(script),
                      "--port", str(QODER_PORT),
                      "--accounts-dir", str(home / "accounts"),
                      "--usage-dir", str(home / "usage")],
                     cwd=str(script.parent), stdout=logf, stderr=subprocess.STDOUT,
                     creationflags=flags)
    for _ in range(24):
        time.sleep(0.5)
        if qoder_is_up():
            return {"ok": True}
    return {"ok": False, "error": f"Qoder 网关 12 秒内未就绪，看 {home / 'gateway.log'}"}


def qoder_import_desktop() -> dict:
    """把本机已登录 Qoder 客户端的凭证导入网关账号池（面板 API，两步确认里的"全部导入"）。"""
    try:
        _s, login = _qoder_post("/panel/login", {"password": QODER_PANEL_PASSWORD}, 10)
        token = login.get("token") or ""
        if not token:
            return {"ok": False, "error": "Qoder 面板登录失败（默认密码 admin 被改过？）"}
        hdr = {"X-Panel-Token": token}
        _s, scan = _qoder_post("/accounts/import/desktop", {}, 60, hdr)
        valid = [d for d in (scan.get("detected") or []) if d.get("valid")]
        if not valid:
            return {"ok": False, "error": "本机未发现已登录的 Qoder 凭证（先装客户端并登录一次）"}
        _s, imp = _qoder_post("/accounts/import/desktop", {"all": True}, 120, hdr)
        if imp.get("error"):
            return {"ok": False, "error": str(imp.get("error"))[:200]}
        return {"ok": True, "imported": [a.get("nickname") or a.get("uid") for a in (imp.get("imported") or [])]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def qoder_models(timeout: int = 20) -> list:
    """网关实时模型目录 [(id, contextWindow)]；失败返回 []。"""
    try:
        _s, d = _qoder_get("/v1/models", timeout,
                           {"Authorization": f"Bearer {QODER_TOKEN}"})
    except Exception:
        return []
    out = []
    for m in d.get("data") or []:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        try:
            ctx = int(m.get("max_input_tokens") or m.get("context_window") or 200000)
        except (TypeError, ValueError):
            ctx = 200000
        out.append((mid, ctx))
    return out


def zcode_register_qoder() -> dict:
    """向 ZCode 注册 Qoder 网关注册商（OpenAI 兼容，端口见 QODER_PORT）。

    对齐上游 app/main.js 的做法：providerId=qoder-openai-provider，
    api.type=openai-chat-completions，模型取网关实时 /v1/models。幂等。
    """
    cfg_path = Path.home() / ".zcode" / "v2" / "provider_config.json"
    if not cfg_path.is_file():
        return {"ok": False, "error": f"未找到 ZCode 配置：{cfg_path}（确认已安装并运行过 ZCode）"}
    models = qoder_models() or QODER_FALLBACK_MODELS
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    conf = cfg.setdefault("config", {})
    backup = cfg_path.with_suffix(".json.bak-qoder")
    if not backup.exists():
        backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")

    rule = {"providerId": QODER_PROVIDER_ID, "providerName": QODER_PROVIDER_NAME, "enabled": True,
            "config": {"group": "standard-personal",
                       "access": {"type": "api-key", "apiKey": QODER_TOKEN},
                       "api": {"type": "openai-chat-completions", "baseUrl": QODER_BASE_URL},
                       "personalModelIds": [m[0] for m in models],
                       "modelOrder": [m[0] for m in models]}}
    rules = conf.setdefault("providerConfigRules", {}).setdefault("providerRules", [])
    rules[:] = [r for r in rules if r.get("providerId") != QODER_PROVIDER_ID]
    rules.append(rule)
    order = conf.setdefault("providerOrder", [])
    if QODER_PROVIDER_ID not in order:
        order.append(QODER_PROVIDER_ID)
    mcr = conf.setdefault("modelConfigRules", {}).setdefault("providerModelRules", [])
    mcr[:] = [r for r in mcr if r.get("providerId") != QODER_PROVIDER_ID]
    for mid, ctx in models:
        mcr.append({"providerId": QODER_PROVIDER_ID, "modelId": mid,
                    "config": {"enabled": True,
                               "properties": {"contextWindow": ctx,
                                              "inputFormat": {"supportsText": True, "supportsImage": True,
                                                              "supportsVideo": False, "supportsAudio": False,
                                                              "supportsPdf": False},
                                              "outputFormat": {"supportsText": True}},
                               "optionSpecs": {"reasoningLevel": {"values": ["low", "high", "max"]}}}})
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "models": [m[0] for m in models]}


def qoder_smoke_test() -> dict:
    """一发真实推理：确认 Qoder 网关（OpenAI 协议）真的可用。"""
    models = qoder_models(10)
    model = next((m[0] for m in models if "flash" in m[0]), models[0][0]) if models else "qwen3.8-flash"
    body = {"model": model, "max_tokens": 16, "stream": False,
            "messages": [{"role": "user", "content": "reply READY"}]}
    req = urllib.request.Request(f"http://127.0.0.1:{QODER_PORT}/v1/chat/completions",
                                 data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {QODER_TOKEN}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    choices = d.get("choices") or []
    text = "".join(c.get("message", {}).get("content", "") for c in choices)
    if not text:
        u = d.get("usage") or {}
        text = f"model={d.get('model')} out={u.get('completion_tokens')}t"
    return {"ok": r.status == 200 and bool(choices), "reply": text[:60], "model": model}


def relay_oneclick_setup() -> dict:
    """一键反代总入口：凭证 → relay → ZCode 注册 → 验证。幂等，可重复点。"""
    steps = []
    # Qoder 独立网关：先跑，AutoClaw 失败也不影响它的接入
    try:
        q = qoder_start()
        if not q.get("ok"):
            steps.append(f"Qoder 网关启动跳过（{q.get('error')}）")
        else:
            steps.append(f"Qoder 网关已就绪（127.0.0.1:{QODER_PORT}）")
            if not (qoder_health().get("accounts") or 0):
                imp = qoder_import_desktop()
                if imp.get("ok"):
                    steps.append("Qoder 账号池已导入：" + ", ".join(str(x) for x in imp.get("imported") or []))
                else:
                    steps.append(f"Qoder 账号导入未完成（{imp.get('error')}）")
            zq = zcode_register_qoder()
            if zq.get("ok"):
                steps.append("Qoder 网关注册商已注册（重启 ZCode 生效）")
            else:
                steps.append(f"Qoder 注册跳过（{zq.get('error')}）")
    except Exception as e:
        steps.append(f"Qoder 接入异常（{type(e).__name__}），继续")
    if not relay_is_up():
        steps.append("relay 启动")
        r = relay_start()
        if not r.get("ok"):
            return {"ok": False, "error": r.get("error"), "steps": steps}
    steps.append("relay 就绪")
    zc = zcode_register_provider()
    if not zc.get("ok"):
        return {"ok": False, "error": zc.get("error"), "steps": steps}
    steps.append("ZCode 供应商已注册（重启 ZCode 生效）")
    try:
        wb = zcode_register_workbuddy()
        if wb.get("ok"):
            steps.append("WorkBuddy 网关注册商已注册（重启 ZCode 生效）")
        else:
            steps.append(f"WorkBuddy 注册跳过（{wb.get('error')}）")
    except Exception as e:
        steps.append(f"WorkBuddy 注册异常（{type(e).__name__}），继续")
    try:
        entries = []
        for acc in discover_accounts():
            try:
                pts = acc.points() or {}
            except Exception:
                pts = {}
            entries.append({"account": acc, "points": pts.get("total"), "expiring": pts.get("expiring")})
        r = export_cloud_pool(entries)
        steps.append(f"账号池已导出 {r.get('wrote', 0)} 号")
    except Exception as e:
        steps.append(f"多号池导出跳过（单凭证模式）：{type(e).__name__}")
    try:
        smoke = relay_smoke_test()
        if smoke.get("ok"):
            steps.append(f"验证推理通过（{smoke.get('reply', '')}）")
        else:
            return {"ok": False, "error": "反代已起但推理验证失败（看 relay 日志）", "steps": steps}
    except Exception as e:
        return {"ok": False, "error": f"验证推理异常：{e}", "steps": steps}
    try:
        wb_smoke = workbuddy_smoke_test()
        if wb_smoke.get("ok"):
            steps.append(f"WorkBuddy 网关验证通过（{wb_smoke.get('reply', '')}）")
        else:
            steps.append(f"WorkBuddy 网关验证未通过（{wb_smoke.get('reply', '')}），但 AutoClaw 反代已就绪")
    except Exception as e:
        steps.append(f"WorkBuddy 网关验证异常（{type(e).__name__}），但 AutoClaw 反代已就绪")
    try:
        q_smoke = qoder_smoke_test()
        if q_smoke.get("ok"):
            steps.append(f"Qoder 网关验证通过（{q_smoke.get('model')} → {q_smoke.get('reply', '')}）")
        else:
            steps.append(f"Qoder 网关验证未通过（{q_smoke.get('reply', '')}），但 AutoClaw 反代已就绪")
    except Exception as e:
        steps.append(f"Qoder 网关验证异常（{type(e).__name__}），但 AutoClaw 反代已就绪")
    return {"ok": True, "steps": steps}


def warm_unwarmed_accounts(rounds: int = 8) -> dict:
    """对未暖号跑暖号（node 脚本，走真推理建立真人形用量基线）。"""
    node = find_node_exe()
    if not node:
        return {"ok": False, "error": "找不到 node.exe"}
    src_dir = _relay_source_dir() / "warm"
    home = relay_home()
    (home / "warm").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_dir / "warm" / "warm_account.mjs", home / "warm" / "warm_account.mjs")
    results = []
    for acc in discover_accounts():
        marker = Path(getattr(acc, "appdata_dir", "")) / WARM_STATE_FILE
        if not marker.is_file():
            continue  # 无状态文件的存量号视为已暖（与导出闸门同口径）
        st = json.loads(marker.read_text(encoding="utf-8") or "{}")
        if st.get("warmed") is True:
            continue
        tokf = home / "warm" / f".warm_{str(acc.user_id)[:8]}.token"
        # 2.0.2 网关校验 token 新鲜度，warm 脚本要自己现刷：把原料写成 JSON
        # （旧格式裸 token 文本仍被 warm_account.mjs 兼容读取）
        tokf.write_text(json.dumps({
            "auth": acc.token if str(acc.token).lower().startswith("bearer ") else f"Bearer {acc.token}",
            "refresh_token": getattr(acc, "refresh_token", "") or "",
            "device_id": getattr(acc, "device_id", "") or "",
        }, ensure_ascii=False), encoding="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        p = subprocess.run([str(node), str(home / "warm" / "warm_account.mjs"), tokf.name, str(rounds)],
                           capture_output=True, text=True, timeout=1800, cwd=str(home / "warm"),
                           creationflags=flags)
        results.append({"uid": str(acc.user_id)[:8], "name": acc.nickname, "tail": (p.stdout or "")[-200:]})
        try:
            tokf.unlink()
        except Exception:
            pass
    return {"ok": True, "results": results}
