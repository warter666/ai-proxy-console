#!/usr/bin/env python3
"""把 assets/icons/ui/*.png 打包进 gateway_ui.html 的 APP_ICON_DATA 块。

面板由 pywebview 以 html 字符串加载，相对路径取不到文件，所以图标必须以
data URI 内嵌。改完图标后运行：

    python tools/pack_app_icons.py

客户端源图：assets/icons/<key>.png（256px）；面板小猪源图：assets/pig.png。
内嵌版放在 assets/icons/ui/（客户端 96px，小猪 128px）。本脚本只用标准库，
不依赖 Pillow。
"""
from pathlib import Path
import base64
import re

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons" / "ui"
HTML = ROOT / "gateway_ui.html"
BEGIN = "/* APP_ICON_DATA:BEGIN */"
END = "/* APP_ICON_DATA:END */"


def main():
    items = []
    for png in sorted(ICONS.glob("*.png")):
        b64 = base64.b64encode(png.read_bytes()).decode("ascii")
        items.append('    %s: "data:image/png;base64,%s"' % (png.stem, b64))
    if not items:
        raise SystemExit("没有找到 assets/icons/ui/*.png")

    block = (
        BEGIN + "\n"
        "  const APP_ICON_DATA = {\n"
        + ",\n".join(items) + "\n"
        "  };\n  " + END
    )
    text = HTML.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise SystemExit("gateway_ui.html 里找不到 APP_ICON_DATA 标记")
    text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END),
                  lambda _m: block, text, flags=re.S)
    # 显式 LF：Windows 上 write_text 默认会把整份 HTML 改成 CRLF
    HTML.write_text(text, encoding="utf-8", newline="\n")
    print("packed %d icons - gateway_ui.html %.1f KB"
          % (len(items), HTML.stat().st_size / 1024))


if __name__ == "__main__":
    main()
