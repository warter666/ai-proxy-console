# -*- coding: utf-8 -*-
"""网关规格表 —— 单一事实来源（Single Source of Truth）。

目的：新增一个网关（或加一个海外/国内双实例）时，只改这一处，
面板、消耗统计、签到、抽屉、客户端按钮全部自动跟上。

之前的问题（2026-10-27 盘点）：一个网关的"能力"散落在 6~7 个地方——
  ai_hub.PROVIDERS（启停/端口）         gateway_ui.PANEL_ORDER（顺序）
  gateway_ui.TOKENS / BADGES（token/角标） gateway_ui.APP_CANDIDATES（客户端路径）
  gateway_ui.html 的 DEMO_PROVIDERS      renderUsage 里的 row(...) 调用
  channelHtml 里的 hardcode（hasCheckin / dashboard 判断）
  DRAWER_MAP（抽屉渲染）
加一个网关要改 7 处，漏一处就出「豆包那样切换报未知角色」的问题。

本模块把上面全部收敛成 GW_SPECS。前端通过 Api().gw_specs() 拿同一份数据，
不在 HTML 里重复写。

## 加新网关 / 加海外版的做法

1. 在 GW_SPECS 里加一条（或给已有条目换个 key）；
2. 在 ai_hub.PROVIDERS 里加对应的启停函数（真正的进程管理仍在 ai_hub）；
3. 前端 DRAWER_MAP 只有在该网关有**特殊抽屉结构**时才需要加；
   普通账号池会走通用 mapper。

## 双实例（国内/海外）约定

同一个能力用 `family` 字段归组，`region` 区分区域：
  family="qoder", region="cn"  → key="qoder"
  family="qoder", region="intl" → key="qoder_intl"
共用同一套 renderUsage 行模板、同一个 DRAWER_MAP mapper（按 family 取），
只是账号池/端口/客户端路径不同。
"""
from __future__ import annotations

# 能力位：前端据此决定显示哪些按钮/行，不需要再写 p.key === "xxx"
#   start_stop    可启停（所有网关都有）
#   checkin       有每日签到
#   usage         计入「今日消耗」
#   drawer        有账号/积分抽屉
#   dashboard     有独立 Web 面板入口
#   client        有可打开的桌面客户端
#   pool          是账号池型（多账号），否则单账号
GW_SPECS = [
    {
        "key": "auto",
        "family": "autoclaw",
        "region": "cn",
        "name": "AutoClaw",
        "full_name": "AutoClaw(国内)",
        "badge": "A",
        "color": "#46a758",
        "proto": "Anthropic + OpenAI",
        "port": 18766,
        "token": "autoclaw-local",
        "caps": ["start_stop", "checkin", "usage", "drawer", "client", "pool"],
        "clients": (),            # 本机安装路径放 clients.local.json（不入库）
        "checkin_reset": ("每日 0:00 (本地) 刷新",
                          "AutoClaw 任务/灵感中心按本地自然日零点轮转，今日可领额度到 0:00 重置。"),
    },
    {
        "key": "auto_intl",
        "family": "autoclaw",
        "region": "intl",
        "name": "AutoClaw(海外)",
        "full_name": "AutoClaw(海外)",
        "badge": "A",
        "color": "#46a758",
        "proto": "Anthropic + OpenAI",
        "port": None,            # 海外暂无独立网关端口，仅客户端
        "token": None,
        "caps": ["client"],
        "clients": (),
        "checkin_reset": None,
    },
    {
        "key": "wb",
        "family": "workbuddy",
        "region": "cn",
        "name": "WorkBuddy",
        "full_name": "WorkBuddy",
        "badge": "W",
        "color": "#3b82f6",
        "proto": "OpenAI",
        "port": 8787,
        "token": "workbuddy-local",
        "tokenEnv": "WB_LOCAL_KEY",
        "caps": ["start_stop", "checkin", "usage", "drawer", "client"],
        "clients": (),
        "checkin_reset": ("每日 0:00 (本地) 刷新",
                          "WorkBuddy 按本地自然日零点轮转，今日额度到 0:00 重置。"),
    },
    {
        "key": "qoder",
        "family": "qoder",
        "region": "cn",
        "name": "Qoder",
        "full_name": "Qoder CN",
        "badge": "Q",
        "color": "#a855f7",
        "proto": "OpenAI",
        "port": 8790,
        "token": "qoder-local",
        "caps": ["start_stop", "checkin", "usage", "drawer", "dashboard", "client"],
        "clients": (
            r"%LOCALAPPDATA%\Qoder CN\Qoder CN Launcher\Qoder CN Launcher.exe",
        ),
        "checkin_reset": ("每日 10:00 (UTC+8) 刷新",
                          "Qoder 官方规则：每日 10:00（北京时间）刷新名额，错过不补。"),
        # 海外版预留：解开下面这段、补 ai_hub 启停函数即可
        # "intl": {"port": 8791, "token": "qoder-intl-local"},
    },
    {
        "key": "doubao",
        "family": "doubao",
        "region": "cn",
        "name": "DoubaoWork",
        "full_name": "豆包工作",
        "badge": "D",
        "color": "#f59e0b",
        "proto": "OpenAI (cookie)",
        "port": 18770,
        "token": "doubao-local-key",
        "caps": ["start_stop", "checkin", "usage", "drawer", "client"],
        "clients": (),
        "checkin_reset": ("每日 0:00 (本地) 刷新",
                          "豆包工作按本地自然日零点轮转，今日可领额度到 0:00 重置。"),
        # 豆包特有：签到与反代可能是两个不同账号，需要账号切换能力
        "multi_account": True,
    },
]

# 纯客户端（无网关，不开面板卡片）
CLIENT_ONLY = [
    {
        "key": "qoder_intl",
        "label": "Qoder 国际版",
        "clients": (
            r"%LOCALAPPDATA%\Qoder\Qoder Launcher\Qoder Launcher.exe",
        ),
    },
    {
        "key": "dsh",
        "label": "DSH",
        "clients": (),
    },
    {
        "key": "zcode",
        "label": "ZCode",
        "clients": (),
    },
]


def _load_client_overrides():
    """读本机客户端路径：仓库根目录 clients.local.json（已 gitignore）。

    格式：{"overrides": {"auto": ["D:\\\\...\\\\AutoClaw2.exe"], "dsh": [...]}}
    值为字符串或字符串数组；%LOCALAPPDATA% / %APPDATA% 由使用方展开。
    文件缺失或损坏一律当没有覆盖，不影响内置候选。
    """
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parent / "clients.local.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    ov = data.get("overrides") if isinstance(data, dict) else None
    if not isinstance(ov, dict):
        return {}
    out = {}
    for key, val in ov.items():
        if isinstance(val, str):
            val = [val]
        if not isinstance(val, list):
            continue
        paths = tuple(str(x).strip() for x in val if str(x).strip())
        if paths:
            out[str(key)] = paths
    return out


def _apply_client_overrides():
    ov = _load_client_overrides()
    for s in list(GW_SPECS) + list(CLIENT_ONLY):
        if s["key"] in ov:
            s["clients"] = ov[s["key"]]


_apply_client_overrides()


def local_overrides(key):
    """clients.local.json 里 key 的路径列表，也支持非网关条目（如 doubao_consumer）。"""
    return _load_client_overrides().get(str(key), ())


# 面板展示顺序（只放有 start_stop 的）
PANEL_ORDER = [s["key"] for s in GW_SPECS if "start_stop" in s["caps"]]

_BY_KEY = {s["key"]: s for s in GW_SPECS}


def get(key):
    return _BY_KEY.get(key)


def by_family(family):
    return [s for s in GW_SPECS if s["family"] == family]


def has_cap(key, cap):
    s = _BY_KEY.get(key)
    return bool(s and cap in s["caps"])


def keys_with(cap):
    return [s["key"] for s in GW_SPECS if cap in s["caps"]]


def token_of(key):
    """占位 token。环境变量优先，方便换机器时不改代码。

    每条 spec 可用 tokenEnv 显式指定环境变量名；默认 <FAMILY>_LOCAL_KEY。
    wb 历史上用的是 WB_LOCAL_KEY（不是 WORKBUDDY_LOCAL_KEY），必须保留兼容。
    """
    import os
    s = _BY_KEY.get(key) or {}
    default = s.get("token")
    if not default:
        return None
    env = s.get("tokenEnv") or ("%s_LOCAL_KEY" % s["family"].upper())
    return os.environ.get(env) or default


def public_specs():
    """给前端用的安全副本：不含真实凭据，只含占位 token 与能力位。"""
    out = []
    for s in GW_SPECS:
        out.append({
            "key": s["key"],
            "family": s["family"],
            "region": s["region"],
            "name": s["name"],
            "full_name": s["full_name"],
            "badge": s["badge"],
            "color": s["color"],
            "proto": s["proto"],
            "port": s["port"],
            "token": token_of(s["key"]),
            "caps": list(s["caps"]),
            "checkin_reset": (
                {"text": s["checkin_reset"][0], "tip": s["checkin_reset"][1]}
                if s.get("checkin_reset") else None
            ),
            "multi_account": bool(s.get("multi_account")),
        })
    return out
