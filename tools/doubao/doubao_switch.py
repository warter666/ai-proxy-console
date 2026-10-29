# -*- coding: utf-8 -*-
"""把豆包反代（18770）切到指定账号的 cookie。

背景：豆包有两条链路，各自抓各的 cookie，默认不是同一个账号。
  签到     -> cookies-consumer.json  普通豆包客户端 Doubao.exe
  反代     -> cookies-cdp.json       豆包工作客户端
  两个 sessionid 不同 = 不同账号 => 签到领的额度反代用不上。

relay.mjs 的 DIR 硬编码为 ~/.doubao-relay，只认 cookies-cdp.json / cookies.json，
所以这里用"换文件"而不是改它：先把当前账号备份成 cookies-<角色>.json，再把目标
角色的 cookie 覆盖到 cookies-cdp.json。relay 按 mtime 热重载，无需重启。

用法：
    python doubao_switch.py status            # 看当前用的是哪个账号
    python doubao_switch.py list              # 列出两个账号的 cookie
    python doubao_switch.py use gateway       # 切回豆包工作
    python doubao_switch.py use consumer      # 切到普通豆包（签到那个账号）
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

RELAY_DIR = Path.home() / ".doubao-relay"
ACTIVE = RELAY_DIR / "cookies-cdp.json"
ROLES = {
    "gateway": ("cookies-gateway.json", "豆包工作（反代默认）"),
    "consumer": ("cookies-consumer.json", "普通豆包（签到账号）"),
}
HEALTH = "http://127.0.0.1:18770/health"


def _fp(path: Path):
    """取 cookie 文件里 sessionid 的不可逆指纹，用于判断是哪个账号。"""
    import hashlib
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    jar = raw.get("jar") or {}
    if not jar and isinstance(raw, list):
        jar = {c.get("name"): c.get("value") for c in raw if isinstance(c, dict)}
    sid = jar.get("sessionid") or ""
    if not sid:
        return None, len(jar)
    return hashlib.sha256(str(sid).encode()).hexdigest()[:10], len(jar)


def _age(path: Path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        saved = raw.get("savedAt") if isinstance(raw, dict) else None
        if not saved:
            return None
        if isinstance(saved, (int, float)):
            ts = saved / 1000.0 if saved > 1e11 else saved
        else:
            import datetime
            dt = datetime.datetime.fromisoformat(str(saved).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            ts = dt.timestamp()
        return int((time.time() - ts) / 60)
    except Exception:
        return None


def _health():
    try:
        with urllib.request.urlopen(HEALTH, timeout=6) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _age_text(m):
    if m is None:
        return "未知"
    if m < 120:
        return "%d 分钟前" % m
    if m < 2880:
        return "%.1f 小时前" % (m / 60.0)
    return "%.1f 天前" % (m / 1440.0)


def cmd_status():
    h = _health()
    print("反代 18770:", "运行中" if h else "未运行")
    if h:
        ck = h.get("cookies") or {}
        print("  cookie ok :", ck.get("ok"), "| 项数:", ck.get("count"))
        print("  模型      :", ", ".join(h.get("models") or []))
    fp, n = _fp(ACTIVE) if ACTIVE.is_file() else (None, None)
    print("\n当前生效账号指纹:", fp or "未知", "| 项数:", n)
    for role, (fname, label) in ROLES.items():
        p = RELAY_DIR / fname
        if not p.is_file():
            continue
        rfp, rn = _fp(p)
        mark = " ← 当前" if rfp and rfp == fp else ""
        print("  %-9s %-22s fp=%s 项数=%s %s%s" % (
            role, fname, rfp or "?", rn, _age_text(_age(p)), mark))


def cmd_list():
    for role, (fname, label) in ROLES.items():
        p = RELAY_DIR / fname
        exists = p.is_file()
        fp, n = _fp(p) if exists else (None, None)
        print("%-9s %-14s %s" % (role, "存在" if exists else "缺失",
                                 "%s | fp=%s | 项数=%s | %s" % (
                                     label, fp or "?", n, _age_text(_age(p)) if exists else "-")))


def cmd_use(role, force=False):
    if role not in ROLES:
        print("未知角色：%s（可选 %s）" % (role, "/".join(ROLES)))
        return 1
    # 切到普通豆包有污染风险：那是用户在手机端日常使用的账号，
    # relay 绑定后代理请求会被豆包归并到「最近活跃会话」= 用户正在聊的那条。
    if role == "consumer" and not force:
        print("⚠ 危险操作：把反代切到「普通豆包」账号")
        print("   该账号是你在手机端日常使用的。relay 绑定后，代理请求会被豆包")
        print("   服务端归并到该账号「最近活跃的会话」——很可能正是你正在聊的那条，")
        print("   从而把测试/代理内容写进你的真实对话（2026-10-27 已实际发生）。")
        print("")
        print("   面板已禁用此操作。确认要用命令行强制切换，请加 --force：")
        print("     python doubao_switch.py use consumer --force")
        return 3
    src = RELAY_DIR / ROLES[role][0]
    if not src.is_file():
        print("源 cookie 不存在：%s" % src)
        return 1
    # 先把当前生效的备份回它所属角色，避免切换丢失
    cur_fp, _ = _fp(ACTIVE) if ACTIVE.is_file() else (None, None)
    if cur_fp:
        for r, (fname, _l) in ROLES.items():
            p = RELAY_DIR / fname
            if p.is_file():
                fp, _n = _fp(p)
                if fp == cur_fp and r != role:
                    # 当前账号已由该角色的文件代表，无需额外备份
                    break
    bak = RELAY_DIR / "cookies-cdp.json.bak-switch"
    if ACTIVE.is_file():
        shutil.copy2(ACTIVE, bak)
    shutil.copy2(src, ACTIVE)
    # 关键：会话 ID 是「账号内唯一」的，跨账号无效。
    # 切换账号必须清掉 config.json 里的 conversationId，否则 relay 会拿
    # A 账号的会话 ID 去请求 B 账号 —— 要么报错，要么被服务端归并到 B 的
    # 最近会话（正是污染用户对话的路径）。
    cleared = _clear_bound_conversation()
    time.sleep(1.5)
    h = _health()
    ok = bool(h and (h.get("cookies") or {}).get("ok"))
    print("已切换到 %s，relay cookie ok=%s（备份 %s）" % (ROLES[role][1], ok, bak.name))
    if cleared:
        print("已清除旧会话绑定 %s（账号已变，会话 ID 不再有效）；"
              "下次请求会自动重新绑定，建议随后确认绑定到哪个会话。" % cleared)
    return 0 if ok else 1


def _clear_bound_conversation():
    """清除 config.json 里的会话绑定，返回被清掉的 ID（无则 None）。"""
    import json as _json
    cfg = RELAY_DIR / "config.json"
    if not cfg.is_file():
        return None
    try:
        c = _json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return None
    old = c.pop("conversationId", None)
    c.pop("conversationBoundAt", None)
    c.pop("conversationNote", None)
    if old is None:
        return None
    try:
        cfg.write_text(_json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        return None
    return old


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    if cmd == "status":
        cmd_status()
        return 0
    if cmd == "list":
        cmd_list()
        return 0
    if cmd == "use":
        if len(argv) < 3:
            print("用法: doubao_switch.py use <gateway|consumer> [--force]")
            return 2
        return cmd_use(argv[2].lower(), force=("--force" in argv))
    print("用法:\n  doubao_switch.py status\n  doubao_switch.py list\n"
          "  doubao_switch.py use <gateway|consumer> [--force]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
