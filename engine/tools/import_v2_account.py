#!/usr/bin/env python3
"""把 AutoClaw 2.x（account-credentials.enc vault）里的登录态桥接成
a_switch.py 认的旧版 auth.json 目录布局。

AutoClaw 2.0.2 起不再写 auth.json，登录凭证存放在
<userData>/accounts/<accountKey>/account-credentials.enc：
Electron safeStorage（Chromium os_crypt v10 = DPAPI + AES-256-GCM）加密的
JSON {accessToken, refreshToken}。与旧 auth.json 里 enc:v10 值是同一套
加密链路，所以直接复用 a_switch 里现成的 _os_crypt_key / v10 解密，
不引入新解密实现。

用法：
    python tools/import_v2_account.py            # 扫描所有 AutoClaw* userData 并导入
    python tools/import_v2_account.py --verify   # 导入后跑 load_account 校验
"""
import base64
import json
import os
import re
import sys
from pathlib import Path

_verify = "--verify" in sys.argv
sys.argv = ["x"]
import importlib.util

_repo = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("a_switch", _repo / "a_switch.py")
a_switch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(a_switch)


def decrypt_vault(vault_path: Path, appdata_dir: Path) -> dict:
    """解密 account-credentials.enc（裸 v10 blob，非 base64 字符串）。"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    blob = vault_path.read_bytes()
    assert blob[:3] == b"v10", f"unknown vault prefix {blob[:3]!r}"
    key = a_switch._os_crypt_key(appdata_dir)
    plaintext = AESGCM(key).decrypt(blob[3:15], blob[15:], None).decode("utf-8")
    return json.loads(plaintext)


def import_one(user_data: Path, out_root: Path) -> dict | None:
    accounts_root = user_data / "accounts"
    if not accounts_root.is_dir():
        return None
    for acc_dir in accounts_root.iterdir():
        if not acc_dir.is_dir():
            continue
        vault = acc_dir / "account-credentials.enc"
        profile_p = acc_dir / "account-profile.json"
        if not vault.is_file() or not profile_p.is_file():
            continue
        cred = decrypt_vault(vault, user_data)
        profile = json.loads(profile_p.read_text(encoding="utf-8")).get("profile") or {}
        token = cred.get("accessToken") or ""
        if not token:
            print(f"[skip] {acc_dir.name[:12]}…: vault 无 accessToken")
            continue
        user_id = profile.get("numericUserId") or profile.get("accountId")
        out_dir = out_root / str(user_id or acc_dir.name)
        out_dir.mkdir(parents=True, exist_ok=True)
        auth = {
            "token": token,
            "refreshToken": cred.get("refreshToken") or "",
            "deviceId": "",
            "userInfo": {
                "user_id": user_id,
                "user_name": profile.get("displayName") or acc_dir.name[:12],
                "email": profile.get("email") or "",
            },
        }
        # deviceId：AutoClaw 2 存在 <userData>/device/device-id.json
        dev = user_data / "device" / "device-id.json"
        if dev.is_file():
            auth["deviceId"] = json.loads(dev.read_text(encoding="utf-8")).get("deviceId") or ""
        (out_dir / "auth.json").write_text(json.dumps(auth, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
        src_channel = user_data / "channel.json"
        if src_channel.is_file() and not (out_dir / "channel.json").exists():
            (out_dir / "channel.json").write_bytes(src_channel.read_bytes())
        # Local State 存着 os_crypt 密钥。缺了它，a_switch 续期后无法用 enc:v10
        # 回写 auth.json（FileNotFoundError），轮换后的 refreshToken 不落盘，
        # 下次续期就是死凭证。存档里已有 Local State 时不能覆盖（旧值可能正是
        # 现有 auth.json 的加密密钥）。
        src_state = user_data / "Local State"
        if src_state.is_file() and not (out_dir / "Local State").exists():
            (out_dir / "Local State").write_bytes(src_state.read_bytes())
        print(f"[ok] 导出 {auth['userInfo']['user_name']} "
              f"(uid={user_id}) -> {out_dir}")
        return {"uid": user_id, "dir": out_dir, "email": auth["userInfo"]["email"]}
    return None


def main() -> None:
    out_root = a_switch.ACCOUNTS_DIR
    appdata = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    cands = [appdata / "AutoClaw", appdata / "AutoClaw-oversea-official"]
    cands += [p for p in appdata.glob("AutoClaw*") if p.is_dir() and p not in cands]
    imported = []
    for ud in cands:
        if not ud.is_dir():
            continue
        r = import_one(ud, out_root)
        if r:
            imported.append(r)
    if not imported:
        print("未发现可导入的 AutoClaw 2.x 登录态")
        sys.exit(1)
    if _verify:
        for r in imported:
            acc = a_switch.load_account(Path(r["dir"]))
            assert acc and acc.token, f"校验失败: {r['dir']}"
            print(f"[verify] {acc.nickname} uid={acc.user_id} "
                  f"jwt_uid={acc.jwt_uid()} token_len={len(acc.token)} "
                  f"device_id={acc.device_id[:8]}…")


if __name__ == "__main__":
    main()
