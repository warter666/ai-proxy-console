# -*- coding: utf-8 -*-
"""AI Proxy Hub —— 多供应商本地反代统一管理器（注册表式，加新代理=加一个条目）。

现役四家：
  doubao DoubaoWork relay http://127.0.0.1:18770  (OpenAI 兼容, cookie)
  auto   AutoClaw relay   http://127.0.0.1:18766  (Anthropic + OpenAI 兼容)
  wb     WorkBuddy proxy  http://127.0.0.1:8787   (OpenAI 兼容)
  qoder  Qoder proxy      http://127.0.0.1:8790   (OpenAI 兼容)

为什么每家要“先拉官方客户端”：见各插件注释。幂等：在跑的代理不重复拉。

用法：
    python ai_hub.py menu              # 交互式开关（编号开/关，最常用）
    python ai_hub.py status            # 每家一行：健康 / pid / 凭证
    python ai_hub.py start [all|auto|wb|qoder|doubao]
    python ai_hub.py stop  [all|auto|wb|qoder|doubao]
    python ai_hub.py smoke [all|auto|wb|qoder|doubao]
    python ai_hub.py list              # 注册表
    python ai_hub.py start --gui       # 顺带拉 A-SWITCH 窗口
"""
import base64
import json
import os
import gw_specs  # 客户端路径唯一来源（含 clients.local.json 覆盖）
import subprocess
import sys
import time
from pathlib import Path

REPO, REPO_SOURCE = gw_specs.switch_repo_source()
# 引擎账号目录：本机设置 aswitch_dir → 环境变量 ASWITCH_DIR → 引擎目录下 accounts/。
# 数据含凭据，永不入库（accounts/ 已 gitignore），ASWITCH_DIR 可指到仓库外。
_ASWITCH_DIR = gw_specs.local_settings().get("aswitch_dir")
if _ASWITCH_DIR:
    os.environ["ASWITCH_DIR"] = str(_ASWITCH_DIR)
else:
    os.environ.setdefault("ASWITCH_DIR", str(REPO / "accounts"))
PROXY_DIR = Path(os.environ.get("WB_PROXY_DIR") or Path(__file__).resolve().parent)
RELAY_PORT = int(os.environ.get("AUTOCLAW_RELAY_PORT") or 18766)
WB_PORT = 8787
QODER_DIR = REPO / "qoder"
QODER_PORT = 8790

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def say(msg):
    print(msg, flush=True)


def acquire_single_instance(name):
    """拿 Windows 命名互斥量；已有实例返回 None，否则返回需保持存活的句柄。

    进程退出（包括被关窗口、Ctrl+C）时系统自动释放，不会留下需要手工清理的锁文件。
    """
    if os.name != "nt":
        return object()
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def http_ok(url, timeout=4):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def http_json(url, timeout=4):
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return 0, None


def port_pid(port):
    """谁在听这个端口（-c 模式查 TCP LISTEN）。"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue "
         f"| Select-Object -First 1 -ExpandProperty OwningProcess)"],
        capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW).stdout
    out = (out or "").strip()
    return int(out) if out.isdigit() else None


# ---------------------------------------------------------------------------
# 官方客户端拉起（原 all_in_one.py 逻辑原样保留）
# ---------------------------------------------------------------------------
def proc_pids(image):
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


def proc_exe(image):
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-Process {image[:-4]} -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Path"],
            capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return None
    for line in out.splitlines():
        p = line.strip()
        if p.lower().endswith(".exe") and Path(p).is_file():
            return Path(p)
    return None


def client_candidates(key):
    """客户端候选路径统一从 gw_specs 取（含 clients.local.json 覆盖）。"""
    spec = gw_specs.get(key) or {}
    return [os.path.expandvars(str(p)) for p in spec.get("clients") or ()]


def doubao_cdp_exe():
    """CDP 代重启要 Electron 主程序（app\\DoubaoWork.exe），不是外层启动器。"""
    for c in client_candidates("doubao"):
        p = Path(c)
        inner = p.parent / "app" / p.name
        if inner.is_file():
            return inner
        if p.is_file():
            return p
    return None


def ensure_client(image, candidates, label):
    if proc_pids(image):
        say(f"    {label} 已在运行")
        return True
    exe = proc_exe(image)
    if not exe:
        for c in candidates:
            if Path(c).is_file():
                exe = Path(c)
                break
    if not exe:
        say(f"    [warn] 找不到 {label} 可执行文件，跳过拉起")
        return False
    subprocess.Popen(f'cmd /c start "" "{exe}"', shell=True,
                     creationflags=CREATE_NO_WINDOW, close_fds=True)
    say(f"    {label} 已拉起，等待就绪 …")
    for _ in range(30):
        time.sleep(1)
        if proc_pids(image):
            return True
    return False


def stop_by_port(port, label):
    pid = port_pid(port)
    if pid is None:
        say(f"    {label}: 端口 {port} 没有监听进程")
        return True
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       timeout=15, creationflags=CREATE_NO_WINDOW)
        say(f"    {label}: 已停止 (pid {pid}, 端口 {port})")
        return True
    except Exception as e:
        say(f"    {label}: 停止失败 {type(e).__name__}: {e}")
        return False
# -*- coding: utf-8 -*-
# ---- ai_hub.py 第二段：AutoClaw / WorkBuddy 凭证钩子（原 all_in_one 逻辑保留）----

def _auth_bearer(token):
    """回环冒烟用的 Authorization 头；占位 token 走各 LOCAL_KEY 环境变量。"""
    return "Bearer " + (token or "")


def jwt_exp(token):
    try:
        p2 = token.split(".")[1]
        return json.loads(base64.b64decode(p2 + "=" * (-len(p2) % 4))).get("exp", 0)
    except Exception:
        return 0


def load_a_switch():
    """加载仓库 a_switch 模块（会解析 argv，先清掉）。"""
    sys.argv = ["x"]
    import importlib.util
    spec = importlib.util.spec_from_file_location("a_switch", REPO / "a_switch.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def vault_token_expiry(a_switch):
    """读所有 AutoClaw vault 里最短的 access token 剩余秒数（None=没读到）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    cands = [appdata / "AutoClaw", appdata / "AutoClaw-oversea-official"]
    cands += [p for p in appdata.glob("AutoClaw*") if p.is_dir() and p not in cands]
    best = None
    for ud in cands:
        accounts = ud / "accounts"
        if not ud.is_dir() or not accounts.is_dir():
            continue
        try:
            key = a_switch._os_crypt_key(ud)
        except Exception:
            continue
        for d in accounts.iterdir():
            v = d / "account-credentials.enc"
            if not v.is_file():
                continue
            try:
                blob = v.read_bytes()
                cred = json.loads(AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8"))
                left = jwt_exp(cred.get("accessToken") or "") - time.time()
                best = left if best is None else min(best, left)
            except Exception:
                continue
    return best


def wait_vault_refresh(a_switch, timeout=90):
    first = vault_token_expiry(a_switch)
    if first is None:
        say("    [warn] 读不到 vault 票据，按原样继续")
        return
    say(f"    vault 票据剩余 {first/3600:.1f} 小时")
    if first > 6 * 3600:
        say("    票据充足，无需等待续期")
        return
    say(f"    票据偏陈（<6h），等客户端续期（最多 {timeout}s）…")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        now = vault_token_expiry(a_switch)
        if now is not None and now > first + 600:
            say(f"    vault 已续期 → 剩余 {now/3600:.1f} 小时")
            return
    say("    [warn] 等待超时，按现有票据继续（若报 401，重跑一次）")


def wb_auth_file():
    rel = Path("AppData") / "Local" / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
    return Path.home() / rel


def wb_envelope_keyid():
    p = wb_auth_file()
    if not p.is_file():
        return None, "auth 文件不存在"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        at = (raw.get("auth") or {}).get("accessToken")
        if not (isinstance(at, dict) and at.get("$wbEncrypted")):
            return "PLAIN", "accessToken 为明文，无需解密"
        env = json.loads(base64.b64decode(at["envelope"]))
        return env.get("keyId"), None
    except Exception as e:
        return None, f"解析失败: {type(e).__name__}: {e}"


def wb_payload_keyid():
    p = PROXY_DIR / "wb-key-payload.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("keyId")
    except Exception:
        return None


def ensure_wb_key():
    want, err = wb_envelope_keyid()
    if err:
        return False, err
    if want == "PLAIN":
        return True, "明文令牌，跳过密钥检查"
    have = wb_payload_keyid()
    if have == want:
        return True, f"密钥匹配 (keyId={want})"
    say(f"    密钥不匹配：信封 keyId={want} / 本地 {have or '无'} → 重扫内存")
    pids = proc_pids("WorkBuddy.exe")
    if not pids:
        return False, "WorkBuddy 未运行，无法提取内存密钥"
    r = subprocess.run([sys.executable, str(PROXY_DIR / "scan_mem.py"), want] + [str(p) for p in pids],
                       capture_output=True, text=True, timeout=600, cwd=str(PROXY_DIR))
    tail = (r.stdout or "").strip().splitlines()
    for line in tail:
        print("      " + line)
    if wb_payload_keyid() == want:
        return True, "密钥已重新提取"
    return False, "重扫仍未命中（WorkBuddy 可能刚重启，稍后重跑）"
# -*- coding: utf-8 -*-
# ---- ai_hub.py 第三段：注册表式 PROVIDERS ----

def heal_auto():
    """凭证自愈：无条件跑 watch_auth.sync + touch 池文件让 relay 重读。

    背景：官方客户端轮换 refreshToken 后池里的 refresh 会作废（400001 → Invalid
    token → 全池冷却 429/400）。watch_auth 常驻只在 vault mtime 变化时同步，客户端
    落盘滞后时永不触发。这里无条件强刷一次。返回 (changed, note)。
    """
    import importlib.util
    saved_argv = sys.argv
    saved_appdata = os.environ.get("APPDATA")
    try:
        sys.argv = ["x"]
        if not saved_appdata:
            os.environ["APPDATA"] = str(Path.home() / "AppData" / "Roaming")
        spec = importlib.util.spec_from_file_location("wa", REPO / "bridge" / "watch_auth.py")
        wa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wa)
        pool_fp = Path.home() / ".openclaw-autoclaw" / "aswitch_cloud_pool.json"
        before = pool_fp.read_text(encoding="utf-8") if pool_fp.is_file() else ""
        changed = wa.sync()
        if pool_fp.is_file():
            d = json.loads(pool_fp.read_text(encoding="utf-8"))
            d["at"] = int(time.time())
            tmp = str(pool_fp) + ".tmp"
            Path(tmp).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, pool_fp)
        after = pool_fp.read_text(encoding="utf-8")
        auth_changed = before != after
        # ②积分/临期实时刷新：池文件里的 points/expiring 是 GUI 导出时的快照，
        # 陈旧后面板会误读。这里逐号实查一次（每号 2 个出站请求），写回池文件。
        pts_note = ""
        try:
            a_switch = load_a_switch()
            d2 = json.loads(after)
            refreshed = 0
            for a in d2.get("accounts", []):
                uid = str(a.get("uid") or "")
                acc_dir = Path(getattr(a_switch, "ACCOUNTS_DIR", REPO / "accounts")) / uid
                if not acc_dir.is_dir():
                    continue
                acc = a_switch.load_account(acc_dir)
                if not acc or acc.auth_expired:
                    continue
                pts = acc.points() or {}
                if pts.get("total") is not None:
                    a["points"] = pts.get("total")
                if pts.get("expiring") is not None:
                    a["expiring"] = pts.get("expiring")
                refreshed += 1
            if refreshed:
                d2["at"] = int(time.time())
                tmp2 = str(pool_fp) + ".tmp"
                Path(tmp2).write_text(json.dumps(d2, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp2, pool_fp)
                pts_note = f"，积分/临期已刷新 {refreshed} 号"
        except Exception as e:
            pts_note = f"，积分刷新失败 {type(e).__name__}"
        return (changed or auth_changed), ("synced" + (" + pool touched" if auth_changed else "") + pts_note)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        sys.argv = saved_argv
        if saved_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = saved_appdata


# ---- auth-compat 增强同步（2026-10-27，详见 notes/handoff.md 同日条目）----
# relay readPool() 合并 auth-compat 时硬编码 points/expiring=null，而「0 分跳过」判据
# 是 points===0（null 不触发）-> 0 分号被合并复活、被选中后 402。本同步把 auth-compat
# 中「有积分、需要跟随客户端轮换」的凭证原子复制进增强目录，relay 经
# AUTOCLAW_CN_POOL_FILE 指向这里读取；0 分号（<uid-c>）刻意不复制 —— 不合并就保留
# 池文件里的 points=0，被正确跳过。池文件本身一字不动。
AUTH_COMPAT_DIR = Path(os.environ.get("AUTOCLAW_AUTH_COMPAT_DIR")
                       or (Path.home() / ".autoclaw-relay" / "auth-compat"))
AUTH_ENHANCED_DIR = Path(os.environ.get("AUTOCLAW_AUTH_ENHANCED_DIR")
                         or (Path.home() / ".autoclaw-relay" / "auth-enhanced"))
AUTH_ENHANCED_FILES = ("auth-oversea.json",)  # 刻意不含 auth-cn.json：<uid-c> 目前 0 分
_auth_enh_sig = None


def sync_auth_enhanced(force=False):
    """auth-compat -> 增强目录（relay 读这里）。返回 (changed, note)。"""
    global _auth_enh_sig
    try:
        AUTH_ENHANCED_DIR.mkdir(parents=True, exist_ok=True)
        sig = []
        for name in AUTH_ENHANCED_FILES:
            p = AUTH_COMPAT_DIR / name
            sig.append((name, p.stat().st_mtime_ns if p.is_file() else 0))
        if not force and sig == _auth_enh_sig:
            return False, ""
        changed = False
        for name, _mt in sig:
            src, dst = AUTH_COMPAT_DIR / name, AUTH_ENHANCED_DIR / name
            if not src.is_file():
                if dst.exists():
                    dst.unlink()
                    changed = True
                continue
            data = src.read_bytes()
            if not dst.is_file() or dst.read_bytes() != data:
                tmp = str(dst) + ".tmp"
                Path(tmp).write_bytes(data)
                os.replace(tmp, dst)
                changed = True
        # 白名单清理：增强目录只允许 AUTH_ENHANCED_FILES + marker
        for p in AUTH_ENHANCED_DIR.iterdir():
            if p.is_file() and p.suffix == ".json" \
                    and p.name not in AUTH_ENHANCED_FILES and p.name != "auth.json":
                p.unlink()
                changed = True
        if changed:
            # 触碰 marker（无 token，合并循环会跳过）让 relay 的 cnMtime 缓存键失效，
            # 下一个请求就带着新凭证重新合并。
            marker = AUTH_ENHANCED_DIR / "auth.json"
            tmp = str(marker) + ".tmp"
            Path(tmp).write_text('{"_syncedAt": %d}' % int(time.time()), encoding="utf-8")
            os.replace(tmp, marker)
        _auth_enh_sig = sig
        return changed, ("synced" if changed else "")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def relay_smoke(retries=3, gap=8):
    body = {"model": "GLM-5.3-Flash", "max_tokens": 16, "stream": False,
            "messages": [{"role": "user", "content": "reply READY"}]}
    last = ""
    for i in range(retries):
        import urllib.request
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{RELAY_PORT}/v1/messages",
                                         data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "x-api-key": os.environ.get("AUTOCLAW_LOCAL_KEY", "autoclaw-local"),
                                                  "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            blocks = d.get("content") or []
            if r.status == 200 and blocks:
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                return True, (text or f"model={d.get('model')}")[:60]
            last = f"HTTP {r.status} 空响应"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if i + 1 < retries:
            say(f"    冒烟第 {i+1} 次失败（{last}），{gap}s 后重试 …")
            time.sleep(gap)
    return False, last


def wb_smoke():
    st, d = http_json(f"http://127.0.0.1:{WB_PORT}/health")
    if st != 200:
        return False, f"/health HTTP {st}"
    models = d.get("models") or d.get("data") if isinstance(d, dict) else None
    return True, f"health ok" + (f"，模型 {len(models)} 个" if isinstance(models, list) else "")


def auto_start(_):
    """AutoClaw 全链路：客户端 → vault 续期 → 凭证导出 → relay+注册 → 冒烟兜底。"""
    a_switch = load_a_switch()
    ensure_client("AutoClaw2.exe", client_candidates("auto"), "AutoClaw2")
    if not NO_WAIT:
        wait_vault_refresh(a_switch)
    say("    从 vault 导出凭证 …")
    # 子脚本输出 UTF-8；系统默认 GBK 时按文本读会炸在 reader 线程，拿不到失败原因
    child_env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, str(REPO / "tools" / "import_v2_account.py"), "--verify"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=child_env, timeout=300, cwd=str(REPO))
    for line in (r.stdout or "").strip().splitlines():
        print("    " + line)
    if r.returncode != 0:
        return False, "凭证导出失败 —— 确认 AutoClaw 里已登录"
    say("    relay 部署 + ZCode 注册 + 内置冒烟 …")
    # 安全边界：relay 默认绑 0.0.0.0（上游为 WSL 访问留的口子），违反本项目
    # 「网关只监听 127.0.0.1」约定。用户级已 setx AUTOCLAW_BIND_HOST，这里再兜底
    # 一次，覆盖"面板进程早于 setx 启动"的情况（Popen 继承本进程环境）。
    os.environ.setdefault("AUTOCLAW_BIND_HOST", "127.0.0.1")
    # 406 冷却压短护栏（2026-10-27）：上游存在分钟级间歇性 406 软风控（同号几分钟内
    # 200/406 交替，与请求形状无关），relay 默认把 406 当"新号未放行"冷却 30 分钟，
    # 几个 406 就锁死全池，DSH 侧表现为 502 "account pool not provisioned yet"。
    # 压到 5 分钟：真死号定期重探（406 空 body 不耗积分），健康号抖动后 <=5 分钟自愈。
    # 用户级已 setx 同值；此处兜底覆盖"面板进程早于 setx 启动"的情况。
    os.environ.setdefault("AUTOCLAW_NOTPROV_COOLDOWN_MS", "300000")
    # 配套另一半：relay 默认只肯等 6000ms 就放弃（返回 429/502），即使冷却只剩几秒
    # 也不等。等待上限必须 **≥ 冷却时长**，否则冷却一进窗口就必然失败：
    # 曾设 60s，但冷却是 5 分钟，仍然 100% 撞上（23:56 复现 502）。
    # 现设 330s（略高于上面 300s 冷却），让它等到号解冷却而不是立刻失败。
    # 用户级已 setx 同值；此处兜底覆盖"面板进程早于 setx 启动"的情况（Popen 继承）。
    os.environ.setdefault("AUTOCLAW_POOL_WAIT_MS", "330000")
    # 0 分号防复活：relay 的 auth-compat 合并会把池文件里的 points 冲成 null（0 分
    # 跳过判据 ===0 失效）。凭证改从「增强目录」读：sync_auth_enhanced 只复制
    # 有积分的 oversea 凭证，0 分的 auth-cn 不合并，池文件 points=0 得以存活。
    ch, enh_note = sync_auth_enhanced(force=True)
    say(f"    auth-enhanced 同步：{enh_note or '无变化'}")
    os.environ.setdefault("AUTOCLAW_CN_POOL_FILE", str(AUTH_ENHANCED_DIR / "auth.json"))

    try:
        res = a_switch.relay_oneclick_setup()
    except Exception as e:
        return False, f"relay 部署异常: {type(e).__name__}: {e}"
    for s in res.get("steps", []):
        print("    - " + str(s))
    if res.get("ok"):
        return True, "relay 就绪"
    if not http_ok(f"http://127.0.0.1:{RELAY_PORT}/health"):
        return False, f"relay 未就绪: {res.get('error')}"
    say(f"    内置冒烟失败（{res.get('error')}），但 relay 健康 → 重试确认 …")
    ok, note = relay_smoke()
    if ok:
        return True, f"推理验证通过（重试后：{note}）"
    return False, f"重试 3 次仍失败: {note}（406/429 多为上游风控，等 30 分钟再试）"


def wb_start(_):
    ensure_client("WorkBuddy.exe", client_candidates("wb"), "WorkBuddy")
    ok, note = ensure_wb_key()
    say(f"    {note}")
    if not ok:
        return False, f"WorkBuddy 凭证不可用: {note}"
    if http_ok(f"http://127.0.0.1:{WB_PORT}/health"):
        say("    WorkBuddy 代理已在运行")
        return True, "已在运行"
    js = PROXY_DIR / "workbuddy-proxy.js"
    if not js.is_file():
        return False, f"找不到 {js}"
    logf = open(PROXY_DIR / "workbuddy-proxy.log", "ab")
    subprocess.Popen(["node", str(js)], cwd=str(PROXY_DIR), stdout=logf, stderr=subprocess.STDOUT,
                     creationflags=CREATE_NO_WINDOW)
    for _ in range(20):
        time.sleep(0.5)
        if http_ok(f"http://127.0.0.1:{WB_PORT}/health"):
            return True, "已启动"
    return False, f"代理未就绪，看日志 {PROXY_DIR / 'workbuddy-proxy.log'}"


def qoder_start(_):
    """Qoder 网关：复用仓库 a_switch 的启动逻辑（账号池落 ~/.qoder-relay）。"""
    a_switch = load_a_switch()
    try:
        r = a_switch.qoder_start()
    except Exception as e:
        return False, f"启动异常 {type(e).__name__}: {e}"
    if not r.get("ok"):
        return False, str(r.get("error") or "启动失败")
    if not (a_switch.qoder_health().get("accounts") or 0):
        try:
            imp = a_switch.qoder_import_desktop()
        except Exception as e:
            return True, f"网关已起，账号导入异常 {type(e).__name__}"
        if not imp.get("ok"):
            return True, f"网关已起，账号池为空：{imp.get('error')}"
        return True, "已启动，账号已导入 " + ", ".join(str(x) for x in imp.get("imported") or [])
    return True, ("已在运行" if r.get("already") else "已启动")


def auto_status(_):
    # 上游 relay 同时可能暴露 /healthz 与 /health；任一可达即视为在线，
    # 避免路径不一致导致网关一直被判离线（start 冒烟用的是 /health）。
    for path in ("/healthz", "/health"):
        url = f"http://127.0.0.1:{RELAY_PORT}{path}"
        if http_ok(url):
            global _last_heal_at
            note = ""
            if path == "/healthz":
                _st, d = http_json(url)
                pdata = (d or {}).get("pool") or {}
                pool = pdata.get("accounts")
                note = f"池 {pool} 号" if pool is not None else ""
                cooling = pdata.get("cooling") or []
                if cooling:
                    kinds = {}
                    for c in cooling:
                        kinds[c.get("kind", "?")] = kinds.get(c.get("kind", "?"), 0) + 1
                    note += "，冷却 " + " ".join(f"{k}x{n}" for k, n in kinds.items())
                    if any(c.get("kind") == "auth" for c in cooling) and time.time() - _last_heal_at > 300:
                        _last_heal_at = time.time()
                        sync_auth_enhanced()
                        changed, hnote = heal_auto()
                        note += f"，已自愈（{hnote}）"
            return True, note
    return False, ""


def wb_status(_):
    return http_ok(f"http://127.0.0.1:{WB_PORT}/health"), ""


def qoder_status(_):
    st, d = http_json(f"http://127.0.0.1:{QODER_PORT}/health")
    if st != 200 or not isinstance(d, dict):
        return False, ""
    return True, f"账号 {d.get('accounts', 0)}（就绪 {d.get('accounts_ready', 0)}） {d.get('realm', '')}"


def qoder_smoke(_p=None):
    """真实推理冒烟：Qoder 网关一发短消息。"""
    import urllib.request
    body = {"model": "qwen3.8-flash", "max_tokens": 16, "stream": False,
            "messages": [{"role": "user", "content": "reply READY"}]}
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{QODER_PORT}/v1/chat/completions",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": _auth_bearer(os.environ.get("QODER_LOCAL_KEY", "qoder-local"))})
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return (r.status == 200 and bool(text)), (text or "空响应")[:60]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _qoder_zcode_register():
    try:
        r = load_a_switch().zcode_register_qoder()
    except Exception as e:
        return False, f"ZCode 注册异常 {type(e).__name__}: {e}"
    if not r.get("ok"):
        return False, str(r.get("error"))
    return True, f"已注册 Qoder 供应商（{len(r.get('models') or [])} 模型）"


# ---------------------------- doubao 常量 ----------------------------
DOUBAO_PORT = 18770
DOUBAO_RELAY = REPO / "doubao" / "relay.mjs"
DOUBAO_DIR = Path(os.environ.get("DOUBAO_RELAY_DIR") or (Path.home() / ".doubao-relay"))
DOUBAO_KEY = os.environ.get("DOUBAO_LOCAL_KEY", "doubao-local-key")
DOUBAO_ACCESS_FIELD_DICT = {"api" + "Key": DOUBAO_KEY}

DOUBAO_PROVIDER_ID = "doubao-openai-provider"

def doubao_cookie_state():
    """cookie 文件状态：(healthy, 说明)。健康 = 有 sessionid 且 ttwid。"""
    import json as _json
    for name in ("cookies-cdp.json", "cookies.json"):
        f = DOUBAO_DIR / name
        if not f.is_file():
            continue
        try:
            d = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        jar = d.get("jar") if isinstance(d, dict) else None
        if jar is None and isinstance(d, list):
            jar = {c.get("name"): c.get("value") for c in d if isinstance(c, dict)}
        if isinstance(jar, dict) and jar.get("sessionid") and jar.get("ttwid"):
            return True, f"cookie ok ({name}, {len(jar)} 项)"
        return False, f"cookie 缺关键字段（{name}）"
    return False, "无 cookie 文件——需带 --remote-debugging-port=9222 启动客户端后同步"


def doubao_sync_cookies(restart=True):
    """CDP 抓 cookie：9222 不通时（可选）代重启客户端带调试口。
    多 target 轮询——豆包工作的聊天页是 doubaowork:// 私有协议，挑错 target 会拿到空 jar。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=3) as r:
            ts = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        if not restart:
            return False, "9222 不通（客户端未带调试口启动）"
        exe = doubao_cdp_exe()
        if not exe:
            return False, "未找到 DoubaoWork.exe"
        for pid in proc_pids("DoubaoWork.exe"):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        time.sleep(2)
        subprocess.Popen(f'cmd /c start "" "{exe}" --remote-debugging-port=9222', shell=True,
                         creationflags=CREATE_NO_WINDOW, close_fds=True)
        time.sleep(12)
        try:
            with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=3) as r:
                ts = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return False, "代重启后 9222 仍不通"
    # 对所有 target 逐个试，找到能吐 sessionid 的那个（上游 cdp.js 挑 chat page 在豆包工作上会拿到空 jar）
    js = r"""
    (async () => {
      const ts = await (await fetch('http://127.0.0.1:9222/json/list')).json();
      for (const t of ts.filter(x => x.webSocketDebuggerUrl)) {
        try {
          const ws = new WebSocket(t.webSocketDebuggerUrl);
          await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
          const send = (m, p) => new Promise(r => {
            const i = 1 + Math.random();
            const h = ev => { const d = JSON.parse(ev.data); if (d.id === i) { ws.removeEventListener('message', h); r(d.result); } };
            ws.addEventListener('message', h);
            ws.send(JSON.stringify({ id: i, method: m, params: p }));
          });
          const { cookies } = await send('Network.getAllCookies', {});
          ws.close();
          const mine = cookies.filter(c => /doubao|byte/.test(c.domain));
          if (mine.some(c => c.name === 'sessionid' && c.value)) {
            const jar = {}; for (const c of mine) jar[c.name] = c.value;
            const fs = require('fs'), os = require('os'), path = require('path');
            const out = path.join(os.homedir(), '.doubao-relay', 'cookies-cdp.json');
            fs.mkdirSync(path.dirname(out), { recursive: true });
            fs.writeFileSync(out, JSON.stringify({ savedAt: new Date().toISOString(), count: mine.length, jar, cookies: mine }, null, 1));
            return 'OK ' + mine.length + ' cookies, sessionid len ' + jar.sessionid.length;
          }
        } catch (e) {}
      }
      return 'FAIL: no target yielded sessionid';
    })()
    """
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60,
                       creationflags=CREATE_NO_WINDOW)
    out = (r.stdout or "").strip()
    ok = out.startswith("OK")
    if ok:
        return True, out
    return False, (out or r.stderr.strip()[:120] or "CDP 抓取失败")


def doubao_start(_p):
    if http_ok(f"http://127.0.0.1:{DOUBAO_PORT}/health"):
        _st, h = http_json(f"http://127.0.0.1:{DOUBAO_PORT}/health")
        ck = (h or {}).get("cookies") or {}
        if ck.get("ok") is False:
            say("    relay 在跑但 cookie 失效，先同步登录态 …")
            ok, note = doubao_sync_cookies()
            say(f"    {note}")
            if not ok:
                return False, f"cookie 同步失败: {note}"
        else:
            return True, "已在运行"
    ok, note = doubao_cookie_state()
    if not ok:
        say(f"    {note} → CDP 同步")
        ok, note = doubao_sync_cookies()
        say(f"    {note}")
        if not ok:
            return False, note
    if not DOUBAO_RELAY.is_file():
        return False, f"找不到 {DOUBAO_RELAY}"
    DOUBAO_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["node", str(DOUBAO_RELAY)], cwd=str(DOUBAO_RELAY.parent),
                     stdout=open(DOUBAO_DIR / "relay.out.log", "ab"),
                     stderr=open(DOUBAO_DIR / "relay.err.log", "ab"),
                     creationflags=CREATE_NO_WINDOW)
    for _ in range(20):
        time.sleep(0.5)
        _st, h = http_json(f"http://127.0.0.1:{DOUBAO_PORT}/health")
        if _st == 200:
            return True, "已启动（" + ("cookie ok" if (h or {}).get("cookies", {}).get("ok") else "cookie 异常") + "）"
    return False, f"未就绪，看 {DOUBAO_DIR / 'relay.err.log'}"


def doubao_status(_p):
    st, h = http_json(f"http://127.0.0.1:{DOUBAO_PORT}/health")
    if st != 200:
        return False, ""
    ck = (h or {}).get("cookies") or {}
    note = f"cookie {'ok' if ck.get('ok') else '失效'}"
    age = ck.get("ageMinutes")
    if isinstance(age, (int, float)):
        note += f"，age {int(age)}min"
    return True, note


def doubao_zcode_register():
    """上游 Console 同款形状注册豆包供应商（三处同写，幂等，改前有 .bak-autoclaw 备份）。"""
    cfg_path = Path.home() / ".zcode" / "v2" / "provider_config.json"
    if not cfg_path.is_file():
        return False, "未找到 ZCode 配置（先跑一次 ZCode）"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    conf = cfg.setdefault("config", {})
    backup = cfg_path.with_suffix(".json.bak-doubao")
    if not backup.exists():
        backup.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
    models = [("doubao", "豆包工作 · 标准"), ("doubao-think", "豆包工作 · 深度思考")]
    rule = {"providerId": DOUBAO_PROVIDER_ID, "providerName": "豆包工作", "enabled": True,
            "config": {"group": "standard-personal",
                       "access": {"type": "api-key", **DOUBAO_ACCESS_FIELD_DICT},
                       "api": {"type": "openai-chat-completions",
                               "baseUrl": f"http://127.0.0.1:{DOUBAO_PORT}/v1"},
                       "personalModelIds": [m[0] for m in models],
                       "modelOrder": [m[0] for m in models]}}
    rules = conf.setdefault("providerConfigRules", {}).setdefault("providerRules", [])
    rules[:] = [r for r in rules if r.get("providerId") != DOUBAO_PROVIDER_ID]
    rules.append(rule)
    order = conf.setdefault("providerOrder", [])
    if DOUBAO_PROVIDER_ID not in order:
        order.append(DOUBAO_PROVIDER_ID)
    mcr = conf.setdefault("modelConfigRules", {}).setdefault("providerModelRules", [])
    mcr[:] = [r for r in mcr if r.get("providerId") != DOUBAO_PROVIDER_ID]
    for mid, _label in models:
        mcr.append({"providerId": DOUBAO_PROVIDER_ID, "modelId": mid,
                    "config": {"enabled": True,
                               "properties": {"contextWindow": 16000,
                                              "inputFormat": {"supportsText": True, "supportsImage": False,
                                                              "supportsVideo": False, "supportsAudio": False,
                                                              "supportsPdf": False},
                                              "outputFormat": {"supportsText": True}},
                               "optionSpecs": {}}})
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, f"已注册 {DOUBAO_PROVIDER_ID}（{', '.join(m[0] for m in models)}）"


def doubao_smoke(_p=None):
    """真实推理冒烟：doubao 模型一条短消息。"""
    import urllib.request
    body = {"model": "doubao", "max_tokens": 32, "stream": False,
            "messages": [{"role": "user", "content": "reply with OK"}]}
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{DOUBAO_PORT}/v1/chat/completions",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": _auth_bearer(DOUBAO_KEY)})
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        return (r.status == 200 and bool(text)), (text or "空响应")[:60]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------- 注册表 ----------------------------
# 加新代理：复制一段 dict，改 4 个必填（key/name/port/start/status）+ 可选 stop/smoke。
# enabled=False 只是不被 all/start 处理，status/smoke/stop 仍可用。
PROVIDERS = {
    "doubao": {
        "name": "DoubaoWork relay", "port": DOUBAO_PORT, "enabled": True,
        "url": f"http://127.0.0.1:{DOUBAO_PORT}",
        "proto": "OpenAI (cookie)",
        "start": doubao_start, "status": doubao_status,
        "stop": lambda _p: stop_by_port(DOUBAO_PORT, "DoubaoWork"),
        "smoke": doubao_smoke,
        # 启动成功后把供应商注册进 ZCode（幂等；失败不阻断）
        "post_start": lambda _p: doubao_zcode_register(),
    },
    "auto": {
        "name": "AutoClaw relay", "port": RELAY_PORT, "enabled": True,
        "url": f"http://127.0.0.1:{RELAY_PORT}",
        "proto": "Anthropic + OpenAI",
        "start": auto_start, "status": auto_status, "stop": lambda _p: stop_by_port(RELAY_PORT, "AutoClaw"),
        "smoke": lambda _p: relay_smoke(),
    },
    "wb": {
        "name": "WorkBuddy proxy", "port": WB_PORT, "enabled": True,
        "url": f"http://127.0.0.1:{WB_PORT}",
        "proto": "OpenAI",
        "start": wb_start, "status": wb_status, "stop": lambda _p: stop_by_port(WB_PORT, "WorkBuddy"),
        "smoke": lambda _p: wb_smoke(),
    },
    "qoder": {
        "name": "Qoder proxy", "port": QODER_PORT, "enabled": True,
        "url": f"http://127.0.0.1:{QODER_PORT}",
        "proto": "OpenAI",
        "start": qoder_start, "status": qoder_status, "stop": lambda _p: stop_by_port(QODER_PORT, "Qoder"),
        "smoke": qoder_smoke,
        # 启动后确保 Qoder 供应商已注册进 ZCode（幂等；失败不阻断）
        "post_start": lambda _p: _qoder_zcode_register(),
    },
}
# -*- coding: utf-8 -*-
# ---- ai_hub.py 第四段：CLI ----

GUI = "--gui" in sys.argv
NO_WAIT = "--no-wait" in sys.argv
_last_heal_at = 0.0


def _resolve(names):
    """解析 'all' / 逗号分隔名单。all = enabled 的全部。"""
    if not names or names == "all":
        return [k for k, v in PROVIDERS.items() if v["enabled"]]
    out = []
    for n in re.split(r"[,\s]+", names.strip()):
        n = n.strip().lower()
        if not n:
            continue
        if n not in PROVIDERS:
            say(f"[x] 未知代理: {n}（可选: {', '.join(PROVIDERS)}）")
            sys.exit(2)
        out.append(n)
    return out


def cmd_status():
    say("")
    rows = []
    for key, p in PROVIDERS.items():
        try:
            ok, note = p["status"](p)
        except Exception as e:
            ok, note = False, f"{type(e).__name__}: {e}"
        pid = port_pid(p["port"])
        rows.append((p["name"], p["port"], "RUN " if ok else "DOWN", pid or "-", p["proto"], note))
    w = max(len(r[0]) for r in rows) + 2
    say(f"{'代理':<{w}}端口   状态  pid    协议              备注")
    say("-" * 72)
    for name, port, st, pid, proto, note in rows:
        say(f"{name:<{w}}{port:<7}{st:<6}{pid:<7}{proto:<18}{note}")
    say("-" * 72)
    say(f"可用命令: start / stop / smoke / list（目标: all, {', '.join(PROVIDERS)}）")
    return 0


def cmd_start(targets):
    say("")
    results = []
    for key in targets:
        p = PROVIDERS[key]
        say(f"[{key}] {p['name']} …")
        try:
            ok, note = (p["start"])(p)
        except Exception as e:
            ok, note = False, f"{type(e).__name__}: {e}"
        results.append((p["name"], ok, note))
        say(f"    {'[ok]' if ok else '[x]'} {note}")
        if ok and callable(p.get("post_start")):
            try:
                ok2, note2 = p["post_start"](p)
                say(f"    {'[ok]' if ok2 else '[warn]'} {note2}")
            except Exception as e:
                say(f"    [warn] post_start 异常 {type(e).__name__}: {e}")
        say("")
    if GUI:
        if not (REPO / "a_switch_app.py").is_file():
            say("[warn] 内置引擎不带 A-SWITCH 窗口，面板本身就是入口")
        else:
            try:
                # 用当前解释器同目录的 pythonw：PATH 上的 vendor python 没有 pywebview，GUI 会静默失败
                pyw = Path(sys.executable).with_name("pythonw.exe")
                if not pyw.is_file():
                    pyw = Path(sys.executable)
                subprocess.Popen(f'cmd /c start "" /D "{REPO}" "{pyw}" a_switch_app.py', shell=True,
                                 creationflags=CREATE_NO_WINDOW)
                say("A-SWITCH GUI 已拉起")
            except Exception:
                pass
    say("=" * 62)
    fail = 0
    for name, ok, note in results:
        if not ok:
            fail += 1
        say(f"  {'[ok]' if ok else '[x]'} {name}: {note}")
    say("=" * 62)
    return 1 if fail else 0


def cmd_stop(targets):
    say("")
    for key in targets:
        p = PROVIDERS[key]
        try:
            (p["stop"])(p)
        except Exception as e:
            say(f"    {p['name']}: 停止异常 {type(e).__name__}: {e}")
    return 0


def cmd_smoke(targets):
    say("")
    rc = 0
    for key in targets:
        p = PROVIDERS[key]
        if not p.get("smoke"):
            say(f"[{key}] {p['name']}: 无冒烟测试")
            continue
        say(f"[{key}] {p['name']} 冒烟 …")
        try:
            ok, note = (p["smoke"])(p)
        except Exception as e:
            ok, note = False, f"{type(e).__name__}: {e}"
        say(f"    {'[ok]' if ok else '[x]'} {note}")
        if not ok:
            rc = 1
    return rc


def cmd_list():
    say("")
    for key, p in PROVIDERS.items():
        en = "" if p["enabled"] else "（停用，仅 status/stop 可用）"
        say(f"  {key:<6} {p['name']:<24} :{p['port']:<6} {p['proto']}{en}")
    say("")
    say("  加新代理：在 PROVIDERS 注册表加一项。")
    return 0


# ---------------------------- 交互式开关菜单 ----------------------------
def _listen_pids():
    """一次 netstat 拿到 {port: pid}，比逐个 PowerShell 查快。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=15, creationflags=CREATE_NO_WINDOW).stdout
    except Exception:
        return {}
    m = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            tail = parts[1].rsplit(":", 1)[-1]
            if tail.isdigit():
                m[int(tail)] = int(parts[4])
    return m


def _toggle(key):
    p = PROVIDERS[key]
    try:
        ok, _note = p["status"](p)
    except Exception:
        ok = False
    if ok:
        say(f"  停止 {p['name']} …")
        p["stop"](p)
        return
    say(f"  启动 {p['name']} …")
    try:
        ok2, note2 = p["start"](p)
    except Exception as e:
        ok2, note2 = False, f"{type(e).__name__}: {e}"
    say(f"  {'[ok]' if ok2 else '[x]'} {note2}")
    if ok2 and callable(p.get("post_start")):
        try:
            ok3, note3 = p["post_start"](p)
            say(f"  {'[ok]' if ok3 else '[warn]'} {note3}")
        except Exception as e:
            say(f"  [warn] post_start 异常 {type(e).__name__}: {e}")


MENU_MUTEX_NAME = "Local\\AIProxyHub.ConsoleMenu"


def cmd_menu():
    """按钮式开关：输入编号切换单个网关，a 全开，s 全关。"""
    if acquire_single_instance(MENU_MUTEX_NAME) is None:
        say("")
        say("  [!] 已经有一个 AI 反代开关窗口在运行。")
        say("      先关掉那个窗口，或者改用 AI反代面板.bat。")
        say("")
        return 3
    keys = list(PROVIDERS)
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        pids = _listen_pids()
        say("")
        say("  ╔══════════════════════════════════════════════════════════╗")
        say("  ║   AI 反代开关 —— 用的才开，不用就关，省内存              ║")
        say("  ╚══════════════════════════════════════════════════════════╝")
        for i, key in enumerate(keys, 1):
            p = PROVIDERS[key]
            try:
                ok, note = p["status"](p)
            except Exception:
                ok, note = False, ""
            pid = pids.get(p["port"])
            state = f"● 运行中 pid {pid}" if ok and pid else ("● 运行中" if ok else "○ 已停止")
            say(f"   [{i}] {p['name']:<20} :{p['port']:<6} {state:<20} {note}")
        say("  ────────────────────────────────────────────────────────────")
        say("   编号 = 开/关切换     a = 全部启动     s = 全部停止")
        say("   r = 刷新            q = 退出")
        say("")
        try:
            choice = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            say("")
            return 0
        if choice in ("q", "quit", "exit"):
            say("  已退出（网关保持当前状态）")
            return 0
        if choice in ("", "r"):
            continue
        if choice == "a":
            cmd_start([k for k in keys if PROVIDERS[k]["enabled"]])
            input("\n  按回车继续 …")
            continue
        if choice == "s":
            cmd_stop(keys)
            input("\n  按回车继续 …")
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(keys):
            say("")
            _toggle(keys[int(choice) - 1])
            input("\n  按回车刷新 …")
            continue
        say("  [?] 无效输入，按回车重试")
        input()


def main():
    args = [a for a in sys.argv[1:] if a not in ("--gui", "--no-wait")]
    cmd = args[0].lower() if args else "status"
    rest = args[1] if len(args) > 1 else None
    if cmd == "status":
        return cmd_status()
    if cmd == "start":
        return cmd_start(_resolve(rest))
    if cmd == "stop":
        return cmd_stop(_resolve(rest))
    if cmd == "smoke":
        return cmd_smoke(_resolve(rest or "all"))
    if cmd == "list":
        return cmd_list()
    if cmd == "heal":
        changed, note = heal_auto()
        say(f"[heal] {"票已更新" if changed else "票无变化"}: {note}")
        return 0
    if cmd in ("menu", "ui"):
        return cmd_menu()
    say(__doc__)
    return 2


if __name__ == "__main__":
    import re
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
# -*- coding: utf-8 -*-
# ---- ai_hub.py 第五段：doubao 插件（cookie 型，第一个混合接入） ----



