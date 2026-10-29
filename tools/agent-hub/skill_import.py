# -*- coding: utf-8 -*-
"""自动发现并导入 skill —— 把用户选的目录/文件整理进 AgentHub 技能库。

支持三种输入：
  1. 单个 skill 目录（含 SKILL.md）
  2. 多 skill 包（子目录里各有 SKILL.md，如 academic-figure-skills）
  3. 批量：一个父目录下多个 skill 目录

只做「复制到 store」，不改动、不删除来源，也不碰已有的同名技能（默认跳过）。

用法：
    python skill_import.py scan <路径>            只检测，报告会导入什么
    python skill_import.py list                  列出 store 里已有技能
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

def _default_store():
    env = os.environ.get("AGENTHUB_SKILLS")
    if env:
        return Path(env)
    hub = os.environ.get("AGENTHUB_DIR")
    if hub:
        return Path(hub) / "skills"
    return Path(__file__).resolve().parent.parent / "AgentHub" / "skills"


STORE = _default_store()
SKILL_FILE = "SKILL.md"


def _read_frontmatter(skill_md: Path):
    """解析 SKILL.md 的 YAML frontmatter（只取 name/description，不依赖 PyYAML）。"""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}, ""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head = text[3:end]
    body = text[end + 4:]
    meta = {}
    cur_key = None
    cur_val = []
    for raw in head.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(\s*)([\w-]+):\s*(.*)$", line)
        if m and not m.group(1):
            if cur_key:
                meta[cur_key] = " ".join(x.strip() for x in cur_val).strip()
            cur_key = m.group(2)
            cur_val = [m.group(3)] if m.group(3) else []
        elif cur_key is not None:
            cur_val.append(line)
    if cur_key:
        meta[cur_key] = " ".join(x.strip() for x in cur_val).strip()
    # 去掉可能残留的引号
    for k in list(meta):
        v = meta[k]
        if v.startswith((">", "|")):
            v = v.lstrip(">|").strip()
        meta[k] = v.strip().strip('"').strip("'")
    return meta, body


def _is_skill_dir(p: Path) -> bool:
    return p.is_dir() and (p / SKILL_FILE).is_file()


def scan(target: str):
    """检测目标路径，返回计划导入的 skill 列表。"""
    root = Path(target).expanduser()
    if not root.exists():
        return {"ok": False, "message": "路径不存在：%s" % root, "items": []}

    found = []   # [{name, src, kind, title}]
    if root.is_file():
        # 单文件：只接受 SKILL.md（导入它所在的目录）
        if root.name.lower() == SKILL_FILE.lower():
            d = root.parent
            meta, _ = _read_frontmatter(root)
            found.append({"name": d.name, "src": str(d), "kind": "single",
                          "title": meta.get("name") or d.name,
                          "desc": meta.get("description", "")[:160]})
        else:
            return {"ok": False, "items": [],
                    "message": "单文件目前只支持 SKILL.md；其他文件请放进 skill 目录再导入"}
    elif _is_skill_dir(root):
        meta, _ = _read_frontmatter(root / SKILL_FILE)
        found.append({"name": root.name, "src": str(root), "kind": "single",
                      "title": meta.get("name") or root.name,
                      "desc": meta.get("description", "")[:160]})
    else:
        # 目录本身不是 skill（无 SKILL.md），检查子目录
        subs = [d for d in sorted(root.iterdir()) if d.is_dir()] if root.is_dir() else []
        skill_subs = [d for d in subs if _is_skill_dir(d)]
        if skill_subs:
            # 有子技能：默认整体导入这个包（保留内部结构，如 academic-figure-skills
            # 里的 fig1-analyze / fig1-draw 共享 scripts、docs 等公共资源）。
            # 只导入子目录会导致公共资源丢失，所以整包搬更安全。
            meta, _ = _read_frontmatter(root / SKILL_FILE) if _is_skill_dir(root) else ({}, "")
            found.append({
                "name": root.name, "src": str(root), "kind": "package",
                "title": meta.get("name") or root.name,
                "desc": (meta.get("description") or
                         "包含 %d 个子技能：%s" % (
                             len(skill_subs), ", ".join(d.name for d in skill_subs)))[:160],
                "children": [d.name for d in skill_subs],
            })
        else:
            return {"ok": False, "items": [],
                    "message": "没找到 SKILL.md。请选择 skill 目录，或包含多个 skill 子目录的父目录"}

    # 标注冲突
    for it in found:
        dest = STORE / it["name"]
        it["dest"] = str(dest)
        it["conflict"] = dest.exists()
        if it["conflict"]:
            it["conflict_reason"] = "技能库已有同名条目"
    return {"ok": True, "items": found, "store": str(STORE)}


def do_import(items, on_conflict="skip", rename_suffix=""):
    """真正把 skill 复制进 store。

    on_conflict: skip（默认，安全） / overwrite（先删同名再复制） / rename（加后缀）
    绝不删除来源目录，只读复制。
    """
    results = []
    for it in items:
        src = Path(it["src"])
        name = it["name"]
        dest = STORE / name
        if dest.exists():
            if on_conflict == "skip":
                results.append({"name": name, "ok": False, "skipped": True,
                                "message": "同名已存在，跳过"})
                continue
            if on_conflict == "rename":
                base = name + (rename_suffix or "-imported")
                n = 1
                while (STORE / base).exists():
                    n += 1
                    base = "%s%s-%d" % (name, rename_suffix or "-imported", n)
                dest = STORE / base
                name = base
            elif on_conflict == "overwrite":
                try:
                    shutil.rmtree(dest)
                except Exception as exc:
                    results.append({"name": name, "ok": False,
                                    "message": "覆盖前删除失败：%s" % exc})
                    continue
        try:
            STORE.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, symlinks=True)
            results.append({"name": name, "ok": True, "dest": str(dest)})
        except Exception as exc:
            # 复制失败时清掉半成品，避免留下不完整技能
            try:
                if dest.exists():
                    shutil.rmtree(dest)
            except Exception:
                pass
            results.append({"name": name, "ok": False,
                            "message": "%s: %s" % (type(exc).__name__, exc)})
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"ok": ok_n > 0 or not results, "imported": ok_n,
            "total": len(results), "results": results}


def cmd_scan(argv):
    if len(argv) < 3:
        print("用法: skill_import.py scan <路径>")
        return 2
    r = scan(argv[2])
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else 1


def cmd_list(argv):
    if not STORE.is_dir():
        print("技能库不存在：%s" % STORE)
        return 1
    n = 0
    for d in sorted(STORE.iterdir()):
        if not d.is_dir():
            continue
        has = (d / SKILL_FILE).is_file()
        sub = [x.name for x in d.iterdir() if x.is_dir() and (x / SKILL_FILE).is_file()] if d.is_dir() else []
        tag = "单技能" if has else ("多技能包" if sub else "非标准")
        n += 1
        print("%-38s %-8s %s" % (d.name[:38], tag, ",".join(sub)[:60]))
    print("\n合计 %d 项" % n)
    return 0


def cmd_import(argv):
    """scan + 导入一步完成：import <路径> [--overwrite|--rename]"""
    if len(argv) < 3:
        print("用法: skill_import.py import <路径> [--overwrite|--rename]")
        return 2
    target = argv[2]
    on_conflict = "skip"
    if "--overwrite" in argv:
        on_conflict = "overwrite"
    elif "--rename" in argv:
        on_conflict = "rename"
    s = scan(target)
    if not s.get("ok"):
        print(json.dumps(s, ensure_ascii=False, indent=1))
        return 1
    items = s.get("items") or []
    conflicts = [x for x in items if x.get("conflict")]
    if conflicts and on_conflict == "skip":
        print(json.dumps({"ok": False,
                          "message": "有 %d 个同名冲突；加 --overwrite 覆盖或 --rename 改名导入" % len(conflicts),
                          "conflicts": [x["name"] for x in conflicts]},
                         ensure_ascii=False, indent=1))
        return 1
    r = do_import(items, on_conflict=on_conflict)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if r.get("ok") else 1


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "list").lower()
    if cmd == "scan":
        return cmd_scan(argv)
    if cmd == "import":
        return cmd_import(argv)
    if cmd == "list":
        return cmd_list(argv)
    print("用法:\n  skill_import.py scan <路径>\n  skill_import.py import <路径> [--overwrite|--rename]\n  skill_import.py list")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
