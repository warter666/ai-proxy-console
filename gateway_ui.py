# -*- coding: utf-8 -*-
"""AI 反代面板 —— 四个本地网关的可视化开关（pywebview + ai_hub 注册表）。

设计要点：
- 状态由后台线程并行刷新，前端只读缓存快照，点按钮不会被 4 秒的健康检查卡住。
- 启停串行执行（同一时间只允许一个操作），日志实时回传，适合 AutoClaw 这种要跑
  几十秒的重活。
- 命名互斥量保证单实例；再次双击会尝试把已有窗口拉到前台，而不是再开一个。
- 面板本身不启动、不停止任何网关，除非用户点按钮；关窗口不影响网关。

用法：
    python gateway_ui.py            # 打开面板
    python gateway_ui.py --status   # 只打印一次状态 JSON（排障用）
"""
from __future__ import annotations

import copy
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import datetime  # 今日消耗聚合用到本地日期窗口

try:
    import zstandard
except Exception:  # 没装就自动降级：DSH 统计返回 available=False
    zstandard = None

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import agent_setup as agentsetup  # noqa: E402  （客户端配置接入：预览 / 备份后写入）
import ai_hub as hub  # noqa: E402  （同目录模块，注册表是唯一数据源）
import env_check as envcheck  # noqa: E402  （环境自检：只读，返回分组检查结果）
import gw_specs  # noqa: E402  （网关规格表：端口/能力/token/客户端路径的唯一来源）
import onboard as onboard_mod  # noqa: E402  （一键接入：起网关 + 写配置 + 复检）

APP_TITLE = "AI 反代面板"
MUTEX_NAME = "Local\\AIProxyHub.GatewayPanel"
HTML_PATH = BASE / "gateway_ui.html"

# 以下三项全部来自 gw_specs，不再各自硬编码。
# 加新网关只改 gw_specs.GW_SPECS 一处，这里自动跟上。
PANEL_ORDER = tuple(gw_specs.PANEL_ORDER)
TOKENS = {s["key"]: gw_specs.token_of(s["key"]) for s in gw_specs.GW_SPECS if s.get("token")}
BADGES = {s["key"]: s["badge"] for s in gw_specs.GW_SPECS}

# 面板上的"打开客户端"。路径统一来自 gw_specs（本机路径在 clients.local.json）：
# 有网关的从 GW_SPECS.clients 取；纯客户端的从 CLIENT_ONLY 取。
APP_CANDIDATES = (
    tuple((s["key"], s["full_name"], s["clients"]) for s in gw_specs.GW_SPECS)
    + tuple((c["key"], c["label"], c["clients"]) for c in gw_specs.CLIENT_ONLY)
)

# 用户自己加进面板的客户端（原生文件选择器挑 exe）。
CLIENTS_FILE = BASE / "clients.json"


def _expand_app_path(raw):
    # 本机部分进程上下文里 LOCALAPPDATA/APPDATA 可能为空（实测），expandvars 会展不开——
    # 显式兜底拼接，保证 %LOCALAPPDATA%\... 形态的候选也能命中。
    lad = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    rad = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    out = raw.replace("%LOCALAPPDATA%", lad).replace("%APPDATA%", rad)
    return os.path.expandvars(out)


def _load_custom_clients():
    """读取用户自定义客户端；文件不存在或损坏都当空列表，不影响内置项。"""
    try:
        data = json.loads(CLIENTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("clients") if isinstance(data, dict) else data
    out, seen = [], set()
    for idx, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        key = str(row.get("key") or "").strip() or "custom-%d" % (idx + 1)
        base, n = key, 2
        while key in seen:
            key = "%s-%d" % (base, n)
            n += 1
        seen.add(key)
        out.append({
            "key": key,
            "label": str(row.get("label") or "").strip() or Path(path).stem,
            "path": path,
            "args": str(row.get("args") or "").strip(),
        })
    return out


def _save_custom_clients(clients):
    payload = {"version": 1, "clients": [
        {"key": c["key"], "label": c["label"], "path": c["path"], "args": c.get("args", "")}
        for c in clients
    ]}
    tmp = CLIENTS_FILE.with_name(CLIENTS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(CLIENTS_FILE))


def _build_app_catalog():
    apps = []
    resolved_paths = {}
    used = set()
    for key, label, candidates in APP_CANDIDATES:
        resolved = [_expand_app_path(raw) for raw in candidates]
        resolved_paths[key] = tuple(resolved)
        path = next((c for c in resolved if os.path.isfile(c)), "")
        apps.append({
            "key": key,
            "label": label,
            "path": path,
            "exists": bool(path),
        })
        used.add(key)
    for c in _load_custom_clients():
        key = c["key"]
        if not key or key in used:
            n = 1
            while "custom-%d" % n in used:
                n += 1
            key = "custom-%d" % n
        used.add(key)
        path = _expand_app_path(c["path"])
        resolved_paths[key] = (path,)
        apps.append({
            "key": key,
            "label": c["label"],
            "path": path,
            "exists": os.path.isfile(path),
            "custom": True,
            "args": c.get("args", ""),
        })
    return apps, resolved_paths


APPS, APP_PATHS = _build_app_catalog()


def _refresh_app_catalog():
    global APPS, APP_PATHS
    APPS, APP_PATHS = _build_app_catalog()

# 命令行里带这个标记的进程绝不结束：AutoClaw 中继是 .autoclaw-relay\server.mjs，
# 可能挂在 DSH 的运行时下（曾观察到以 DSH Desktop.exe 身份运行），
# 只按 exe 名/路径关闭 DSH 时会把它一起带走。
_CLOSE_PROTECTED_MARKERS = (".autoclaw-relay",)


def _process_table():
    """一次 CIM 查询拿到 PID / 路径 / 命令行。PowerShell 冷启动约 0.5~1s。"""
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,ExecutablePath,CommandLine | "
        "ConvertTo-Json -Compress -Depth 2"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    try:
        data = json.loads((proc.stdout or b"").decode("utf-8", "replace") or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return [p for p in data if isinstance(p, dict)]


def _app_processes(key):
    """按 exe 全路径（找不到路径时退回 exe 名）挑出该客户端的进程。"""
    paths = APP_PATHS.get(key, ())
    path_set = {p.lower() for p in paths if p}
    name_set = {os.path.basename(p).lower() for p in paths if p}
    matched = []
    for item in _process_table():
        try:
            pid = int(item.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 4 or pid == os.getpid():
            continue
        cmdline = str(item.get("CommandLine") or "")
        if any(marker in cmdline.lower() for marker in _CLOSE_PROTECTED_MARKERS):
            continue
        exe = str(item.get("ExecutablePath") or "").strip().lower()
        name = str(item.get("Name") or "").strip().lower()
        # 有完整路径时必须按路径匹配：CN/INTL 两套 AutoClaw2.exe 同名不同目录，
        # 按名字会把另一区的客户端一起关掉。只有拿不到路径时才退回名字。
        if exe:
            hit = exe in path_set
        else:
            hit = bool(name) and name in name_set
        if hit:
            matched.append(pid)
    return matched


def _alive_pids(pids):
    if os.name != "nt":
        return list(pids)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    alive = []
    for pid in pids:
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            continue
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        if ok and code.value == 259:  # STILL_ACTIVE
            alive.append(int(pid))
    return alive


def _visible_pids(pids, close=False):
    """返回仍有可见顶层窗口的 PID；close=True 时顺带发 WM_CLOSE。"""
    pids = {int(x) for x in pids}
    if os.name != "nt" or not pids:
        return set()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    found = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            found.add(pid.value)
            if close:
                user32.PostMessageW(hwnd, 0x0010, None, None)  # WM_CLOSE
        return True

    user32.EnumWindows(enum_proc, None)
    return found


_WINDOW_SKIP_CLASSES = {
    "IME",
    "MSCTFIME UI",
    "Electron_NotifyIconHostWindow",
    "Electron_SystemPreferencesHostWindow",
    "Base_PowerMessageWindow",
    "crashpad_SessionEndWatcher",
}


def _app_windows(pids):
    """枚举这些 PID 的顶层窗口（含最小化/隐藏），用于唤醒已有实例。"""
    pids = {int(x) for x in pids}
    if os.name != "nt" or not pids:
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def enum_proc(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        cls = ctypes.create_unicode_buffer(256)
        title = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(hwnd, cls, 256)
        user32.GetWindowTextW(hwnd, title, 512)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append({
            "hwnd": hwnd,
            "pid": pid.value,
            "cls": cls.value,
            "title": title.value,
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "iconic": bool(user32.IsIconic(hwnd)),
            "area": max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top),
        })
        return True

    user32.EnumWindows(enum_proc, None)
    return found


def _activate_app_windows(pids):
    """把已有实例的主窗口恢复/置前；找不到可用窗口返回 False。"""
    windows = _app_windows(pids)
    if not windows:
        return False

    def score(w):
        if w["cls"] in _WINDOW_SKIP_CLASSES:
            return -1
        if w["cls"] == "#32770" and not w["title"]:
            return -1
        s = 0
        if w["cls"] == "Chrome_WidgetWin_1":
            s += 100
        if w["title"]:
            s += 40
        if w["visible"]:
            s += 20
        if w["iconic"]:
            s += 5
        if w["area"] > 0:
            s += 10
        return s

    target = max(windows, key=score)
    if score(target) < 0:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

    hwnd = target["hwnd"]
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    user32.BringWindowToTop(hwnd)
    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_cur = kernel32.GetCurrentThreadId()
    # 前台窗口属于别的线程时，SetForegroundWindow 可能被系统拒绝；
    # 短暂附加到那个线程的输入队列可以绕过这个限制。
    if tid_fg and tid_fg != tid_cur:
        user32.AttachThreadInput(tid_cur, tid_fg, True)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(tid_cur, tid_fg, False)
    else:
        user32.SetForegroundWindow(hwnd)
    return True


def _launch_app(path, args=""):
    """按资源管理器双击的方式启动；cwd 以安装目录为基准。args 仅自定义客户端使用。"""
    workdir = os.path.dirname(path)
    # 从 Electron 宿主终端启动面板时会继承 ELECTRON_RUN_AS_NODE，
    # 子进程会以纯 Node 模式跑（无窗口、秒退），启动前临时摘掉。
    node_mode = os.environ.pop("ELECTRON_RUN_AS_NODE", None)
    try:
        if os.name == "nt":
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.ShellExecuteW.argtypes = [
                ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
            ]
            shell32.ShellExecuteW.restype = ctypes.c_void_p
            params = args.strip() or None
            res = shell32.ShellExecuteW(None, "open", path, params, workdir, 1)
            if (res or 0) <= 32:
                raise OSError("ShellExecute 返回 %s" % res)
            return
        import shlex
        subprocess.Popen([path] + shlex.split(args), cwd=workdir, close_fds=True)
    finally:
        if node_mode is not None:
            os.environ["ELECTRON_RUN_AS_NODE"] = node_mode


def _open_app_worker(key, label, path, args=""):
    matched = _app_processes(key)
    if matched and _activate_app_windows(matched):
        state.log("%s 已在运行，已切到前台" % label)
        state.note_result("打开 " + label, True, "已切换到正在运行的窗口")
        return
    try:
        _launch_app(path, args)
    except Exception as exc:
        state.log("启动 %s 失败：%s" % (label, exc), "error")
        state.note_result("打开 " + label, False, "启动失败：%s" % exc)
        return
    # Electron 应用冷启动常见 2~6 秒；进程先出现、窗口后出现，
    # 所以每次都要重新取 PID 再找窗口，不能只查一次。
    for wait in (1.5, 2.5, 4.0):
        time.sleep(wait)
        pids = _app_processes(key)
        if pids and _activate_app_windows(pids):
            state.log("已打开 %s" % label)
            state.note_result("打开 " + label, True, "已启动")
            return
    pids = _app_processes(key)
    if pids:
        state.log("%s 已启动但暂未找到窗口（可能驻留托盘）" % label, "warn")
        state.note_result("打开 " + label, True, "进程已启动，窗口未出现（可能驻留托盘）")
    else:
        state.log("启动 %s 后进程没有出现" % label, "error")
        state.note_result("打开 " + label, False, "启动后进程未出现，可能被安全软件拦截")


def _close_app_worker(key, label):
    matched = _app_processes(key)
    if not matched:
        state.log("%s 没有在运行的进程" % label, "warn")
        state.note_result("关闭 " + label, True, "没有在运行")
        return
    _visible_pids(matched, close=True)
    deadline = time.time() + 5.0
    remaining = _alive_pids(matched)
    while remaining and time.time() < deadline:
        time.sleep(0.3)
        remaining = _alive_pids(remaining)
    if remaining and not _visible_pids(remaining):
        # 窗口已消失但进程还在（托盘/后台子进程）→ 逐个结束匹配到的 PID。
        # 不加 /T：AutoClaw 中继可能挂在 DSH 的进程树里，按树杀会把它一起带走。
        for pid in remaining:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass
        time.sleep(0.5)
        remaining = _alive_pids(remaining)
    if remaining:
        state.log("%s 仍在运行（%d 个进程），可能有未保存内容" % (label, len(remaining)), "warn")
        state.note_result("关闭 " + label, False, "窗口还在，可能等待保存或确认")
    else:
        state.log("已关闭 %s" % label)
        state.note_result("关闭 " + label, True, "已退出")


_closing_keys = set()
_closing_lock = threading.Lock()
_opening_keys = set()
_opening_lock = threading.Lock()


def _secret_scrub(line: str) -> str:
    """日志里顺手抹掉可能被客户端打印出来的长凭据；占位 token 不受影响。"""
    line = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}", r"\1***", line)
    line = re.sub(
        r"(?i)((?:access_?token|refresh_?token|sessionid|api[_-]?key)\s*[=:]\s*)[^\s,;]{8,}",
        r"\1***", line)
    return line[:600]


class Collector:
    """把 hub 里 print / say 的输出按行送到 UI 日志。"""

    def __init__(self, sink):
        self.sink = sink
        self.buf = ""

    def write(self, text):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.strip()
            if line:
                self.sink(line)

    def flush(self):
        line = self.buf.strip()
        self.buf = ""
        if line:
            self.sink(line)


class PanelState:
    def __init__(self):
        self.lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.busy = ""
        self.busy_name = ""
        self.logs = deque(maxlen=240)
        self.log_id = 0
        self.updated = 0.0
        self.stop_event = threading.Event()
        # 动作完成回执：前端按 id 变化弹提示，不依赖瞬时 busy_name 翻转，
        # 否则 Qoder 这种 1 秒内完成的签到会落在两次轮询之间，用户看不到结果。
        self.result_id = 0
        self.last_result = None
        self.snapshot = self._placeholder()

    def note_result(self, label, ok, message):
        with self.lock:
            self.result_id += 1
            self.last_result = {
                "id": self.result_id,
                "label": label,
                "ok": bool(ok),
                "message": str(message or ""),
                "t": time.strftime("%H:%M:%S"),
            }

    def _placeholder(self):
        rows = []
        for key in PANEL_ORDER:
            p = hub.PROVIDERS.get(key)
            if not p:
                continue
            rows.append({
                "key": key,
                "name": p["name"],
                "badge": BADGES.get(key, p["name"][:1].upper()),
                "port": p["port"],
                "proto": p["proto"],
                "url": p["url"],
                "token": TOKENS.get(key, ""),
                "running": False,
                "pid": None,
                "note": "检测中…",
                "latency_ms": None,
                "dashboard": "http://127.0.0.1:%s/" % p["port"] if key == "qoder" else "",
            })
        return rows

    def log(self, text, level="info"):
        clean = _secret_scrub(str(text))
        if not clean:
            return
        with self.lock:
            self.log_id += 1
            self.logs.append({
                "id": self.log_id,
                "t": time.strftime("%H:%M:%S"),
                "text": clean,
                "level": level,
            })

    def payload(self):
        with self.lock:
            data = {
                "providers": [dict(x) for x in self.snapshot],
                "apps": [dict(x) for x in APPS],
                "busy": self.busy,
                "busy_name": self.busy_name,
                "last_result": dict(self.last_result) if self.last_result else None,
                "logs": list(self.logs)[-50:],
                "updated": self.updated,
            }
        # 关闭客户端要等进程真的退出；前端把「关闭中」画在按钮上，
        # 只有这里清空才恢复，不能用瞬时 toast 代替。
        with _closing_lock:
            data["closing"] = sorted(_closing_keys)
        return data


state = PanelState()
_mutex_handle = None


# ---- 外部网关（cc-switch 式手动接入）----
# 用户自己起了一个本地 agent 网关（任何 OpenAI/Anthropic 兼容服务），
# 在面板里填端口+token 即可接入。面板**只做探活与配置展示**，不管理进程——
# 这是与内置四路最大的区别：进程生死归用户自己。
CUSTOM_GW_FILE = BASE / "custom_gateways.json"


def _load_custom_gateways():
    try:
        d = json.loads(CUSTOM_GW_FILE.read_text(encoding="utf-8"))
        gws = d.get("gateways") if isinstance(d, dict) else d
        return [g for g in (gws or []) if isinstance(g, dict) and g.get("port")]
    except Exception:
        return []


def _save_custom_gateways(gws):
    CUSTOM_GW_FILE.write_text(
        json.dumps({"gateways": gws}, ensure_ascii=False, indent=1), encoding="utf-8")


def _probe_custom(gw):
    """轻探活：GET /v1/models（带 token）。返回 (running, note, latency_ms, models_n)。"""
    import urllib.request
    port = gw.get("port")
    proto = (gw.get("proto") or "openai").lower()
    path = "/v1/models" if proto == "openai" else "/v1/models"
    url = "http://127.0.0.1:%s%s" % (port, path)
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer %s" % (gw.get("token") or "x"),
        })
        with urllib.request.urlopen(req, timeout=2.5) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
            models = body.get("data") or body.get("models") or []
            lat = int((time.time() - started) * 1000)
            return True, "外部网关在线，%d 个模型" % len(models), lat, len(models)
    except urllib.error.HTTPError as e:
        lat = int((time.time() - started) * 1000)
        # 401/403 也说明服务在，只是 token 不对
        if e.code in (401, 403):
            return True, "在线（token 被拒绝）", lat, 0
        return False, "HTTP %s" % e.code, lat, 0
    except Exception as exc:
        return False, "%s" % str(exc)[:60], int((time.time() - started) * 1000), 0


def _custom_rows():
    rows = []
    for gw in _load_custom_gateways():
        ok, note, lat, mlist = _probe_custom(gw)
        rows.append({
            "key": gw.get("key") or ("custom-%s" % gw.get("port")),
            "name": gw.get("name") or ("外部网关 %s" % gw.get("port")),
            "badge": (gw.get("name") or "E")[:1].upper(),
            "port": gw.get("port"),
            "proto": gw.get("proto") or "OpenAI",
            "url": "http://127.0.0.1:%s" % gw.get("port"),
            "token": gw.get("token") or "",
            "running": ok,
            "pid": None,
            "note": note,
            "latency_ms": lat if ok else None,
            "dashboard": "",
            "external": True,
            "models_n": mlist,
        })
    return rows


def _check_one(key):
    p = hub.PROVIDERS[key]
    started = time.time()
    try:
        ok, note = p["status"](p)
    except Exception as exc:  # 健康检查失败不该拖垮整张面板
        ok, note = False, "%s: %s" % (type(exc).__name__, exc)
    return key, bool(ok), str(note or ""), time.time() - started


def refresh_status():
    try:
        pids = hub._listen_pids()
    except Exception:
        pids = {}
    keys = [k for k in PANEL_ORDER if k in hub.PROVIDERS]
    results = {}
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(keys))) as pool:
            for key, ok, note, elapsed in pool.map(_check_one, keys):
                results[key] = (ok, note, elapsed)
    except Exception as exc:
        state.log("状态刷新失败：%s: %s" % (type(exc).__name__, exc), "error")
        return
    rows = []
    for key in keys:
        p = hub.PROVIDERS[key]
        ok, note, elapsed = results.get(key, (False, "", 0.0))
        if ok and not note:
            note = "健康检查通过"
        if not ok and not note:
            note = "端口未监听"
        rows.append({
            "key": key,
            "name": p["name"],
            "badge": BADGES.get(key, p["name"][:1].upper()),
            "port": p["port"],
            "proto": p["proto"],
            "url": p["url"],
            "token": TOKENS.get(key, ""),
            "running": ok,
            "pid": pids.get(p["port"]),
            "note": note,
            "latency_ms": int(elapsed * 1000) if ok else None,
            "dashboard": "http://127.0.0.1:%s/" % p["port"] if key == "qoder" else "",
        })
    with state.lock:
        state.snapshot = rows
        # 外部网关追加在后面（探活轻量，不参与启停/签到/消耗）
        try:
            rows.extend(_custom_rows())
        except Exception:
            pass
        state.updated = time.time()


def status_loop():
    while not state.stop_event.is_set():
        refresh_status()
        state.stop_event.wait(4.0)


def _set_busy(key, name):
    with state.lock:
        state.busy = key
        state.busy_name = name


def _run_action(keys, action):
    try:
        for key in keys:
            p = hub.PROVIDERS[key]
            _set_busy(key, p["name"])
            verb = "启动" if action == "start" else "停止"
            state.log("%s %s（:%s）…" % (verb, p["name"], p["port"]))
            collector = Collector(lambda line, k=key: state.log("[%s] %s" % (k, line)))
            started = time.time()
            try:
                with redirect_stdout(collector), redirect_stderr(collector):
                    ok, note = p[action](p)
                    if ok and action == "start" and callable(p.get("post_start")):
                        try:
                            ok2, note2 = p["post_start"](p)
                            if note2:
                                note = "%s；%s" % (note, note2)
                            if not ok2:
                                note = "%s（后置步骤失败：%s）" % (note, note2)
                        except Exception as exc:
                            note = "%s（后置步骤异常：%s: %s）" % (
                                note, type(exc).__name__, exc)
            except Exception as exc:
                ok, note = False, "%s: %s" % (type(exc).__name__, exc)
            collector.flush()
            elapsed = time.time() - started
            state.log(
                "%s %s：%s（%.1fs）" % (verb, p["name"], note or ("完成" if ok else "失败"), elapsed),
                "ok" if ok else "error",
            )
    finally:
        try:
            refresh_status()
        finally:
            _set_busy("", "")
            state.action_lock.release()


def request_action(keys, action):
    keys = [k for k in keys if k in hub.PROVIDERS]
    if not keys:
        return {"ok": False, "message": "没有可操作的网关"}
    if not state.action_lock.acquire(blocking=False):
        with state.lock:
            busy = state.busy_name or "另一个操作"
        return {"ok": False, "message": "%s 正在执行，请稍候" % busy}
    label = "全部网关" if len(keys) > 1 else hub.PROVIDERS[keys[0]]["name"]
    _set_busy(keys[0] if len(keys) == 1 else "all", label)
    threading.Thread(target=_run_action, args=(keys, action), daemon=True).start()
    verb = "启动" if action == "start" else "停止"
    return {"ok": True, "message": "已开始%s %s" % (verb, label)}


# ---- 一键接入向导 ----
# 起网关 + 写配置是几十秒的重活，做成后台任务：前端拿 id 轮询，不阻塞 pywebview 桥。
_ONBOARD_LOCK = threading.Lock()
_ONBOARD_STEPS = (
    ("env", "占位 token"),
    ("gateways", "启动网关"),
    ("configs", "写入配置"),
    ("verify", "验证接入"),
)
_ONBOARD = {
    "id": 0, "running": False, "done": False, "ok": False,
    "clients": [], "steps": [], "result": None, "message": "",
    "started": 0.0, "elapsed": 0.0, "updated": 0.0,
}


def _onboard_steps(clients):
    steps = []
    for key, label in _ONBOARD_STEPS:
        if key == "env":
            items = [{"key": "tokens", "label": "占位 token 环境变量",
                      "state": "pending", "message": ""}]
        elif key == "gateways":
            items = []
            for g in onboard_mod.needed_gateways(clients):
                p = hub.PROVIDERS.get(g) or {}
                items.append({"key": g, "label": p.get("name") or g,
                              "state": "pending", "message": ""})
        elif key == "configs":
            items = [{"key": c, "label": onboard_mod.CLIENT_PLANS[c]["label"],
                      "state": "pending", "message": ""} for c in clients]
        else:
            items = []
            for g in onboard_mod.needed_gateways(clients):
                p = hub.PROVIDERS.get(g) or {}
                items.append({"key": "gw:" + g, "label": "网关 " + (p.get("name") or g),
                              "state": "pending", "message": ""})
            for c in clients:
                items.append({"key": c, "label": onboard_mod.CLIENT_PLANS[c]["label"],
                              "state": "pending", "message": ""})
        steps.append({"key": key, "label": label, "state": "pending", "items": items})
    return steps


def _onboard_emit(step_key, item_key, state, message):
    with _ONBOARD_LOCK:
        for step in _ONBOARD["steps"]:
            if step["key"] != step_key:
                continue
            for item in step["items"]:
                if item["key"] == item_key:
                    item["state"] = state
                    item["message"] = message
                    break
            states = [i["state"] for i in step["items"]]
            if any(s == "running" for s in states):
                step["state"] = "running"
            elif any(s == "fail" for s in states):
                step["state"] = "fail"
            elif any(s == "warn" for s in states):
                step["state"] = "warn"
            elif states and all(s in ("ok", "skip") for s in states):
                step["state"] = "ok"
            else:
                step["state"] = "pending"
            break
        _ONBOARD["updated"] = time.time()


def _onboard_worker(clients):
    try:
        _set_busy("onboard", "一键接入")
        state.log("一键接入开始：%s" % " / ".join(
            onboard_mod.CLIENT_PLANS[c]["label"] for c in clients))
        res = onboard_mod.run(clients, emit=_onboard_emit)
        with _ONBOARD_LOCK:
            _ONBOARD["result"] = res
            _ONBOARD["ok"] = bool(res.get("ok"))
            _ONBOARD["message"] = res.get("message") or ""
            _ONBOARD["elapsed"] = round(time.time() - _ONBOARD["started"], 1)
            _ONBOARD["updated"] = time.time()
            _ONBOARD["done"] = True
        state.log("一键接入完成：%s" % (res.get("message") or ""),
                  "ok" if res.get("ok") else "error")
    except Exception as exc:
        with _ONBOARD_LOCK:
            for step in _ONBOARD["steps"]:
                for item in step["items"]:
                    if item["state"] in ("pending", "running"):
                        item["state"] = "fail" if step["key"] != "configs" else "pending"
                        item["message"] = item["message"] or "任务异常中断"
                if step["state"] != "ok":
                    step["state"] = "fail"
            _ONBOARD["result"] = {"ok": False,
                                  "message": "%s: %s" % (type(exc).__name__, exc)}
            _ONBOARD["ok"] = False
            _ONBOARD["message"] = _ONBOARD["result"]["message"]
            _ONBOARD["elapsed"] = round(time.time() - _ONBOARD["started"], 1)
            _ONBOARD["updated"] = time.time()
            _ONBOARD["done"] = True
        state.log("一键接入异常：%s" % exc, "error")
    finally:
        try:
            refresh_status()
        finally:
            _set_busy("", "")
            try:
                state.action_lock.release()
            except RuntimeError:
                pass
            with _ONBOARD_LOCK:
                _ONBOARD["running"] = False
                _ONBOARD["updated"] = time.time()


def _onboard_payload():
    with _ONBOARD_LOCK:
        return copy.deepcopy(_ONBOARD)


CHECKIN_LABEL = {
    "wb": "WorkBuddy 签到",
    "doubao": "豆包签到",
    "auto": "AutoClaw 认领",
}


def _run_checkin(keys):
    try:
        for key in keys:
            label = CHECKIN_LABEL.get(key, key)
            _set_busy(key, label)
            state.log("开始 %s…" % label)
            collector = Collector(lambda line, k=key: state.log("[%s] %s" % (k, line)))
            started = time.time()
            ok, detail = False, ""
            try:
                with redirect_stdout(collector), redirect_stderr(collector):
                    if key == "qoder":
                        # Qoder 走网关 HTTP 接口；不能落到下面的 else，否则会误跑豆包。
                        ok_n, total, summary = _log_qoder_results(_qoder_checkin_core())
                        ok = ok_n > 0
                        detail = summary if total else "网关里没有可签到的账号"
                        if not total:
                            state.log("Qoder 签到：" + detail, "warn")
                    elif key == "auto":
                        if not AUTOCLAW_SWITCH.is_file():
                            state.log("找不到 a_switch.py：%s" % AUTOCLAW_SWITCH, "error")
                            detail = "找不到 a_switch.py"
                        else:
                            out, err = _run_subprocess([sys.executable, str(AUTOCLAW_SWITCH), "auto"])
                            if err.strip():
                                state.log("stderr: %s" % _trim(err), "warn")
                            ok, detail = True, "认领流程已执行"
                    else:
                        scope = ["--workbuddy"] if key == "wb" else ["--doubao"]
                        out, err = _run_subprocess(
                            [sys.executable, str(BASE / "daily_checkin.py"), "--json"] + scope)
                        if err.strip():
                            state.log("stderr: %s" % _trim(err), "warn")
                        ok, detail = True, "签到流程已执行"
            except Exception as exc:
                detail = "%s: %s" % (type(exc).__name__, exc)
                state.log("%s 异常：%s" % (label, detail), "error")
            collector.flush()
            elapsed = time.time() - started
            state.log("%s 完成（%.1fs）" % (label, elapsed), "ok")
            state.note_result(label, ok, detail or "已结束")
    finally:
        try:
            refresh_status()
        finally:
            _set_busy("", "")
            try:
                state.action_lock.release()
            except RuntimeError:
                pass


# AutoClaw 账号管理 CLI（认领 / 导出池）。默认取引擎仓库（内置 engine/ 或 A_SWITCH_REPO）。
AUTOCLAW_SWITCH = Path(os.environ.get("AUTOCLAW_SWITCH")
                       or (hub.REPO / "a_switch.py"))


def _trim(text, n=240):
    text = (text or "").strip()
    return text[-n:] if len(text) > n else text


def _run_subprocess(cmd):
    """跑一个签到/状态子进程，返回 (stdout, stderr)。超时与异常都兜底，不让面板崩。"""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )
        return (proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired:
        return ("", "subprocess 超时（300s）")
    except Exception as exc:  # 路径不存在 / 环境问题都应降级而非崩溃
        return ("", "subprocess error: %s" % exc)


def _parse_daily_summary(stdout, platform):
    """daily_checkin.py 在 --json 模式下把 summary 打到 stdout（多行 JSON）。"""
    raw = (stdout or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "summary" in data:
                return data["summary"].get(platform)
            if isinstance(data, dict) and platform in data:
                return data.get(platform)
        except Exception:
            pass
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if isinstance(data, dict) and "summary" in data:
            return data["summary"].get(platform)
        if isinstance(data, dict) and platform in data:
            return data.get(platform)
    return None


def _qoder_checkin_core():
    """纯 HTTP：面板登录 -> 全账号签到。不碰 action_lock / busy_name，可被复用。"""
    import urllib.request

    def _post(path, body, hdr=None):
        req = urllib.request.Request(f"http://127.0.0.1:8790{path}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", **(hdr or {})})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    login = _post("/panel/login", {"password": "admin"})
    tok = login.get("token") or ""
    if not tok:
        raise RuntimeError("Qoder 面板登录失败（默认密码 admin 被改过？）")
    res = _post("/accounts/checkin", {"all": True}, {"X-Panel-Token": tok})
    return res.get("results") or []


def _log_qoder_results(results):
    """把 /accounts/checkin 的结果写进面板日志，返回 (成功数, 总数, 摘要)。"""
    ok_n = sum(1 for x in results if (x.get("ok") or x.get("already")))
    total = len(results)
    state.log(f"Qoder 签到完成：{ok_n}/{total} 成功")
    lines = []
    for x in results:
        name = x.get("nickname") or x.get("uid") or "?"
        # message 是「活动领取成功 +100 Credits」这类结果描述；msg 常只是余额，
        # 只在 message 缺失时退回 msg / error。
        msg = x.get("message") or x.get("msg") or x.get("error") or ""
        tick = "✓" if (x.get("ok") or x.get("already")) else "✗"
        state.log(f"  {tick} {name}: {str(msg).strip()[:80]}")
        if msg:
            lines.append(str(msg).strip()[:60])
    summary = "；".join(x for x in lines if x) or ("%d/%d 成功" % (ok_n, total))
    return ok_n, total, summary


def _run_qoder_checkin():
    """后台：Qoder 网关全账号签到（面板「签到」按钮与本函数共用核心）。"""
    label = hub.PROVIDERS["qoder"]["name"]
    with state.lock:
        state.busy_name = label + " 签到"
    ok, detail = False, ""
    try:
        state.log("Qoder 签到开始 …")
        ok_n, total, summary = _log_qoder_results(_qoder_checkin_core())
        ok, detail = (ok_n > 0), summary
        if total == 0:
            ok, detail = False, "网关里没有可签到的账号"
            state.log("Qoder 签到：" + detail, "warn")
    except Exception as e:
        detail = "%s: %s" % (type(e).__name__, e)
        state.log(f"Qoder 签到失败：{detail}", "error")
    finally:
        try:
            refresh_status()
        finally:
            with state.lock:
                state.busy_name = ""
            # 锁由 Api.checkin 在起线程前获取；这里必须归还，否则面板后续全部动作被卡死。
            try:
                state.action_lock.release()
            except RuntimeError:
                pass
        state.note_result(label + " 签到", ok, detail or "已结束")


def _usage_qoder():
    """Qoder 网关今日聚合（POST /panel/login(admin) -> /usage/analytics）。"""
    qo = {"requests": 0, "in": 0, "out": 0, "errors": 0, "cache_pct": None}
    try:
        import urllib.request
        def _qpost(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:8790{path}",
                data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        tok = (_qpost("/panel/login", {"password": "admin"}) or {}).get("token") or ""
        req = urllib.request.Request("http://127.0.0.1:8790/usage/analytics",
                                    headers={"X-Panel-Token": tok})
        with urllib.request.urlopen(req, timeout=15) as r:
            an = (json.loads(r.read().decode("utf-8", "replace")) or {}).get("summary") or {}
        t = an.get("today") or {}
        qo["requests"] = t.get("requests") or 0
        qo["in"] = t.get("prompt_tokens") or 0
        qo["out"] = (t.get("completion_tokens") or 0) + (t.get("reasoning_tokens") or 0)
        qo["errors"] = t.get("errors") or 0
        qo["cache_pct"] = t.get("cache_hit_pct")
    except Exception:
        pass
    return qo


# ---- 网关日志增量 tail（按 path+size+mtime 缓存偏移，只解析新增字节）----
_log_tails = {}          # path(str) -> (size, mtime, offset, last_lines_for_rescan)
_LOG_TAIL_MAX = 32 * 1024 * 1024


def _log_tail_scan(log_path, pattern, keep_line):
    """扫今日命中行，返回 (requests, metric_sum)。增量解析，全量重扫只在文件被轮转/换天时发生。"""
    import re
    rx = re.compile(pattern)
    cnt, metric = 0, 0
    try:
        p = Path(log_path)
        if not p.is_file():
            return cnt, metric
        st = p.stat()
        key = str(p)
        cached = _log_tails.get(key)
        # 文件被截断/换天 → 从头扫；仅增长 → 从旧偏移续扫
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
            start = cached[2]
        else:
            start = 0
        offset = start
        with p.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            if end - start > _LOG_TAIL_MAX:
                start = max(0, end - _LOG_TAIL_MAX)
            f.seek(start)
            for raw in f:
                offset = start + len(raw)  # 近似字节进度（换行切割，足够 30s 级增量）
                try:
                    line = raw.decode("utf-8", "replace")
                except Exception:
                    line = ""
                if keep_line(line):
                    cnt += 1
                    m = rx.search(line)
                    if m:
                        metric += int(m.group(1))
        _log_tails[key] = (st.st_size, st.st_mtime, offset)
    except Exception:
        pass
    return cnt, metric


# ---- DSH 会话回放（解压 session.v4.jsonl.zstd，按 provider 聚合今日 usage）----
_dsh_cache = {}  # path(str) -> (size, mtime, payload)


def _dsh_session_files():
    """枚举本机 DSH 会话文件（harness/sessions/**/session.v4.jsonl.zstd）。"""
    import glob as _glob
    root = Path.home() / "AppData" / "Roaming" / "dsh-desktop" / "harness" / "sessions"
    if not root.is_dir():
        return []
    out = []
    for f in _glob.iglob(str(root / "**" / "session.v4.jsonl.zstd"), recursive=True):
        out.append(Path(f))
    return out


# DSH 里指向本机反代的 provider → 对应面板网关卡片 key。
# 这些流量在网关卡与 DSH 卡各记一次，属于同一笔消耗的两个视角，不可相加。
_DSH_LOCAL_PROVIDERS = {
    "autoclaw": "auto",
    "workbuddy": "wb",
    "qoder": "qoder",
    "doubao": "doubao",
}


def _usage_dsh(today):
    """聚合 DSH 今日消耗：外层一张卡（agent 维度），内层按 provider 分行。

    返回结构（前端直接吃）：
      available  : 有无可解析的会话数据（无 zstandard / 无文件时为 False）
      requests   : 今日总请求数（跨 provider 求和）
      in/out/cache_read : 今日总 token（输入总量=in+cache_read）
      via_gateway / direct : 两组汇总（经本地网关 / 直连上游）
      by_provider: [{provider, requests, in, out, cache_read, models:[...], via_gateway:bool}]
    口径说明：via_gateway 组走本地网关，与网关卡是同一笔消耗的两个视角 → 不可相加；
      direct 组是 DSH 直连上游，仅 DSH 卡可见。
    """
    res = {
        "available": False,
        "requests": 0, "in": 0, "out": 0, "cache_read": 0,
        "by_provider": [],
        "note": "DSH 经 auto/wb/qoder 的消耗已计入本地网关卡，此处不重复累计；"
                "aliyun/deepseek 为 DSH 直连，仅此处可见。",
    }
    if zstandard is None:
        res["note"] = "本机未安装 zstandard，无法解析 DSH 会话；pip install zstandard 后重启面板即可。"
        return res
    files = _dsh_session_files()
    if not files:
        res["note"] = "未找到 DSH 会话文件（%s 下无 session.v4.jsonl.zstd）。" % (
            Path.home() / "AppData" / "Roaming" / "dsh-desktop" / "harness" / "sessions")
        return res
    # 今日窗口（本地时区），避免跨天把昨天尾巴算进来
    try:
        y, m, d = (int(x) for x in today.split("-"))
        start_ms = int(datetime.datetime(y, m, d, 0, 0, 0).timestamp() * 1000)
        end_ms = int(datetime.datetime(y, m, d, 23, 59, 59, 999000).timestamp() * 1000)
    except Exception:
        start_ms = end_ms = 0

    agg = {}  # provider -> {requests, in, out, cache_read, models:set}
    total = {"requests": 0, "in": 0, "out": 0, "cache_read": 0}
    seen_any = False
    for fp in files:
        try:
            st = fp.stat()
        except Exception:
            continue
        cached = _dsh_cache.get(str(fp))
        payload = None
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
            payload = cached[2]
        else:
            try:
                with fp.open("rb") as fh:
                    dctx = zstandard.ZstdDecompressor()
                    with dctx.stream_reader(fh) as rdr:
                        payload = rdr.read().decode("utf-8", "replace")
                _dsh_cache[str(fp)] = (st.st_size, st.st_mtime, payload)
            except Exception:
                continue
        for line in payload.splitlines():
            if not line:
                continue
            if "assistant/message" not in line or "usage" not in line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            ev = obj.get("type") or obj.get("event")
            if ev != "assistant/message":
                continue
            d = obj.get("data") or {}
            u = d.get("usage")
            if not isinstance(u, dict):
                continue
            # 时间过滤（毫秒 epoch）
            tms = obj.get("time")
            if isinstance(tms, (int, float)) and start_ms and not (start_ms <= tms < end_ms):
                continue
            src = (d.get("message") or {}).get("source") or {}
            prov = src.get("provider") or "unknown"
            model = src.get("model") or ""
            it = int(u.get("inputTokens") or 0)
            ot = int(u.get("outputTokens") or 0)
            ct = int(u.get("cacheReadTokens") or 0)
            if it == 0 and ot == 0 and ct == 0:
                continue
            seen_any = True
            rec = agg.setdefault(prov, {"requests": 0, "in": 0, "out": 0, "cache_read": 0, "models": set()})
            rec["requests"] += 1
            rec["in"] += it
            rec["out"] += ot
            rec["cache_read"] += ct
            if model:
                rec["models"].add(model)
            total["requests"] += 1
            total["in"] += it
            total["out"] += ot
            total["cache_read"] += ct
    if not seen_any:
        res["note"] = "今日（%s）DSH 会话暂无可解析的 assistant/message usage。" % today
        return res
    res["available"] = True
    res["requests"] = total["requests"]
    res["in"] = total["in"]
    res["out"] = total["out"]
    res["cache_read"] = total["cache_read"]
    res["by_provider"] = [
        {"provider": p, "requests": v["requests"], "in": v["in"], "out": v["out"],
         "cache_read": v["cache_read"], "models": sorted(v["models"]),
         "via_gateway": p in _DSH_LOCAL_PROVIDERS,
         "gateway_key": _DSH_LOCAL_PROVIDERS.get(p)}
        for p, v in sorted(agg.items())
    ]
    # 两组汇总：经本地网关（与网关卡重复） / 直连上游（仅此处可见）
    def _group(flag):
        g = {"providers": [], "requests": 0, "in": 0, "out": 0, "cache_read": 0}
        for p, v in agg.items():
            if (p in _DSH_LOCAL_PROVIDERS) == flag:
                g["providers"].append(p)
                for k in ("requests", "in", "out", "cache_read"):
                    g[k] += v[k]
        g["providers"].sort()
        return g
    res["via_gateway"] = _group(True)
    res["direct"] = _group(False)
    if res["via_gateway"]["requests"] and res["direct"]["requests"]:
        res["note"] = ("经本地网关 %s 的部分已计入上方各网关卡（同一笔消耗，勿重复相加）；"
                       "%s 为 DSH 直连上游，仅此处可见。"
                       % ("/".join(res["via_gateway"]["providers"]),
                          "/".join(res["direct"]["providers"])))
    return res



_QODER_PANEL = {"token": "", "at": 0.0}
_QODER_PANEL_LOCK = threading.Lock()

QODER_REALM_ORDER = ("cn", "intl", "qworkcn")
QODER_REALM_LABELS = {"cn": "国内版", "intl": "国际版", "qworkcn": "千问办公"}


def _qoder_realm_label(realm):
    return QODER_REALM_LABELS.get(realm or "", realm or "未标注")


def _qoder_gateway_base():
    port = hub.PROVIDERS.get("qoder", {}).get("port", 8790)
    return "http://127.0.0.1:%s" % port


def _qoder_panel_token(force=False):
    """Qoder 面板会话 token（进程内缓存）。

    网关对 /panel/login 有 5 次/分钟的限流，不能每个面板请求都登录一次；
    这里缓存 token，401 时强制重登一次即可。
    """
    with _QODER_PANEL_LOCK:
        if _QODER_PANEL["token"] and not force:
            return _QODER_PANEL["token"]
        password = os.environ.get("QODER_PANEL_PASSWORD") or "admin"
        req = urllib.request.Request(
            _qoder_gateway_base() + "/panel/login",
            data=json.dumps({"password": password}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        token = str(payload.get("token") or "")
        if not token:
            raise RuntimeError("Qoder 面板登录失败（默认密码 admin 被改过？）")
        _QODER_PANEL["token"] = token
        _QODER_PANEL["at"] = time.time()
        return token


def _qoder_panel_call(method, path, body=None, timeout=30, retry=True):
    """带面板 token 调用 Qoder 网关管理接口；401 时重登并重试一次。"""
    headers = {"X-Panel-Token": _qoder_panel_token()}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_qoder_gateway_base() + path, data=data,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and retry:
            _qoder_panel_token(force=True)
            return _qoder_panel_call(method, path, body, timeout, retry=False)
        raise


def _qoder_day(epoch):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(epoch))) if epoch else ""
    except Exception:
        return ""


def _qoder_detail():
    """Qoder 账号池明细：按 CN / 国际版分组，同时返回当前出口。

    关键点：/accounts 不带 realm 时只返回当前出口的账号，必须显式传
    realm=all，否则国际版账号永远不会出现在面板里。
    """
    try:
        data = _qoder_panel_call("GET", "/accounts?realm=all", timeout=15)
        realm_info = _qoder_panel_call("GET", "/realm", timeout=10)
        accs = data.get("accounts") or []
        current = realm_info.get("current") or "cn"
        out = []
        for a in accs:
            c = a.get("credits") or {}
            realm = a.get("realm") or "cn"
            out.append({
                "uid": a.get("uid"),
                "name": a.get("nickname") or a.get("uid"),
                "realm": realm,
                "realm_name": _qoder_realm_label(realm),
                "domain": a.get("domain") or "",
                "plan": a.get("plan") or "—",
                "remain": c.get("remain"),
                "size": c.get("size"),
                "usage_pct": c.get("usage_pct"),
                "credits_expires_text": _qoder_day(c.get("expires_at")),
                "login_expires_text": _qoder_day(a.get("expiresAt")),
                "enabled": bool(a.get("enabled", True)),
                "source": a.get("source") or "",
                "can_checkin": bool(a.get("canCheckin")),
                "last_checkin": a.get("lastCheckin") or "",
                "in_cooldown": bool(a.get("inCooldown")),
                "cooldown_for": a.get("cooldownFor") or "",
                "last_error": a.get("lastError") or "",
                "has_pat": bool(a.get("hasPAT")),
                "token_family": a.get("tokenFamily") or "",
            })
        out.sort(key=lambda x: (QODER_REALM_ORDER.index(x["realm"])
                                if x["realm"] in QODER_REALM_ORDER else 9,
                                str(x["name"] or "").lower()))
        realms = []
        for key in QODER_REALM_ORDER:
            subset = [x for x in out if x["realm"] == key]
            # 国内版/国际版始终列出（0 账号也要能看见），千问办公有账号才显示
            if not subset and key not in ("cn", "intl") and key != current:
                continue
            numerics = [x["remain"] for x in subset
                        if isinstance(x["remain"], (int, float))]
            realms.append({
                "key": key, "name": _qoder_realm_label(key),
                "count": len(subset),
                "enabled": sum(1 for x in subset if x["enabled"]),
                "remain": sum(numerics),
                "has_remain": bool(numerics),
            })
        return {"ok": True, "kind": "qoder",
                "current_realm": current,
                "current_realm_name": _qoder_realm_label(current),
                "accounts": out, "realms": realms,
                "message": "共 %d 个账号（%s）" % (
                    len(out), " / ".join("%s %d" % (r["name"], r["count"])
                                         for r in realms) or "空")}
    except Exception as e:
        return {"ok": False, "kind": "qoder", "message": f"{type(e).__name__}: {e}"}


def _qoder_scan_candidates():
    """只读扫描本机 Qoder CN / 国际版登录态，返回候选（含内部 path）。"""
    scan = _qoder_panel_call("POST", "/accounts/import/desktop", {}, timeout=45)
    detected = scan.get("detected") or []
    pool_uids = set(scan.get("pool_uids") or [])
    current = _qoder_panel_call("GET", "/realm", timeout=10).get("current") or "cn"
    candidates = []
    for item in detected:
        realm = str(item.get("realm") or "")
        if realm not in ("cn", "intl"):
            continue          # 千问办公不属于 Qoder 双区
        path = item.get("path") or ""
        if not item.get("readable") and "not found" in str(item.get("error") or "").lower():
            continue      # 该区客户端没装/没登录，不当作候选
        candidates.append({
            "pick_key": "%s::%s" % (realm, path),
            "realm": realm,
            "realm_name": _qoder_realm_label(realm),
            "kind": item.get("kind") or "",
            "file": item.get("file") or "",
            "nickname": item.get("nickname") or "",
            "uid": item.get("uid") or "",
            "valid": bool(item.get("valid")),
            "readable": bool(item.get("readable")),
            "in_pool": bool(item.get("uid") and item.get("uid") in pool_uids),
            "expires_text": _qoder_day(item.get("expiresAt")),
            "error": item.get("error") or "",
            "_path": path,
        })
    candidates.sort(key=lambda x: (QODER_REALM_ORDER.index(x["realm"]),
                                   str(x["nickname"] or "").lower()))
    return candidates, current


def _qoder_public_candidate(c):
    return {k: v for k, v in c.items() if not k.startswith("_")}


def _doubao_accounts():
    """区分豆包的两条链路各自的 cookie，判断是不是同一个账号。

    背景（2026-10-27 查实）：
      - 签到（daily_checkin.py）读 cookies-consumer.json —— 普通豆包客户端 Doubao.exe
      - 反代（doubao-relay 18770）读 cookies-cdp.json   —— 豆包工作客户端
      两者 sessionid 指纹不同 = 不同账号 → 签到领的额度反代用不上。
    这里只做只读比对，绝不返回 cookie 值本身。
    """
    import hashlib
    d = Path.home() / ".doubao-relay"
    out = {"same_account": None, "gateway": None, "checkin": None}
    for role, name in (("gateway", "cookies-cdp.json"), ("checkin", "cookies-consumer.json")):
        f = d / name
        info = {"file": name, "exists": f.is_file(), "accounts": None,
                "age_min": None, "fp": None, "has_session": False}
        if info["exists"]:
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                jar = raw.get("jar") or {}
                if not jar and isinstance(raw, list):
                    jar = {c.get("name"): c.get("value") for c in raw if isinstance(c, dict)}
                sid = jar.get("sessionid") or ""
                info["has_session"] = bool(sid)
                # 只暴露不可逆指纹前 10 位，用于判断"是否同一账号"，不泄露原值
                info["fp"] = hashlib.sha256(str(sid).encode()).hexdigest()[:10] if sid else None
                info["accounts"] = len(jar)
                saved = raw.get("savedAt") if isinstance(raw, dict) else None
                if saved:
                    try:
                        # savedAt 是 ISO 字符串（2026-10-27T02:30:22.088Z），也兼容 epoch 数字
                        if isinstance(saved, (int, float)):
                            ts = saved / 1000.0 if saved > 1e11 else saved
                        else:
                            s = str(saved).replace("Z", "+00:00")
                            dt = datetime.datetime.fromisoformat(s)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=datetime.timezone.utc)
                            ts = dt.timestamp()
                        info["age_min"] = int((time.time() - ts) / 60)
                    except Exception:
                        pass
            except Exception as exc:
                info["error"] = str(exc)
        out[role] = info
    g, c = out["gateway"], out["checkin"]
    if g and c and g.get("fp") and c.get("fp"):
        out["same_account"] = (g["fp"] == c["fp"])
    out["split"] = (out["same_account"] is False)
    return out


def _agenthub_dsh_status(project_root):
    """扫描项目根下所有项目，报告各自的 .dsh\\skills 联接状态。

    DSH 与另外四个 agent 不同：它按**项目**发现技能，
    `<项目>\\.dsh\\skills` 必须是指向 store 的 junction，否则该项目看不到共享技能。
    """
    import os as _os
    root = Path(project_root or (AGENTHUB_DIR.parent / "Code" / "Projects"))
    if not root.is_dir():
        return {"ok": False, "message": "项目根不存在：%s" % root,
                "root": str(root), "projects": []}
    # 与 agent-hub.ps1 的 scan-projects 保持同一套判定
    noise = {"build", "dist", "node_modules", "bin", "obj", "target", "out",
             "NVIDIA Corporation", ".vs", ".idea"}
    markers = (".git", "package.json", "pyproject.toml", "Cargo.toml",
               "go.mod", "README.md", "AGENTS.md", "CLAUDE.md", ".dsh")
    store = (AGENTHUB_DIR / "skills")
    try:
        want = str(store.resolve()).lower() if store.is_dir() else ""
    except Exception:
        want = ""
    out = []
    skipped = 0
    for d in sorted(root.iterdir()):
        try:
            if not d.is_dir():
                continue
        except Exception:
            continue
        if d.name.startswith("_") or d.name.startswith(".") or d.name in noise:
            skipped += 1
            continue
        if not any((d / m).exists() for m in markers):
            skipped += 1
            continue
        p = d / ".dsh" / "skills"
        exists = p.exists()
        is_link, target = _junction_target(p) if exists else (False, "")
        unified = bool(is_link and want and target.lower() == want)
        if not exists:
            state = "missing"
        elif unified:
            state = "ok"
        elif is_link:
            state = "wrong-target"
        else:
            state = "plain-dir"
        out.append({"name": d.name, "path": str(d), "dsh": str(p),
                    "state": state, "target": target, "exists": exists})
    fixed = sum(1 for x in out if x["state"] == "ok")
    return {"ok": True, "root": str(root), "projects": out,
            "total": len(out), "ok_count": fixed,
            "need_count": len(out) - fixed, "skipped": skipped,
            "store": str(store)}


def _autoclaw_detail():
    """读 AutoClaw 云池快照（账号名/积分/临期/在线），绝不外传 token。"""
    pool = Path.home() / ".openclaw-autoclaw" / "aswitch_cloud_pool.json"
    if not pool.is_file():
        return {"ok": False, "kind": "autoclaw",
                "message": "账号池文件不存在，先运行 AutoClaw 暖号导出"}
    try:
        data = json.loads(pool.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "kind": "autoclaw", "message": "读取账号池失败：%s" % exc}
    accounts = []
    for a in data.get("accounts", []):
        accounts.append({
            "name": a.get("name") or a.get("uid"),
            "uid": a.get("uid"),
            "points": a.get("points"),
            "expiring": a.get("expiring"),
            # is_live 语义 = 「该号是 AutoClaw 桌面端当前登录的号」，
            # 不是在线/离线 —— 池内所有账号凭证都是可用状态。
            "is_live": bool(a.get("is_live", False)),
        })
    # 池文件里的 points 是 GUI 上次导出时的快照，注明时间避免误读
    at = data.get("at")
    snapshot_at = time.strftime("%m-%d %H:%M", time.localtime(at)) if at else "未知"
    return {"ok": True, "kind": "autoclaw", "accounts": accounts,
            "snapshot_at": snapshot_at,
            "message": "共 %d 个账号（快照 %s）" % (len(accounts), snapshot_at)}


# ---- AgentHub 集成（只读检测 + 调用已有脚本，不重复实现其能力）----
# AgentHub 负责「agent 会什么」（skills/记忆/项目规则），本面板负责「agent 用什么模型」。
# 这里只做两件事：① 直接读文件系统判断 junction 是否完好；② 需要变更时调 agent-hub.ps1。
# 为什么不解析 agent-hub.ps1 status 的 stdout：那是给人看的文本，格式一变就崩；
# 文件系统状态才是权威事实，判断 junction 指向比解析中文字符串可靠得多。
def _default_agenthub_dir():
    """AgentHub 是可选的外部目录：环境变量优先，否则取仓库同级的 AgentHub。"""
    env = os.environ.get("AGENTHUB_DIR")
    if env:
        return Path(env)
    return BASE.parent / "AgentHub"


AGENTHUB_DIR = _default_agenthub_dir()
AGENTHUB_SCRIPT = AGENTHUB_DIR / "scripts" / "agent-hub.ps1"

# 五个技能来源 → 各自 skills 目录。原目录迁移后应是指向 <hub>/skills 的 junction。
_AGENTHUB_SOURCES = (
    ("claude", "Claude Code", ".claude", "skills"),
    ("codex", "Codex", ".codex", "skills"),
    ("qoder", "Qoder", ".qoder-cn", "skills"),
    ("zcode", "ZCode", ".zcode", "skills"),
    ("workbuddy", "WorkBuddy", ".workbuddy", "skills"),
)

# 记忆入口：接入后应含 agent-hub:begin/end 托管块
_AGENTHUB_MEMORY = (
    ("qoder", "Qoder", ".qoder-cn", "memory/MEMORY.md"),
    ("claude", "Claude Code", ".claude", "CLAUDE.md"),
    ("codex", "Codex", ".codex", "AGENTS.md"),
)


def _junction_target(path: Path):
    """返回 (是否联接, 实际目标)。

    junction 不能用 os.path.realpath 判断：realpath 会一路解析到最终目标，
    使 realpath(path) 与 path 的解析结果相同，is_link 恒为 False。
    Windows 上 st_reparse_tag / os.readlink 才是权威判据。
    """
    try:
        if not path.exists():
            return False, ""
        # os.readlink 对 junction 返回目标路径（Python 3.2+ 在 Windows 支持）
        try:
            tgt = os.readlink(str(path))
            if tgt:
                # junction 目标形如 \\?\D:\AgentHub\skills，去掉长路径前缀
                if tgt.startswith("\\\\?\\"):
                    tgt = tgt[4:]
                return True, str(Path(tgt))
        except OSError:
            pass
        # 兜底：st_reparse_tag == IO_REPARSE_TAG_MOUNT_POINT(0xA0000003) 即 junction
        st = path.lstat()
        tag = getattr(st, "st_reparse_tag", 0)
        if tag in (0xA0000003, 0xA000000C):  # mount point / symlink
            return True, str(Path(os.path.realpath(str(path))))
        return False, ""
    except Exception:
        return False, ""


def _agenthub_status():
    """AgentHub 底座状态：store 规模 + 五个来源联接 + 记忆入口接入。只读，不修改任何文件。"""
    res = {
        "ok": True,
        "hub": str(AGENTHUB_DIR),
        "available": AGENTHUB_DIR.is_dir(),
        "store": None, "sources": [], "memory": [],
        "script_found": AGENTHUB_SCRIPT.is_file(),
        "broken": 0,
        "message": "",
    }
    if not res["available"]:
        res["ok"] = False
        res["message"] = "未找到 AgentHub 目录：%s（可选集成；用 AGENTHUB_DIR 指定）" % AGENTHUB_DIR
        return res

    store = AGENTHUB_DIR / "skills"
    if store.is_dir():
        try:
            entries = [p for p in store.iterdir() if p.is_dir()]
            n_files = 0
            total = 0
            for p in store.rglob("*"):
                if p.is_file():
                    n_files += 1
                    try:
                        total += p.stat().st_size
                    except Exception:
                        pass
            res["store"] = {"path": str(store), "entries": len(entries),
                            "files": n_files, "mb": round(total / 1048576.0, 1)}
        except Exception as exc:
            res["store"] = {"path": str(store), "entries": 0, "files": 0,
                            "mb": 0, "error": str(exc)}

    # 用 realpath 比较：D:\AgentHub 是兼容 junction，旧联接指向它也算统一
    want = os.path.normcase(os.path.realpath(str(store))) if store.is_dir() else ""
    for key, label, home_sub, leaf in _AGENTHUB_SOURCES:
        p = Path.home() / home_sub / leaf
        exists = p.exists()
        is_link, target = _junction_target(p) if exists else (False, "")
        target_norm = os.path.normcase(os.path.realpath(target)) if target else ""
        unified = bool(is_link and want and target_norm == want)
        if not exists:
            state = "missing"
        elif unified:
            state = "ok"
        elif is_link:
            state = "wrong-target"   # 指向别处（例如换机后遗留）
        else:
            state = "plain-dir"      # agent 重建了目录，联接已断
        if state in ("missing", "wrong-target", "plain-dir"):
            res["broken"] += 1
        res["sources"].append({
            "key": key, "label": label, "path": str(p), "state": state,
            "target": target, "exists": exists, "unified": unified,
        })

    for key, label, home_sub, rel in _AGENTHUB_MEMORY:
        p = Path.home() / home_sub / rel
        exists = p.is_file()
        hooked = False
        if exists:
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
                hooked = "agent-hub:begin" in txt
            except Exception:
                pass
        res["memory"].append({
            "key": key, "label": label, "path": str(p),
            "exists": exists, "hooked": hooked,
            "state": ("ok" if hooked else ("plain" if exists else "missing")),
        })
    if res["broken"]:
        res["message"] = "%d 个技能来源联接异常，建议重新 link-skills" % res["broken"]
    else:
        res["message"] = "五个技能来源均已统一到 store"
    return res


# ---- 启动与端口 ----
# 自启只登记面板本身，不登记网关：网关要不要跑由用户在面板里点。
# 写 HKCU 而不是计划任务：不需要管理员权限，卸载时删一个值即可。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# 不能叫 AIProxyPanel：本机安全软件（火绒 usysdiag）会把这个名字的启动项
# 写进去后立刻删掉（改名后同一条命令即可稳定保留，2026-10-28 实测）。
RUN_VALUE = "AIProxyHubPanel"
_LEGACY_RUN_VALUES = ("AIProxyPanel",)
# 任务管理器 / 设置里的启动项开关存在这里；03 开头 = 被系统禁用。
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
_APPROVED_ENABLED = b"\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"

# 端口归因：监听进程命令行里出现这些标记，就算该网关自己的进程。
# 与 ai_hub 的启动方式对应（relay 部署在用户目录，路径里带标记）。
_GATEWAY_MARKERS = {
    "auto": (".autoclaw-relay",),
    "wb": ("workbuddy-proxy.js",),
    "qoder": (".qoder-relay", "qoder_proxy.py"),
    "doubao": ("relay.mjs",),
}

_PORT_CACHE = {"t": 0.0, "rows": None}


def _autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s"' % Path(sys.executable)
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    if pyw.is_file():
        exe = pyw
    return '"%s" "%s"' % (exe, BASE / "gateway_ui.py")


def _norm_cmd(cmd):
    return " ".join(str(cmd or "").replace('"', " ").split()).lower()


def _approved_state():
    """任务管理器 / 设置里的启用状态；None = 没有单独记录（跟随 Run）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as k:
            try:
                raw, _type = winreg.QueryValueEx(k, RUN_VALUE)
            except FileNotFoundError:
                return None
    except OSError:
        return None
    if not isinstance(raw, bytes):
        return None
    return raw[:1] != b"\x03"


def _write_approved(enabled):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_BINARY, _APPROVED_ENABLED)
        else:
            try:
                winreg.DeleteValue(k, RUN_VALUE)
            except FileNotFoundError:
                pass


def _startup_status():
    expected = _autostart_command()
    res = {
        "ok": True, "supported": os.name == "nt", "enabled": False,
        "matches": False, "approved": None, "value": RUN_VALUE,
        "command": expected, "stored": "", "message": "",
    }
    if os.name != "nt":
        res["ok"] = False
        res["message"] = "仅支持 Windows 开机自启"
        return res
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            try:
                value, _type = winreg.QueryValueEx(k, RUN_VALUE)
            except FileNotFoundError:
                value = ""
    except OSError as exc:
        res["ok"] = False
        res["message"] = "读取注册表失败：%s" % exc
        return res
    stored = str(value or "")
    res["enabled"] = bool(stored)
    res["stored"] = stored
    res["matches"] = bool(stored) and _norm_cmd(stored) == _norm_cmd(expected)
    res["approved"] = _approved_state() if stored else None
    if not stored:
        res["message"] = "未开启"
    elif not res["matches"]:
        res["message"] = "已登记，但命令与当前面板不一致（换过 Python 或移动过目录）"
    elif res["approved"] is False:
        res["message"] = "已登记，但被系统启动项管理禁用（任务管理器 / 设置里重新启用）"
    else:
        res["message"] = "已开启"
    return res


def _startup_set(enabled):
    if os.name != "nt":
        return {"ok": False, "message": "仅支持 Windows 开机自启"}
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            for legacy in _LEGACY_RUN_VALUES:
                try:
                    winreg.DeleteValue(k, legacy)
                except FileNotFoundError:
                    pass
            if enabled:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        _write_approved(enabled)
    except OSError as exc:
        return {"ok": False, "message": "写入注册表失败：%s" % exc}
    if not enabled:
        after = _startup_status()
        return {"ok": True, "message": "已关闭开机自启", "startup": after}
    # 安全软件可能在毫秒级删掉启动项，写完稍等再读，别把假成功报给用户。
    time.sleep(0.3)
    after = _startup_status()
    if not after.get("ok"):
        return after
    if not after["enabled"] or not after["matches"]:
        return {"ok": False, "startup": after,
                "message": "启动项写入后被立即删除，多半是安全软件拦截了 %s；"
                           "请在安全软件的启动项管理里允许，或换个值名" % RUN_VALUE}
    if after.get("approved") is False:
        return {"ok": False, "startup": after,
                "message": "启动项被系统启动项管理禁用，请在任务管理器 / 设置里手动启用"}
    return {"ok": True, "startup": after,
            "message": "已开启开机自启（登录后只打开面板，不自动启动网关）"}


# ---- 每日签到计划任务 ----
# 「安装签到任务」注册的就是 Windows 计划任务 AI每日签到；面板只改同一个任务，
# 不另建第二份，桌面上的安装 EXE 与面板入口因此始终指向同一条触发时间。
CHECKIN_TASK_NAME = "AI每日签到"
CHECKIN_TASK_SCRIPT = BASE / "install_checkin_task.ps1"
CHECKIN_AT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _checkin_task_run(*args, timeout=60):
    """调用 install_checkin_task.ps1，返回 (returncode, stdout, stderr)。"""
    if not CHECKIN_TASK_SCRIPT.is_file():
        return 1, "", "找不到 %s" % CHECKIN_TASK_SCRIPT
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(CHECKIN_TASK_SCRIPT)] + list(args),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return 1, "", "%s: %s" % (type(exc).__name__, exc)
    return (proc.returncode,
            (proc.stdout or b"").decode("utf-8", "replace"),
            (proc.stderr or b"").decode("utf-8", "replace"))


def _parse_json_obj(text):
    raw = (text or "").strip()
    for line in reversed([x.strip() for x in raw.splitlines() if x.strip()]):
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except Exception:
            pass
    return None


def _checkin_result_text(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "尚未运行"
    if code == 0:
        return "成功"
    if code == 267011:
        return "尚未运行"
    if code == 267009:
        return "正在运行"
    if code == 267014:
        return "被手动终止"
    return "失败（退出码 %d）" % code


def _checkin_status_from(data):
    res = {
        "ok": True, "supported": True, "exists": False,
        "task": CHECKIN_TASK_NAME, "state": "", "time": "",
        "last_run": "", "last_result": None, "next_run": "",
        "action": "", "arguments": "", "script": str(CHECKIN_TASK_SCRIPT),
        "message": "",
    }
    for key in ("exists", "task", "state", "time", "last_run",
                "last_result", "next_run", "action", "arguments"):
        if key in data:
            res[key] = data[key]
    res["exists"] = bool(res["exists"])
    res["last_result_text"] = _checkin_result_text(res["last_result"])
    if not res["exists"]:
        res["message"] = "未安装定时签到"
    elif res["state"] == "Disabled":
        res["message"] = "已安装，但任务被禁用"
    elif res["time"]:
        res["message"] = "每天 %s 自动签到" % res["time"]
    else:
        res["message"] = "已安装（无法解析触发时间）"
    return res


def _checkin_task_status():
    if os.name != "nt":
        return {"ok": False, "supported": False, "task": CHECKIN_TASK_NAME,
                "message": "仅支持 Windows 计划任务"}
    code, out, err = _checkin_task_run("-Status")
    data = _parse_json_obj(out)
    if not isinstance(data, dict):
        return {"ok": False, "task": CHECKIN_TASK_NAME,
                "message": "读取计划任务失败：%s" % _trim(err or out)}
    return _checkin_status_from(data)


def _checkin_task_install(at):
    at = str(at or "").strip()
    if not CHECKIN_AT_RE.match(at):
        return {"ok": False, "message": "时间格式应为 HH:mm（00:00–23:59）"}
    code, out, err = _checkin_task_run("-At", at)
    data = _parse_json_obj(out)
    status = _checkin_status_from(data) if isinstance(data, dict) and "exists" in data \
        else _checkin_task_status()
    if code != 0:
        return {"ok": False, "schedule": status,
                "message": "注册计划任务失败：%s" % _trim(err or out, 400)}
    if not status.get("exists"):
        return {"ok": False, "schedule": status,
                "message": "命令已执行，但没有读到计划任务"}
    if status.get("time") != at:
        return {"ok": False, "schedule": status,
                "message": "触发时间与预期不一致：当前 %s（期望 %s）" % (
                    status.get("time") or "未知", at)}
    return {"ok": True, "schedule": status,
            "message": "已设为每天 %s 自动签到（错过开机后补跑）" % at}


def _checkin_task_remove():
    if os.name != "nt":
        return {"ok": False, "message": "仅支持 Windows 计划任务"}
    if not CHECKIN_TASK_SCRIPT.is_file():
        return {"ok": False, "message": "找不到 %s" % CHECKIN_TASK_SCRIPT}
    code, out, err = _checkin_task_run("-Remove")
    data = _parse_json_obj(out)
    status = _checkin_status_from(data) if isinstance(data, dict) and "exists" in data \
        else _checkin_task_status()
    if code != 0:
        return {"ok": False, "schedule": status,
                "message": "移除计划任务失败：%s" % _trim(err or out, 400)}
    if status.get("exists"):
        return {"ok": False, "schedule": status, "message": "命令已执行，但计划任务仍在"}
    return {"ok": True, "schedule": status, "message": "已移除定时签到（手动签到不受影响）"}


def _port_rows(force=False):
    now = time.time()
    if not force and _PORT_CACHE["rows"] is not None and now - _PORT_CACHE["t"] < 5:
        return _PORT_CACHE["rows"]
    try:
        listen = hub._listen_pids()
    except Exception:
        listen = {}
    procs = {}
    if listen:
        for item in _process_table():
            try:
                procs[int(item.get("ProcessId") or 0)] = item
            except (TypeError, ValueError):
                continue
    with state.lock:
        snapshot = {r["key"]: r for r in state.snapshot}
        checked = state.updated > 0
    rows = []
    for key in PANEL_ORDER:
        p = hub.PROVIDERS.get(key)
        if not p:
            continue
        port = p["port"]
        pid = listen.get(port)
        row = {"key": key, "name": p["name"], "port": port, "pid": pid,
               "proc": "", "state": "free", "text": "空闲", "note": ""}
        if pid:
            info = procs.get(pid) or {}
            cmd = str(info.get("CommandLine") or "")
            row["proc"] = str(info.get("Name") or "")
            markers = _GATEWAY_MARKERS.get(key, ())
            owned = any(m in cmd.lower() for m in markers)
            healthy = bool((snapshot.get(key) or {}).get("running"))
            if owned or healthy:
                row["state"], row["text"] = "gateway", "本网关"
                if checked and not healthy:
                    row["note"] = "监听中，但健康检查未通过"
            else:
                row["state"], row["text"] = "other", "被其他进程占用"
        else:
            # 未监听但健康检查通过 = 快照还没刷新，别急着报空闲
            if checked and (snapshot.get(key) or {}).get("running"):
                row["state"], row["text"], row["note"] = "unknown", "检测中", ""
        rows.append(row)
    _PORT_CACHE["t"] = now
    _PORT_CACHE["rows"] = rows
    return rows


def _agenthub_run(command, apply=False):
    """调用 agent-hub.ps1 执行变更。默认 dry-run，apply=True 才真改文件。"""
    if not AGENTHUB_SCRIPT.is_file():
        return {"ok": False, "message": "找不到 agent-hub.ps1：%s" % AGENTHUB_SCRIPT}
    args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(AGENTHUB_SCRIPT), command]
    if apply:
        args.append("-Apply")
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=180,
                           encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
        return {"ok": p.returncode == 0, "output": out[:4000],
                "dry_run": not apply, "command": command}
    except Exception as exc:
        return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}


class Api:
    """pywebview 暴露给前端的接口。所有方法都应快速返回，重活放到后台线程。"""

    _window = None   # main() 里创建窗口后回填，供 pick_folder 弹原生对话框

    def status(self):
        return state.payload()

    def gw_specs(self):
        """网关规格表（能力位/端口/占位 token/颜色）。前端不再硬编码 agent 列表。

        加新网关时前端自动跟上：卡片、消耗行、签到按钮、抽屉全部按 caps 决定。
        """
        try:
            return {"ok": True, "specs": gw_specs.public_specs(),
                    "panel_order": gw_specs.PANEL_ORDER}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def agenthub_status(self):
        """AgentHub 底座状态（只读）：store 规模 + 技能来源联接 + 记忆入口。"""
        try:
            return _agenthub_status()
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def env_check(self, force=False):
        """环境自检（只读）：运行环境 / 网关依赖 / 客户端配置 / 占位 token。

        结果在后端缓存 60s；force=True 时重扫（前端「重新检测」按钮）。
        """
        try:
            return envcheck.probe(APPS, APP_PATHS, force=bool(force))
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def agent_setup_status(self):
        """各客户端配置接入状态（只读）。"""
        try:
            return agentsetup.status()
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def agent_setup_preview(self, key, add_missing=False):
        """预览要改的字段；Claude/Codex 返回可复制片段。只读，不写文件。"""
        try:
            return agentsetup.preview(str(key), add_missing=bool(add_missing))
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def agent_setup_apply(self, key, add_missing=False):
        """备份后写入 DSH / ZCode；Claude/Codex 只返回片段，拒绝自动写。"""
        if not state.action_lock.acquire(blocking=False):
            return {"ok": False, "message": "另一个操作正在执行，请稍候"}
        try:
            res = agentsetup.apply(str(key), add_missing=bool(add_missing))
            if res.get("ok"):
                state.log("配置接入 %s：%s" % (key, res.get("message") or "已写入"))
            return res
        except Exception as exc:
            state.log("配置接入 %s 失败：%s" % (key, exc), "error")
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            try:
                state.action_lock.release()
            except RuntimeError:
                pass

    # ---- 一键接入 ----
    def onboard_plan(self, force=False):
        """向导第一步：检测客户端 / 网关 / 环境。只读，不启动任何进程。"""
        try:
            return onboard_mod.plan(APPS, APP_PATHS, force=bool(force))
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def onboard_start(self, clients):
        """向导执行：起网关 + 写配置。后台线程执行，前端轮询 onboard_status。"""
        if not isinstance(clients, list):
            return {"ok": False, "message": "参数错误：clients 需要列表"}
        keys = [k for k in onboard_mod.CLIENT_PLANS if k in {str(c) for c in clients}]
        if not keys:
            return {"ok": False, "message": "至少选择一个客户端"}
        with _ONBOARD_LOCK:
            if _ONBOARD["running"]:
                return {"ok": False, "message": "一键接入正在执行，请等它结束"}
        if not state.action_lock.acquire(blocking=False):
            with state.lock:
                busy = state.busy_name or "另一个操作"
            return {"ok": False, "message": "%s 正在执行，请稍候" % busy}
        with _ONBOARD_LOCK:
            _ONBOARD.update({
                "id": _ONBOARD["id"] + 1,
                "running": True, "done": False, "ok": False,
                "clients": keys, "steps": _onboard_steps(keys),
                "result": None, "message": "正在接入…",
                "started": time.time(), "elapsed": 0.0, "updated": time.time(),
            })
            job_id = _ONBOARD["id"]
        threading.Thread(target=_onboard_worker, args=(keys,), daemon=True).start()
        return {"ok": True, "job": job_id, "clients": keys}

    def onboard_status(self):
        """向导进度 / 结果。前端每 700ms 轮询一次。"""
        return {"ok": True, "job": _onboard_payload()}

    def agenthub_run(self, command, apply=False):
        """调用 agent-hub.ps1。默认 dry-run；apply=True 才真改文件。

        只做转发，不自己实现 link-skills / init-project —— 变更逻辑留在 AgentHub 一处，
        面板另写一套必然与其演化脱节。
        """
        allowed = {"link-skills", "unlink-skills", "point-memory", "status"}
        if command not in allowed:
            return {"ok": False, "message": "不支持的命令：%s" % command}
        # 改文件的操作必须串行，避免与网关启停互相干扰
        if apply and not state.action_lock.acquire(blocking=False):
            return {"ok": False, "message": "另一个操作正在执行，请稍候"}
        try:
            res = _agenthub_run(command, bool(apply))
            state.log(("执行" if apply else "演练") + " agent-hub " + command)
            return res
        finally:
            if apply:
                try:
                    state.action_lock.release()
                except RuntimeError:
                    pass

    def pick_folder(self, initial=""):
        """弹出系统原生文件夹选择框，返回用户选中的绝对路径。

        用 pywebview 的原生对话框（不是浏览器的 <input webkitdirectory>）——
        后者只能选"已存在的文件夹"且拿不到绝对路径，做不了"安装目录"式交互。
        """
        import webview
        try:
            win = self._window
            if win is None:
                return {"ok": False, "message": "窗口尚未就绪"}
            # 新版本用 FileDialog.FOLDER，旧版本用 FOLDER_DIALOG
            dialog_type = getattr(webview, "FOLDER_DIALOG", None)
            try:
                fd = getattr(webview, "FileDialog", None)
                if fd is not None and hasattr(fd, "FOLDER"):
                    dialog_type = fd.FOLDER
            except Exception:
                pass
            if dialog_type is None:
                return {"ok": False, "message": "当前 pywebview 不支持文件夹选择"}
            start = initial or str(Path.home())
            if not Path(start).is_dir():
                start = str(Path.home())
            picked = win.create_file_dialog(dialog_type, directory=start)
            if not picked:
                return {"ok": False, "canceled": True, "message": "已取消"}
            # 不同版本返回 string / list / tuple
            if isinstance(picked, (list, tuple)):
                picked = picked[0] if picked else ""
            if not picked:
                return {"ok": False, "canceled": True, "message": "已取消"}
            return {"ok": True, "path": str(Path(picked))}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def agenthub_migrate_store(self, path):
        """换技能库位置并**自动搬运**现有技能。

        等价于 `agent-hub.ps1 migrate-store -SetStore <path> -Apply`：
        同盘即刻完成，跨盘先复制再删源。旧位置若确认无误可手动删。
        迁移会在后台线程跑（大批文件可能很慢），完成后状态由面板轮询反映。
        """
        p = (path or "").strip()
        if not p:
            return {"ok": False, "message": "路径为空"}
        try:
            target = Path(p).expanduser()
            if not target.is_absolute():
                return {"ok": False, "message": "需要绝对路径"}
        except Exception as exc:
            return {"ok": False, "message": "路径无效：%s" % exc}
        if not AGENTHUB_SCRIPT.is_file():
            return {"ok": False, "message": "找不到 agent-hub.ps1"}
        if not state.action_lock.acquire(blocking=False):
            return {"ok": False, "message": "另一个操作正在执行，请稍候"}
        try:
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(AGENTHUB_SCRIPT), "migrate-store",
                    "-SetStore", str(target), "-Apply"]
            r = subprocess.run(args, capture_output=True, text=True, timeout=1800,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            ok = r.returncode == 0
            state.log(("技能库已迁移 -> %s" % target) if ok else "技能库迁移失败",
                      "info" if ok else "warn")
            return {"ok": ok, "output": out.strip()[-3000:], "path": str(target)}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            try:
                state.action_lock.release()
            except RuntimeError:
                pass

    def agenthub_set_store(self, path):
        """把技能库(store)位置写入 AgentHub 的 config.json。

        等价于 `agent-hub.ps1 config -SetStore <path>`，但不搬文件——
        只改配置。调用方要先提醒用户：换位置后旧技能不会自动搬过去。
        """
        p = (path or "").strip()
        if not p:
            return {"ok": False, "message": "路径为空"}
        try:
            target = Path(p).expanduser()
            if not target.is_absolute():
                return {"ok": False, "message": "需要绝对路径"}
        except Exception as exc:
            return {"ok": False, "message": "路径无效：%s" % exc}
        if not AGENTHUB_SCRIPT.is_file():
            return {"ok": False, "message": "找不到 agent-hub.ps1"}
        try:
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(AGENTHUB_SCRIPT), "config", "-SetStore", str(target)]
            r = subprocess.run(args, capture_output=True, text=True, timeout=60,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            ok = r.returncode == 0
            state.log(("技能库位置 -> %s" % target) if ok else "设置技能库位置失败",
                      "info" if ok else "warn")
            return {"ok": ok, "output": out.strip()[:2000], "path": str(target)}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def agenthub_dsh_status(self, project_root=""):
        """列出各项目的 DSH 技能联接状态（只读）。"""
        try:
            return _agenthub_dsh_status(project_root)
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc),
                    "projects": []}

    def agenthub_scan_projects(self, apply=False, project_root=""):
        """扫描并（可选）补全所有项目的 DSH 联接。

        apply=False 只报告；apply=True 调 scan-projects -Apply 真正建 junction。
        """
        if apply and not state.action_lock.acquire(blocking=False):
            return {"ok": False, "message": "另一个操作正在执行，请稍候"}
        try:
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(AGENTHUB_SCRIPT), "scan-projects"]
            if project_root:
                args += ["-ProjectRoot", str(project_root)]
            if apply:
                args.append("-Apply")
            r = subprocess.run(args, capture_output=True, text=True, timeout=600,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            ok = r.returncode == 0
            if apply:
                state.log("已扫描并补全项目 DSH 联接" if ok else "扫描项目失败",
                          "info" if ok else "warn")
            return {"ok": ok, "output": out.strip()[-4000:], "applied": bool(apply)}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            if apply:
                try:
                    state.action_lock.release()
                except RuntimeError:
                    pass

    def agenthub_link_dsh(self, project):
        """给单个项目补 .dsh\\skills 联接（相当于 link-dsh -Apply）。"""
        p = (project or "").strip()
        if not p:
            return {"ok": False, "message": "项目路径为空"}
        try:
            target = Path(p).expanduser()
            if not target.is_absolute():
                return {"ok": False, "message": "需要绝对路径"}
        except Exception as exc:
            return {"ok": False, "message": "路径无效：%s" % exc}
        if not state.action_lock.acquire(blocking=False):
            return {"ok": False, "message": "另一个操作正在执行，请稍候"}
        try:
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(AGENTHUB_SCRIPT), "link-dsh",
                    "-Project", str(target), "-Apply"]
            r = subprocess.run(args, capture_output=True, text=True, timeout=180,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            ok = r.returncode == 0
            state.log(("已为 %s 补 DSH 联接" % target.name) if ok else "补 DSH 联接失败",
                      "info" if ok else "warn")
            return {"ok": ok, "output": out.strip()[-2000:]}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            try:
                state.action_lock.release()
            except RuntimeError:
                pass

    def add_custom_gateway(self, name, port, token, proto="openai"):
        """接入一个外部网关（cc-switch 式）：只填端口+token，面板负责探活与展示。

        更轻的假设：面板不管理该网关的进程——启动/停止/重启由用户自己的工具负责，
        面板提供可见性（在线状态/延迟/模型数）与占位 token 复制。
        """
        try:
            port = int(port)
        except Exception:
            return {"ok": False, "message": "端口必须是数字"}
        if not (1 <= port <= 65535):
            return {"ok": False, "message": "端口范围 1-65535"}
        name = (name or "").strip() or ("外部网关 %s" % port)
        proto = (proto or "openai").strip().lower()
        if proto not in ("openai", "anthropic"):
            proto = "openai"
        gws = _load_custom_gateways()
        key = "custom-%s" % port
        if any(g.get("key") == key or int(g.get("port") or 0) == port for g in gws):
            return {"ok": False, "message": "端口 %s 已接入过" % port}
        if any(int(g.get("port") or 0) in hub.PORTS_IN_USE.get("builtin", []) for g in []):
            pass  # 占位：内置端口冲突检查在下方
        builtin_ports = {p["port"] for p in hub.PROVIDERS.values()}
        if port in builtin_ports:
            return {"ok": False, "message": "端口 %s 已被内置网关占用" % port}
        gws.append({"key": key, "name": name, "port": port,
                    "token": (token or "").strip(), "proto": proto,
                    "added": time.strftime("%Y-%m-%d %H:%M")})
        _save_custom_gateways(gws)
        state.log("已接入外部网关 %s（:%s，%s）" % (name, port, proto))
        # 立即探活一次，让用户马上看到结果
        refresh_status()
        return {"ok": True, "key": key, "message": "已接入，可在面板查看在线状态"}

    def remove_custom_gateway(self, key):
        gws = _load_custom_gateways()
        kept = [g for g in gws if g.get("key") != key]
        if len(kept) == len(gws):
            return {"ok": False, "message": "未找到：%s" % key}
        _save_custom_gateways(kept)
        state.log("已移除外部网关 %s" % key)
        refresh_status()
        return {"ok": True}

    def usage(self):
        """四家反代 + DSH 今日消耗聚合。五路并行，单路失败不影响其它。"""
        try:
            today = time.strftime("%Y-%m-%d")

            def _auto():
                try:
                    log = Path.home() / ".autoclaw-relay" / "server.log"
                    cnt, it, ot = _log_tail_scan(
                        log, r"in=(\d+) out=(\d+)",
                        lambda ln: today in ln and "POST /v1" in ln)
                    return {"requests": cnt, "in": it, "out": ot}
                except Exception:
                    return {"requests": 0, "in": 0, "out": 0}

            def _wb():
                try:
                    log = BASE / "workbuddy-proxy.log"
                    cnt, ch = _log_tail_scan(
                        log, r"contentChars\":(\d+)",
                        lambda ln: today in ln and ("stream end" in ln or "流结束" in ln))
                    return {"requests": cnt, "chars": ch}
                except Exception:
                    return {"requests": 0, "chars": 0}

            def _doubao():
                try:
                    log = Path.home() / ".doubao-relay" / "relay.out.log"
                    cnt, ch = _log_tail_scan(
                        log, r"len=(\d+)",
                        lambda ln: today in ln and "chat model=" in ln)
                    return {"requests": cnt, "chars": ch}
                except Exception:
                    return {"requests": 0, "chars": 0}

            with ThreadPoolExecutor(max_workers=5) as ex:
                f_auto = ex.submit(_auto)
                f_wb = ex.submit(_wb)
                f_db = ex.submit(_doubao)
                f_qo = ex.submit(_usage_qoder)
                f_dsh = ex.submit(_usage_dsh, today)
                usage = {
                    "auto": f_auto.result(),
                    "wb": f_wb.result(),
                    "doubao": f_db.result(),
                    "qoder": f_qo.result(),
                    "dsh": f_dsh.result(),
                }
            return {"ok": True, "usage": usage}
        except Exception as e:
            return {"ok": False, "message": f"{type(e).__name__}: {e}"}

    def toggle(self, key):
        if key not in hub.PROVIDERS:
            return {"ok": False, "message": "未知网关：%s" % key}
        with state.lock:
            row = next((x for x in state.snapshot if x["key"] == key), None)
        if row is None:
            return {"ok": False, "message": "状态还没准备好，稍后再试"}
        return request_action([key], "stop" if row["running"] else "start")

    def start_all(self):
        keys = [k for k in PANEL_ORDER if k in hub.PROVIDERS and hub.PROVIDERS[k]["enabled"]]
        return request_action(keys, "start")

    def stop_all(self):
        keys = [k for k in PANEL_ORDER if k in hub.PROVIDERS]
        return request_action(keys, "stop")

    def open_dashboard(self, key):
        if key != "qoder":
            return {"ok": False, "message": "这个网关没有网页面板"}
        port = hub.PROVIDERS["qoder"]["port"]
        url = "http://127.0.0.1:%s/" % port
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return {"ok": True, "message": "已打开 Qoder 面板"}

    def open_app(self, key):
        app = next((x for x in APPS if x["key"] == key), None)
        if app is None:
            return {"ok": False, "message": "未知应用：%s" % key}
        if not app["exists"]:
            return {"ok": False, "message": "找不到 %s，检查安装路径" % app["label"]}
        label = app["label"]
        with _opening_lock:
            if key in _opening_keys:
                return {"ok": False, "message": "%s 正在打开，请稍候" % label}
            _opening_keys.add(key)

        def _run():
            try:
                _open_app_worker(key, label, app["path"], app.get("args", ""))
            finally:
                with _opening_lock:
                    _opening_keys.discard(key)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "message": "正在打开 %s…" % label}

    def close_app(self, key):
        app = next((x for x in APPS if x["key"] == key), None)
        if app is None:
            return {"ok": False, "message": "未知应用：%s" % key}
        label = app["label"]
        with _closing_lock:
            if key in _closing_keys:
                return {"ok": False, "message": "%s 正在关闭，请稍候" % label}
            _closing_keys.add(key)

        def _run():
            try:
                _close_app_worker(key, label)
            except Exception as exc:
                state.log("关闭 %s 失败：%s" % (label, exc), "error")
                state.note_result("关闭 " + label, False, "出错：%s" % exc)
            finally:
                with _closing_lock:
                    _closing_keys.discard(key)

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "message": "正在关闭 %s…" % label}

    def client_add(self):
        """原生选择器挑一个程序，加入面板客户端列表（写入 clients.json）。"""
        import webview
        win = self._window
        if win is None:
            return {"ok": False, "message": "窗口尚未就绪"}
        dialog_type = getattr(webview, "OPEN_DIALOG", None)
        try:
            fd = getattr(webview, "FileDialog", None)
            if fd is not None and hasattr(fd, "OPEN"):
                dialog_type = fd.OPEN
        except Exception:
            pass
        if dialog_type is None:
            return {"ok": False, "message": "当前 pywebview 不支持文件选择"}
        try:
            picked = win.create_file_dialog(
                dialog_type, allow_multiple=False,
                file_types=("可执行文件 (*.exe;*.bat;*.cmd;*.lnk)", "所有文件 (*.*)"))
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        if not picked:
            return {"ok": False, "canceled": True, "message": "已取消"}
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        path = str(path or "").strip()
        if not path or not os.path.isfile(path):
            return {"ok": False, "message": "选择的文件不存在"}
        clients = _load_custom_clients()
        norm = os.path.normcase(os.path.abspath(path))
        for c in clients:
            if os.path.normcase(os.path.abspath(_expand_app_path(c["path"]))) == norm:
                return {"ok": False, "message": "%s 已经在列表里" % c["label"]}
        used = {x["key"] for x in APPS}
        n = 1
        while "custom-%d" % n in used:
            n += 1
        key = "custom-%d" % n
        label = Path(path).stem
        clients.append({"key": key, "label": label, "path": path, "args": ""})
        _save_custom_clients(clients)
        _refresh_app_catalog()
        state.log("已添加客户端 %s（%s）" % (label, path))
        return {"ok": True, "key": key, "label": label, "path": path,
                "message": "已添加 %s" % label}

    def client_remove(self, key):
        """从面板移除自定义客户端；不删程序文件，也不动内置项。"""
        key = str(key or "").strip()
        app = next((x for x in APPS if x["key"] == key), None)
        if app is None:
            return {"ok": False, "message": "未知客户端：%s" % key}
        if not app.get("custom"):
            return {"ok": False, "message": "内置客户端不能从面板移除"}
        clients = [c for c in _load_custom_clients() if c["key"] != key]
        _save_custom_clients(clients)
        _refresh_app_catalog()
        state.log("已从面板移除客户端 %s（文件未删除）" % app["label"])
        return {"ok": True, "message": "已移除 %s（文件未删除）" % app["label"]}

    def startup_status(self, force=False):
        """自启登记状态 + 各网关端口占用归因（只读）。force 跳过 5 秒端口缓存。"""
        try:
            return {"ok": True, "startup": _startup_status(), "ports": _port_rows(force=bool(force))}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def startup_set(self, enabled):
        """开/关面板开机自启（HKCU Run）。只动面板，不启动任何网关。"""
        if not state.action_lock.acquire(blocking=False):
            return {"ok": False, "message": "另一个操作正在执行，请稍候"}
        try:
            on = enabled if isinstance(enabled, bool) else \
                str(enabled).strip().lower() in ("1", "true", "on", "yes")
            res = _startup_set(on)
            if res.get("ok"):
                state.log("开机自启：" + (res.get("message") or ""))
            return res
        except Exception as exc:
            state.log("开机自启设置失败：%s" % exc, "error")
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            try:
                state.action_lock.release()
            except RuntimeError:
                pass

    def checkin_schedule_status(self):
        """定时签到计划任务状态（只读）：触发时间 / 上次结果 / 下次运行。"""
        try:
            return _checkin_task_status()
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def checkin_schedule_set(self, at):
        """安装 / 更新 Windows 计划任务「AI每日签到」，at 为 HH:mm。"""
        if not state.action_lock.acquire(blocking=False):
            return {"ok": False, "message": "另一个操作正在执行，请稍候"}
        try:
            res = _checkin_task_install(at)
            if res.get("ok"):
                state.log("定时签到：" + (res.get("message") or ""))
            else:
                state.log("定时签到设置失败：%s" % (res.get("message") or ""), "error")
            return res
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            try:
                state.action_lock.release()
            except RuntimeError:
                pass

    def checkin_schedule_remove(self):
        """移除「AI每日签到」计划任务；面板里手动签到不受影响。"""
        if not state.action_lock.acquire(blocking=False):
            return {"ok": False, "message": "另一个操作正在执行，请稍候"}
        try:
            res = _checkin_task_remove()
            state.log("定时签到：" + (res.get("message") or ""),
                      "info" if res.get("ok") else "error")
            return res
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            try:
                state.action_lock.release()
            except RuntimeError:
                pass

    def gateway_detail(self, key):
        """面板抽屉：返回该网关的账号/积分/临期明细。

        返回体统一注入 `family`：前端 DRAWER_MAP 先按 kind 找 mapper，
        找不到再按 family 找 —— 海外版（如 qoder_intl）因此可直接复用国内版的
        抽屉结构，不必再写一份 mapper。
        """
        res = self._gateway_detail_raw(key)
        if isinstance(res, dict) and res.get("ok") and not res.get("family"):
            spec = gw_specs.get(key)
            if spec:
                res["family"] = spec["family"]
            if not res.get("key"):
                res["key"] = key
        return res

    def _gateway_detail_raw(self, key):
        """各网关的明细取数（不含 family 注入）。"""
        if key == "auto":
            return _autoclaw_detail()
        if key == "wb":
            out, err = _run_subprocess(
                [sys.executable, str(BASE / "daily_checkin.py"), "--status", "--json", "--workbuddy"])
            return {"ok": True, "kind": "daily",
                    "summary": _parse_daily_summary(out, "workbuddy"),
                    "stderr": _trim(err), "key": key}
        if key == "doubao":
            out, err = _run_subprocess(
                [sys.executable, str(BASE / "daily_checkin.py"), "--status", "--json", "--doubao"])
            return {"ok": True, "kind": "daily",
                    "summary": _parse_daily_summary(out, "doubao"),
                    "accounts_info": _doubao_accounts(),
                    "stderr": _trim(err), "key": key}
        if key == "qoder":
            return _qoder_detail()
        return {"ok": False, "message": "未知网关：%s" % key}

    def doubao_switch_account(self, role):
        """豆包反代账号切换 —— **已禁用 consumer**。

        原因（2026-10-27）：普通豆包是用户在手机端日常使用的账号。
        relay 一旦绑定它，代理请求会被豆包服务端归并到「最近活跃会话」，
        也就是用户正在聊的那条，造成真实污染（已实际发生）。
        面板固定只用「豆包工作」反代，普通豆包仅作只读展示（签到/额度）。

        需要临时切换时请走命令行：python doubao_switch.py use consumer
        """
        if role == "consumer":
            return {"ok": False, "message":
                    "面板已禁用切换到普通豆包——那是手机端在用的账号，"
                    "代理请求会污染你的真实对话。如需临时切换请用命令行。"}
        if role != "gateway":
            return {"ok": False, "message": "未知账号角色：%s" % role}
        try:
            p = subprocess.run(
                [sys.executable, str(BASE / "tools" / "doubao" / "doubao_switch.py"), "use", role],
                capture_output=True, text=True, timeout=60,
                encoding="utf-8", errors="replace")
            out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
            ok = p.returncode == 0
            state.log(("已切换豆包账号：" + role) if ok else ("切换豆包账号失败：" + role),
                      "info" if ok else "warn")
            return {"ok": ok, "output": out.strip()[:1200]}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def qoder_scan_desktop(self):
        """只读扫描本机 Qoder 国内版/国际版登录态（不导入、不改账号池）。"""
        try:
            candidates, current = _qoder_scan_candidates()
            return {"ok": True,
                    "candidates": [_qoder_public_candidate(c) for c in candidates],
                    "current_realm": current,
                    "current_realm_name": _qoder_realm_label(current),
                    "message": "扫描到 %d 个登录态" % len(candidates)}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def qoder_import_desktop(self, keys):
        """把选中的登录态导入 Qoder 账号池。

        keys 为扫描结果里的 pick_key（realm::路径），后端重新扫描后匹配，
        前端不直接传文件路径，避免任意路径被导入。
        """
        if not isinstance(keys, list) or not keys:
            return {"ok": False, "message": "没有选中任何登录态"}
        wanted = {str(k) for k in keys if k}
        if not wanted:
            return {"ok": False, "message": "没有选中任何登录态"}
        try:
            candidates, _ = _qoder_scan_candidates()
            by_key = {c["pick_key"]: c for c in candidates}
            imported, errors, missing = [], [], []
            for key in wanted:
                cand = by_key.get(key)
                if not cand:
                    missing.append(key)
                    continue
                if not cand["valid"]:
                    errors.append("%s（%s）：登录态不可用" % (
                        cand["nickname"] or cand["uid"] or "未知", cand["realm_name"]))
                    continue
                try:
                    res = _qoder_panel_call(
                        "POST", "/accounts/import/desktop",
                        {"path": cand["_path"], "realm": cand["realm"]}, timeout=45)
                    for acc in (res.get("imported") or []):
                        imported.append({
                            "uid": acc.get("uid") or "",
                            "name": acc.get("nickname") or acc.get("uid") or "",
                            "realm": acc.get("realm") or cand["realm"],
                            "realm_name": _qoder_realm_label(
                                acc.get("realm") or cand["realm"]),
                            "plan": acc.get("plan") or "—",
                        })
                except Exception as exc:
                    errors.append("%s：%s: %s" % (
                        cand["nickname"] or cand["uid"] or "未知",
                        type(exc).__name__, exc))
            if missing:
                errors.append("%d 个登录态在重新扫描时消失（文件被移动或客户端已退出登录）"
                              % len(missing))
            if imported:
                detail = "、".join("%s（%s）" % (a["name"], a["realm_name"])
                                   for a in imported)
                state.log("Qoder 已导入本机登录态：%s" % detail, "info")
            if not imported:
                return {"ok": False, "imported": [], "errors": errors,
                        "message": "导入失败：%s" % ("；".join(errors) or "未匹配到登录态")}
            return {"ok": True, "imported": imported, "errors": errors,
                    "message": "已导入 %d 个账号：%s%s" % (
                        len(imported), "、".join(a["name"] for a in imported),
                        ("；%d 个失败" % len(errors)) if errors else "")}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def qoder_set_realm(self, realm):
        """切换 Qoder 网关出口（国内版 / 国际版），单端口 8790 不变。

        出口切换是全局的：目标区必须至少有一个启用账号，否则会把整条
        通道切到无人可用的区，所有客户端一起 429。
        """
        realm = str(realm or "")
        if realm not in ("cn", "intl"):
            return {"ok": False,
                    "message": "只能在国内版（cn）与国际版（intl）之间切换"}
        try:
            data = _qoder_panel_call("GET", "/accounts?realm=all", timeout=15)
            accs = data.get("accounts") or []
            target = [a for a in accs if (a.get("realm") or "cn") == realm]
            enabled = [a for a in target if a.get("enabled", True)]
            if not enabled:
                return {"ok": False,
                        "message": "%s还没有可用账号，请先导入本机登录态；"
                                   "出口未切换" % _qoder_realm_label(realm)}
            res = _qoder_panel_call("POST", "/realm", {"realm": realm}, timeout=15)
            current = res.get("current") or realm
            state.log("Qoder 出口已切到 %s（%d 个可用账号）" % (
                _qoder_realm_label(current), len(enabled)), "info")
            return {"ok": True, "current_realm": current,
                    "current_realm_name": _qoder_realm_label(current),
                    "enabled": len(enabled),
                    "message": "出口已切到 %s（%d 个可用账号）" % (
                        _qoder_realm_label(current), len(enabled))}
        except Exception as exc:
            return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}

    def checkin(self, key):
        """单个网关签到：WB/Doubao 走 daily_checkin，AutoClaw 走 a_switch.py auto，
        Qoder 走网关 /accounts/checkin（panel token）。"""
        if key == "qoder":
            if not state.action_lock.acquire(blocking=False):
                with state.lock:
                    busy = state.busy_name or "另一个操作"
                return {"ok": False, "message": "%s 正在执行，请稍候" % busy}
            threading.Thread(target=_run_qoder_checkin, daemon=True).start()
            return {"ok": True, "message": "已开始签到：Qoder proxy"}
        if not state.action_lock.acquire(blocking=False):
            with state.lock:
                busy = state.busy_name or "另一个操作"
            return {"ok": False, "message": "%s 正在执行，请稍候" % busy}
        threading.Thread(target=_run_checkin, args=([key],), daemon=True).start()
        label = hub.PROVIDERS[key]["name"]
        return {"ok": True, "message": "已开始签到：%s" % label}

    def checkin_all(self):
        keys = [k for k in ("wb", "doubao", "qoder", "auto") if k in hub.PROVIDERS]
        if not keys:
            return {"ok": False, "message": "没有可签到的网关"}
        if not state.action_lock.acquire(blocking=False):
            with state.lock:
                busy = state.busy_name or "另一个操作"
            return {"ok": False, "message": "%s 正在执行，请稍候" % busy}
        threading.Thread(target=_run_checkin, args=(keys,), daemon=True).start()
        return {"ok": True, "message": "已开始签到全部（WorkBuddy / 豆包 / Qoder / AutoClaw）"}

    def close(self):
        state.stop_event.set()
        return {"ok": True}


def _find_panel_window():
    """按标题包含匹配，连旧版本留下的窗口也能找到。"""
    if os.name != "nt":
        return 0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if APP_TITLE in buf.value:
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(cb, 0)
        return found[0] if found else 0
    except Exception:
        return 0


def focus_existing_window():
    if os.name != "nt":
        return False
    hwnd = _find_panel_window()
    if not hwnd:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.ShowWindow(hwnd, 9)       # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def print_status():
    try:
        # 重定向到文件/管道时 Windows 默认用 GBK，JSON 里的中文会解不开
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    refresh_status()
    print(json.dumps(state.payload(), ensure_ascii=False, indent=2))
    return 0


def main():
    global _mutex_handle
    if "--status" in sys.argv:
        return print_status()
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    if os.name == "nt":
        _mutex_handle = hub.acquire_single_instance(MUTEX_NAME)
        if _mutex_handle is None:
            focus_existing_window()
            return 0
        # 兜底：旧版本可能用别的互斥量名、或根本没有互斥量。此时互斥量是空闲的，
        # 说明同标题窗口不是本进程的“当前面板”，而是旧版本残留：请它关闭，
        # 然后继续用新代码开新窗口，避免用户一直看到旧界面。
        legacy = _find_panel_window()
        if legacy:
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostMessageW(legacy, 0x0010, 0, 0)  # WM_CLOSE
                time.sleep(0.5)
            except Exception:
                pass
    if not HTML_PATH.is_file():
        print("找不到界面文件：%s" % HTML_PATH)
        return 2
    try:
        import webview
    except ImportError:
        print("缺少 pywebview，改用命令行开关：python ai_hub.py menu")
        return 2
    html = HTML_PATH.read_text(encoding="utf-8")
    state.log("面板已就绪，等待操作")
    threading.Thread(target=status_loop, daemon=True).start()
    icon_path = str(BASE / "assets" / "pig.ico")
    # create_window 在某些 pywebview 版本不接受 icon 关键字，运行时探测签名，
    # 不支持则跳过（窗口图标缺失不影响面板，顶部 SVG 小猪 Logo 仍显示）。
    import inspect
    _cw_params = inspect.signature(webview.create_window).parameters
    _api = Api()
    _cw_kwargs = {"title": APP_TITLE, "html": html, "js_api": _api,
                  "width": 1040, "height": 780, "min_size": (860, 640),
                  "background_color": "#E9EEF0", "text_select": True}
    if "icon" in _cw_params:
        _cw_kwargs["icon"] = icon_path
    _win = webview.create_window(**_cw_kwargs)
    # 原生文件夹选择框需要窗口句柄（create_file_dialog 是 Window 的方法）
    _api._window = _win
    try:
        webview.start()
    finally:
        state.stop_event.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
