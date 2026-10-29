# -*- coding: utf-8 -*-
"""兼容薄壳：转发到 ai_hub.py（多供应商注册表式管理器）。

现位于 tools/compat/；历史上桌面 AI反代一键启动.bat 调 `python all_in_one.py --gui`，
这里保持参数兼容，仍转发回仓库根 ai_hub.py：
    all_in_one.py          -> ai_hub.py start
    all_in_one.py --gui    -> ai_hub.py start --gui
    all_in_one.py --no-wait-> ai_hub.py start --no-wait
    all_in_one.py wb,doubeao --gui -> 只启这两个（不透传 auto 即不启 AutoClaw relay）
旧版"一损俱损"的行为已由 ai_hub 修复：每家代理独立 try/except，一家失败不阻断其他。
原 364 行实现备份在同目录 all_in_one.py.bak-*。
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB = HERE.parent.parent / "ai_hub.py"

if __name__ == "__main__":
    extra = [a for a in sys.argv[1:] if a in ("--gui", "--no-wait")]
    # 第一个非 flag 参数 = 代理名单（逗号分隔），透传给 ai_hub 的 _resolve。
    # 给了名单就只启这几家（例如 wb,doubeao 表示暂时不启 AutoClaw relay）。
    names = [a for a in sys.argv[1:] if a not in ("--gui", "--no-wait")]
    sys.exit(subprocess.call([sys.executable, str(HUB), "start"] + names + extra))
