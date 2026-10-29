#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy + 豆包（消费版）每日签到。

默认执行两个平台；已签到会安全跳过，不重复领取。
日志写入 logs/checkin-YYYY-MM-DD.log，不记录令牌、cookie。
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import gw_specs

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
LOG_DIR = ROOT / "logs"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

WB_AUTH = HOME / "AppData" / "Local" / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
WB_KEY = ROOT / "wb-key-payload.json"

DOUBAO_COOKIE = HOME / ".doubao-relay" / "cookies-consumer.json"
DOUBAO_CONFIG = ROOT / "tools" / "doubao" / "doubao-consumer-config.json"
DOUBAO_SYNC_JS = ROOT / "tools" / "doubao" / "doubao_sync.mjs"
DOUBAO_EXE_ENV = os.environ.get("DOUBAO_CONSUMER_EXE") or ""
DOUBAO_CDP = 9223
DOUBAO_PORT = "9223"
DOUBAO_EXE_ARGS = [f"--remote-debugging-port={DOUBAO_CDP}", "--remote-allow-origins=*"]


def _doubao_exe():
    """消费版豆包客户端：环境变量 → clients.local.json(doubao_consumer) → 常见安装位置。"""
    cands = []
    if DOUBAO_EXE_ENV:
        cands.append(Path(DOUBAO_EXE_ENV))
    cands += [Path(os.path.expandvars(p)) for p in gw_specs.local_overrides("doubao_consumer")]
    local = Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local"))
    program_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
    cands += [
        local / "Doubao" / "app" / "Doubao.exe",
        local / "Programs" / "Doubao" / "app" / "Doubao.exe",
        program_files / "Doubao" / "app" / "Doubao.exe",
    ]
    for p in cands:
        if p.is_file():
            return p
    return None


class Logger:
    def __init__(self, quiet=False, json_mode=False):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / ("checkin-%s.log" % datetime.now().strftime("%Y-%m-%d"))
        self.quiet = quiet
        self.stream = sys.stderr if json_mode else sys.stdout

    def __call__(self, msg, level="INFO"):
        line = "[%s] [%s] %s" % (datetime.now().strftime("%H:%M:%S"), level, msg)
        if not self.quiet:
            print(line, file=self.stream, flush=True)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def _post_json(url, data, headers, timeout=30):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e)


def _parse_json(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _strip_font(text):
    if not text:
        return ""
    import re
    return re.sub(r"<[^>]+>", "", str(text))


def _configure_stdio():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------- WorkBuddy ----------------------------

def _u32(n):
    return int(n).to_bytes(4, "big")


def _lp(s):
    b = s.encode() if isinstance(s, str) else s
    return _u32(len(b)) + b


def _wb_aad(key_id, framing):
    name, fbyte = {"file": ("WBEF1", 1), "field": ("WBEV1", 2)}[framing]
    return (b"WB-AAD\x00" + b"\x01" + _lp(name) + _lp("sym-v1") + _u32(1)
            + _lp(key_id) + bytes([fbyte]) + b"\x00" + b"\x00")


def _wb_decrypt(wrapper):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    payload = json.loads(WB_KEY.read_text(encoding="utf-8"))
    if payload.get("keyHex"):
        key = bytes.fromhex(payload["keyHex"])
    elif payload.get("secretB64"):
        key = hashlib.sha256(payload["secretB64"].encode()).digest()
    else:
        raise RuntimeError("wb-key-payload.json 缺少 keyHex/secretB64")
    env = json.loads(base64.b64decode(wrapper["envelope"]))
    ct = base64.b64decode(env["ciphertext"]) + base64.b64decode(env["authTag"])
    nonce = base64.b64decode(env["nonce"])
    for framing in ("field", "file"):
        try:
            return AESGCM(key).decrypt(nonce, ct, _wb_aad(env["keyId"], framing)).decode("utf-8")
        except Exception:
            pass
    raise RuntimeError("WorkBuddy 令牌解密失败（密钥可能已轮换，需重跑 scan_mem.py）")


def _wb_headers(log):
    if not WB_AUTH.is_file():
        raise RuntimeError("未找到 WorkBuddy 登录文件：%s" % WB_AUTH)
    raw = json.loads(WB_AUTH.read_text(encoding="utf-8"))
    at = raw.get("auth", {}).get("accessToken")
    if not at:
        raise RuntimeError("WorkBuddy 登录文件里没有 accessToken")
    token = _wb_decrypt(at) if isinstance(at, dict) else str(at)
    uid = raw.get("account", {}).get("uid") or ""
    domain = raw.get("auth", {}).get("domain") or "www.workbuddy.cn"
    ver = "0.0.0"
    try:
        ver = json.loads((HOME / ".workbuddy" / "last-launch.json").read_text(encoding="utf-8")).get("version") or ver
    except Exception:
        pass
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "X-User-Id": uid,
        "X-Domain": domain,
        "User-Agent": "WorkBuddy/" + ver,
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": ver,
    }
    return headers, uid, ver


def _wb_status(headers):
    last = "未知错误"
    for base in ("https://www.workbuddy.cn", "https://copilot.tencent.com"):
        st, text = _post_json(base + "/v2/billing/meter/checkin-activity-status", {}, headers)
        data = _parse_json(text)
        if st == 200 and data and data.get("code") == 0 and data.get("data"):
            return data["data"], None
        if data:
            last = "HTTP %s code=%s msg=%s" % (st, data.get("code"), data.get("msg") or data.get("message"))
        else:
            last = "HTTP %s %s" % (st, (text or "")[:160])
    return None, last


def _wb_checkin(headers):
    st, text = _post_json("https://www.workbuddy.cn/v2/billing/meter/daily-checkin", {}, headers)
    data = _parse_json(text)
    if not data:
        return {"ok": False, "message": "HTTP %s %s" % (st, (text or "")[:160])}
    code = data.get("code")
    if code == 0:
        return {"ok": True, "claimed": True, "data": data}
    if code == 1001:
        return {"ok": True, "claimed": False, "already": True, "message": data.get("msg") or "今天已签到"}
    if code == 1002:
        return {"ok": False, "message": data.get("msg") or "当前账号不满足签到条件（1002）"}
    if code == 1003:
        return {"ok": False, "message": data.get("msg") or "签到活动已结束（1003）"}
    return {"ok": False, "message": "HTTP %s code=%s msg=%s" % (st, code, data.get("msg") or data.get("message"))}


def run_workbuddy(log, status_only=False):
    result = {"platform": "WorkBuddy", "status": "error", "message": ""}
    try:
        headers, uid, ver = _wb_headers(log)
    except Exception as e:
        result["message"] = str(e)
        log("WorkBuddy: " + result["message"], "ERROR")
        return result
    log("WorkBuddy: 登录态已解密（uid %s…, 版本 %s）" % (uid[:8], ver))
    data, err = _wb_status(headers)
    if not data:
        result["message"] = "查询签到状态失败：" + str(err)
        log("WorkBuddy: " + result["message"], "ERROR")
        return result
    active = bool(data.get("active"))
    checked = bool(data.get("today_checked_in"))
    streak = data.get("streak_days", 0)
    daily = data.get("daily_credit", 0)
    today = data.get("today_credit", 0)
    total = data.get("total_credits")
    result.update({
        "active": active,
        "today_checked_in": checked,
        "streak_days": streak,
        "daily_credit": daily,
        "today_credit": today,
        "total_credits": total,
        "end_time": data.get("end_time"),
    })
    if not active:
        result["status"] = "no_activity"
        result["message"] = "当前没有进行中的签到活动"
        log("WorkBuddy: " + result["message"])
        return result
    if checked:
        result["status"] = "already"
        result["message"] = "今日已签到（连续 %s 天，今日 %s 额度，累计 %s）" % (streak, today or daily, total)
        log("WorkBuddy: " + result["message"])
        return result
    if status_only:
        result["status"] = "pending"
        result["message"] = "今日未签到（连续 %s 天，今日可得 %s 额度）" % (streak, daily)
        log("WorkBuddy: " + result["message"])
        return result
    log("WorkBuddy: 今日未签到，执行签到…")
    r = _wb_checkin(headers)
    if not r.get("ok"):
        result["message"] = "签到失败：" + r.get("message", "")
        log("WorkBuddy: " + result["message"], "ERROR")
        return result
    if r.get("already"):
        result["status"] = "already"
        result["message"] = "今天已签到（%s）" % r.get("message", "")
        log("WorkBuddy: " + result["message"])
        return result
    info = r.get("data") or {}
    credit = info.get("credit") or info.get("data", {}).get("credit") or daily
    new_streak = info.get("streak_days") or streak + 1
    result["status"] = "checked_in"
    result["message"] = "签到成功（+%s 额度，连续 %s 天）" % (credit, new_streak)
    result["credit"] = credit
    result["streak_days"] = new_streak
    log("WorkBuddy: " + result["message"])
    return result


# ---------------------------- 豆包（消费版） ----------------------------

def _load_doubao():
    if not DOUBAO_COOKIE.is_file():
        raise RuntimeError("缺少豆包 cookie：%s" % DOUBAO_COOKIE)
    raw = json.loads(DOUBAO_COOKIE.read_text(encoding="utf-8"))
    jar = raw.get("jar") or {}
    if not jar.get("sessionid"):
        raise RuntimeError("豆包 cookie 中没有 sessionid，需要重新同步登录态")
    cfg = {}
    if DOUBAO_CONFIG.is_file():
        cfg = json.loads(DOUBAO_CONFIG.read_text(encoding="utf-8"))
    if not cfg.get("device_id"):
        raise RuntimeError("缺少豆包设备参数，需要重新同步（doubao_sync.mjs）")
    return raw, jar, cfg


def _doubao_headers(jar, cfg):
    cookie = "; ".join("%s=%s" % (k, v) for k, v in jar.items() if v)
    pc = cfg.get("pc_version") or "2.33.3"
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 SamanthaDoubao/" + pc)
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.doubao.com",
        "Referer": "chrome://doubao-chat/",
        "User-Agent": ua,
        "Agw-Js-Conv": "str",
        "Cookie": cookie,
    }


def _doubao_query(cfg):
    pc = cfg.get("pc_version") or "2.33.3"
    device_id = cfg["device_id"]
    params = {
        "version_code": cfg.get("version_code") or "20800",
        "language": "zh",
        "device_platform": "web",
        "doubao_device_platform": "desktop",
        "aid": "582478",
        "real_aid": "582478",
        "pkg_type": "release_version",
        "device_id": device_id,
        "pc_version": pc,
        "doubao_pc_version": pc,
        "web_id": cfg.get("web_id") or "",
        "tea_uuid": device_id,
        "region": "CN",
        "sys_region": "CN",
        "samantha_web": "1",
        "web_platform": "desktop",
        "use-olympus-account": "1",
        "runtime": "web",
        "runtime_version": cfg.get("runtime_version") or "3.42.4",
        "client_platform": "pc_client",
        "chromium_version": cfg.get("chromium_version") or "147.0.0.0",
        "channel": "win",
        "fp": "verify_" + device_id,
    }
    return urllib.parse.urlencode(params)


def _doubao_post(path, body, jar, cfg, timeout=30):
    url = "https://www.doubao.com" + path + "?" + _doubao_query(cfg)
    return _post_json(url, body, _doubao_headers(jar, cfg), timeout=timeout)


def _cdp_alive():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%s/json/list" % DOUBAO_PORT, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _doubao_running():
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Doubao.exe"], capture_output=True,
                           text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
        return "Doubao.exe" in (r.stdout or "")
    except Exception:
        return False


def _launch_doubao(log, allow_restart=True):
    exe = _doubao_exe()
    if not exe:
        return False, "未找到豆包客户端（设 DOUBAO_CONSUMER_EXE，或在 clients.local.json 配 doubao_consumer）"
    if _doubao_running():
        if not allow_restart:
            return False, "豆包正在运行但没有调试端口；已禁用自动重启"
        log("豆包: 正在运行但未开启调试端口，重启以同步登录态（约 15 秒）…")
        subprocess.run(["taskkill", "/IM", "Doubao.exe", "/F", "/T"], capture_output=True,
                       timeout=30, creationflags=CREATE_NO_WINDOW)
        time.sleep(3)
    else:
        log("豆包: 客户端未运行，启动并开启调试端口…")
    try:
        subprocess.Popen([str(exe)] + DOUBAO_EXE_ARGS, cwd=str(exe.parent),
                         creationflags=CREATE_NO_WINDOW, close_fds=True)
    except Exception as e:
        return False, "启动豆包失败：%s" % e
    for _ in range(30):
        time.sleep(0.7)
        if _cdp_alive():
            time.sleep(2)
            return True, "已启动"
    return False, "豆包启动后 21 秒内调试端口未就绪"


def _sync_doubao(log, allow_restart=True):
    if not DOUBAO_SYNC_JS.is_file():
        return False, "缺少 doubao_sync.mjs"
    if not _cdp_alive():
        ok, note = _launch_doubao(log, allow_restart)
        if not ok:
            return False, note
    last = "同步失败"
    for attempt in range(3):
        try:
            r = subprocess.run(["node", str(DOUBAO_SYNC_JS)], capture_output=True, text=True,
                               timeout=60, creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            last = "调用 node 同步失败：%s" % e
        else:
            info = None
            for line in reversed((r.stdout or "").splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    info = _parse_json(line)
                    break
            if not info:
                last = "同步脚本无输出：%s" % ((r.stderr or r.stdout or "")[-200:])
            elif not info.get("ok"):
                last = str(info.get("error") or "同步失败")
            else:
                return True, "cookie %s 项，sessionid %s 位，活动 %s" % (
                    info.get("cookies"), info.get("sessionid_len"), info.get("campaign_group_code"))
        if attempt < 2:
            time.sleep(3)
    return False, last


def _doubao_checkin_once(log, jar, cfg):
    campaign = cfg.get("campaign_group_code") or "pc_incentive_task_mvp_v3"
    st, text = _doubao_post("/alice/ug/campaign/landing/checkin", {"scene": campaign}, jar, cfg)
    data = _parse_json(text)
    if not data:
        # 网络错误不重启客户端，避免无谓打断
        return {"ok": False, "retry_sync": st in (401, 403), "message": "HTTP %s %s" % (st, (text or "")[:160])}
    code = data.get("code")
    if code == 0:
        d = data.get("data") or {}
        return {
            "ok": True,
            "checked_in": bool(d.get("checked_in")),
            "idempotent": bool(d.get("idempotent")),
            "reward": d.get("reward_amount"),
            "days": d.get("continuous_days"),
            "reward_status": d.get("reward_status"),
            "reward_code": d.get("reward_code"),
            "campaign": campaign,
        }
    msg = data.get("message") or data.get("msg") or ""
    return {"ok": False, "retry_sync": code in (710010702, 1) or st in (401, 403),
            "code": code, "message": msg}


def run_doubao(log, status_only=False, allow_restart=True):
    result = {"platform": "豆包", "status": "error", "message": ""}
    try:
        raw, jar, cfg = _load_doubao()
    except Exception as e:
        log("豆包: " + str(e), "WARN")
        ok, note = _sync_doubao(log, allow_restart)
        if not ok:
            result["message"] = str(e) + "；同步失败：" + note
            log("豆包: " + result["message"], "ERROR")
            return result
        log("豆包: 同步完成（%s）" % note)
        try:
            raw, jar, cfg = _load_doubao()
        except Exception as e2:
            result["message"] = str(e2)
            log("豆包: " + result["message"], "ERROR")
            return result
    campaign = cfg.get("campaign_group_code") or "pc_incentive_task_mvp_v3"
    result["campaign"] = campaign
    if status_only:
        error = "查询失败"
        for attempt in range(2):
            st, text = _doubao_post("/alice/ug/campaign/landing/get", {"scene": campaign}, jar, cfg)
            data = _parse_json(text) or {}
            d = data.get("data") or {}
            title = _strip_font((d.get("main_page_info") or {}).get("title") or (d.get("complete_page_info") or {}).get("title") or "")
            if data.get("code") == 0 and d:
                result["status"] = "active"
                result["message"] = "签到活动可用：%s（签到时会按幂等接口执行）" % (title or campaign)
                log("豆包: " + result["message"])
                return result
            code = data.get("code")
            error = "查询失败：code=%s %s" % (code, _strip_font(data.get("message") or data.get("msg")))
            if attempt == 0 and (code in (710010702, 1) or st in (401, 403)):
                ok, note = _sync_doubao(log, allow_restart)
                if ok:
                    log("豆包: 查询失败已自动同步登录态（%s），重试…" % note, "WARN")
                    try:
                        raw, jar, cfg = _load_doubao()
                        campaign = cfg.get("campaign_group_code") or campaign
                        continue
                    except Exception as e:
                        error = str(e)
                        break
                error += "；同步失败：" + note
                break
            break
        result["message"] = error
        log("豆包: " + result["message"], "ERROR")
        return result

    log("豆包: 活动 %s，执行签到…" % campaign)
    r = _doubao_checkin_once(log, jar, cfg)
    if not r.get("ok") and r.get("retry_sync"):
        log("豆包: 接口返回错误（%s），尝试同步登录态后重试…" % r.get("message"), "WARN")
        ok, note = _sync_doubao(log, allow_restart)
        if ok:
            log("豆包: 同步完成（%s）" % note)
            try:
                raw, jar, cfg = _load_doubao()
                campaign = cfg.get("campaign_group_code") or campaign
                r = _doubao_checkin_once(log, jar, cfg)
            except Exception as e:
                r = {"ok": False, "message": str(e)}
        else:
            r["message"] = (r.get("message") or "") + "；同步失败：" + note
    if not r.get("ok"):
        result["message"] = "签到失败：%s" % r.get("message", "")
        log("豆包: " + result["message"], "ERROR")
        return result

    days = r.get("days")
    reward = r.get("reward")
    result["continuous_days"] = days
    result["reward_amount"] = reward
    if r.get("idempotent"):
        result["status"] = "already"
        result["message"] = "今日已签到（连续 %s 天，奖励 %s，接口幂等）" % (days, reward)
    else:
        result["status"] = "checked_in"
        result["message"] = "签到成功（+%s，连续 %s 天）" % (reward, days)
    if r.get("reward_status") == 4:
        result["message"] += "；奖励发放失败（reward_status=4）"
        result["status"] = "error"
    log("豆包: " + result["message"], "ERROR" if result["status"] == "error" else "INFO")
    return result


# ---------------------------- main ----------------------------

def main():
    _configure_stdio()
    parser = argparse.ArgumentParser(description="WorkBuddy + 豆包每日签到")
    parser.add_argument("--workbuddy", action="store_true", help="只执行 WorkBuddy")
    parser.add_argument("--doubao", action="store_true", help="只执行豆包")
    parser.add_argument("--status", action="store_true", help="只查看状态，不签到")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    parser.add_argument("--no-restart", action="store_true", help="豆包调试端口不可用时不要自动重启客户端")
    parser.add_argument("--scheduled", action="store_true", help="计划任务模式（不输出到控制台）")
    args = parser.parse_args()

    if args.json:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    log = Logger(quiet=args.scheduled or args.json, json_mode=args.json)

    do_wb = args.workbuddy or not args.doubao
    do_db = args.doubao or not args.workbuddy

    log("===== 每日签到开始 =====")
    summary = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "status_only": args.status}
    if do_wb:
        summary["workbuddy"] = run_workbuddy(log, status_only=args.status)
    if do_db:
        summary["doubao"] = run_doubao(log, status_only=args.status, allow_restart=not args.no_restart)

    bad = [v for k, v in summary.items() if isinstance(v, dict) and v.get("status") == "error"]
    summary["ok"] = not bad
    log("===== 每日签到结束：%s =====" % ("有失败项" if bad else "正常"))
    log("日志：%s" % log.path)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
