# -*- coding: utf-8 -*-
"""环境自检 —— 换机器 / 新装时一次看清「还缺什么」。

只读：只查文件是否存在、配置指向哪里、占位环境变量是否设了，
不修改任何文件，也不回显任何真实凭据（只看 URL 与键名）。

面板通过 Api.env_check() 调用；调试可直接跑：
    python env_check.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import ai_hub as hub
import gw_specs

CACHE_TTL = 60.0
_CACHE = {"at": 0.0, "data": None}

# 本机网关地址判据。只看 host，不把整段配置内容带出去。
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _item(key, label, state, detail, impact=""):
    return {
        "key": key, "label": label, "state": state,
        "detail": str(detail or ""), "impact": str(impact or ""),
    }


def _group(key, label, note, items):
    return {
        "key": key, "label": label, "note": note, "items": items,
        "state": _worst(items),
    }


def _worst(items):
    states = [i["state"] for i in items]
    if "fail" in states:
        return "fail"
    if "warn" in states:
        return "warn"
    return "ok"


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _host_of(url):
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except Exception:
        return ""


def _ports_of(urls):
    ports = []
    for u in urls:
        try:
            p = urllib.parse.urlsplit(str(u)).port
        except Exception:
            p = None
        if p and p not in ports:
            ports.append(p)
    return ports


def _fmt_ports(ports):
    return " / ".join(str(p) for p in sorted(ports))


def _check_item(key, label, path, impact, optional=False):
    p = Path(path)
    if p.is_file():
        return _item(key, label, "ok", str(p))
    return _item(key, label, "warn" if optional else "fail",
                 "未找到：%s" % p, impact)


# ---------------------------------------------------------------- 运行环境
def _check_runtime():
    items = []
    ver = "%d.%d.%d" % sys.version_info[:3]
    vstate = "ok" if sys.version_info >= (3, 10) else "warn"
    items.append(_item(
        "python", "Python", vstate, "%s · %s" % (ver, sys.executable),
        "面板与 ai_hub 需要 Python 3.10 及以上" if vstate != "ok" else ""))

    try:
        has_webview = importlib.util.find_spec("webview") is not None
    except Exception:
        has_webview = False
    items.append(_item(
        "pywebview", "pywebview", "ok" if has_webview else "fail",
        "已安装" if has_webview else "未安装",
        "" if has_webview else "没有 pywebview 打不开面板，只能用命令行 ai_hub"))

    node = shutil.which("node")
    if node:
        try:
            r = subprocess.run([node, "-v"], capture_output=True, text=True,
                               timeout=5, creationflags=hub.CREATE_NO_WINDOW)
            nver = (r.stdout or r.stderr or "").strip() or "版本未知"
        except Exception:
            nver = "无法执行"
        head = nver.lstrip("v").split(".", 1)[0]
        major = int(head) if head.isdigit() else 0
        if 0 < major < 22:
            items.append(_item(
                "node", "Node.js", "warn", "%s · %s" % (nver, node),
                "豆包同步用到全局 WebSocket，建议 Node.js 22+；其余网关不受影响"))
        else:
            items.append(_item("node", "Node.js", "ok", "%s · %s" % (nver, node)))
    else:
        items.append(_item(
            "node", "Node.js", "fail", "未找到 node",
            "WorkBuddy / Qoder / 豆包三个网关都是 Node 进程，缺了无法启动"))

    ps = shutil.which("powershell")
    if not ps:
        cand = Path(os.environ.get("SystemRoot") or r"C:\Windows") / \
            "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        ps = str(cand) if cand.is_file() else ""
    items.append(_item(
        "powershell", "PowerShell", "ok" if ps else "warn",
        ps or "未找到 powershell.exe",
        "" if ps else "进程枚举 / 端口查询 / 关闭客户端会降级"))
    return _group("runtime", "运行环境", "面板和网关进程的基础", items)


# ---------------------------------------------------------------- 网关依赖
def _check_deps():
    items = []
    repo = Path(hub.REPO)
    from_env = bool(os.environ.get("A_SWITCH_REPO"))
    if repo.is_dir():
        items.append(_item(
            "repo", "反代仓库", "ok",
            "%s%s" % (repo, "（A_SWITCH_REPO）" if from_env else "（默认路径）")))
    else:
        items.append(_item(
            "repo", "反代仓库", "fail", "未找到：%s" % repo,
            "AutoClaw / Qoder / 豆包网关都从这里启动；设 A_SWITCH_REPO 指向实际仓库"))
        return _group("deps", "网关依赖", "四个网关的源码与辅助脚本", items)

    items.append(_check_item(
        "a_switch", "a_switch.py", repo / "a_switch.py",
        "AutoClaw / Qoder 的启停与注册不可用"))
    items.append(_check_item(
        "importer", "凭证导入脚本", repo / "tools" / "import_v2_account.py",
        "AutoClaw 无法从 vault 导出凭证"))
    qoder_dir = repo / "qoder"
    items.append(_item(
        "qoder_src", "Qoder 网关源码", "ok" if qoder_dir.is_dir() else "fail",
        str(qoder_dir) if qoder_dir.is_dir() else "未找到：%s" % qoder_dir,
        "" if qoder_dir.is_dir() else "Qoder 网关无法启动"))
    items.append(_check_item(
        "doubao_relay", "豆包 relay", repo / "doubao" / "relay.mjs",
        "豆包网关无法启动"))

    proxy = Path(hub.PROXY_DIR)
    items.append(_check_item(
        "wb_proxy", "WorkBuddy 代理", proxy / "workbuddy-proxy.js",
        "WorkBuddy 网关无法启动"))
    items.append(_check_item(
        "scan_mem", "WorkBuddy 取密钥脚本", proxy / "scan_mem.py",
        "WorkBuddy 换密钥后无法自动重扫", optional=True))
    key_file = proxy / "wb-key-payload.json"
    if key_file.is_file():
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(key_file.stat().st_mtime))
        items.append(_item(
            "wb_key", "WorkBuddy 密钥文件", "ok",
            "%s · 更新于 %s（启动时核对 keyId）" % (key_file, stamp)))
    else:
        items.append(_item(
            "wb_key", "WorkBuddy 密钥文件", "warn", "未找到：%s" % key_file,
            "首次启动 WorkBuddy 时会重扫；需先打开客户端并登录"))
    return _group("deps", "网关依赖", "四个网关的源码与辅助脚本", items)


# ---------------------------------------------------------------- 本机客户端
def _default_apps():
    """仅供 `python env_check.py` 单独运行；面板调用时传入自己的 APPS。"""
    lad = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    rad = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")

    def expand(raw):
        return os.path.expandvars(raw.replace("%LOCALAPPDATA%", lad).replace("%APPDATA%", rad))

    cats = [(s["key"], s["full_name"], s["clients"]) for s in gw_specs.GW_SPECS]
    cats += [(c["key"], c["label"], c["clients"]) for c in gw_specs.CLIENT_ONLY]
    apps, paths = [], {}
    for key, label, cands in cats:
        resolved = [expand(x) for x in cands]
        paths[key] = tuple(resolved)
        hit = next((x for x in resolved if os.path.isfile(x)), "")
        apps.append({"key": key, "label": label, "path": hit, "exists": bool(hit)})
    return apps, paths


def _check_apps(apps, app_paths):
    items = []
    for a in apps or []:
        key = a.get("key")
        if a.get("exists"):
            items.append(_item("app:" + key, a.get("label") or key, "ok", a.get("path") or ""))
            continue
        cands = (app_paths or {}).get(key) or ()
        if cands:
            items.append(_item(
                "app:" + key, a.get("label") or key, "warn", "未找到：%s" % cands[0],
                "面板无法一键打开它；已装好后重新检测"))
        else:
            items.append(_item(
                "app:" + key, a.get("label") or key, "warn", "未配置客户端路径",
                "在 clients.local.json 的 overrides 里给 %s 加 exe 路径（见 clients.example.json）" % key))
    if not items:
        items.append(_item("app:none", "本机客户端", "warn", "没有可检测的客户端清单"))
    return _group("apps", "本机客户端", "只影响面板一键打开，不影响反代", items)


# ---------------------------------------------------------------- 客户端接入
def _parse_dsh(text):
    """只抽 llm-pi-ai 里 provider 名 / baseURL / 两个超时，忽略其它字段与插件块。"""
    out, cur, prov_indent, item = [], None, None, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.strip().startswith("- id:"):
            item = line.split(":", 1)[1].strip()
            prov_indent, cur = None, None
            continue
        if item != "llm-pi-ai":
            continue
        indent = len(line) - len(line.lstrip(" "))
        s = line.strip()
        if s == "providers:":
            prov_indent, cur = indent, None
            continue
        if prov_indent is None:
            continue
        if indent <= prov_indent:
            prov_indent, cur = None, None
            continue
        if indent == prov_indent + 2 and s.endswith(":"):
            cur = {"name": s[:-1], "base": "", "timeout": None, "idle": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        if s.startswith("baseURL:"):
            cur["base"] = s.split(":", 1)[1].strip().strip("\"'")
        elif s.startswith("timeoutMs:") or s.startswith("streamIdleTimeoutMs:"):
            try:
                val = int(s.split(":", 1)[1].strip())
            except Exception:
                val = None
            cur["idle" if s.startswith("streamIdle") else "timeout"] = val
    return out


def _check_dsh():
    p = Path.home() / "AppData" / "Roaming" / "dsh-desktop" / \
        "harness" / "profiles" / "web" / "cordis.patch.yml"
    if not p.is_file():
        return _item("dsh", "DSH", "warn", "未检测到 %s" % p,
                     "装好 DSH 并初始化后再检测；不影响其他客户端")
    text = _read_text(p)
    if text is None:
        return _item("dsh", "DSH", "warn", "配置存在但读取失败：%s" % p,
                     "检查文件权限")
    providers = _parse_dsh(text)
    local = [x for x in providers if _host_of(x["base"]) in _LOCAL_HOSTS]
    remote = [x for x in providers if x["base"] and _host_of(x["base"]) not in _LOCAL_HOSTS]
    if not local:
        return _item("dsh", "DSH", "warn", "配置里没有 provider 指向本机网关",
                     "DSH 的请求不会经过本机反代")
    ports = _ports_of([x["base"] for x in local])
    detail = "%d 个 provider 接入本机（%s）" % (len(local), _fmt_ports(ports))
    if remote:
        detail += " · 另有 %d 个直连" % len(remote)
    ac = next((x for x in local if "autoclaw" in x["name"].lower()), None)
    if ac is not None:
        if not ac["timeout"] or ac["timeout"] < 330000:
            detail += "；autoclaw 总超时低于 relay 330s 全池等待"
            return _item("dsh", "DSH", "warn", detail,
                         "长排队时 DSH 会先断开；把 timeoutMs 调到 420000")
        if ac["idle"] is None:
            detail += "；autoclaw 未设置 streamIdleTimeoutMs"
            return _item("dsh", "DSH", "warn", detail,
                         "空流转不会及时切 fallback；补 streamIdleTimeoutMs: 120000")
        if ac["idle"] > 330000:
            detail += "；autoclaw 空流转超时 %ss，guard 无法及时切 fallback" % (ac["idle"] // 1000)
            return _item("dsh", "DSH", "warn", detail,
                         "空流转会久挂；建议 streamIdleTimeoutMs: 120000")
    return _item("dsh", "DSH", "ok", detail)


def _check_zcode():
    p = Path.home() / ".zcode" / "v2" / "provider_config.json"
    if not p.is_file():
        return _item("zcode", "ZCode", "warn", "未检测到 %s" % p,
                     "装好 ZCode 并在面板注册 Qoder 后再检测")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return _item("zcode", "ZCode", "warn", "配置解析失败：%s" % type(exc).__name__,
                     "配置损坏时 ZCode 读不到 provider")
    locals_ = []

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and _host_of(o) in _LOCAL_HOSTS:
            locals_.append(o)

    walk(data)
    if not locals_:
        return _item("zcode", "ZCode", "warn", "配置存在，但没有 provider 指向本机网关",
                     "ZCode 的请求不会经过本机反代")
    return _item("zcode", "ZCode", "ok",
                 "%d 个 provider 接入本机（%s）" % (len(locals_), _fmt_ports(_ports_of(locals_))))


def _check_claude():
    p = Path.home() / ".claude" / "settings.json"
    if not p.is_file():
        return _item("claude", "Claude Code", "warn", "未检测到 %s" % p,
                     "不影响其他客户端；需要时在设置里接本机网关")
    try:
        env = (json.loads(p.read_text(encoding="utf-8")).get("env") or {})
    except Exception as exc:
        return _item("claude", "Claude Code", "warn", "配置解析失败：%s" % type(exc).__name__,
                     "settings.json 损坏时 Claude 启动会报错")
    base = str(env.get("ANTHROPIC_BASE_URL") or "")
    if not base:
        return _item("claude", "Claude Code", "warn", "未设置 ANTHROPIC_BASE_URL",
                     "Claude 会走官方地址，不经过本机反代")
    host = _host_of(base)
    if host in _LOCAL_HOSTS:
        return _item("claude", "Claude Code", "ok",
                     "ANTHROPIC_BASE_URL → %s" % base)
    return _item("claude", "Claude Code", "warn",
                 "当前直连 %s（未经过本机网关）" % host,
                 "直连消耗不走账号池；需要时把 ANTHROPIC_BASE_URL 指到本机网关")


def _check_codex():
    p = Path.home() / ".codex" / "config.toml"
    if not p.is_file():
        return _item("codex", "Codex", "warn", "未检测到 %s" % p,
                     "不影响其他客户端；需要时补 base_url")
    text = _read_text(p) or ""
    bases = re.findall(r"""(?m)^\s*base_url\s*=\s*["']([^"']+)["']""", text)
    local = [b for b in bases if _host_of(b) in _LOCAL_HOSTS]
    if local:
        return _item("codex", "Codex", "ok",
                     "%d 个 base_url 指向本机（%s）" % (len(local), _fmt_ports(_ports_of(local))))
    if bases:
        hosts = sorted({_host_of(b) or b for b in bases})
        return _item("codex", "Codex", "warn",
                     "base_url 指向 %s" % " / ".join(hosts),
                     "没有经过本机网关；需要时改成本机端口")
    return _item("codex", "Codex", "warn", "未配置 base_url",
                 "Codex 会走默认官方地址，不经过本机反代")


def _check_qoder_accounts():
    acc = Path.home() / ".qoder-relay" / "accounts"
    if not acc.is_dir():
        return _item("qoder_accounts", "Qoder 账号池", "warn", "未创建：%s" % acc,
                     "Qoder 网关启动后创建；先在面板 Qoder 抽屉导入本机登录态")
    try:
        files = [p for p in acc.glob("*.json")
                 if p.is_file() and p.name != "active_realm.json"]
    except Exception:
        files = []
    realm = ""
    try:
        realm = str(json.loads((acc / "active_realm.json").read_text(encoding="utf-8"))
                    .get("realm") or "")
    except Exception:
        pass
    realm_label = {"cn": "国内版", "intl": "国际版"}.get(realm, realm or "未知")
    if not files:
        return _item("qoder_accounts", "Qoder 账号池", "warn", "账号池为空",
                     "面板 Qoder 抽屉 → 导入本机登录态")
    return _item("qoder_accounts", "Qoder 账号池", "ok",
                 "%d 个账号 · 当前出口 %s" % (len(files), realm_label))


def _check_configs():
    items = [_check_dsh(), _check_zcode(), _check_claude(),
             _check_codex(), _check_qoder_accounts()]
    return _group("configs", "客户端接入", "各 agent 是否已经接到本机网关", items)


# ---------------------------------------------------------------- 占位 token
def _check_tokens():
    items = []
    for s in gw_specs.GW_SPECS:
        if not s.get("token"):
            continue
        env_name = s.get("tokenEnv") or ("%s_LOCAL_KEY" % s["family"].upper())
        val = os.environ.get(env_name)
        if val:
            items.append(_item(
                "env:" + env_name, "%s（%s）" % (env_name, s["name"]), "ok",
                "已设置"))
        else:
            items.append(_item(
                "env:" + env_name, "%s（%s）" % (env_name, s["name"]), "warn",
                "未设置，回退默认占位值 %s" % s["token"],
                "DSH 用 apiKeyEnv 读取；用户环境里缺失时请求可能被拒"))
    return _group("tokens", "占位 token", "客户端读取的环境变量", items)


# ---------------------------------------------------------------- 入口
def probe(apps=None, app_paths=None, force=False):
    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["at"] < CACHE_TTL:
        return _CACHE["data"]

    t0 = time.perf_counter()
    if apps is None:
        apps, app_paths = _default_apps()
    groups = [
        _check_runtime(),
        _check_deps(),
        _check_apps(apps, app_paths),
        _check_configs(),
        _check_tokens(),
    ]
    summary = {"ok": 0, "warn": 0, "fail": 0, "total": 0}
    for g in groups:
        for it in g["items"]:
            summary[it["state"]] = summary.get(it["state"], 0) + 1
            summary["total"] += 1
    data = {
        "ok": True,
        "checked_at": now,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "summary": summary,
        "groups": groups,
    }
    _CACHE["at"], _CACHE["data"] = now, data
    return data


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(probe(force=True), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
