"""qoder_accounts.py —— Qoder 双区域账号池、OAuth 设备授权与凭证生命周期

覆盖与 WorkBuddy 网关同等完整的账号能力：

  - 双区域常量表 REALM_CONFIGS（国内 qoder.com.cn / 国际 qoder.com）
  - OAuth 设备授权（PKCE S256，浏览器授权 + /deviceToken/poll 轮询，
    dt- 30 天 / drt- 1 年）——免桌面客户端一键登录
  - PAT 导入（pt- 长期令牌 -> jobToken 交换 jt-/jrt-）
  - 按 token 前缀路由的刷新（drt- -> deviceToken/refresh；
    jrt- -> jobToken/refresh，失败回落 PAT 重新交换）
  - 每日签到 / 额度（quota）/ 套餐（plan）查询
  - 会话亲和（同一对话固定落到同一账号）与轮询负载
  - 账号导入导出（Dry-Run 预检）与 JSON 持久化（原子写）
"""
import base64
import datetime
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import uuid

from qoder_fingerprint import (derive_id, generate_request_id,
                               derive_machine_token, derive_machine_type,
                               vm_status)

# ---------------------------------------------------------------------------
# 区域常量（逆向自官方桌面/CLI 客户端）
# ---------------------------------------------------------------------------
REALM_CONFIGS = {
    "cn": {
        "name": "国内版 (China)",
        "openapi": "https://openapi.qoder.com.cn",
        "gateway": "https://gateway.qoder.com.cn",
        "website": "https://qoder.com.cn",
        "client_id": "1c5e33e1-364d-4ce6-b02c-acaa81274a5c",
        "redirect_uri": "qoder-work-cn://",
        "domain": "qoder.com.cn",
        "ua": "QoderWork/1.1.64",
        # has_checkin 只是"历史上该区域曾开放 sash 签到"的提示位，**不再作为
        # 门控**：能力改为运行时探测（见 Account.checkin_capability）。官方把
        # 每日领取活动搬到 campaign 平台后，任何区域都可能新增/下线接口。
        "has_checkin": True,
        "send_client_id": True,    # CN 设备授权 URL: client_id + machine_id + redirect_uri
        "send_redirect_uri": True,
        "nonce_dashed": True,      # CN nonce 使用带横线 uuid
        "home_dir": ".qoder-cn",   # 官方客户端本地目录（凭证 / 模型目录缓存）
        "app_dir": "com.qodercn.app.stable",   # 桌面 App Roaming 数据目录
        "algo_prefix": "/algo",    # 推理路径前缀（cn/intl 两端点一致）
        "models_prefix": "/algo",
        "auth_file": "auth.v1.dat",
    },
    "qworkcn": {
        # 千问办公（QoderWork CN，D:\QwenWorkCN，%APPDATA%\QwenWorkCN）：
        # 同 COSY 签名体系，但网关独立（gateway.qwenwork.cn）、路径无 /algo
        # 前缀、凭证为 auth-v2.dat（schemaVersion=2，Ory JWT + ory_rt_ 刷新
        # 令牌）。ory_rt_ 不走 deviceToken/jobToken 刷新端点（实测 400，
        # 前缀校验拒绝），到期后需从客户端重新同步。
        "name": "千问办公 (QwenWork CN)",
        "openapi": "https://gateway.qwenwork.cn",
        "gateway": "https://gateway.qwenwork.cn",
        "website": "https://qwenwork.cn",
        "client_id": "",
        "redirect_uri": "",
        "domain": "qwenwork.cn",
        "ua": "QoderWork/1.1.64",
        "has_checkin": False,
        "send_client_id": False,
        "send_redirect_uri": False,
        "nonce_dashed": True,
        "home_dir": ".qwenworkcn",
        "app_dir": "QwenWorkCN",
        "algo_prefix": "/algo",     # agent_chat_generation 实证带 /algo
        "models_prefix": "",        # model/list 实证不带（CLI 日志）
        "auth_file": "auth-v2.dat",
        "no_refresh": True,
        # model/list 按场景分区返回，本区只有 qwork 有货（chat 为空数组）。
        # 取错场景时服务端查不到用户目录，SSE 里内嵌 503 Model catalog
        # unavailable。
        "model_scene": "qwork",
        # 上行 body 必须声明工作台形态，否则同样命中上面的 503：取值即
        # worker 在 QODER_WORK_INTEGRATION_MODE=1 下的 session_type 与
        # business.product。
        "session_type": "qoder_work",
        "business_product": "qoder_work",
    },
    "intl": {
        "name": "国际版 (Global)",
        "openapi": "https://openapi.qoder.sh",
        # 推理主机取自 0.4.3 客户端的 endpoint 缓存/内置候选（api1 主选，
        # api2/api3 为官方故障切换域名）：api1 连不上时按顺序切换。
        "gateway": "https://api1.qoder.sh",
        "gateway_fallbacks": ("https://api2.qoder.sh", "https://api3.qoder.sh"),
        "website": "https://qoder.com",
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "redirect_uri": "qoder://aicoding.aicoding-agent/login-success",
        "domain": "qoder.com",
        "ua": "Qoder/1.1.64",
        # 国际版目前 /sash/api/v1/me/daily-check-in/* 返回 404（实测），但活动
        # 页面同样挂着"每日领取 100 Credits"。该字段仅作提示，门控靠运行时探测。
        "has_checkin": False,
        "send_client_id": True,    # Intl 设备授权 URL: client_id + machine_id (无 redirect_uri)
        "send_redirect_uri": False,
        "nonce_dashed": False,     # intl nonce 为 32-hex uuid-simple
        "home_dir": ".qoder",
        "app_dir": "com.qoder.app.stable",
        "algo_prefix": "/algo",
        "models_prefix": "/algo",
        "auth_file": "auth.v1.dat",
    },
}

CLIENT_UA = "Go-http-client/2.0"
LOGIN_TTL_SECONDS = 600
# 账号文件写入锁（/tasks 并行刷新时多个字段各自 save，必须串行落盘）
_SAVE_LOCK = threading.Lock()
DEFAULT_USER_TYPE = "personal_professional_trial"

# 业务端点（全部挂 openapi 基址，纯 Bearer，无 COSY 签名）
PATH_DEVICE_POLL = "/api/v1/deviceToken/poll"
PATH_DEVICE_REFRESH = "/api/v1/deviceToken/refresh"
PATH_JOB_EXCHANGE = "/api/v1/jobToken/exchange"
PATH_JOB_REFRESH = "/api/v1/jobToken/refresh"
PATH_USERINFO = "/api/v1/userinfo"
PATH_QUOTA = "/api/v2/quota/usage"
PATH_PLAN = "/api/v2/user/plan"
PATH_CHECKIN_STATUS = "/sash/api/v1/me/daily-check-in/status"
PATH_CHECKIN_CLAIM = "/sash/api/v1/me/daily-check-in/claim"
PATH_PRO_ELIGIBILITY = "/sash/api/v1/me/pro-upgrade/eligibility"
PATH_PRO_CLAIM = "/sash/api/v1/me/pro-upgrade/claim"
# 官方新活动平台（双区域通用，实测 cn/intl 均 200）：服务端下发活动列表与
# campaignUrl/JS，领取动作由桌面客户端承接；网关用它做状态呈现与提示。
PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
# 单个活动的奖励查询 / 领取（逆向自官方 growth-page/activity-iframe 页面：
#   GET  /sash/api/v1/me/campaigns/{campaignId}/reward
#   POST /sash/api/v1/me/campaigns/{campaignId}/claim   —— 领取（幂等：
#        已领取返回 {"status":"CLAIMED","replayed":true}，不会重复发放）
PATH_CAMPAIGN_REWARD = "/sash/api/v1/me/campaigns/%s/reward"
PATH_CAMPAIGN_CLAIM = "/sash/api/v1/me/campaigns/%s/claim"

# 桌面端专用请求头（活动平台必需；CLI=5 / QoderWork=6 / 桌面端=10）
DESKTOP_CLIENT_TYPE = "10"
DESKTOP_CLIENT_VERSION = "0.4.3"      # 可用 QD_DESKTOP_VERSION 覆盖
MACHINE_OS = "x86_64_win32"
MACHINE_HOSTNAME = "DESKTOP-QODER"


def desktop_version():
    """桌面端版本号（Cosy-Version）；客户端更新后可用环境变量覆盖，
    或直接跑 `python _refresh_catalog.py` 时按已安装客户端自动对齐。"""
    return (os.environ.get("QD_DESKTOP_VERSION") or DESKTOP_CLIENT_VERSION).strip() \
        or DESKTOP_CLIENT_VERSION


# ---------------------------------------------------------------------------
# 官方桌面端原生风控身份（activity/campaign 列表按它过滤，必须是"真"身份）
# ---------------------------------------------------------------------------
# 官方桌面端在调用活动平台前，会 spawn 自己的原生桥取机器身份：
#     <install>/resources/umid/runtime-info.exe prod --account-stdin
#     stdin: {"account": <uid>}   stdout: {"machineToken","machineType","machineCode",...}
# 服务端**按这些值过滤设备定向活动**：派生的假身份不会报错，但活动列表里
# 会静默少掉"每日领取 100 Credits"这类条目（实测：换用原生身份后立刻出现
# CLAIMABLE 活动）。因此网关优先调用同一个官方二进制取真值，失败才回退派生值。
# 原生身份缓存：组件的输出由**种子文件** $HOME/.config/.locale_cfg 决定——
# 同一种子下重复调用返回同一个身份（#18 实测：69 分钟 24 次采样不变），
# 换身份只能靠"清种子 + 重调组件"（见 _purge_identity_seed）。旧身份长期被
# 服务端接受（实测复用 25s+ 依然 showCampaign=true），真正的成本是每次调
# 组件要跑约 3.7 秒的官方二进制。因此做长缓存（30 分钟）+ 落盘缓存，并用
# "列表被判为未认可时清种子换新身份重试一次"兜底自愈。
# 注意：身份是**机器级**的（不同账号/不存在的账号 id 都返回同一份），
# 因此按区域缓存即可，同一台机器上的多个账号共用是正确的。
NATIVE_IDENTITY_TTL = 1800
# 首次落盘前的**自适应**多数表决（#18 补充：组件的 VM 判定会抖到另一分支
# KVM/13 ↔ Docker/50——报告者实测 1.56%，本机复现 10.29%；单次采样会把用户
# 永久固定到少数派）。先投 IDENTITY_VOTE_ROUNDS 次：全一致直接采纳；出现
# 分歧再补 IDENTITY_VOTE_EXTEND_ROUNDS 次（上限 5，首次延迟有界）。
# 轮数按**最坏抖动率**选：10% 抖动下 3 轮误判≈2.7%，自适应到 5 轮≈0.85%。
# 表决只在"首次无可用缓存"这一次发生；可用 QD_MACHINE_IDENTITY_VOTE=0 关闭。
IDENTITY_VOTE_ROUNDS = 3
IDENTITY_VOTE_EXTEND_ROUNDS = 2
# machine_identity_source（及 campaigns().identity）的合法取值只有两种：
#   "runtime-info" —— runtime-info.exe 原生桥给出真身份（native_machine_identity）
#   "derived"      —— 无原生桥时的派生回退（desktop_headers）
# 生产端一律使用本常量。历史上消费端（campaigns() 自愈条件）误写为 "native"，
# 与生产端字面量不一致导致该分支永不命中（口径分裂 bug，已修）；"native" 仅
# 作为历史/测试桩别名在消费端兼容，不得作为新的生产端取值。
MACHINE_IDENTITY_NATIVE = "runtime-info"
# desktop_headers() 本次实际是否携带 cosy-machine* 机器头（与"身份来源"
# machine_identity_source 是两个正交维度，勿混用）：
#   "native"  —— 本次发送了原生桥给出的全套六头
#   "omitted" —— 本次未发送任何 cosy-machine* 头（无原生桥时的正确行为；
#                issue #10 修复前会发派生假头，实测会被服务端整条过滤活动）
MACHINE_HEADERS_NATIVE = "native"
MACHINE_HEADERS_OMITTED = "omitted"
_native_exe_cache = {}
_native_ident_cache = {}


def _localappdata():
    return os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")


def _read_ini(path, key):
    """读 launcher 的 state.ini（UTF-8 或 UTF-16），返回 key=value 的 value。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return ""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith(key.lower() + "="):
                return line.split("=", 1)[1].strip()
    return ""


def desktop_install_dir(realm):
    """桌面端安装目录（launcher state.ini 的 installDir；找不到返回空串）。"""
    base = _localappdata()
    names = ("Qoder CN", "QoderCN", "Qoder") if realm == "cn" else ("Qoder",)
    for name in names:
        for launcher in ("%s Launcher" % name, "Launcher"):
            ini = os.path.join(base, name, launcher, "state.ini")
            if os.path.isfile(ini):
                d = _read_ini(ini, "installDir")
                if d and os.path.isdir(d):
                    return d
    for name in names:
        d = os.path.join(base, "Programs", name)
        if os.path.isdir(d):
            return d
    return ""


def runtime_info_exe(realm):
    """定位 runtime-info 原生风控身份桥；找不到返回空串。

    查找顺序（先桌面客户端，再提取目录）：
      1. <桌面客户端安装目录>/resources/umid/runtime-info.exe（Windows 桌面端）；
      2. POSIX：$QD_UMID_DIR/runtime-info、<repo>/umid/runtime-info
         （由 _install_umid.py 从 @qoder-ai/qodercli 提取；两者调用契约一致。
          Windows 不参与此分支，保持原有桌面客户端路径逐字不变）。

    可用 QD_NATIVE_IDENTITY=0 关闭（测试/受限环境不希望拉起客户端二进制时）。
    """
    if (os.environ.get("QD_NATIVE_IDENTITY") or "1").strip() in ("0", "false", "no"):
        return ""
    if realm in _native_exe_cache:
        return _native_exe_cache[realm]
    found = ""
    roots = [os.path.join(desktop_install_dir(realm), "resources", "umid")]
    for root in roots:
        cand = os.path.join(root, "runtime-info.exe")
        if os.path.isfile(cand):
            found = cand
            break
    if not found and os.name == "posix":
        # 提取目录（_install_umid.py 的落地位置；Windows 不参与本分支，
        # 保持原有桌面客户端路径逐字不变）：显式 QD_UMID_DIR 优先于约定目录。
        extracted = []
        umid_dir = (os.environ.get("QD_UMID_DIR") or "").strip()
        if umid_dir:
            extracted.append(umid_dir)
        extracted.append(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "umid"))
        for root in extracted:
            cand = os.path.join(root, "runtime-info")
            if os.path.isfile(cand):
                found = cand
                break
    _native_exe_cache[realm] = found
    return found


# ---------------------------------------------------------------------------
# 原生桥执行失败的一次性提示（issue #12）
# ---------------------------------------------------------------------------
# 「组件不存在」（runtime_info_exe 返回空串）是正常降级、保持静默；「组件在但
# 执行失败」在子进程 exec 阶段才暴露（典型：alpine 缺 glibc loader / libstdc++，
# 报 FileNotFoundError: /lib64/ld-linux-x86-64.so.2），过去被静默吞掉后只剩
# 「身份退化为 derived」，最容易被误诊成路径没配好。同类失败同进程只提示一次，
# 避免批量签到/巡检时刷屏。
_runtime_info_warned = set()


def _runtime_info_warn(category, message):
    """把原生桥失败按类别提示到 stderr（同一类别进程内只打一次）。"""
    if category in _runtime_info_warned:
        return
    _runtime_info_warned.add(category)
    try:
        import sys
        print("[runtime-info] %s" % message, file=sys.stderr)
    except Exception:
        pass


def run_runtime_info(realm, account_id=""):
    """调用 runtime-info 原生桥，返回其 JSON（失败返回 {}）。

    account 为空串同样可用：机器身份是机器级的，活动平台之外（如虚拟化体检）
    不需要账号上下文。

    失败可见性（issue #12）：「组件不存在」（runtime_info_exe 返回空串）保持
    静默；「组件在但执行失败」（缺 glibc loader / libstdc++、执行位丢失、输出
    异常等）会把底层异常打一次到 stderr——两种情况都照旧返回 {} 供上层回退。
    """
    exe = runtime_info_exe(realm)
    if not exe:
        return {}
    try:
        import subprocess
        proc = subprocess.run(
            [exe, "prod", "--account-stdin"],
            input=json.dumps({"account": account_id or ""}).encode("utf-8") + b" ",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25,
            cwd=os.path.dirname(exe))
        out = proc.stdout.decode("utf-8", "replace").strip()
        if out:
            return json.loads(out.split("\n", 1)[0])
        _runtime_info_warn(
            "empty-output",
            "%s 运行结束但没有输出（exit=%s），身份将退化为 derived"
            % (exe, proc.returncode))
    except OSError as exc:
        # 文件存在但 exec 失败：FileNotFoundError 缺的通常不是组件本身，而是它
        # 的动态 loader（glibc 的 /lib64/ld-linux-x86-64.so.2，alpine/musl 没有）；
        # 底层异常必须透出来，否则会被误读成「路径没配好」。
        _runtime_info_warn(
            "exec:" + type(exc).__name__,
            "无法执行 %s：%s: %s（组件存在但跑不起来；Docker/alpine 需要 "
            "gcompat libstdc++ libgcc 兼容层，见 README 已知限制）"
            % (exe, type(exc).__name__, exc))
    except Exception as exc:
        # 其它失败（超时 / 输出不是 JSON / 非 OSError 异常）
        _runtime_info_warn(
            "run:" + type(exc).__name__,
            "%s 调用失败：%s: %s" % (exe, type(exc).__name__, exc))
    return {}


_vm_cache = {}


def local_vm_status(realm=None, force=False):
    """本机虚拟化状态（**中文输出**）：看板与 _diag_campaign.py 共用。

    优先用官方风控桥的 vmInfo（官方客户端就是这么判的），桥不可用时退化为
    本机交叉校验（CPU 型号 / 系统制造商 / 虚拟化驱动文件）。结果缓存 300s。
    """
    r = realm if realm in ("cn", "intl") else (
        "cn" if realm != "qworkcn" else "cn")
    now = time.time()
    hit = _vm_cache.get(r)
    if hit and not force and now - hit[0] < 300:
        return hit[1]
    data = run_runtime_info(r)
    vm_info = data.get("vmInfo") if isinstance(data.get("vmInfo"), dict) else {}
    st = vm_status(bridge_vm_info=vm_info, bridge_available=bool(runtime_info_exe(r)))
    st["realm"] = r
    st["bridge_available"] = bool(runtime_info_exe(r))
    _vm_cache[r] = (now, st)
    return st


# ---------------------------------------------------------------------------
# 机器身份落盘缓存（issue #18：Docker 重建/升级容器后设备身份不变）
# ---------------------------------------------------------------------------
# 组件每次调用都会给出**新**身份——所以"调用后顺便更新缓存"等于没缓存。
# 本实现的纪律（设计 .team/00-IDENTITY-CACHE-DESIGN.md §9）：
#   · **缓存优先**：落盘命中就不调组件（「重建不换」的唯一来源）；
#   · 落盘只在两处写入：① 落盘没有该 realm 的可用记录（不存在 / 已按 TTL
#     失效 / 损坏）时取到的新身份；② 自愈路径（campaigns() 被判未认可后的
#     force 刷新）覆盖。其它路径一律不写，否则身份又会被自己刷掉。
#   · 失败回退：组件不可用时用落盘缓存（哪怕过期）——比 derived 假身份好。
_IDENTITY_CACHE_LOCK = threading.Lock()
_identity_cache_reuse_logged = set()     # 「复用缓存」INFO：每个 realm 一次
_identity_cache_corrupt_warned = False   # 「损坏/字段不全」WARN：进程内一次
_identity_cache_diff_warned = False      # 「与组件输出不一致」INFO：进程内一次
_identity_reset_done = False             # QD_MACHINE_IDENTITY_RESET 只消费一次


def machine_identity_cache_path():
    """机器身份落盘缓存路径：$ACCOUNTS_DIR/machine_identity.json（未设时用
    <repo>/accounts/machine_identity.json，与网关的账号目录一致）。"""
    base = (os.environ.get("ACCOUNTS_DIR") or "").strip()
    if not base:
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts")
    return os.path.join(base, "machine_identity.json")


def _identity_cache_enabled():
    """QD_MACHINE_IDENTITY_CACHE：auto/on（默认启用）/ off（关闭＝仅内存＝旧行为）。"""
    value = (os.environ.get("QD_MACHINE_IDENTITY_CACHE") or "auto").strip().lower()
    return value not in ("off", "0", "false", "no", "disable", "disabled")


def _identity_vote_enabled():
    """QD_MACHINE_IDENTITY_VOTE：默认开启；0/off 跳过首次表决（只调一次组件）。

    给"不在乎这 1~2% 抖动、想要最快首启"的用户——首启表决约需 3×组件耗时。
    """
    value = (os.environ.get("QD_MACHINE_IDENTITY_VOTE") or "1").strip().lower()
    return value not in ("off", "0", "false", "no", "disable", "disabled")


def _identity_cache_ttl():
    """QD_MACHINE_IDENTITY_CACHE_TTL：秒；0（默认）＝不过期。

    正数=到期后清组件种子并重取（**真的换出新身份**，见 _purge_identity_seed）
    ——这是设计 §3.3 的"定期轮换"开关；到期只是重新调组件、不换种子的话，
    同种子下会拿回同一个身份，开关等于没生效。
    """
    try:
        ttl = float(os.environ.get("QD_MACHINE_IDENTITY_CACHE_TTL") or 0)
    except (TypeError, ValueError):
        ttl = 0.0
    return max(0.0, ttl)


def _identity_cache_log(level, message):
    """缓存事件输出到 stderr；去重由各调用点的标记控制。"""
    try:
        import sys
        print("[machine-identity] %s: %s" % (level, message), file=sys.stderr)
    except Exception:
        pass


def _identity_entry_to_ident(entry):
    """缓存条目 -> ident dict；字段不全/类型不对时返回 None（视为无缓存）。"""
    if not isinstance(entry, dict):
        return None
    token = str(entry.get("machineToken") or "").strip()
    mtype = str(entry.get("machineType") or "").strip()
    code = str(entry.get("machineCode") or "").strip()
    if not (token and mtype and code):
        return None
    vm_info = entry.get("vm_info") if isinstance(entry.get("vm_info"), dict) else {}
    return {"machineToken": token, "machineType": mtype, "machineCode": code,
            "vm": bool(entry.get("vm")),
            "vm_info": vm_info,
            "source": str(entry.get("source") or MACHINE_IDENTITY_NATIVE)}


def _identity_entry_expired(entry, ttl, now):
    """按 TTL 判断条目是否过期（TTL=0 永不过期；cached_at 缺失视为过期）。"""
    if ttl <= 0:
        return False
    try:
        cached_at = float(entry.get("cached_at") or 0)
    except (TypeError, ValueError):
        return True
    return (now - cached_at) >= ttl


def _identity_cache_warn_corrupt(reason):
    """损坏/字段不全：WARN 一次（进程内），随后按"无缓存"重建。"""
    global _identity_cache_corrupt_warned
    if _identity_cache_corrupt_warned:
        return
    _identity_cache_corrupt_warned = True
    _identity_cache_log(
        "WARN", "machine identity cache unreadable (%s); regenerating" % reason)


def _read_identity_cache_doc():
    """读缓存文档；文件缺失返回空壳；损坏/结构异常返回空壳（并首次 WARN）。

    绝不抛异常——缓存损坏只降级为"无缓存"，不影响业务流程。
    """
    path = machine_identity_cache_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {"version": 1, "realm": {}}
    except Exception as exc:
        _identity_cache_warn_corrupt("%s: %s" % (type(exc).__name__, exc))
        return {"version": 1, "realm": {}}
    if not (isinstance(doc, dict) and isinstance(doc.get("realm"), dict)):
        _identity_cache_warn_corrupt("unexpected structure")
        return {"version": 1, "realm": {}}
    return doc


def _load_identity_cache(realm, now, allow_expired=False):
    """读落盘缓存；返回 ident dict 或 None（无 / 损坏 / 字段不全 / 过期）。"""
    entry = (_read_identity_cache_doc().get("realm") or {}).get(realm)
    if entry is None:
        return None
    ident = _identity_entry_to_ident(entry)
    if ident is None:
        _identity_cache_warn_corrupt("missing fields")
        return None
    if not allow_expired and _identity_entry_expired(entry, _identity_cache_ttl(), now):
        return None
    return ident


def _identity_cache_expired_entry_exists(realm, now):
    """落盘存在该 realm 的**有效条目**、但已按 TTL 过期（TTL 轮换路径专用）。

    用于区分"首次无缓存/损坏"与"TTL 到期"：只有后者才在重取前清组件种子
    （让"定期轮换"真的换出新身份，见 _purge_identity_seed）。
    """
    entry = (_read_identity_cache_doc().get("realm") or {}).get(realm)
    if entry is None or _identity_entry_to_ident(entry) is None:
        return False
    return _identity_entry_expired(entry, _identity_cache_ttl(), now)


def _write_identity_cache_doc(doc):
    """原子写缓存文档（临时文件 + os.replace、权限 0600）；失败静默返回 False。"""
    path = machine_identity_cache_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _save_identity_cache(realm, ident, force=False):
    """把身份写入落盘缓存（读-改-写 + 锁 + 原子替换）。

    返回 None = 采纳新值（已写盘）；返回 dict = 以磁盘既有值为准（并发防御：
    另一个实例刚写入未过期的缓存，见设计 §4②"keeping cached"）。
    """
    global _identity_cache_diff_warned
    with _IDENTITY_CACHE_LOCK:
        doc = _read_identity_cache_doc()
        realms = doc.setdefault("realm", {})
        old_entry = realms.get(realm)
        old_ident = _identity_entry_to_ident(old_entry) if old_entry else None
        if (not force and old_ident is not None
                and not _identity_entry_expired(old_entry, _identity_cache_ttl(),
                                                time.time())
                and old_ident != ident):
            if not _identity_cache_diff_warned:
                _identity_cache_diff_warned = True
                _identity_cache_log(
                    "INFO",
                    "identity cache differs from fresh component output; keeping cached")
            return old_ident
        entry = dict(ident)
        entry["cached_at"] = time.time()
        realms[realm] = entry
        doc["version"] = 1
        _write_identity_cache_doc(doc)
        if force and old_ident is not None:
            _identity_cache_log(
                "WARN",
                "machine identity refreshed after upstream rejection (cache updated) — %s"
                % realm)
        return None


def _purge_identity_seed():
    """删除组件的身份种子 $HOME/.config/.locale_cfg；缺失即忽略、异常不外抛。

    组件输出由该随机种子决定（#18 实测：同种子下重复调用返回同一身份，
    删种子后必换新身份并写回新种子）。只有三条**显式换身份**路径才清它：
    ① force 自愈（否则拿到还是被拒的同一个身份，自愈空转）；②
    QD_MACHINE_IDENTITY_RESET（用户主动换）；③ TTL 到期后的那次调用
    （定期轮换——不换种子这个开关就是空的）。其它路径（缓存命中、组件
    失败回退、首次无缓存获取）绝不动，否则「重建不换」的缓存会失去意义。
    返回是否真的删除了文件；任何失败都静默（身份拿不到才是大事故）。
    """
    try:
        path = os.path.join(os.path.expanduser("~"), ".config", ".locale_cfg")
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def _consume_identity_reset():
    """QD_MACHINE_IDENTITY_RESET=1：清空落盘缓存（进程内只消费一次）。

    直接删 accounts/machine_identity.json 同样有效（最直观，不必记变量名）。
    """
    global _identity_reset_done
    if _identity_reset_done:
        return
    if (os.environ.get("QD_MACHINE_IDENTITY_RESET") or "").strip().lower() \
            not in ("1", "true", "yes", "on"):
        return
    _identity_reset_done = True
    with _IDENTITY_CACHE_LOCK:
        try:
            os.remove(machine_identity_cache_path())
            _identity_cache_log(
                "INFO", "machine identity cache cleared (QD_MACHINE_IDENTITY_RESET=1)")
        except FileNotFoundError:
            pass
        except OSError as exc:
            _identity_cache_log(
                "WARN", "could not clear machine identity cache: %s" % exc)
    # 主动换身份必须连种子一起换：只清缓存会复刻出一个**完全相同**的身份
    # （#18：身份由种子决定），等于没换。
    _purge_identity_seed()


def _sample_identity(realm, account_id):
    """单次调用组件并构造 ident dict；组件无输出或字段不全时返回 {}。"""
    data = run_runtime_info(realm, account_id)
    if not data:
        return {}
    token = str(data.get("machineToken") or "").strip()
    mtype = str(data.get("machineType") or "").strip()
    code = str(data.get("machineCode") or "").strip()
    vm_info = data.get("vmInfo") if isinstance(data.get("vmInfo"), dict) else {}
    if not (token and mtype and code):
        return {}
    return {"machineToken": token, "machineType": mtype, "machineCode": code,
            "vm": bool(vm_info.get("isVm")),
            "vm_info": vm_info,
            "source": MACHINE_IDENTITY_NATIVE}


def _identity_vote_key(ident):
    """表决键：token + 三元组判别字段（type/code/isVm/brand/vmTypeCode）。

    刻意排除 vmInfo.percentage 这类连续噪声字段——它们波动不应破坏多数表决；
    写盘采用选中样本的**完整** vmInfo（三元组天然自洽、不逐字段杂交）。
    """
    vm = ident.get("vm_info") if isinstance(ident.get("vm_info"), dict) else {}
    try:
        vm_type_code = int(vm.get("vmTypeCode") or 0)
    except (TypeError, ValueError):
        vm_type_code = -1
    return (ident.get("machineToken"), ident.get("machineType"),
            ident.get("machineCode"), bool(vm.get("isVm")),
            str(vm.get("brand") or ""), vm_type_code)


def _identity_branch_label(ident):
    """提取样本的（品牌, vmTypeCode），用于投票日志。"""
    vm = ident.get("vm_info") if isinstance(ident.get("vm_info"), dict) else {}
    return str(vm.get("brand") or "unknown"), vm.get("vmTypeCode")


def _first_identity_with_vote(realm, account_id, rounds=IDENTITY_VOTE_ROUNDS,
                              extend_rounds=IDENTITY_VOTE_EXTEND_ROUNDS):
    """首次落盘前的**自适应**多数表决：先采样 rounds 次；全一致直接采纳，
    出现分歧则再补 extend_rounds 次（上限有界），取出现次数最多的**完整样本**。

    - 取完整样本 = machineType / machineCode / vmInfo 天然自洽（不逐字段拼）；
    - 唯一多数 → 采纳；平票 / 全分歧 → 取**首次出现的**样本（确定性），
      日志明确写出不一致与补投过程；
    - 全部采样都无输出（组件不可用）时返回 {}，交给既有失败回退路径；
    - 本函数绝不清种子（那是 force / RESET / TTL 轮换的换身份路径）。
    """
    def take(count):
        got = []
        for _ in range(max(0, int(count))):
            sample = _sample_identity(realm, account_id)
            if sample:
                got.append(sample)
        return got

    samples = take(max(1, int(rounds)))
    if not samples:
        return {}
    tally = {}
    for sample in samples:
        tally.setdefault(_identity_vote_key(sample), []).append(sample)
    if len(tally) == 1:
        # 首轮全一致：直接采纳（证据见常量区注释——最常见情形，成本不增加）
        picked = samples[0]
        brand, vm_type_code = _identity_branch_label(picked)
        _identity_cache_log(
            "INFO", "identity vote: %d/%d %s (vmTypeCode=%s)"
            % (len(samples), len(samples), brand, vm_type_code))
        return picked
    # 出现分歧：补投（上限 rounds+extend_rounds，保证首次延迟有界）
    samples.extend(take(extend_rounds))
    tally = {}
    for sample in samples:
        tally.setdefault(_identity_vote_key(sample), []).append(sample)
    top_key = max(tally, key=lambda key: len(tally[key]))
    top_count = len(tally[top_key])
    total = len(samples)
    unique_top = sum(1 for key in tally if len(tally[key]) == top_count) == 1
    first_brand, first_vtc = _identity_branch_label(samples[0])
    if unique_top:
        picked = tally[top_key][0]
        brand, vm_type_code = _identity_branch_label(picked)
        minority_names = "/".join(
            _identity_branch_label(tally[key][0])[0]
            for key in tally if key != top_key)
        summary = ("%d rounds disagreed; extended to %d -> %d/%d %s "
                   "(%d minority %s; vmTypeCode=%s)"
                   % (int(rounds), total, top_count, total, brand,
                      total - top_count, minority_names, vm_type_code))
    else:
        picked = samples[0]
        distribution = " / ".join(
            "%s %d" % (_identity_branch_label(tally[key][0])[0], len(tally[key]))
            for key in tally)
        summary = ("%d rounds disagreed; extended to %d -> no majority (%s); "
                   "using first sample (%s, vmTypeCode=%s)"
                   % (int(rounds), total, distribution, first_brand, first_vtc))
    _identity_cache_log("INFO", "identity vote: " + summary)
    return picked


def native_machine_identity(realm, account_id, force=False):
    """调用官方原生桥取真实机器身份；任何失败返回 {}（调用方回退派生值）。

    身份是**机器级**的（实测不同 account id 返回同一份），按区域短缓存
    NATIVE_IDENTITY_TTL 秒；force=True 跳过缓存重新取值（服务端明确拒绝时的
    自愈路径才用它——见设计 §9.1）。

    落盘缓存（issue #18）：组件输出由本机种子文件（$HOME/.config/.locale_cfg）
    决定——同一种子下重复调用返回**同一个**身份，删种子才换新（见
    _purge_identity_seed）。只有**缓存优先**（落盘命中就不调组件）才能让
    Docker 重建/升级容器后身份不变；组件不可用时回退落盘缓存（哪怕过期），
    不再被迫退化到 derived 假身份。

    另：**首次落盘**前对组件做自适应多数表决（先 IDENTITY_VOTE_ROUNDS 次，
    分歧时补至 IDENTITY_VOTE_ROUNDS + IDENTITY_VOTE_EXTEND_ROUNDS 次，取出现
    次数最多的完整样本）——组件的 VM 判定会抖到另一分支（实测 1.56%~10.29%），
    单次采样会把用户永久固定到少数派（QD_MACHINE_IDENTITY_VOTE=0 可关闭表决）。
    """
    now = time.time()
    cache_on = _identity_cache_enabled()
    ttl_rotation = False
    _consume_identity_reset()
    if not force:
        # ① 内存缓存（1800s，现状不变）
        hit = _native_ident_cache.get(realm)
        if hit and now - hit[0] < NATIVE_IDENTITY_TTL:
            return hit[1]
        # ② 落盘缓存：命中就**不调组件**（本设计的核心）
        if cache_on:
            stored = _load_identity_cache(realm, now)
            if stored is not None:
                _native_ident_cache[realm] = (now, stored)
                if realm not in _identity_cache_reuse_logged:
                    _identity_cache_reuse_logged.add(realm)
                    _identity_cache_log(
                        "INFO",
                        "machine identity cache: reusing cached identity for %s "
                        "(rebuilds keep the same device)" % realm)
                return stored
            # TTL 到期（有记录但已过期）＝"定期轮换"路径；与"首次无缓存"
            # 区分开：只有它才在重取前清种子，让轮换真的换出**新身份**。
            ttl_rotation = _identity_cache_expired_entry_exists(realm, now)
    # 清种子的三条路径（后来人最容易漏第三条）：
    #   ① force=True：服务端已不认当前身份 → 自愈必须换一个**新的**
    #      （同种子下组件只会返回同一个被拒身份，不换种子等于自愈空转）；
    #   ② QD_MACHINE_IDENTITY_RESET=1：用户主动换
    #      （在 _consume_identity_reset 里处理：缓存与种子一起清）；
    #   ③ TTL 到期后的这次调用：让"定期轮换"开关真的生效——不换种子它
    #      只是重新调一次组件、拿回同一个身份，等于空开关。
    # 不清种子的路径：缓存命中（根本不调组件）、组件失败回退缓存、首次无
    # 缓存的正常获取（那时用户并没有要求换身份）。
    if force or ttl_rotation:
        _purge_identity_seed()
    # ③ 调组件（首次落盘时做多数表决：组件的 VM 判定有 1~2% 概率抖到另一
    #    分支，单次采样会把用户永久固定到少数派。表决只在"首次无可用缓存"
    #    发生；force 自愈 / TTL 轮换 / 缓存关闭 保持单次调用不变。）
    if cache_on and not force and not ttl_rotation and _identity_vote_enabled():
        ident = _first_identity_with_vote(realm, account_id)
    else:
        ident = _sample_identity(realm, account_id)
    if ident:
        if cache_on:
            kept = _save_identity_cache(realm, ident, force=force)
            if kept is not None:
                ident = kept
        _native_ident_cache[realm] = (now, ident)
        return ident
    # ④ 组件失败：回退落盘缓存（哪怕已过期）——alpine 缺兼容层/组件被删时
    #    仍能用真身份，比 derived 好（设计 §3.2 第 3 行）。
    if cache_on:
        stored = _load_identity_cache(realm, now, allow_expired=True)
        if stored is not None:
            _native_ident_cache[realm] = (now, stored)
            return stored
    _native_ident_cache[realm] = (now, {})
    return {}

# 签到能力探测缓存：404（接口不存在）后 N 秒内不再重复探测，避免每次巡检都
# 打一个必然失败的请求；到期自动重探，官方上线即可自动恢复。
CHECKIN_PROBE_TTL = 6 * 3600
# 活动列表缓存 TTL：活动状态变化很慢（每日一轮），20 秒内复用可让看板切换视图
# /账号不再等那 1–4 秒的上游请求；领取动作会强制绕过并立即失效缓存。
CAMPAIGNS_TTL = 20

# 活动平台 claim 请求之间的默认间隔（秒）：调用方（qoder_tasks.run_checkin）的
# gap 未透传时的安全默认，保持「>= 1.0s 防风控」语义（见 qoder_tasks 模块声明）。
CLAIM_GAP_DEFAULT = 1.0

def campaign_label(c):
    """活动显示名：官方中文标题（placements content.zh.title）优先，其次 key。"""
    c = c or {}
    return str(c.get("title_zh") or "").strip() or         str(c.get("campaign_key") or c.get("campaign_id") or "")


# 活动领取失败码 -> 中文说明（与官方 growth-page/activity-iframe 前端一致）
_CAMPAIGN_FAILURE_CN = {
    "REDEMPTION_CODE_OUT_OF_STOCK": "今日名额已发完（每日 10:00 刷新，次日再来）",
    "ACHIEVEMENT_NOT_COMPLETED": "需先完成新人任务（成就未完成）",
    "CAMPAIGN_NOT_ACTIVE": "活动已结束/未开始",
    "RISK_BLOCKED": "风控拦截（当前设备/账号不可领取）",
    "RISK_DEPENDENCY_UNAVAILABLE": "风控服务不可用，稍后重试",
}
CHECKIN_REASON_NOT_FOUND = "checkin_endpoint_not_found"

# 会话死亡标记：上游主动吊销离线会话，刷新已无意义，需要重新登录。
SESSION_DEAD_MARKERS = ("TOKEN_EXPIRE", "12153", "Offline user session not found")


def session_dead(msg):
    s = str(msg or "")
    return any(m in s for m in SESSION_DEAD_MARKERS)


def get_realm_config(realm):
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["cn"]


def gateway_candidates(realm):
    """该区域的推理主机候选列表（官方客户端同款：主选 + 故障切换域名）。

    签名只覆盖 path，因此同一请求换主机后签名依旧有效。
    """
    cfg = get_realm_config(realm)
    out = [cfg["gateway"]]
    for host in cfg.get("gateway_fallbacks") or ():
        if host and host not in out:
            out.append(host)
    return out


def detect_realm_from_domain(domain):
    d = str(domain or "").lower()
    if "qoder.sh" in d or (d.endswith("qoder.com") and "qoder.com.cn" not in d) \
            or "qoder.com/" in d:
        return "intl"
    return "cn"


def normalize_epoch(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1e11:      # 毫秒
        number /= 1000.0
    return int(number)


# ---------------------------------------------------------------------------
# 每日签到的「下次可签到时间」
# ---------------------------------------------------------------------------
# 官方规则：每日 10:00（UTC+8）刷新，错过不补。因此这条时间**必须**按固定
# UTC+8 计算与呈现，**不能**用系统本地时区——容器里常是 UTC，若按本地渲染，
# 用户看到的「下次」会和官方说明差 8 小时，对不上。
CHECKIN_WINDOW_HOUR_UTC8 = 10
_UTC8 = datetime.timezone(datetime.timedelta(hours=8))


def next_checkin_window(now=None):
    """下一个「每日 10:00（UTC+8）」窗口 → (epoch 秒:int, 人类可读:str)。

    边界（与 issue #20 的口径一致）：
      · 今天 10:00 **之前**（now < 当日 10:00）→ 今天 10:00；
      · 到达/晚于 10:00（含刚领取成功的情形）→ 明天 10:00。
      取「到达即算下一轮」是为了不返回一个已经到点的时刻。
    note 形如 "10-05 10:00（UTC+8）"，始终以 UTC+8 呈现；两个返回值同源，
    调用方只需调一次即可拿到成对的字段，避免两处各算一遍导致不一致。
    """
    ts = time.time() if now is None else float(now)
    now8 = datetime.datetime.fromtimestamp(ts, _UTC8)
    boundary = now8.replace(hour=CHECKIN_WINDOW_HOUR_UTC8, minute=0,
                            second=0, microsecond=0)
    if now8 >= boundary:
        boundary += datetime.timedelta(days=1)
    return int(boundary.timestamp()), boundary.strftime("%m-%d %H:%M") + "（UTC+8）"


# ---------------------------------------------------------------------------
# 带重试的 HTTP JSON 工具
# ---------------------------------------------------------------------------
# 198.18.0.0/15 (RFC 2544 benchmarking) 与 fdfe:dcba:9876::/48 被 Clash/mihomo
# 等本地代理用作 fake-IP DNS 段：开启透明代理的机器上所有公网域名都会解析到
# 这些网段。命中它说明 DNS 已被本机代理接管、真实 IP 不可见，此时跳过解析级
# 校验（名称级校验已完成）。
_FAKEIP_NETS = [
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
]


def _host_boundary_violation(ip, allow_local):
    """True when the resolved/IP address must be refused."""
    if ip.is_loopback:
        return not allow_local
    if (ip.is_private or ip.is_reserved or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified):
        return True
    return False


def validate_public_http_url(url, allow_local=False):
    """SSRF 防护：仅允许 http/https，且 host 不得指向本机/私有/保留网段。

    上游网关与 openapi 域名均为公网地址；任何指向 localhost、回环、内网或
    保留地址的 URL 一律拒绝，防止上游配置或导入数据把请求引向内网。
    allow_local 仅供显式面向本机网关的开发/验证脚本开启（如 _verify_models.py），
    服务端请求路径一律使用默认 False。
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").strip().strip("[]").lower()
    if not host:
        raise ValueError("URL host is required")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        if not allow_local:
            raise ValueError("requests to localhost are not allowed")
        return url
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _host_boundary_violation(literal, allow_local):
            raise ValueError(
                "requests to private/reserved address %s are not allowed" % literal)
        return url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("cannot resolve URL host %r: %s" % (host, exc))
    addrs = []
    for info in infos:
        addr = str(info[4][0]).strip("[]")
        try:
            addrs.append(ipaddress.ip_address(addr))
        except ValueError:
            raise ValueError("URL host resolved to a non-IP address: %r" % addr)
    if addrs and all(any(ip in net for net in _FAKEIP_NETS) for ip in addrs):
        return url  # fake-IP DNS：真实 IP 不可见，名称级校验已通过
    for ip in addrs:
        if _host_boundary_violation(ip, allow_local):
            raise ValueError(
                "requests to private/reserved address %s are not allowed" % ip)
    return url


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None):
    """urlopen + json decode with retries. 所有 openapi 调用统一走这里。"""
    validate_public_http_url(url)
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=headers or {},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
class Account(object):
    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        self.uid = str(data.get("uid") or "")
        self.nickname = str(data.get("nickname") or "")
        self.domain = str(data.get("domain") or "")
        self.realm = str(data.get("realm") or detect_realm_from_domain(self.domain))
        if self.realm not in REALM_CONFIGS:
            self.realm = "cn"
        if not self.domain:
            self.domain = get_realm_config(self.realm)["domain"]
        self.platform = str(data.get("platform") or "CLI")
        self.access_token = str(data.get("accessToken") or "")
        self.refresh_token = str(data.get("refreshToken") or "")
        self.personal_token = str(data.get("personalToken") or "")
        self.expires_at = normalize_epoch(data.get("expiresAt"))
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source") or "oauth")
        self.enabled = data.get("enabled", True)
        self.last_error = str(data.get("lastError") or "")
        self.cooldown_until = float(data.get("cooldownUntil") or 0)
        # 按模型粒度的限流冷却：上游频控只针对单模型，不能拖垮整个账号。
        self.model_cooldowns = {}
        self.credits = data.get("credits") or None
        self.plan = str(data.get("plan") or "")
        self.last_checkin = data.get("lastCheckin") or None
        # 已领取活动的兑换码（如「奶茶免单卡」REDEMPTION_CODE）：活动只发一次，
        # 必须落盘持久化，否则网关重启后用户就找不回兑换码了。
        self.campaign_codes = dict(data.get("campaignCodes") or {})
        # 被"同人已领取"挡下的活动 -> 冷却截止时间戳。服务端按"人"去重（同机器/
        # 同身份多账号共用一张），被挡后反复重试没有意义，只会刷日志。
        self.campaign_blocked_until = dict(data.get("campaignBlockedUntil") or {})
        self.user_type = str(data.get("userType") or "") or DEFAULT_USER_TYPE
        self.organization_id = str(data.get("organizationId") or "")
        self.organization_name = str(data.get("organizationName") or "")
        # 签到能力：None=未探测 / True=接口存在 / False=接口不存在（404）。
        # 运行时探测而非按区域硬编码——官方随时可能在任一区域增删活动接口。
        self._checkin_cap = None
        self._checkin_cap_reason = ""
        self._checkin_cap_at = 0.0
        # 最近一次 campaign 平台状态快照（/sash/api/v1/me/campaigns）
        self.campaign_status = None
        # 活动列表短缓存 (at, payload)：该请求约 1–4 秒（上游最慢的一环），
        # 看板切换视图/账号会连续取，缓存后由"领取动作"显式失效。
        self._campaigns_cache = None
        # 活动平台用的机器身份来源：runtime-info(官方原生桥) / derived(派生回退)
        self.machine_identity_source = "derived"
        # 最近一次 desktop_headers() 实际是否携带 cosy-machine* 头：
        # native / omitted（未构造过时按 omitted 保守处理）；与身份来源正交
        self.machine_headers_state = MACHINE_HEADERS_OMITTED

    # -- 持久化 ------------------------------------------------------------
    def to_dict(self):
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "personalToken": self.personal_token,
            "expiresAt": self.expires_at,
            "addedAt": self.added_at,
            "source": self.source,
            "enabled": self.enabled,
            "lastError": self.last_error,
            "cooldownUntil": self.cooldown_until,
            "credits": self.credits,
            "plan": self.plan,
            "lastCheckin": self.last_checkin,
            "campaignCodes": self.campaign_codes,
            "campaignBlockedUntil": self.campaign_blocked_until,
            "userType": self.user_type,
            "organizationId": self.organization_id,
            "organizationName": self.organization_name,
        }

    def public(self):
        exp = self.expires_at
        return {
            "uid": self.uid,
            "nickname": self.nickname or (self.uid[:8] if self.uid else "?"),
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "enabled": bool(self.enabled),
            "source": self.source,
            "tokenFamily": token_family(self),
            "expiresAt": exp,
            "expiresIn": _human_delta(exp - time.time()) if exp else None,
            "hasRefreshToken": bool(self.refresh_token),
            "hasPAT": bool(self.personal_token),
            "lastError": self.last_error,
            "inCooldown": self.cooldown_until > time.time(),
            "cooldownFor": round(max(0.0, self.cooldown_until - time.time())) or None,
            "addedAt": self.added_at,
            "file": os.path.basename(self.path) if self.path else None,
            "credits": self.credits,
            "plan": self.plan,
            "lastCheckin": self.last_checkin,
            # 运行时探测：None=未探测（照常尝试）/ True / False（本区域无接口）
            "canCheckin": self.can_checkin(),
            "checkinCapability": ("unknown" if self.checkin_capability()[0] is None
                                  else ("available" if self.checkin_capability()[0]
                                        else "not_found")),
            "checkinReason": self.checkin_capability()[1],
            "userType": self.user_type,
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": derive_id(self.uid, "session"),
        }

    def save(self, directory):
        base = Path(directory).resolve()
        base.mkdir(parents=True, exist_ok=True)
        safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.uid or "")).strip("_ ")
        name = (safe_uid or uuid.uuid4().hex) + ".json"
        path = base / name
        tmp = base / (name + ".tmp")
        if not (path.is_relative_to(base) and tmp.is_relative_to(base)):
            raise ValueError("invalid path for account save")
        # 并发保护：/tasks 面板接口会并行刷新同一账号的多个字段（credits/plan/…），
        # 每个都可能触发 save；同一 tmp 路径被两个线程同时写会落出坏 JSON。
        with _SAVE_LOCK:
            tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        self.path = str(path)
        return self.path
    def delete(self):
        if self.path and os.path.exists(self.path):
            os.remove(self.path)

    # -- 健康与冷却 --------------------------------------------------------
    def ready(self, model=None):
        if not self.enabled or not self.access_token:
            return False
        if self.cooldown_until > time.time():
            return False
        if model and self.model_cooldowns.get(model, 0.0) > time.time():
            return False
        exp = self.expires_at
        if not exp:
            return True
        remaining = exp - time.time()
        if remaining > 240:          # 剩余 >4 分钟直接用（jt- 24h / dt- 30d）
            return True
        if remaining > 0:
            self.refresh()
            return True
        return self.refresh()

    def note_error(self, message, cooldown=60, single_account=False, model=None, until=None):
        self.last_error = str(message)[:200]
        if model:
            wait = max(1.0, float(until) - time.time()) if until else (
                3.0 if single_account else float(cooldown))
            self.model_cooldowns[model] = time.time() + wait
            return
        actual_cooldown = 3 if single_account else cooldown
        self.cooldown_until = time.time() + actual_cooldown

    def throttle_wait(self, model=None):
        """Seconds until this account can serve `model` again (0 = right now)."""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        wait = max(0.0, self.cooldown_until - now)
        if model:
            wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None):
        if model:
            self.model_cooldowns.pop(model, None)
        else:
            self.model_cooldowns.clear()
        if self.last_error or self.cooldown_until:
            self.last_error = ""
            self.cooldown_until = 0

    # -- 出站头 ------------------------------------------------------------
    def headers(self, purpose="openapi"):
        cfg = get_realm_config(self.realm)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": CLIENT_UA,
            "Authorization": "Bearer " + self.access_token,
            "X-Request-ID": generate_request_id(self.uid),
            "X-Machine-ID": derive_id(self.uid, "machine"),
            "X-Session-ID": derive_id(self.uid, "session"),
            "Origin": cfg["website"],
            "Referer": cfg["website"] + "/",
        }

    def desktop_headers(self):
        """桌面端 0.4.3 同款出站头（活动平台 /sash/... 必需）。

        官方桌面端调用 `/sash/api/v1/me/campaigns` 时携带：
          Authorization: Bearer <token>
          User-Agent: Qoder
          Cosy-ClientType: 10（桌面端；CLI 是 5、QoderWork 是 6）
          Cosy-Version: <桌面端版本>
          Cosy-MachineOS / MachineHostname / MachineId / MachineToken /
          MachineType / MachineCode

        两层坑（都已踩过）：
          1. 缺 UA / cosy-clienttype / cosy-version → 服务端不报错但返回
             **空活动列表**（实测这三个是展示活动所必需）。
          2. 机器头「半套」问题（issue #10，Linux/Docker 实测）：服务端把
             **全套派生** cosy-machine* 六头判定为非官方客户端，把 CLAIMABLE 的
             「每日领取 100 Credits」**整条过滤**，列表只剩 VIEW_DETAILS 类；
             已领取账号不受影响（所以首次领取时最易误判为"本来就没活动"）。
             逐头隔离实测：六头任一个**单独**出现 → 活动可见；六头全发 →
             被过滤；去掉 machinetoken 或 machineid → 可见。
        因此本函数的策略（issue #10 修复）：
          - 原生桥给出真身份（machineToken 非空）→ 发全套六头，保持官方
            客户端同款行为（真头是否额外解锁设备定向活动未验证，维持现状）；
          - derived 分支（无原生桥，如 Linux/Docker）→ **一律不发** cosy-machine*
            六头，只发 UA / cosy-clienttype / cosy-version。
        machine_identity_source 依旧如实记录（看板与诊断在用）。
        """
        h = dict(self.headers())
        h["User-Agent"] = "Qoder"
        h["cosy-clienttype"] = DESKTOP_CLIENT_TYPE
        h["cosy-version"] = desktop_version()
        ident = native_machine_identity(self.realm, self.uid)
        if ident.get("machineToken"):
            # 仅原生身份可用时发送机器头（issue #10：派生六头会被服务端判定
            # 为非官方客户端并整条过滤 CLAIMABLE 活动；详见上方 docstring）。
            h["cosy-machineid"] = derive_id(self.uid, "machine")
            h["cosy-machinetoken"] = ident.get("machineToken") or \
                derive_machine_token(self.uid)
            h["cosy-machinetype"] = ident.get("machineType") or \
                derive_machine_type(self.uid)
            h["cosy-machinecode"] = ident.get("machineCode") or \
                derive_id(self.uid, "machinecode")
            h["cosy-machineos"] = MACHINE_OS
            h["cosy-machinehostname"] = MACHINE_HOSTNAME
            self.machine_headers_state = MACHINE_HEADERS_NATIVE
        else:
            # 无原生桥：一个机器头都不发（issue #10）。状态如实登记，供
            # campaigns().machine_headers 与 INTL 已知限制提示使用。
            self.machine_headers_state = MACHINE_HEADERS_OMITTED
        self.machine_identity_source = ident.get("source") or "derived"
        return h

    def _machine_headers_hint(self):
        """机器头状态相关的可读提示；当前只在 INTL + 未发送机器头时非空。

        国际版服务端要求真实的 UMID 机器身份（官方客户端组件生成、每 50 分钟
        刷新）。本机无该组件时必须"不发头"（issue #10），服务端可能因此不返回
        活动——这是**已知限制**，不要误报成"今天没有活动"。CN 侧不发头是正确
        行为，不给提示。
        """
        if self.realm == "intl" and \
                getattr(self, "machine_headers_state", "") == MACHINE_HEADERS_OMITTED:
            return ("国际版服务端要求真实的 UMID 机器身份（由官方客户端组件生成、"
                    "每 50 分钟刷新），本机没有该组件、本次未发送机器头：活动列表"
                    "可能不可见、领取可能失败。这是已知限制，不等于今天没有活动。")
        return ""

    # -- 刷新（按 token 前缀路由） ----------------------------------------
    def refresh(self):
        """刷新 access token。drt- 走 deviceToken，jrt-/PAT 走 jobToken。

        PAT 永不覆盖活跃的 OAuth 会话，只做 jrt- 过期后的最终兜底。
        """
        cfg = get_realm_config(self.realm)
        base = cfg["openapi"]
        # 1) OAuth 设备族
        if self.refresh_token.startswith("drt-"):
            return self._post_token(base + PATH_DEVICE_REFRESH,
                                    {"refresh_token": self.refresh_token}, kind="device")
        # 2) jobToken 族：jrt- 优先，失败回落 PAT 重新交换
        if self.refresh_token:
            if self._post_token(base + PATH_JOB_REFRESH,
                                {"refresh_token": self.refresh_token}, kind="job"):
                return True
        if self.personal_token:
            if self._post_token(base + PATH_JOB_EXCHANGE,
                                {"personal_token": self.personal_token}, kind="job"):
                return True
        if not self.refresh_token and not self.personal_token:
            self.last_error = "no refresh token; sign in again"
        return False

    def _post_token(self, url, payload, kind):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": CLIENT_UA,
        }
        try:
            data = http_json(url, data=json.dumps(payload).encode(), method="POST",
                             headers=headers, timeout=30)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            self.last_error = "refresh failed: HTTP %d %s" % (exc.code, body[:160])
            if exc.code in (401, 403) and session_dead(body):
                self.enabled = False
                self.last_error = "session dead (TOKEN_EXPIRE): re-login required"
            return False
        except Exception as exc:
            self.last_error = "refresh failed: %s" % exc
            return False

        if kind == "device":
            token = data.get("token") or data.get("device_token") or ""
            refresh = data.get("refresh_token") or self.refresh_token
            exp = _device_expiry(data)
        else:
            token = data.get("token") or ""
            refresh = data.get("refresh_token") or self.refresh_token
            if data.get("expires_in"):
                exp = int(time.time() + int(data["expires_in"]) / 1000)
            else:
                exp = self.expires_at
        if not token:
            self.last_error = "refresh returned no token"
            return False
        self.access_token = token
        self.refresh_token = refresh
        self.expires_at = exp or self.expires_at
        self.last_error = ""
        self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        try:
            from qoder_sign import SESSIONS
            SESSIONS.invalidate(self.uid)   # 旧 COSY 会话携带旧 token，必须重建
        except Exception:
            pass
        return True

    # -- 签到 / 额度 / 套餐 ------------------------------------------------
    def _mark_checkin_capability(self, available, reason=""):
        self._checkin_cap = bool(available)
        self._checkin_cap_reason = reason or ""
        self._checkin_cap_at = time.time()

    def checkin_capability(self):
        """签到能力（运行时探测结果）。

        None  = 尚未探测（调用方应实际尝试一次）
        True  = 本账号所在区域存在 /daily-check-in 接口
        False = 接口不存在（404/405/410，实测国际版即如此）——缓存 TTL 内跳过
        """
        if self._checkin_cap is None:
            return None, ""
        if time.time() - self._checkin_cap_at > CHECKIN_PROBE_TTL:
            return None, self._checkin_cap_reason
        return self._checkin_cap, self._checkin_cap_reason

    def can_checkin(self):
        """今日是否还需要签到（能力由运行时探测，不再按区域硬编码）。"""
        capable, _ = self.checkin_capability()
        if capable is False:
            return False
        if not self.last_checkin:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_checkin).startswith(today_str)

    def checkin_status(self):
        """GET daily-check-in/status -> (ok, summary|error)。

        接口在本区域不存在时返回 (False, {"unavailable": True, reason:...})，
        并记录能力探测结果（活动上线后 TTL 到期会自动重探）。
        """
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_CHECKIN_STATUS
        try:
            q = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=2)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if exc.code in (404, 405, 410):
                self._mark_checkin_capability(
                    False, "%s (HTTP %d)" % (CHECKIN_REASON_NOT_FOUND, exc.code))
                return False, {
                    "unavailable": True,
                    "reason": CHECKIN_REASON_NOT_FOUND,
                    "http": exc.code,
                    "error": "HTTP %d %s" % (exc.code, body[:160]),
                }
            return False, {"unavailable": False,
                           "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return False, {"unavailable": False, "error": str(exc)}
        self._mark_checkin_capability(True, "")
        last = ""
        if q.get("lastClaimedAt"):
            try:
                last = time.strftime("%Y-%m-%d",
                                     time.localtime(int(q["lastClaimedAt"])))
            except Exception:
                last = ""
        today = time.strftime("%Y-%m-%d")
        status = str(q.get("status") or "")
        return True, {
            "status": status,
            "active": status in ("CLAIMABLE", "CLAIMED"),
            "today_checked_in": status == "CLAIMED" and last == today,
            "streak_days": int(q.get("currentStreakDays") or 0),
            "total_claim_days": int(q.get("totalClaimDays") or 0),
            "reward_credits": int(q.get("rewardCredits") or 0),
            "total_reward_credits": int(q.get("totalRewardCredits") or 0),
            "next_claim_at": int(q.get("nextClaimAt") or 0),
            "last_claimed_at": int(q.get("lastClaimedAt") or 0),
            "reward_expires_at": int(q.get("rewardExpiresAt") or 0),
        }

    def checkin(self):
        """每日签到：先查状态，未签则领取。返回 {ok, msg, ...}。

        不再按区域门控：接口不存在（国际版 404）按"本区域无此接口"跳过并给
        出明确原因，而不是静默什么都不做（历史问题：看板点签到毫无反应）。
        """
        ok, st = self.checkin_status()
        if not ok:
            if st.get("unavailable"):
                return {"ok": True, "unavailable": True,
                        "reason": st.get("reason"),
                        "msg": "本区域未开放 /sash/api/v1/me/daily-check-in 接口"
                               "（HTTP %s）：每日领取活动改由官方客户端承接"
                               % st.get("http")}
            return {"ok": False, "error": st.get("error") or str(st)}
        if st["today_checked_in"]:
            return {"ok": True, "already": True, "msg": "今日已签到",
                    "streak_days": st["streak_days"], "reward_credits": st["reward_credits"]}
        if not st["active"]:
            # CLAIMABLE / CLAIMED 之外的状态（如 DISABLED：活动批次下线），
            # 不发起无意义的 claim，按"活动未开放"成功跳过。
            return {"ok": True, "disabled": True, "status": st.get("status"),
                    "msg": "官方签到活动未开放 (status=%s)" % (st.get("status") or "?")}
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_CHECKIN_CLAIM
        try:
            res = http_json(url, data=b"{}", method="POST",
                            headers=self.headers(), timeout=15, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            # 上游并发/重复领取返回 409 ALREADY_CLAIMED —— 归一化为“已签”
            if "ALREADY_CLAIMED" in body:
                self._stamp_checkin()
                return {"ok": True, "already": True, "msg": "今日已签到"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if str(res.get("result") or "") == "ALREADY_CLAIMED" or res.get("success") is False and "ALREADY" in str(res.get("error") or ""):
            self._stamp_checkin()
            return {"ok": True, "already": True, "msg": "今日已签到"}
        if res.get("success") is False:
            # 复查一次：上游可能已记账
            ok2, st2 = self.checkin_status()
            if ok2 and st2["today_checked_in"]:
                self._stamp_checkin()
                return {"ok": True, "already": True, "msg": "今日已签到（复查确认）"}
            return {"ok": False, "error": str(res.get("error") or res)[:160]}
        reward = int(res.get("rewardCredits") or 0)
        self._stamp_checkin()
        return {"ok": True, "msg": "签到成功 +%d 积分" % reward,
                "reward_credits": reward}

    def _campaigns_get(self):
        """一次活动列表请求（桌面端头 → 401/403 回退普通头）。

        返回 (payload|None, http_code, error_str)；payload 为服务端 JSON。
        """
        url = get_realm_config(self.realm)["openapi"] + PATH_CAMPAIGNS
        try:
            return http_json(url, method="GET", headers=self.desktop_headers(),
                             timeout=15, retries=1), 200, ""
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            # 桌面端头被拒（401/403）时回退普通头，至少保留"能不能看到"的信息
            if exc.code in (401, 403):
                try:
                    return http_json(url, method="GET", headers=self.headers(),
                                     timeout=15, retries=1), 200, ""
                except urllib.error.HTTPError as exc2:
                    return None, exc2.code, ("HTTP %d (desktop) / HTTP %d (plain)"
                                             % (exc.code, exc2.code))
                except Exception as exc2:
                    return None, exc.code, "HTTP %d (desktop) / %s (plain)" % (exc.code, exc2)
            return None, exc.code, "HTTP %d %s" % (exc.code, body[:160])
        except Exception as exc:
            return None, 0, str(exc)

    def campaigns(self, force=False):
        """GET /sash/api/v1/me/campaigns -> 官方活动平台状态（双区域通用）。

        官方把"每日领取 100 Credits"等限时活动搬到了 campaign 平台，取列表要
        **两层都对**：① 桌面端请求头（`desktop_headers()`）；② 服务端认可的
        **真实机器身份**（原生桥取，见 `native_machine_identity`）。两者任一
        不对都表现为 HTTP 200 + 列表里少活动（不报错），这正是"领不到"的根因。

        身份被拒时的自愈：若本次 `showCampaign=false`（通常意味着身份被判定为
        非官方客户端），清掉组件种子、强制换一个**新的**身份并重试，避免被拒
        的身份一直卡到重建容器（#18：同种子下组件只会返回同一个身份）。
        结果短缓存 CAMPAIGNS_TTL 秒（force=True 绕过）——该请求是上游最慢的
        一环，看板切换视图时不该重复等它。

        返回 {ok, available, show_campaign, claimable, campaign_url, campaigns,
              identity}，每条活动含 action_type / claim_status / benefit / end_at。
        """
        now = time.time()
        if not force and self._campaigns_cache \
                and now - self._campaigns_cache[0] < CAMPAIGNS_TTL:
            return self._campaigns_cache[1]
        q, code, err = self._campaigns_get()
        # 自愈条件必须与生产端同源：machine_identity_source 的合法值是
        # MACHINE_IDENTITY_NATIVE("runtime-info") 或 "derived"；"native" 仅为
        # 历史/测试桩别名。此前误用 native 字面量做等值判断，导致此分支永不命中
        # （口径分裂，已按 Lead 裁决统一到 runtime-info）。
        # 取舍留痕（勿误读为漏写）：此处**不做**缓存新鲜度节流。曾评估"仅当
        # _native_ident_cache 缺失或 ≥NATIVE_IDENTITY_TTL 才自愈"的方案，未采纳：
        # 该阈值会在"缓存新鲜但身份已被服务端作废"时阻止自愈，把用户重新打回
        # "整天领不到"的原始故障；宁可多一次刷新（原生桥实测 3.7s，且触发条件
        # 仅为服务端明确返回 showCampaign=false、并非高频路径），也不制造
        # "为什么没自愈"的新谜题（Lead 裁决，不实施）。
        if isinstance(q, dict) and not q.get("showCampaign") \
                and getattr(self, "machine_identity_source", "") in \
                (MACHINE_IDENTITY_NATIVE, "native"):
            # 服务端已不认当前身份，必须换一个**新的**：force 跳过全部缓存并
            # **覆盖落盘**（设计 §9.1/§9.2 写入点②），否则重建后仍会用被拒的旧身份。
            native_machine_identity(self.realm, self.uid, force=True)
            q2, code2, err2 = self._campaigns_get()
            if isinstance(q2, dict) and q2.get("showCampaign"):
                q, code, err = q2, code2, err2
        if not isinstance(q, dict):
            return {"ok": False, "available": code not in (404, 405, 410),
                    "error": err or ("HTTP %d" % code),
                    "machine_headers": getattr(self, "machine_headers_state",
                                               MACHINE_HEADERS_OMITTED),
                    "hint": self._machine_headers_hint()}
        items = []
        raw = q.get("campaigns")
        for c in (raw if isinstance(raw, list) else []):
            if not isinstance(c, dict):
                continue
            placements = c.get("placements")
            benefit = c.get("benefit") if isinstance(c.get("benefit"), dict) else {}
            # 官方中文标题/说明/详情页（placements[].content.zh）——活动名直接显示中文
            title_zh = title_en = desc_zh = detail = button = ""
            for pl in (placements if isinstance(placements, list) else []):
                cont = (pl or {}).get("content")
                if not isinstance(cont, dict):
                    continue
                zh = cont.get("zh") if isinstance(cont.get("zh"), dict) else {}
                en = cont.get("en") if isinstance(cont.get("en"), dict) else {}
                title_zh = title_zh or str(zh.get("title") or "")
                title_en = title_en or str(en.get("title") or "")
                desc_zh = desc_zh or str(zh.get("description") or "")
                detail = detail or str(zh.get("detailUrl") or en.get("detailUrl") or "")
                button = button or str(zh.get("buttonText") or en.get("buttonText") or "")
                if title_zh and desc_zh and detail:
                    break
            items.append({
                "title_zh": title_zh,
                "title_en": title_en,
                "desc_zh": desc_zh,
                "detail_url": detail,
                "button_text": button,
                "campaign_id": str(c.get("campaignId") or c.get("campaign_id") or ""),
                "campaign_key": str(c.get("campaignKey") or c.get("campaign_key") or ""),
                "action_type": str(c.get("actionType") or c.get("action_type") or ""),
                "claim_status": str(c.get("claimStatus") or c.get("claim_status") or ""),
                "start_at": normalize_epoch(c.get("startAt") or c.get("start_at")),
                "end_at": normalize_epoch(c.get("endAt") or c.get("end_at")),
                "benefit": {
                    "kind": str(benefit.get("kind") or ""),
                    "amount": int(benefit.get("amount") or 0),
                },
                "required_achievement_key": str(
                    c.get("requiredAchievementKey") or ""),
                "achievement_completed": bool(c.get("achievementCompleted")),
                "unavailable_reason": str(c.get("unavailableReason") or ""),
                "placements": placements if isinstance(placements, list) else [],
            })
        st = {
            "ok": True,
            "available": True,
            "show_campaign": bool(q.get("showCampaign")),
            "claimable": bool(q.get("claimable")),
            "campaign_url": str(q.get("campaignUrl") or ""),
            "campaigns": items,
            # 身份来源（runtime-info=原生桥 / derived=派生回退；与下面的
            # machine_headers 正交——derived 时本次根本不发机器头，issue #10）
            "identity": getattr(self, "machine_identity_source", "derived"),
            # 本次实际是否携带 cosy-machine* 机器头：native / omitted
            "machine_headers": getattr(self, "machine_headers_state",
                                       MACHINE_HEADERS_OMITTED),
            # INTL+omitted 时的"已知限制"提示；其余场景为空串（CN 不发提示）
            "hint": self._machine_headers_hint(),
        }
        self.campaign_status = st
        self._campaigns_cache = (time.time(), st)
        return st

    def campaign_reward(self, campaign_id):
        """GET …/campaigns/{id}/reward -> 该活动的发放状态（幂等，只读）。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + (PATH_CAMPAIGN_REWARD % campaign_id)
        try:
            return http_json(url, method="GET", headers=self.desktop_headers(),
                             timeout=20, retries=1)
        except Exception as exc:
            return {"error": str(exc)}

    def claim_campaign(self, campaign_id):
        """POST …/campaigns/{id}/claim -> 领取该活动奖励（官方幂等语义）。

        返回 {ok, status, replayed, failure_code, grant_id, amount, message}。
        服务端按"人"去重（同一机器指纹下的多账号合并为一人）：
          - 已领取 -> status=CLAIMED + replayed=true（幂等，不重复发放）；
          - 同人已领 -> status=BLOCKED + failureCode=SAME_PERSON_ALREADY_CLAIMED
            （实测：同机多号共享每轮一次的额度，第二个号会被 BLOCKED 且列表里
            隐藏该活动——官方文档写"每账号"，实际执行是"每人"）。
        """
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + (PATH_CAMPAIGN_CLAIM % campaign_id)
        try:
            r = http_json(url, data=b"{}", method="POST",
                          headers=self.desktop_headers(), timeout=20, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            code = ""
            try:
                code = str((json.loads(body) or {}).get("errorCode") or "")
            except Exception:
                code = ""
            if exc.code == 409 or code.upper() in ("ALREADY_CLAIMED", "REPLAYED"):
                return {"ok": True, "status": "CLAIMED", "replayed": True,
                        "message": "今日已领取（上游幂等确认）"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        status = str(r.get("status") or "").upper()
        failure = str(r.get("failureCode") or "").upper()
        if failure == "SAME_PERSON_ALREADY_CLAIMED" or status == "BLOCKED":
            return {"ok": False, "blocked": True, "status": status or "BLOCKED",
                    "replayed": False, "failure_code": failure or "BLOCKED",
                    "amount": int(((r.get("benefit") or {})
                                   if isinstance(r.get("benefit"), dict)
                                   else {}).get("amount") or 0),
                    "message": "同人已领取（同一设备/身份下其他账号本轮已领，"
                               "服务端按人去重）",
                    "raw": r}
        if not status and r.get("success") is False:
            return {"ok": False, "error": str(r.get("error") or r)[:160]}
        # 兑换码类奖励（REDEMPTION_CODE）：官方客户端语义 = CLAIMED 且
        # redemptionCode 非空才算拿到；仅 CLAIMED 无码 = 发放确认中。
        code = str(r.get("redemptionCode") or "").strip()
        if code:
            self.campaign_codes[campaign_id] = code
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
        if failure in _CAMPAIGN_FAILURE_CN:
            return {"ok": False, "status": status or "NOT_ELIGIBLE",
                    "failure_code": failure, "replayed": False,
                    "redemption_code": code,
                    "message": _CAMPAIGN_FAILURE_CN[failure], "raw": r}
        return {
            "ok": status in ("CLAIMED", "GRANTED", "SUCCESS"),
            "status": status,
            "replayed": bool(r.get("replayed")),
            "failure_code": failure or "",
            "grant_id": str(r.get("grantId") or ""),
            "redemption_code": code,
            "confirming": bool(status == "CLAIMED" and not code),
            "amount": int(((r.get("benefit") or {}) if isinstance(r.get("benefit"), dict)
                           else {}).get("amount") or r.get("amount") or 0),
            "message": ("已领取" if r.get("replayed") else "领取成功")
                       + (("，兑换码：%s" % code) if code else
                          ("，兑换码发放确认中" if status == "CLAIMED" else "")),
            "raw": r,
        }

    # 兼容类内调用：self.campaign_label(c) / qoder_accounts.campaign_label(c)
    campaign_label = staticmethod(campaign_label)

    def campaign_checkin(self, gap=None, only_kinds=None):
        """活动平台签到：领取所有 CLAIMABLE 的 Credits 活动（每日 100 等）。

        先取一次机器身份（内存 → 落盘 → 组件；**不**强制换新——避免把
        「重建不换」的落盘缓存刷掉，设计 §9.1），再列活动、逐个领取。
        多账户场景下每个账号独立走这一遍。

        only_kinds: 只领这些 benefit.kind 的活动（如 ("", "CREDITS") 表示只做
                    每日签到领积分，不动兑换码/券类福利）。
        gap: 相邻两次 claim 请求之间的间隔（秒）；None=使用模块级安全默认
             CLAIM_GAP_DEFAULT（>=1.0s），显式传入时按传入值（下限 0）。

        返回 {ok, claimed:[...], already:[...], earned, message, campaigns,
              next_available_at, next_available_note}
          - 已是 CLAIMED 的活动计入 already（"今日已领取"）
          - 无可领取项且没有任何活动 -> ok=True + message 说明
          - next_available_at / next_available_note：下一个「每日 10:00（UTC+8）」
            的 epoch 秒与可读文本（见 next_checkin_window）；**无论本次是否真的
            领到都给出**，前端只在"没到账"时渲染。
        """
        claim_gap = CLAIM_GAP_DEFAULT if gap is None else max(0.0, float(gap))
        # 领取前只需"身份有效"（内存 → 落盘 → 组件），**不要** force 刷新：
        # force 会让每次签到都换一套身份并把落盘缓存刷掉，「重建不换」就失效了
        # （设计 §9.1）。身份真被服务端作废时由 campaigns() 的自愈兜底刷新。
        native_machine_identity(self.realm, self.uid)
        st = self.campaigns(force=True)      # 领取路径必须绕过缓存，看最新状态
        if not st.get("ok"):
            next_at, next_note = next_checkin_window()
            return {"ok": False, "error": st.get("error") or "campaigns 查询失败",
                    "earned": 0, "claimed": [], "already": [], "blocked": [],
                    "pending": [], "locked": [], "codes": [], "views": [],
                    "next_available_at": next_at,
                    "next_available_note": next_note}
        claimed, already, earned, errors, blocked = [], [], 0, [], []
        pending, locked, codes, views = [], [], [], []   # 券/成就/详情类
        for c in st["campaigns"]:
            if only_kinds is not None:
                kind = str((c.get("benefit") or {}).get("kind") or "").upper()
                if kind not in only_kinds:
                    continue      # 只要"每日签到领积分"时跳过券类/其它奖励
                if str(c.get("action_type") or "") not in ("", "CLAIM_BENEFIT"):
                    continue      # 详情类（VIEW_DETAILS）无奖励，不算签到
            cid = c["campaign_id"]
            cname = c["campaign_key"] or cid
            label = self.campaign_label(c)
            reason = str(c.get("unavailable_reason") or "").upper()
            if c["claim_status"] == "CLAIMED":
                if str(c.get("action_type") or "") == "VIEW_DETAILS":
                    views.append(c)      # 详情类活动：没有奖励可领，不算"已领取"
                else:
                    already.append(c)
                if self.campaign_codes.get(cid):
                    codes.append({"campaign": label, "code": self.campaign_codes[cid]})
                continue
            if c["claim_status"] != "CLAIMABLE":
                # 不可领取但并非"与我们无关"：名额发完 / 成就未完成要如实报出来
                # （官方前端状态机：REDEMPTION_CODE_OUT_OF_STOCK=outOfStock，
                #   ACHIEVEMENT_NOT_COMPLETED=locked）
                if reason == "REDEMPTION_CODE_OUT_OF_STOCK":
                    pending.append(c)
                elif reason == "ACHIEVEMENT_NOT_COMPLETED"                         or c.get("achievement_completed") is False:
                    locked.append(c)
                continue
            if c["action_type"] and c["action_type"] != "CLAIM_BENEFIT":
                continue      # VIEW_DETAILS 类活动无需（也不能）领取
            if self.campaign_blocked_until.get(cid, 0) > time.time():
                blocked.append({"campaign": cname,
                                "failure_code": "SAME_PERSON_ALREADY_CLAIMED",
                                "cooldown": True})
                continue      # 同人已领（冷却中）：不再重复 POST
            res = self.claim_campaign(cid)
            if res.get("ok"):
                amount = res.get("amount") or c["benefit"]["amount"] or 0
                self._stamp_checkin()
                if res.get("replayed"):
                    already.append(c)
                else:
                    claimed.append(c)
                    earned += int(amount or 0)
                if res.get("redemption_code"):
                    codes.append({"campaign": label,
                                  "code": res["redemption_code"]})
            elif res.get("blocked"):
                # 服务端按"人"去重：同机器/同身份下其他账号本轮已领。
                # 记 6 小时冷却（多账号同机器时不必每轮都试），并保留原因。
                self.campaign_blocked_until[cid] = time.time() + 6 * 3600
                if self.path and os.path.exists(os.path.dirname(self.path)):
                    self.save(os.path.dirname(self.path))
                blocked.append({"campaign": label,
                                "failure_code": res.get("failure_code")})
            elif res.get("failure_code") in _CAMPAIGN_FAILURE_CN:
                # 领取瞬间名额发完 / 任务未完成等：按"待重试/被锁"分类，不算失败
                slot = (pending if res["failure_code"] == "REDEMPTION_CODE_OUT_OF_STOCK"
                        else locked)
                slot.append(c)
            else:
                errors.append("%s: %s" % (cname, res.get("error")))
            # 每个 claim 请求之后统一等待一次：成功/被挡/失败都刚打过上游，
            # 相邻请求间隔由 gap 保证（默认 CLAIM_GAP_DEFAULT，防风控）。
            time.sleep(claim_gap)
        if claimed:
            msg = "活动领取成功 +%d Credits（%s）" % (
                earned, "、".join(self.campaign_label(c) for c in claimed))
        elif blocked:
            msg = ("同人已领取：同一设备/身份下的其他账号本轮已领过（服务端按人去重，"
                   "failureCode=%s）" % blocked[0].get("failure_code"))
        elif already:
            msg = "今日活动奖励已领取（%s）" % "、".join(
                self.campaign_label(c) for c in already)
        elif pending:
            msg = ("名额已发完，次日 10:00 后可再领：%s（活动 %s）" % (
                "、".join(c["required_achievement_key"] or "无门槛任务"
                          for c in pending),
                ", ".join(c["campaign_key"] or c["campaign_id"] for c in pending)))
        elif locked:
            msg = ("需先在官方桌面端完成新人任务后可领：%s（活动 %s）" % (
                ", ".join(c["required_achievement_key"] or "?"
                          for c in locked),
                ", ".join(c["campaign_key"] or c["campaign_id"] for c in locked)))
        elif errors:
            msg = "活动领取失败：%s" % "; ".join(errors)[:200]
        else:
            msg = "当前账号暂无可领取的官方活动"
        # 有待补货/待完成任务的活动时，把它们附在结论里（一键签到日志要能看到）
        extra = ""
        if pending:
            extra = "；另有活动今日名额已发完、次日 10:00 后重试：%s" % "、".join(
                self.campaign_label(c) for c in pending)
        elif locked:
            extra = "；另有活动需先完成新人任务（%s）：%s" % (", ".join(
                c.get("required_achievement_key") or "?" for c in locked),
                "、".join(self.campaign_label(c) for c in locked))
        if extra and "名额已发完" not in msg and "新人任务" not in msg:
            msg += extra
        # 领取动作会改变活动状态：让下一次列表查询重新拉取（不吃 20s 缓存）
        self._campaigns_cache = None
        # 「下次可签到时间」无条件给出（真领取 / 已领 / 被挡 / 名额发完 / 任务未完成 /
        # 无可领项 全部走这一个出口）：是否渲染由前端按需决定，后端不替前端判断。
        next_at, next_note = next_checkin_window()
        return {"ok": not errors, "claimed": claimed, "already": already,
                "blocked": blocked, "earned": earned, "message": msg,
                "pending": pending, "locked": locked, "codes": codes,
                "views": views,
                "campaigns": st["campaigns"], "errors": errors,
                "next_available_at": next_at,
                "next_available_note": next_note}


    def _stamp_checkin(self):
        self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))

    def fetch_credits(self):
        """GET /api/v2/quota/usage -> 聚合基础额度 + 赠送/签到额度。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_QUOTA
        try:
            q = http_json(url, method="GET", headers=self.headers(), timeout=30)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        uq = q.get("userQuota") or {}
        aq = q.get("addOnQuota") or {}

        def _num(d, k):
            try:
                return float(d.get(k) or 0)
            except Exception:
                return 0.0

        remain = int(_num(uq, "remaining") + _num(aq, "remaining"))
        used = int(_num(uq, "used") + _num(aq, "used"))
        size = int(_num(uq, "total") + _num(aq, "total"))
        self.credits = {
            "remain": remain,
            "used": used,
            "size": size,
            "exceeded": bool(q.get("isQuotaExceeded")),
            "usage_pct": q.get("totalUsagePercentage"),
            "expires_at": normalize_epoch(q.get("expiresAt")),
            "packages": [
                {"name": "基础额度", "remain": int(_num(uq, "remaining")),
                 "used": int(_num(uq, "used")), "size": int(_num(uq, "total"))},
                {"name": "赠送/签到额度", "remain": int(_num(aq, "remaining")),
                 "used": int(_num(aq, "used")), "size": int(_num(aq, "total"))},
            ],
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def fetch_plan(self):
        """GET /api/v2/user/plan -> 套餐名（Pro Trial 等）。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PLAN
        try:
            p = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=1)
        except Exception:
            return self.plan
        name = str(p.get("plan_tier_name") or p.get("user_type") or "")
        if name and name != self.plan:
            self.plan = name
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
        return name

    # -- Pro 升级包（一次性 +1800） ---------------------------------------
    def pro_eligibility(self):
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PRO_ELIGIBILITY
        try:
            m = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=1)
            return True, bool(m.get("eligible"))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 403, 410):
                # 端点不存在 / 活动已下线：查询成功，只是不可领取
                return True, False
            return False, "HTTP %d" % exc.code
        except Exception as exc:
            return False, str(exc)

    def pro_claim(self):
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PRO_CLAIM
        try:
            m = http_json(url, data=b"{}", method="POST", headers=self.headers(),
                          timeout=15, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if exc.code == 409 or "ALREADY" in body:
                return {"ok": True, "already": True, "msg": "Pro 升级包已领取过"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if m.get("success") is False:
            return {"ok": False, "error": str(m.get("message") or m)[:160]}
        return {"ok": True, "msg": "Pro 升级包领取成功", "data": m}


def token_family(acc):
    """返回账号当前的凭证族：device(OAuth) / job(PAT交换) / pat。"""
    rt = acc.refresh_token or ""
    if rt.startswith("drt-"):
        return "device"
    if rt.startswith("jrt-"):
        return "job"
    if (acc.access_token or "").startswith("pt-"):
        return "pat"
    return "unknown"


def _device_expiry(data):
    """deviceToken 响应的过期时间：expires_in(ms) / expires_at(RFC3339)，默认 30 天。"""
    if data.get("expires_in"):
        return int(time.time() + int(data["expires_in"]) / 1000)
    if data.get("expires_at"):
        try:
            import datetime
            dt = datetime.datetime.strptime(str(data["expires_at"])[:19],
                                             "%Y-%m-%dT%H:%M:%S")
            return int(dt.timestamp())
        except Exception:
            pass
    return int(time.time()) + 30 * 86400


def _human_delta(seconds):
    if seconds is None:
        return None
    if seconds <= 0:
        return "expired"
    days = seconds / 86400.0
    if days >= 1:
        return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1:
        return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)


# ---------------------------------------------------------------------------
# 会话亲和（同一对话固定同一账号，上游按账号缓存 prompt prefix）
# ---------------------------------------------------------------------------
class SessionAffinity(object):
    def __init__(self, ttl=7200, max_entries=5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self.bindings = {}
        self._lock = threading.Lock()

    def get(self, key):
        if not key:
            return None
        with self._lock:
            entry = self.bindings.get(key)
            if not entry:
                return None
            uid, exp = entry
            if time.time() > exp:
                self.bindings.pop(key, None)
                return None
            self.bindings[key] = (uid, time.time() + self.ttl)
            return uid

    def bind(self, key, uid):
        if not key or not uid:
            return
        with self._lock:
            if len(self.bindings) >= self.max_entries:
                now = time.time()
                self.bindings = {k: v for k, v in self.bindings.items() if v[1] > now}
            self.bindings[key] = (uid, time.time() + self.ttl)

    def unbind(self, key):
        if not key:
            return
        with self._lock:
            self.bindings.pop(key, None)


# ---------------------------------------------------------------------------
# AccountPool
# ---------------------------------------------------------------------------
_ACTIVE_POOL = None


def add_to_pool(account):
    """把导入的账号写入活动账号池（AccountPool 构造时自注册）。"""
    if _ACTIVE_POOL is None:
        raise RuntimeError("account pool not initialised")
    return _ACTIVE_POOL.add(account)


class AccountPool(object):
    def __init__(self, directory, log=None):
        global _ACTIVE_POOL
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        self.affinity = SessionAffinity()
        _ACTIVE_POOL = self

    def load(self):
        with self._lock:
            self.accounts = []
            if not os.path.isdir(self.dir):
                return self.accounts
            for name in sorted(os.listdir(self.dir)):
                if not name.endswith(".json"):
                    continue
                if name in ("settings.json", "active_realm.json"):
                    continue
                path = os.path.join(self.dir, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        account = Account(json.load(fh), path)
                except Exception as exc:
                    self.log("account %s unreadable: %s" % (name, exc))
                    continue
                if account.uid:
                    self.accounts.append(account)
            return self.accounts

    def list_public(self, realm=None):
        with self._lock:
            accs = self.accounts if (not realm or realm == "all") else \
                [a for a in self.accounts if a.realm == realm]
            return [a.public() for a in accs]

    def get(self, uid):
        with self._lock:
            for account in self.accounts:
                if account.uid == uid:
                    return account
        return None

    def add(self, account):
        with self._lock:
            existing = self.get(account.uid)
            if existing is not None:
                account.added_at = existing.added_at
                account.path = existing.path
                if not account.credits and existing.credits:
                    account.credits = existing.credits
                if not account.plan and existing.plan:
                    account.plan = existing.plan
                if not account.last_checkin and existing.last_checkin:
                    account.last_checkin = existing.last_checkin
                if not account.personal_token and existing.personal_token:
                    account.personal_token = existing.personal_token
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            return account

    def remove(self, uid):
        with self._lock:
            account = self.get(uid)
            if account is None:
                return False
            account.delete()
            self.accounts.remove(account)
            return True

    # -- 选择与健康 --------------------------------------------------------
    def count_ready(self, realm=None, model=None):
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot if a.enabled and a.access_token and
                   a.ready(model=model))

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None):
        exclude = exclude or set()
        if session_key:
            bound_uid = self.affinity.get(session_key)
            if bound_uid and bound_uid not in exclude:
                account = self.get(bound_uid)
                if account and account.realm == realm and account.ready(model=model):
                    return account
                self.affinity.unbind(session_key)
        account = self.pick(realm=realm, exclude=exclude, model=model)
        if account and session_key:
            self.affinity.bind(session_key, account.uid)
        return account

    def pick(self, realm=None, exclude=None, model=None):
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
            start = self._cursor
        total = len(snapshot)
        if total == 0:
            return None
        for offset in range(total):
            index = (start + offset) % total
            account = snapshot[index]
            if account.uid in exclude:
                continue
            if account.ready(model=model):
                with self._lock:
                    self._cursor = (index + 1) % total
                return account
        return None

    def representative(self, realm=None):
        with self._lock:
            candidates = [a for a in self.accounts if not realm or a.realm == realm]
            for account in candidates:
                if account.access_token:
                    return account
            return candidates[0] if candidates else None

    def set_enabled(self, uid, enabled):
        account = self.get(uid)
        if account is None:
            return None
        account.enabled = bool(enabled)
        if enabled:
            account.clear_error()
        account.save(self.dir)
        return account.public()

    def set_all_enabled(self, enabled, realm=None):
        with self._lock:
            for account in self.accounts:
                if realm and account.realm != realm:
                    continue
                account.enabled = bool(enabled)
                if enabled:
                    account.clear_error()
                account.save(self.dir)

    # -- 导入 / 导出 -------------------------------------------------------
    def preview_import_rows(self, rows, realm=None, overwrite=False):
        """报告 import_rows() 会做什么，不触碰账号池（Dry-Run）。"""
        preview = {"added": [], "updated": [], "skipped": [], "invalid": []}
        known = {a.uid for a in self.accounts}
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                preview["invalid"].append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                preview["skipped"].append({"uid": uid,
                                           "reason": "duplicate inside the document"})
            elif uid in known and not overwrite:
                preview["skipped"].append({"uid": uid, "reason": "already exists"})
            elif uid in known:
                preview["updated"].append(uid)
            else:
                preview["added"].append(uid)
            seen.add(uid)
        return preview

    def import_rows(self, rows, realm=None, overwrite=False):
        """从导出/外部文档批量导入账号。

        返回报告：added / updated / skipped / invalid。
        一行解析失败不影响其余行；全部解析通过才写盘。
        """
        added, updated, skipped, invalid = [], [], [], []
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                skipped.append({"uid": uid,
                                "reason": "duplicate inside the document"})
                continue
            seen.add(uid)
            existing = self.get(uid) is not None
            if existing and not overwrite:
                skipped.append({"uid": uid, "reason": "already exists"})
                continue
            try:
                self.add(Account(kwargs))
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue
            (updated if existing else added).append(uid)
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "invalid": invalid,
        }

    # -- OAuth 设备授权登录 ------------------------------------------------
    @staticmethod
    def _local_machine_id(realm):
        """读取本机官方客户端的 machine_id（优先，保持设备一致），缺则生成。"""
        cfg = get_realm_config(realm)
        home = os.path.join(os.path.expanduser("~"), cfg["home_dir"], ".auth")
        p = os.path.join(home, "machine_id")
        try:
            with open(p, encoding="utf-8") as fh:
                mid = fh.read().strip()
            if mid:
                return mid
        except Exception:
            pass
        return str(uuid.uuid4())

    def start_login(self, realm="cn", platform="CLI"):
        """构造 PKCE 设备授权 URL（浏览器打开完成授权）。

        双区 URL 参数差异（官方逆向）：
          CN   : challenge, challenge_method, nonce(带横线), redirect_uri,
                 client_id, machine_id
          Intl : challenge, challenge_method, nonce(32-hex), client_id,
                 machine_id（新协议带 client_id/machine_id、不带 redirect_uri）
        """
        cfg = get_realm_config(realm)
        verifier, challenge = _make_pkce()
        nonce = uuid.uuid4().hex if not cfg["nonce_dashed"] else str(uuid.uuid4())
        q = {
            "challenge": challenge,
            "challenge_method": "S256",
            "nonce": nonce,
        }
        if cfg.get("send_redirect_uri"):
            q["redirect_uri"] = cfg["redirect_uri"]
        if cfg.get("send_client_id"):
            q["client_id"] = cfg["client_id"]
            q["machine_id"] = self._local_machine_id(realm)
        auth_url = cfg["website"] + "/device/selectAccounts?" + urllib.parse.urlencode(q)
        state = "qd-%d" % time.time_ns()
        with self._lock:
            self.logins[state] = {
                "created": time.time(),
                "verifier": verifier,
                "nonce": nonce,
                "region": realm,
                "platform": platform,
            }
        return {"state": state, "authUrl": auth_url, "realm": realm,
                "platform": platform}

    def poll_login(self, state):
        state = str(state or "").strip()
        with self._lock:
            info = self.logins.get(state)
        if not info:
            return {"status": "unknown",
                    "message": "state not recognised - start the login again"}
        if time.time() - info["created"] > LOGIN_TTL_SECONDS:
            with self._lock:
                self.logins.pop(state, None)
            return {"status": "expired", "message": "login window expired - start again"}
        realm = info.get("region") or "cn"
        cfg = get_realm_config(realm)
        q = urllib.parse.urlencode({
            "nonce": info["nonce"],
            "verifier": info["verifier"],
            "challenge_method": "S256",
        })
        url = cfg["openapi"] + PATH_DEVICE_POLL + "?" + q
        validate_public_http_url(url)
        req = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/json",
            "User-Agent": "QoderWork",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            # 404 / 202 = 用户尚未完成授权（继续轮询）
            if exc.code in (404, 202):
                return {"status": "pending",
                        "message": "等待浏览器完成 Qoder 设备授权"}
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return {"status": "error", "message": "poll http %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"status": "pending", "message": "poll error: %s" % exc}
        if status in (404, 202):
            return {"status": "pending", "message": "等待浏览器完成 Qoder 设备授权"}
        try:
            data = json.loads(raw)
        except Exception:
            return {"status": "pending", "message": "waiting for grant"}
        token = data.get("token") or data.get("device_token") or ""
        if not token:
            return {"status": "pending", "message": "waiting for token"}

        uid = str(data.get("user_id") or "")
        nickname = ""
        # 拉取 userinfo 补全昵称/用户类型（尽力而为，不阻塞入库）
        try:
            ui_url = cfg["openapi"] + PATH_USERINFO
            validate_public_http_url(ui_url)
            req_ui = urllib.request.Request(ui_url, method="GET", headers={
                "Accept": "application/json",
                "User-Agent": CLIENT_UA,
                "Authorization": "Bearer " + token,
            })
            with urllib.request.urlopen(req_ui, timeout=15) as resp_ui:
                ui = json.loads(resp_ui.read().decode("utf-8"))
            uid = str(ui.get("id") or uid)
            nickname = str(ui.get("name") or "")
            user_type = str(ui.get("user_type") or "") or DEFAULT_USER_TYPE
            org_id = str(ui.get("organization_id") or "")
            org_name = str(ui.get("organization_name") or "")
        except Exception:
            user_type, org_id, org_name = DEFAULT_USER_TYPE, "", ""

        account = Account({
            "uid": uid or ("q-" + uuid.uuid4().hex[:24]),
            "nickname": nickname or ("u" + (uid[-8:] if uid else "")),
            "domain": cfg["domain"],
            "realm": realm,
            "platform": info.get("platform") or "CLI",
            "accessToken": token,
            "refreshToken": data.get("refresh_token") or "",
            "expiresAt": _device_expiry(data),
            "source": "oauth",
            "enabled": True,
            "userType": user_type,
            "organizationId": org_id,
            "organizationName": org_name,
        })
        self.add(account)
        with self._lock:
            self.logins.pop(state, None)
        return {"status": "ok", "account": account.public()}

    def cancel_login(self, state):
        with self._lock:
            return self.logins.pop(state, None) is not None

    # -- PAT 导入 ----------------------------------------------------------
    def import_pat(self, pat, realm="cn"):
        """导入 pt- 个人访问令牌：交换 jobToken 并拉取身份后入库。"""
        pat = str(pat or "").strip()
        if not pat.startswith("pt-"):
            raise ValueError("PAT must start with pt-")
        cfg = get_realm_config(realm)
        data = http_json(cfg["openapi"] + PATH_JOB_EXCHANGE,
                         data=json.dumps({"personal_token": pat}).encode(),
                         method="POST",
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json",
                                  "User-Agent": CLIENT_UA},
                         timeout=30)
        token = data.get("token") or ""
        if not token:
            raise ValueError("jobToken exchange returned no token")
        uid, nickname, user_type, org_id, org_name = "", "", DEFAULT_USER_TYPE, "", ""
        try:
            ui = http_json(cfg["openapi"] + PATH_USERINFO, method="GET",
                           headers={"Accept": "application/json",
                                    "User-Agent": CLIENT_UA,
                                    "Authorization": "Bearer " + token},
                           timeout=15, retries=2)
            uid = str(ui.get("id") or "")
            nickname = str(ui.get("name") or "")
            user_type = str(ui.get("user_type") or "") or DEFAULT_USER_TYPE
            org_id = str(ui.get("organization_id") or "")
            org_name = str(ui.get("organization_name") or "")
        except Exception:
            pass
        if data.get("expires_in"):
            exp = int(time.time() + int(data["expires_in"]) / 1000)
        else:
            exp = int(time.time()) + 24 * 3600
        account = Account({
            "uid": uid or ("p-" + uuid.uuid4().hex[:24]),
            "nickname": nickname or ("u" + (uid[-8:] if uid else uid[:8])),
            "domain": cfg["domain"],
            "realm": realm,
            "platform": "CLI",
            "accessToken": token,
            "refreshToken": data.get("refresh_token") or "",
            "personalToken": pat,
            "expiresAt": exp,
            "source": "pat",
            "enabled": True,
            "userType": user_type,
            "organizationId": org_id,
            "organizationName": org_name,
        })
        self.add(account)
        return account


def _make_pkce():
    """RFC 7636 S256: (verifier, challenge)。"""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    raw = os.urandom(64)
    verifier = "".join(alphabet[b % len(alphabet)] for b in raw)
    challenge = __import__("hashlib").sha256(verifier.encode("ascii")).digest()
    import base64 as _b64
    return verifier, _b64.urlsafe_b64encode(challenge).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# 本机已登录凭证的只读探测与导入（与 wb 网关的桌面扫描同构，双区都支持）
#
# 两类官方存储：
#   1. 桌面 App（Electron）： %APPDATA%\com.qoder[.cn].app.stable\auth.v1.dat
#      布局 "v10" + AES-256-GCM；密钥在同目录 Local State 的
#      os_crypt.encrypted_key（DPAPI 保护）-> 剥 "DPAPI" 前缀 -> DPAPI 解出。
#      明文 schema: {schemaVersion, token(dt-), refreshToken(drt-), expiresAt,
#                    user:{id,name,email,...}}
#   2. CLI/官方客户端： ~/.qoder[.cn]/.auth/user[.{profile}]
#      AES-128-CBC key=iv=machine_id 前 16 字符，标准 Base64（strict padding）；
#      明文 UserInfo JSON（或明文以 "{" 开头的兼容形态）。
# 扫描全程只读；看板两步确认后才写入网关账号池。
# ---------------------------------------------------------------------------
def _read_chromium_os_crypt_key(app_dir):
    """Local State.os_crypt.encrypted_key -> DPAPI 解出的 32 字节 AES key。"""
    import base64 as _b64
    from qoder_sign import dpapi_unprotect
    p = os.path.join(app_dir, "Local State")
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    ek = (state.get("os_crypt") or {}).get("encrypted_key")
    if not ek:
        raise RuntimeError("Local State has no os_crypt.encrypted_key")
    blob = _b64.b64decode(ek)
    if blob[:5] != b"DPAPI":
        raise RuntimeError("unexpected encrypted_key header %r" % blob[:5])
    return dpapi_unprotect(blob[5:])


def _roaming_app_dir(cfg):
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, cfg["app_dir"])


def _load_app_auth(realm):
    """解出桌面 App auth.v1.dat 的明文 dict；失败抛异常。"""
    from qoder_sign import chromium_decrypt_v10
    cfg = get_realm_config(realm)
    app_dir = _roaming_app_dir(cfg)
    key = _read_chromium_os_crypt_key(app_dir)
    with open(os.path.join(app_dir, cfg.get("auth_file") or "auth.v1.dat"),
              "rb") as fh:
        blob = fh.read()
    plain = chromium_decrypt_v10(blob, key)
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("token"):
        raise RuntimeError("auth.v1.dat has unexpected schema")
    return data


def _load_cli_user(realm, path, machine_key):
    """解 CLI 端 ~/.qoder*/.auth/user（AES-128-CBC）或明文兼容形态。"""
    from qoder_sign import aes_cbc_decrypt
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw.startswith("{"):
        return json.loads(raw)
    key = (machine_key or "")[:16].encode("utf-8")
    if len(key) != 16:
        raise RuntimeError("machine_id shorter than 16 bytes")
    pt = aes_cbc_decrypt(base64.b64decode(raw), key, key)
    return json.loads(pt.decode("utf-8"))


def scan_desktop_credentials():
    """只读探测本机双区已登录凭证。返回候选列表（不含任何明文令牌）。"""
    found = []
    for realm in ("intl", "cn", "qworkcn"):
        cfg = get_realm_config(realm)
        auth_file = cfg.get("auth_file") or "auth.v1.dat"
        # 1) 桌面 App (auth.v1.dat / auth-v2.dat)
        item = {
            "kind": "app",
            "path": os.path.join(_roaming_app_dir(cfg), auth_file),
            "file": auth_file,
            "realm": realm,
            "realmName": cfg["name"],
            "domain": cfg["domain"],
            "readable": False,
            "valid": False,
            "uid": "",
            "nickname": "",
            "expiresAt": 0,
            "error": "",
        }
        try:
            data = _load_app_auth(realm)
            user = data.get("user") or {}
            item["readable"] = True
            exp = normalize_epoch(data.get("expiresAt")) or 0
            if not exp and data.get("token"):
                # expiresAt 是 RFC3339 -> normalize_epoch 处理不了，单独解析
                try:
                    import datetime
                    exp = int(datetime.datetime.strptime(
                        str(data["expiresAt"])[:19], "%Y-%m-%dT%H:%M:%S"
                    ).timestamp())
                except Exception:
                    exp = 0
            token_prefix = str(data.get("token") or "")[:3]
            item.update({
                "valid": token_prefix == "dt-" or bool(data.get("refreshToken")),
                "uid": str(user.get("id") or ""),
                "nickname": str(user.get("name") or ""),
                "expiresAt": exp,
                "expiresIn": _human_delta(exp - time.time()) if exp else None,
            })
        except FileNotFoundError:
            item["error"] = "not found (未登录或未安装该版本客户端)"
        except Exception as exc:
            item["error"] = str(exc)
        found.append(item)

        # 2) CLI 端 user / user.{profile}
        auth_dir = os.path.join(os.path.expanduser("~"), cfg["home_dir"], ".auth")
        if os.path.isdir(auth_dir):
            machine_key = ""
            try:
                with open(os.path.join(auth_dir, "machine_id"),
                          encoding="utf-8") as fh:
                    machine_key = fh.read().strip()
            except Exception:
                pass
            try:
                names = [n for n in os.listdir(auth_dir)
                         if n == "user" or n.startswith("user.")]
            except Exception:
                names = []
            for n in names:
                p = os.path.join(auth_dir, n)
                cli_item = {
                    "kind": "cli",
                    "path": p,
                    "file": n,
                    "realm": realm,
                    "realmName": cfg["name"],
                    "domain": cfg["domain"],
                    "readable": False,
                    "valid": False,
                    "uid": "",
                    "nickname": "",
                    "expiresAt": 0,
                    "error": "",
                }
                try:
                    data = _load_cli_user(realm, p, machine_key)
                    token = str(data.get("access_token") or "")
                    cli_item["readable"] = True
                    exp = normalize_epoch(data.get("expire_time"))
                    cli_item.update({
                        "valid": token.startswith(("dt-", "jt-")),
                        "uid": str(data.get("uid") or ""),
                        "nickname": str(data.get("name") or ""),
                        "expiresAt": exp,
                        "expiresIn": _human_delta(exp - time.time()) if exp else None,
                    })
                except Exception as exc:
                    cli_item["error"] = str(exc)
                found.append(cli_item)
    return found


def import_desktop_credential(path=None, realm=None):
    """把扫描到的凭证导入账号池。path=None 时导入扫描到的全部有效项。"""
    if not path:
        imported, errors = [], []
        for item in scan_desktop_credentials():
            if not item.get("valid"):
                continue
            try:
                imported.append(import_desktop_credential(
                    path=item["path"], realm=item["realm"]))
            except Exception as exc:
                errors.append("%s/%s: %s" % (item["realm"], item["file"], exc))
        if errors:
            raise RuntimeError("; ".join(errors[:3]))
        return imported

    # 定位该 path 归属的 realm（按扫描结果匹配；否则按目录名猜）
    target_realm = realm
    matched = None
    for item in scan_desktop_credentials():
        if os.path.abspath(item["path"]) == os.path.abspath(path):
            matched = item
            target_realm = item["realm"]
            break
    if target_realm not in REALM_CONFIGS:
        target_realm = "cn"
    cfg = get_realm_config(target_realm)

    token = refresh = ""
    uid = nickname = ""
    exp = 0
    base = os.path.basename(path)
    if base in ("auth.v1.dat", "auth-v2.dat"):
        data = _load_app_auth(target_realm)
        token = str(data.get("token") or "")
        refresh = str(data.get("refreshToken") or "")
        user = data.get("user") or {}
        uid = str(user.get("id") or "")
        nickname = str(user.get("name") or "")
        try:
            import datetime
            exp = int(datetime.datetime.strptime(
                str(data.get("expiresAt") or "")[:19], "%Y-%m-%dT%H:%M:%S"
            ).timestamp())
        except Exception:
            exp = 0
    else:
        auth_dir = os.path.dirname(path)
        machine_key = ""
        try:
            with open(os.path.join(auth_dir, "machine_id"),
                      encoding="utf-8") as fh:
                machine_key = fh.read().strip()
        except Exception:
            pass
        data = _load_cli_user(target_realm, path, machine_key)
        token = str(data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or "")
        uid = str(data.get("uid") or "")
        nickname = str(data.get("name") or "")
        exp = normalize_epoch(data.get("expire_time"))

    if not token:
        raise RuntimeError("credential has no access token")
    if not uid:
        uid = "d-" + uuid.uuid4().hex[:24]
    account = Account({
        "uid": uid,
        "nickname": nickname or uid[:8],
        "domain": cfg["domain"],
        "realm": target_realm,
        "platform": "CLI",
        "accessToken": token,
        "refreshToken": refresh,
        "expiresAt": exp or (int(time.time()) + 30 * 86400),
        "source": "desktop-app",
        "enabled": True,
    })
    return add_to_pool(account)


# ---------------------------------------------------------------------------
# 导入 / 导出（与 WorkBuddy 网关同构的文档格式）
# ---------------------------------------------------------------------------
EXPORT_FORMAT = "qoder-accounts"
EXPORT_VERSION = 1


def account_to_export(account):
    data = account.to_dict()
    data.pop("path", None)
    return data


def build_export_document(accounts, realm=None, include_secrets=True, uids=None):
    wanted = None
    if uids is not None:
        wanted = {str(u) for u in uids}
    rows = []
    for account in accounts:
        if realm and account.realm != realm:
            continue
        if wanted is not None and account.uid not in wanted:
            continue
        row = account_to_export(account)
        if not include_secrets:
            row.pop("accessToken", None)
            row.pop("refreshToken", None)
            row.pop("personalToken", None)
        rows.append(row)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "accounts": rows,
    }


def _coerce_account_rows(blob):
    """把任意受支持的容器规整成账号 dict 列表。返回 (rows, error)。"""
    if isinstance(blob, list):
        rows = blob
    elif isinstance(blob, dict) and isinstance(blob.get("accounts"), list):
        rows = blob["accounts"]
    elif isinstance(blob, dict):
        looks_like_account = (
            blob.get("accessToken")
            or isinstance(blob.get("auth"), dict)
            or isinstance(blob.get("account"), dict)
        )
        if not looks_like_account:
            keys = ", ".join(sorted(blob.keys())[:6]) or "none"
            return [], ("not an account document (expected an accounts array, "
                        "a list, or an account object; got keys: %s)" % keys)
        rows = [blob]
    else:
        return [], "expected an object or a list of accounts"

    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return [], "account #%d is not an object" % (index + 1)
        out.append(row)
    if not out:
        return [], "no accounts found in the document"
    return out, ""


def normalise_import_row(row, realm=None):
    """把一行导入数据规整成 Account kwargs；无可用凭证时 raise ValueError。

    运行期字段（cooldownUntil/lastError/credits/lastCheckin/plan）不随导入采信：
    返回值只构造凭证与身份字段，运行期状态一律重置或缺失（白名单构造）。
    """
    auth = row.get("auth") if isinstance(row.get("auth"), dict) else None
    profile = row.get("account") if isinstance(row.get("account"), dict) else None

    def pick(key, default=None):
        for layer in (row, auth, profile):
            if isinstance(layer, dict) and layer.get(key) not in (None, ""):
                return layer.get(key)
        return default

    token = str(pick("accessToken") or "").strip()
    if not token:
        raise ValueError("no accessToken")
    detected = str(realm or pick("realm") or "").strip().lower()
    if detected not in ("cn", "intl", "qworkcn"):
        detected = detect_realm_from_domain(pick("domain"))
    if detected == "qwenwork.cn":
        detected = "qworkcn"
    cfg = get_realm_config(detected)
    raw_uid = str(pick("uid") or "").strip()
    uid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_uid).strip("_ ")
    if not uid:
        uid = "p-" + uuid.uuid4().hex[:24]
    exp = normalize_epoch(pick("expiresAt"))
    if not exp:
        exp = int(time.time()) + 3600
    return {
        "uid": uid,
        "nickname": str(pick("nickname") or ""),
        "domain": str(pick("domain") or cfg["domain"]),
        "realm": detected,
        "platform": str(pick("platform") or "CLI"),
        "accessToken": token,
        "refreshToken": str(pick("refreshToken") or ""),
        "personalToken": str(pick("personalToken") or ""),
        "expiresAt": exp,
        "source": "import",
        "enabled": True,
        "userType": str(pick("userType") or "") or DEFAULT_USER_TYPE,
        "organizationId": str(pick("organizationId") or ""),
        "organizationName": str(pick("organizationName") or ""),
        "lastError": "",
        "cooldownUntil": 0.0,
    }
