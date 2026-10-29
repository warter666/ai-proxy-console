# -*- coding: utf-8 -*-
"""一键接入 —— 把「新机器从零到可用」压成一次点击。

固定顺序：补占位 token 环境变量 → 启动所需网关 → 写入 DSH / ZCode provider →
重新体检。只写占位 token、只读本机客户端配置；真实凭据不经过这里。

面板通过 Api().onboard_plan / onboard_start / onboard_status 调用；
调试可以直接跑：
    python onboard.py plan
    python onboard.py run dsh
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import agent_setup as agentsetup
import ai_hub as hub
import env_check as envcheck
import gw_specs

CLIENT_PLANS = {
    "dsh": {
        "label": "DSH", "gateways": ("auto", "wb", "qoder"), "write": True,
        "note": "写入 provider 与超时；改完要完全退出 DSH 再启动才生效",
    },
    "zcode": {
        "label": "ZCode", "gateways": ("auto", "wb", "qoder", "doubao"), "write": True,
        "note": "注册四个本地 provider；写前自动备份",
    },
    "claude": {
        "label": "Claude Code", "gateways": ("auto",), "write": False,
        "note": "只给可复制片段，不自动改 settings.json",
    },
    "codex": {
        "label": "Codex", "gateways": ("auto",), "write": False,
        "note": "只给可复制片段，不自动改 config.toml",
    },
}
OPTIONAL_GATEWAYS = {"doubao"}   # 豆包缺 cookie 时不影响其他客户端
GATEWAY_ORDER = ("auto", "wb", "qoder", "doubao")


# ---------------------------------------------------------------- 只读
def _snapshot(key):
    p = hub.PROVIDERS.get(key)
    if not p:
        return {"key": key, "name": key, "port": None,
                "running": False, "note": "未注册"}
    try:
        ok, note = p["status"](p)
    except Exception as exc:
        ok, note = False, "%s: %s" % (type(exc).__name__, exc)
    return {"key": key, "name": p["name"], "port": p["port"],
            "running": bool(ok), "note": str(note or "")}


def _qoder_accounts():
    acc = Path.home() / ".qoder-relay" / "accounts"
    try:
        return len([p for p in acc.glob("*.json")
                    if p.is_file() and p.name != "active_realm.json"])
    except Exception:
        return 0


def needed_gateways(clients):
    keys = []
    for c in clients:
        for g in (CLIENT_PLANS.get(c) or {}).get("gateways", ()):
            if g not in keys:
                keys.append(g)
    return [g for g in GATEWAY_ORDER if g in keys]


def _env_entry(spec):
    name = spec.get("tokenEnv") or ("%s_LOCAL_KEY" % spec["family"].upper())
    return {"env": name, "gateway": spec["key"], "name": spec["name"],
            "value": spec["token"], "set": bool(os.environ.get(name))}


def plan(apps=None, app_paths=None, force=False):
    """检测现状：客户端、网关、占位 token、环境缺口。只读。"""
    setup = agentsetup.status()
    env = envcheck.probe(apps, app_paths, force=bool(force))
    clients = []
    for row in setup.get("rows") or []:
        meta = CLIENT_PLANS.get(row.get("key"))
        if not meta:
            continue
        gws = list(meta["gateways"])
        clients.append({
            "key": row["key"], "label": row["label"], "path": row.get("path") or "",
            "exists": bool(row.get("exists")), "state": row.get("state"),
            "detail": row.get("detail") or "", "action": row.get("action"),
            "write": meta["write"], "note": meta["note"], "gateways": gws,
            "gateway_names": [hub.PROVIDERS[g]["name"] for g in gws if g in hub.PROVIDERS],
            "selected": bool(meta["write"] and row.get("exists")
                             and row.get("state") != "ok"),
        })

    gateways = []
    for key in GATEWAY_ORDER:
        row = _snapshot(key)
        row["optional"] = key in OPTIONAL_GATEWAYS
        gateways.append(row)

    tokens = [_env_entry(s) for s in gw_specs.GW_SPECS
              if s.get("token") and s.get("key") in GATEWAY_ORDER]

    issues = []
    for group in env.get("groups") or []:
        if group.get("key") not in ("runtime", "deps"):
            continue
        for item in group.get("items") or []:
            if item.get("state") != "ok":
                issues.append({
                    "group": group.get("label") or group.get("key"),
                    "label": item.get("label") or item.get("key"),
                    "state": item.get("state"), "detail": item.get("detail") or "",
                    "impact": item.get("impact") or "",
                })

    return {
        "ok": True,
        "checked_at": time.time(),
        "clients": clients,
        "gateways": gateways,
        "tokens": tokens,
        "issues": issues,
        "blocked": any(i.get("state") == "fail" for i in issues),
        "env_summary": env.get("summary") or {},
        "qoder_accounts": _qoder_accounts(),
        "need": sum(1 for c in clients if c.get("state") != "ok"),
    }


# ---------------------------------------------------------------- 写入
def _broadcast_env_change():
    import ctypes
    from ctypes import wintypes
    result = wintypes.DWORD()
    ctypes.windll.user32.SendMessageTimeoutW(
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, ctypes.byref(result))


def _write_user_env(name, value):
    if os.name != "nt":
        raise OSError("仅支持 Windows 用户环境变量")
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
    try:
        _broadcast_env_change()
    except Exception:
        pass


def _ensure_env(spec):
    name = spec.get("tokenEnv") or ("%s_LOCAL_KEY" % spec["family"].upper())
    row = {"env": name, "gateway": spec["key"], "name": spec["name"],
           "action": "exists", "message": ""}
    if os.environ.get(name):
        return row
    try:
        _write_user_env(name, spec["token"])
        os.environ[name] = spec["token"]   # 本进程内的后续健康检查也立即生效
        row["action"] = "set"
        row["message"] = "已写入用户环境变量（占位值）"
    except Exception as exc:
        row["action"] = "fail"
        row["message"] = "%s: %s" % (type(exc).__name__, exc)
    return row


def _start_one(key):
    p = hub.PROVIDERS[key]
    try:
        ok, note = p["status"](p)
    except Exception:
        ok, note = False, ""
    # Qoder 即使已在运行也要走 start：池为空时会顺带导入本机登录态
    if ok and not (key == "qoder" and _qoder_accounts() == 0):
        return True, "已在运行"
    try:
        ok, note = p["start"](p)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    note = str(note or "")
    if ok and callable(p.get("post_start")):
        try:
            ok2, note2 = p["post_start"](p)
            if note2:
                note = ("%s；%s" % (note, note2)) if note else str(note2)
            if not ok2:
                note = "%s（后置步骤失败：%s）" % (note, note2)
        except Exception as exc:
            note = "%s（后置步骤异常：%s: %s）" % (
                note, type(exc).__name__, exc)
    return bool(ok), note


def run(clients, emit=None):
    """执行接入。emit(step, item, state, message) 用于面板实时进度。"""
    def say(step, item, state, message=""):
        if emit:
            try:
                emit(step, str(item), str(state), str(message or ""))
            except Exception:
                pass

    clients = [c for c in CLIENT_PLANS if c in set(clients or [])]
    needed = needed_gateways(clients)
    res = {"ok": True, "clients": clients, "needed_gateways": needed,
           "env": [], "gateways": [], "configs": [], "snippets": {},
           "verify": None, "message": ""}

    # 1 占位 token 环境变量
    say("env", "tokens", "running", "检查占位 token…")
    failed = 0
    changed = 0
    for spec in gw_specs.GW_SPECS:
        if spec.get("key") not in needed or not spec.get("token"):
            continue
        row = _ensure_env(spec)
        res["env"].append(row)
        if row["action"] == "set":
            changed += 1
        elif row["action"] == "fail":
            failed += 1
    if failed:
        say("env", "tokens", "warn", "%d 个环境变量写入失败（不影响已有占位值）" % failed)
    else:
        say("env", "tokens", "ok", ("已补齐 %d 个" % changed) if changed else "占位 token 已齐全")

    # 2 启动网关
    for key in needed:
        p = hub.PROVIDERS.get(key)
        if not p:
            res["gateways"].append({"key": key, "ok": False, "note": "未注册"})
            say("gateways", key, "fail", "未注册")
            res["ok"] = False
            continue
        say("gateways", key, "running", "启动中…")
        ok, note = _start_one(key)
        res["gateways"].append({"key": key, "ok": bool(ok), "note": note,
                                "name": p["name"], "port": p["port"],
                                "optional": key in OPTIONAL_GATEWAYS})
        if ok:
            say("gateways", key, "ok", note or "已启动")
        elif key in OPTIONAL_GATEWAYS:
            say("gateways", key, "warn", note or "未启动（可选）")
        else:
            res["ok"] = False
            say("gateways", key, "fail", note or "启动失败")

    # 3 写客户端配置
    for key in clients:
        meta = CLIENT_PLANS[key]
        if meta["write"]:
            say("configs", key, "running", "写入中…")
            try:
                r = agentsetup.apply(key, add_missing=(key == "dsh"))
            except Exception as exc:
                r = {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc)}
            row = {"key": key, "label": meta["label"], "ok": bool(r.get("ok")),
                   "message": r.get("message") or "",
                   "backup": r.get("backup") or "",
                   "changes": r.get("changes") or r.get("results") or []}
            res["configs"].append(row)
            if r.get("ok"):
                say("configs", key, "ok",
                    (r.get("message") or "已写入") + ("（已备份）" if r.get("backup") else ""))
            else:
                res["ok"] = False
                say("configs", key, "fail", r.get("message") or "写入失败")
        else:
            p = agentsetup.preview(key)
            if p.get("snippet"):
                res["snippets"][key] = p["snippet"]
            res["configs"].append({"key": key, "label": meta["label"], "ok": True,
                                   "manual": True, "message": "只提供片段，需手动合并",
                                   "backup": "", "changes": []})
            say("configs", key, "warn", "只提供片段，需手动合并")

    # 4 验证
    for key in needed:
        st = _snapshot(key)
        if st["running"]:
            say("verify", "gw:" + key, "ok", st["note"] or "在线")
        elif key in OPTIONAL_GATEWAYS:
            say("verify", "gw:" + key, "warn", st["note"] or "未启动（可选）")
        else:
            say("verify", "gw:" + key, "fail", st["note"] or "离线")
    try:
        status = agentsetup.status()
        rows = {r["key"]: r for r in status.get("rows") or []}
    except Exception:
        rows = {}
    for key in clients:
        row = rows.get(key) or {}
        if CLIENT_PLANS[key]["write"]:
            ok = row.get("state") == "ok"
            say("verify", key, "ok" if ok else "warn", row.get("detail") or "")
        else:
            say("verify", key, "warn", "片段已生成；粘贴后再看环境自检")

    try:
        env = envcheck.probe(force=True)
    except Exception as exc:
        env = {"summary": {}, "groups": [], "message": str(exc)}
    res["verify"] = {
        "env_summary": env.get("summary") or {},
        "configs": [{"key": k, "label": v.get("label") or k,
                     "state": v.get("state"), "detail": v.get("detail") or ""}
                    for k, v in rows.items()],
        "qoder_accounts": _qoder_accounts(),
    }
    if not res["ok"]:
        res["message"] = "部分步骤未完成，看下方逐项结果"
    elif res["snippets"]:
        manual = [CLIENT_PLANS[k]["label"] for k in clients
                  if not CLIENT_PLANS[k]["write"]]
        res["message"] = "自动接入已完成；%s 的片段需要手动合并" % " / ".join(manual)
    else:
        res["message"] = "接入完成"
    return res


def _cli():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = sys.argv[1:]
    if args and args[0] == "run":
        clients = [x for x in args[1:] if x in CLIENT_PLANS] or ["dsh"]
        print(json.dumps(run(clients), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(plan(force=True), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
