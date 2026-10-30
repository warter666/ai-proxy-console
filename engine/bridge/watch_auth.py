# -*- coding: utf-8 -*-
"""凭证自动同步器：监听 AutoClaw 2.x 凭证文件变化（应用轮换 token 时会重写
account-credentials.enc），变化即重新解密并同步到 relay 的凭证源。
纯文件监听，零网络出站 —— 不触碰上游仓库"禁止固定节奏出站"红线。

用法：python watch_auth.py  （后台常驻）
"""
import json
import os
import sys
import time
from pathlib import Path

APPDATA = Path(os.environ["APPDATA"])
OUT = Path.home() / ".autoclaw-relay" / "auth-compat"
STATE_DIR = Path.home() / ".openclaw-autoclaw"
HERE = Path(__file__).resolve().parent
# 双安装源：国内官方版(AutoClaw-official→CN 线) + 国际 2.x 版(AutoClaw-oversea-official→海外线)。
# 每个源带自己的 lane 与输出文件，谁登录了就同步谁（互不覆盖）。
SOURCES = [
    (APPDATA / "AutoClaw-official", "cn", OUT / "auth-cn.json"),
    (APPDATA / "AutoClaw-oversea-official", "oversea", OUT / "auth-oversea.json"),
]
SRC = SOURCES[0][0]  # 兼容旧引用

sys.argv = ["x"]
import importlib.util
spec = importlib.util.spec_from_file_location(
    "a_switch", HERE.parent / "a_switch.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def sync():
    any_ok = False
    for src_dir, lane, out_file in SOURCES:
        if not (src_dir / "Local State").is_file():
            continue
        key = m._os_crypt_key(src_dir)
        acc_root = src_dir / "accounts"
        if not acc_root.is_dir():
            continue
        for acc_dir in acc_root.iterdir():
            enc = acc_dir / "account-credentials.enc"
            if not enc.is_file():
                continue
            blob = enc.read_bytes()
            if blob[:3] != b"v10":
                continue
            plain = AESGCM(key).decrypt(blob[3:15], blob[15:], None)
            cred = json.loads(plain.decode("utf-8"))
            prof_p = acc_dir / "account-profile.json"
            prof = json.loads(prof_p.read_text(encoding="utf-8")).get("profile", {}) if prof_p.is_file() else {}
            auth = {
                "lane": lane,
                "token": cred.get("accessToken"),
                "refreshToken": cred.get("refreshToken"),
                "deviceId": cred.get("deviceId") or "",
                "userInfo": {
                    "user_id": cred.get("userId") or prof.get("numericUserId"),
                    "user_name": cred.get("nickname") or prof.get("displayName"),
                    "email": cred.get("email") or prof.get("email") or "",
                },
            }
            OUT.mkdir(parents=True, exist_ok=True)
            out_file.write_text(json.dumps(auth, ensure_ascii=False, indent=2), encoding="utf-8")
            any_ok = True
    return any_ok


def fingerprint():
    """只监听凭证相关文件：accounts/<hash>/account-credentials.enc + Local State。
    应用轮换 token 时会重写 account-credentials.enc（其 mtime 变化即轮换信号）。"""
    fp = {}
    acc = SRC / "accounts"
    if acc.is_dir():
        for sub in acc.iterdir():
            enc = sub / "account-credentials.enc"
            if enc.is_file():
                fp[str(enc)] = enc.stat().st_mtime
    ls = SRC / "Local State"
    fp[str(ls)] = ls.stat().st_mtime if ls.exists() else 0
    return fp


def main():
    state = fingerprint()
    print(f"[watch_auth] watching {SRC} ({len(state)} files)", flush=True)
    Path.home().joinpath(".autoclaw-relay/watch_auth.pid").write_text(str(os.getpid()), encoding="utf-8")
    synced_at = 0
    while True:
        time.sleep(5)  # 本地文件轮询，无网络
        now = fingerprint()
        if now == state:
            continue
        state = now
        if time.time() - synced_at < 30:  # 防抖：30 秒内不重复同步
            continue
        try:
            if sync():
                synced_at = time.time()
                print(f"[watch_auth] credentials synced at {time.strftime('%H:%M:%S')}", flush=True)
        except Exception as e:
            print(f"[watch_auth] sync error: {type(e).__name__}: {e}", flush=True)
            time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            pf = Path.home() / ".autoclaw-relay/watch_auth.pid"
            if pf.exists() and pf.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pf.unlink()
        except Exception:
            pass
