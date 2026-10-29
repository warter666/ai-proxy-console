# -*- coding: utf-8 -*-
"""豆包代理会话管理 —— 确保代理请求不会污染你正在用的对话。

问题背景（2026-10-27 查实）：
    relay 带空 conversation_id 请求时，豆包服务端会把它**追加到该账号最近的
    一个会话**。如果那个会话正好是你在手机/网页上聊的，就会被塞进代理的测试内容
    （历史上 38445638943124994 就这样被污染了十几条）。

方案 B（已实施在 relay.mjs）：
    首次请求后把上游分配的会话 ID 固化到 ~/.doubao-relay/config.json，
    之后所有请求都发往那个「代理专用会话」，与你自己的对话彻底分开。

用法：
    python doubao_session.py status     # 看当前绑定的专用会话
    python doubao_session.py reset      # 解绑（下次请求会重新绑一个）
    python doubao_session.py list       # 列出账号下最近的会话（只读）
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

RELAY_DIR = Path.home() / ".doubao-relay"
CONFIG = RELAY_DIR / "config.json"
BASE = Path(__file__).resolve().parent


def _cfg():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cmd_status():
    c = _cfg()
    conv = c.get("conversationId")
    print("配置文件:", CONFIG, "(存在)" if CONFIG.is_file() else "(不存在)")
    if conv:
        print("专用会话:", conv)
        print("  绑定时间:", c.get("conversationBoundAt") or "未知")
        print("  备注    :", c.get("conversationNote") or "—")
        print("\n所有代理请求都会发到这个会话，不会碰你自己的对话。")
    else:
        print("专用会话: 未绑定")
        print("\n⚠ 下次请求时会自动绑定一个 —— 但豆包会选「最近的会话」，")
        print("  如果那个会话是你在用的，仍可能被污染。建议先手动建一个空会话，")
        print("  或直接跑一次测试请求让它绑定下来，再确认绑定的是哪个。")
    print("\n隔离开关:", "开启" if c.get("isolate") is not False else "关闭（老行为，会污染）")


def cmd_reset():
    c = _cfg()
    if not c.get("conversationId"):
        print("本来就没绑定，无需重置。")
        return 0
    old = c.pop("conversationId", None)
    c.pop("conversationBoundAt", None)
    c.pop("conversationNote", None)
    CONFIG.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已解绑旧会话:", old)
    print("下次请求会重新绑定（relay 需重启才读到新配置）。")
    return 0


def cmd_list():
    """只读查询两个账号下最近的会话，不发送任何消息。"""
    probe = BASE / "doubao_probe_conv.mjs"
    if not probe.is_file():
        print("缺少只读探针脚本 doubao_probe_conv.mjs")
        return 1
    cur = _cfg().get("conversationId")
    for cookie in ("cookies-cdp.json", "cookies-consumer.json"):
        if not (RELAY_DIR / cookie).is_file():
            continue
        tag = "豆包工作" if cookie == "cookies-cdp.json" else "普通豆包"
        print("=" * 66)
        print("【%s】%s" % (tag, cookie))
        r = subprocess.run(["node", str(probe), "10", cookie],
                           capture_output=True, text=True, encoding="utf-8",
                           cwd=str(BASE))
        out = (r.stdout or "").strip()
        for line in out.splitlines():
            if line.startswith("查询账号") or line.startswith("返回码"):
                continue
            if line.startswith("  3") or line.startswith("  1"):
                mark = "  ← 代理绑定" if cur and cur in line else ""
                print(line + mark)
            elif line and not line.startswith("完整响应"):
                print("  " + line)
        if r.returncode != 0 and r.stderr:
            print("  错误:", r.stderr[:300])
        print()
    if cur:
        print("代理当前绑定的会话:", cur)
    else:
        print("代理当前未绑定会话（下次请求时自动绑定）")
    return 0


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    if cmd == "status":
        cmd_status(); return 0
    if cmd == "reset":
        return cmd_reset()
    if cmd == "list":
        return cmd_list()
    print("用法:\n  doubao_session.py status\n  doubao_session.py reset\n  doubao_session.py list")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
