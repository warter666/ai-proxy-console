# -*- coding: utf-8 -*-
"""客户端配置接入 —— 检测 → 预览 → 备份后写入。

能安全自动写的只有两类：
  DSH   cordis.patch.yml：修正三个本地 provider 的 baseURL / 超时，保留其它字段；
        add_missing=True 时还会补上缺失的 provider 块（模型目录优先取网关实时
        /v1/models，读不到再用内置清单），写前模拟解析，缩进不对就不写
  ZCode provider_config.json：复用 a_switch 的注册函数，重写四个本地 provider

Claude Code / Codex 只生成可复制片段，不自动写：
它们的模型名必须与网关 /v1/models 对齐，自动改 base_url 容易把原本能用的
直连配置弄坏。片段里的 token 一律是占位值，真实凭据不经过这里。

所有写入前先备份为 <文件名>.bak-ai-proxy-YYYYmmdd-HHMMSS。
预览阶段只回显 URL / 字段名，不展示任何密钥。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import ai_hub as hub

DSH_PATH = Path.home() / "AppData" / "Roaming" / "dsh-desktop" / \
    "harness" / "profiles" / "web" / "cordis.patch.yml"
ZCODE_PATH = Path.home() / ".zcode" / "v2" / "provider_config.json"
CLAUDE_PATH = Path.home() / ".claude" / "settings.json"
CODEX_PATH = Path.home() / ".codex" / "config.toml"

DSH_TARGETS = {
    # streamIdleTimeoutMs 保持 120000：空流转 120s 触发 guard 切 workbuddy，
    # 不是越大越好；只有缺失时才补，已存在就不动。
    "autoclaw": {
        "base": "http://127.0.0.1:18766/v1", "timeout": 420000, "idle": 120000,
        "apiKeyEnv": "AUTOCLAW_LOCAL_KEY", "retry": True,
        "models": (("GLM-5.3", 1048576, 131072),
                   ("GLM-5.3-Flash", 1048576, 131072)),
    },
    "workbuddy": {
        "base": "http://127.0.0.1:8787/v1", "apiKeyEnv": "WB_LOCAL_KEY",
        "compat": True,
        "models": (("hy3-b", 96000, 64000),
                   ("hy4-preview-f", 1000000, 64000),
                   ("deepseek-v4.1-flash", 1000000, 128000),
                   ("glm-5.3-flash", 1000000, 131072)),
    },
    "qoder": {
        "base": "http://127.0.0.1:8790/v1", "apiKeyEnv": "QODER_LOCAL_KEY",
        "compat": True,
        "models": (("qwen3.8-flash", 180000, 32768),
                   ("deepseek-flash", 180000, 32768),
                   ("glm-5.3-flash", 1000000, 32768),
                   ("kimi-k3", 180000, 32768)),
    },
}

ZCODE_TARGETS = [
    {"id": "autoclaw-glm-provider", "name": "AutoClaw",
     "base": "http://127.0.0.1:18766", "fn": "zcode_register_provider"},
    {"id": "workbuddy-provider", "name": "WorkBuddy",
     "base": "http://127.0.0.1:8787/v1", "fn": "zcode_register_workbuddy"},
    {"id": "qoder-openai-provider", "name": "Qoder CN",
     "base": "http://127.0.0.1:8790/v1", "fn": "zcode_register_qoder"},
    {"id": "doubao-openai-provider", "name": "豆包工作",
     "base": "http://127.0.0.1:18770/v1", "fn": "doubao"},
]

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


# ---------------------------------------------------------------- 通用
def _host_of(url):
    try:
        from urllib.parse import urlsplit
        return (urlsplit(str(url)).hostname or "").lower()
    except Exception:
        return ""


def _backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(path.name + ".bak-ai-proxy-" + stamp)
    n = 2
    while bak.exists():
        bak = path.with_name(path.name + ".bak-ai-proxy-%s-%d" % (stamp, n))
        n += 1
    bak.write_bytes(path.read_bytes())
    return bak


def _read_lines(path):
    raw = Path(path).read_bytes().decode("utf-8", errors="replace")
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n").split("\n"), nl


def _line_value(line):
    s = line.split("#", 1)[0].strip()
    if ":" not in s:
        return ""
    return s.split(":", 1)[1].strip().strip("\"'")


# ---------------------------------------------------------------- DSH
def _gateway_models(base, fallback):
    """读网关实时 /v1/models；读不到就用内置清单。返回 [(id, ctx, max)]。"""
    out, seen = [], set()
    try:
        import urllib.request
        with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=2.5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        for m in (data.get("data") or []):
            mid = str(m.get("id") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            ctx = (m.get("context_length") or m.get("max_input_tokens")
                   or m.get("context_window") or 200000)
            mx = m.get("max_output_tokens") or m.get("max_tokens") or 32768
            try:
                out.append((mid, int(ctx), int(mx)))
            except Exception:
                out.append((mid, 200000, 32768))
    except Exception:
        out = []
    return out[:20] if out else list(fallback)


def _yaml_scalar(text):
    if re.match(r"^[A-Za-z0-9._/+-]+$", text):
        return text
    return json.dumps(text, ensure_ascii=False)


def _dsh_provider_block(name, tgt, indent):
    pad = " " * indent
    models = _gateway_models(tgt["base"], tgt.get("models") or ())
    lines = [pad + name + ":",
             pad + "  apiKeyEnv: " + tgt["apiKeyEnv"],
             pad + "  api: openai-completions",
             pad + "  baseURL: " + tgt["base"]]
    if tgt.get("timeout"):
        lines.append(pad + "  timeoutMs: %d" % tgt["timeout"])
    if tgt.get("idle"):
        lines.append(pad + "  streamIdleTimeoutMs: %d" % tgt["idle"])
    if tgt.get("retry"):
        lines += [pad + "  retryPolicy:",
                  pad + "    mode: normal",
                  pad + "    maxRetries: 2"]
    if tgt.get("compat"):
        lines += [pad + "  compat:",
                  pad + "    supportsDeveloperRole: false"]
    lines.append(pad + "  models:")
    for mid, ctx, mx in models:
        lines += [pad + "    - id: " + _yaml_scalar(mid),
                  pad + "      contextWindow: %d" % ctx,
                  pad + "      maxTokens: %d" % mx]
    return "\n".join(lines)


def _dsh_blocks(lines):
    """只解析 `- id: llm-pi-ai` 这一项的 config.providers。

    插件（如 autoclaw-guard）自己也有 providers.autoclaw，名字相同但字段不同；
    按全文找 providers 会错改插件配置。

    返回 (blocks, prov_end, prov_indent)：
    prov_end 是 providers 块最后一行内容的下标，新增 provider 插在它后面；
    没有 providers 块时两者都为 None。
    """
    out, cur, prov_indent, item = [], None, None, None
    prov_end, prov_indent_seen = None, None
    for i, line in enumerate(lines):
        body = line.split("#", 1)[0].rstrip()
        if not body.strip():
            continue
        if not line.startswith(" ") and body.startswith("- id:"):
            item = body.split(":", 1)[1].strip()
            prov_indent, cur = None, None
            continue
        if item != "llm-pi-ai":
            continue
        indent = len(body) - len(body.lstrip(" "))
        s = body.strip()
        if s == "providers:" and prov_indent is None:
            prov_indent, cur = indent, None
            prov_indent_seen = indent
            prov_end = i
            continue
        if prov_indent is None:
            continue
        if indent <= prov_indent:
            prov_indent, cur = None, None
            continue
        prov_end = i
        if indent == prov_indent + 2 and s.endswith(":"):
            cur = {"name": s[:-1], "start": i, "indent": indent,
                   "base": None, "timeout": None, "idle": None}
            out.append(cur)
            continue
        if cur is None:
            continue
        if s.startswith("baseURL:"):
            cur["base"] = i
        elif s.startswith("streamIdleTimeoutMs:"):
            cur["idle"] = i
        elif s.startswith("timeoutMs:"):
            cur["timeout"] = i
    return out, prov_end, prov_indent_seen


def _dsh_apply_lines(lines, edits, inserts):
    out = []
    for i, line in enumerate(lines):
        out.append(edits.get(i, line))
        for chunk in inserts.get(i, []):
            out.extend(str(chunk).split("\n"))
    return out


def _dsh_plan(path, add_missing=False):
    if not path.is_file():
        return {"ok": False, "message": "未找到 DSH 配置：%s" % path}
    lines, nl = _read_lines(path)
    blocks, prov_end, prov_indent = _dsh_blocks(lines)
    by_name = {b["name"]: b for b in blocks}
    edits, inserts, changes, missing, added = {}, {}, [], [], []

    for name, tgt in DSH_TARGETS.items():
        b = by_name.get(name)
        if not b:
            if add_missing and prov_end is not None:
                block = _dsh_provider_block(name, tgt, prov_indent + 2)
                inserts.setdefault(prov_end, []).append(block)
                added.append(name)
                changes.append({"provider": name, "field": "provider",
                                "from": "(缺失)", "to": "新增（%d 个模型）" % block.count("- id:")})
            else:
                missing.append(name)
            continue
        pad = " " * (b["indent"] + 2)
        if b["base"] is None:
            inserts.setdefault(b["start"], []).append(pad + "baseURL: " + tgt["base"])
            changes.append({"provider": name, "field": "baseURL",
                            "from": "(缺失)", "to": tgt["base"]})
        else:
            cur = _line_value(lines[b["base"]])
            if cur != tgt["base"]:
                edits[b["base"]] = pad + "baseURL: " + tgt["base"]
                changes.append({"provider": name, "field": "baseURL",
                                "from": cur or "(空)", "to": tgt["base"]})
        if "timeout" not in tgt:
            continue
        # 总超时必须覆盖 relay 全池等待；空流转超时只补缺失，现有值视为有意配置
        rules = (("timeoutMs", b["timeout"], tgt["timeout"], "min"),
                 ("streamIdleTimeoutMs", b["idle"], tgt.get("idle"), "fill"))
        for field, idx, value, mode in rules:
            if value is None:
                continue
            if idx is None:
                anchor = b["base"] if b["base"] is not None else b["start"]
                inserts.setdefault(anchor, []).append(pad + "%s: %s" % (field, value))
                changes.append({"provider": name, "field": field,
                                "from": "(缺失)", "to": str(value)})
                continue
            if mode != "min":
                continue
            cur = _line_value(lines[idx])
            if not cur.isdigit() or int(cur) < 330000:
                edits[idx] = pad + "%s: %s" % (field, value)
                changes.append({"provider": name, "field": field,
                                "from": cur or "(空)", "to": str(value)})

    notes = []
    if added:
        notes.append("缺少的 provider 将自动新增：%s（模型目录取自网关实时 /v1/models，"
                     "读不到时用内置清单）" % " / ".join(added))
        # 新增块是纯文本拼接，写前先模拟并重新解析，缩进不对就不写
        sim = _dsh_apply_lines(lines, edits, inserts)
        sim_blocks, _end, _ind = _dsh_blocks(sim)
        sim_by = {b["name"]: b for b in sim_blocks}
        bad = []
        for name in added:
            b = sim_by.get(name)
            if (not b or b["base"] is None
                    or _line_value(sim[b["base"]]) != DSH_TARGETS[name]["base"]):
                bad.append(name)
        if bad:
            inserts.clear()
            changes[:] = [c for c in changes if c.get("provider") not in bad]
            missing.extend(bad)
            notes.append("自动新增后校验失败：%s；请手动接入或检查缩进" % " / ".join(bad))
    if missing:
        notes.append("缺少 provider：%s；自动写入不会新增（配置里没有可用的 providers 块），"
                     "请先手动接入" % " / ".join(missing))

    return {
        "ok": True,
        "manual": False,
        "target": str(path),
        "backup": "写入前备份为 %s.bak-ai-proxy-<时间戳>" % path.name,
        "changes": changes,
        "missing": missing,
        "added": added,
        "lines": lines,
        "newline": nl,
        "edits": edits,
        "inserts": inserts,
        "notes": notes,
    }


def _dsh_status():
    if not DSH_PATH.is_file():
        return {"key": "dsh", "label": "DSH", "path": str(DSH_PATH),
                "exists": False, "state": "missing", "action": "none",
                "detail": "未检测到配置"}
    plan = _dsh_plan(DSH_PATH)
    if not plan.get("ok"):
        return {"key": "dsh", "label": "DSH", "path": str(DSH_PATH),
                "exists": True, "state": "warn", "action": "apply",
                "detail": plan.get("message") or "配置读取失败"}
    missing = plan.get("missing") or []
    changes = plan.get("changes") or []
    state = "ok" if not missing and not changes else "warn"
    detail = "3 个本地 provider 已就绪" if state == "ok" else \
        ("%d 处待修正" % len(changes) if not missing else
         "缺少 %s" % " / ".join(missing))
    return {"key": "dsh", "label": "DSH", "path": str(DSH_PATH),
            "exists": True, "state": state, "action": "apply", "detail": detail}


def _apply_dsh(add_missing=False):
    plan = _dsh_plan(DSH_PATH, add_missing=add_missing)
    if not plan.get("ok"):
        return {"ok": False, "message": plan.get("message")}
    missing = plan.get("missing") or []
    if missing:
        return {"ok": False, "manual": False,
                "message": "缺少 provider：%s，自动写入不会新增。请先手动接入。" % " / ".join(missing)}
    changes = plan.get("changes") or []
    if not changes:
        return {"ok": True, "changed": 0, "message": "DSH 已经是最新，无需写入",
                "changes": []}
    lines, edits, inserts, nl = plan["lines"], plan["edits"], plan["inserts"], plan["newline"]
    text = nl.join(_dsh_apply_lines(lines, edits, inserts))
    bak = _backup(DSH_PATH)
    with open(DSH_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return {"ok": True, "changed": len(changes), "changes": changes,
            "backup": str(bak), "message": "已修正 DSH 配置（%d 处）" % len(changes)}


# ---------------------------------------------------------------- ZCode
def _zcode_rules(data):
    conf = (data or {}).get("config") or {}
    return (conf.get("providerConfigRules") or {}).get("providerRules") or []


def _zcode_plan(path):
    if not path.is_file():
        return {"ok": False, "message": "未找到 ZCode 配置：%s（先安装并运行一次 ZCode）" % path}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "message": "ZCode 配置解析失败：%s" % type(exc).__name__}
    rules = {r.get("providerId"): r for r in _zcode_rules(data)}
    changes = []
    for t in ZCODE_TARGETS:
        rule = rules.get(t["id"]) or {}
        cur = (((rule.get("config") or {}).get("api") or {}).get("baseUrl")) or ""
        action = "新增" if not rule else ("更新" if cur != t["base"] else "已就绪")
        changes.append({"provider": t["name"], "id": t["id"],
                        "from": cur or "(缺失)", "to": t["base"], "action": action})
    return {"ok": True, "manual": False, "target": str(path),
            "backup": "写入前备份为 %s.bak-ai-proxy-<时间戳>" % path.name,
            "changes": changes, "missing": [],
            "notes": ["保留其他 provider；四家都按本地占位 token 重写"]}


def _zcode_status():
    if not ZCODE_PATH.is_file():
        return {"key": "zcode", "label": "ZCode", "path": str(ZCODE_PATH),
                "exists": False, "state": "missing", "action": "none",
                "detail": "未检测到配置"}
    plan = _zcode_plan(ZCODE_PATH)
    if not plan.get("ok"):
        return {"key": "zcode", "label": "ZCode", "path": str(ZCODE_PATH),
                "exists": True, "state": "warn", "action": "apply",
                "detail": plan.get("message") or "配置读取失败"}
    changes = plan.get("changes") or []
    ready = sum(1 for c in changes if c["action"] == "已就绪")
    state = "ok" if ready == len(changes) else "warn"
    return {"key": "zcode", "label": "ZCode", "path": str(ZCODE_PATH),
            "exists": True, "state": state, "action": "apply",
            "detail": "%d/%d 个本地 provider 已就绪" % (ready, len(changes))}


def _apply_zcode():
    plan = _zcode_plan(ZCODE_PATH)
    if not plan.get("ok"):
        return {"ok": False, "message": plan.get("message")}
    bak = _backup(ZCODE_PATH)
    results = []
    try:
        a = hub.load_a_switch()
        for t in ZCODE_TARGETS:
            if t["fn"] == "doubao":
                ok, msg = hub.doubao_zcode_register()
            else:
                r = getattr(a, t["fn"])()
                ok = bool(r.get("ok"))
                msg = r.get("error") or ("模型 %d 个" % len(r.get("models") or []))
            results.append({"provider": t["name"], "ok": ok, "message": str(msg)})
    except Exception as exc:
        return {"ok": False, "message": "%s: %s" % (type(exc).__name__, exc),
                "backup": str(bak), "results": results}
    return {"ok": all(x["ok"] for x in results), "changed": len(results),
            "results": results, "backup": str(bak),
            "message": "已重新注册 ZCode 本地 provider" if all(x["ok"] for x in results)
                       else "部分 provider 注册失败，看结果详情"}


# ---------------------------------------------------------------- Claude / Codex（只给片段）
def _claude_snippet():
    data = {
        "env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:18766",
            "ANTHROPIC_AUTH_TOKEN": "autoclaw-local",
            "ANTHROPIC_MODEL": "GLM-5.3",
            "ANTHROPIC_SMALL_FAST_MODEL": "GLM-5.3-Flash",
        }
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def _codex_snippet():
    return "\n".join([
        'model_provider = "autoclaw"',
        'model = "GLM-5.3"',
        "",
        "[model_providers.autoclaw]",
        'name = "AutoClaw"',
        'base_url = "http://127.0.0.1:18766/v1"',
        'env_key = "AUTOCLAW_LOCAL_KEY"',
        'wire_api = "responses"',
    ])


def _claude_status():
    if not CLAUDE_PATH.is_file():
        return {"key": "claude", "label": "Claude Code", "path": str(CLAUDE_PATH),
                "exists": False, "state": "missing", "action": "snippet",
                "detail": "未检测到 settings.json"}
    try:
        env = (json.loads(CLAUDE_PATH.read_text(encoding="utf-8")).get("env") or {})
    except Exception as exc:
        return {"key": "claude", "label": "Claude Code", "path": str(CLAUDE_PATH),
                "exists": True, "state": "warn", "action": "snippet",
                "detail": "配置解析失败：%s" % type(exc).__name__}
    base = str(env.get("ANTHROPIC_BASE_URL") or "")
    ok = _host_of(base) in _LOCAL_HOSTS
    return {"key": "claude", "label": "Claude Code", "path": str(CLAUDE_PATH),
            "exists": True, "state": "ok" if ok else "warn", "action": "snippet",
            "detail": ("已接入本机：%s" % base) if ok else
                      ("当前直连 %s" % _host_of(base) if base else "未设置 ANTHROPIC_BASE_URL")}


def _codex_status():
    if not CODEX_PATH.is_file():
        return {"key": "codex", "label": "Codex", "path": str(CODEX_PATH),
                "exists": False, "state": "missing", "action": "snippet",
                "detail": "未检测到 config.toml"}
    text = CODEX_PATH.read_text(encoding="utf-8", errors="replace")
    bases = re.findall(r"""(?m)^\s*base_url\s*=\s*["']([^"']+)["']""", text)
    local = [b for b in bases if _host_of(b) in _LOCAL_HOSTS]
    if local:
        detail, ok = "已接入本机：%s" % local[0], True
    elif bases:
        detail, ok = "base_url 指向 %s" % (_host_of(bases[0]) or bases[0]), False
    else:
        detail, ok = "未配置 base_url", False
    return {"key": "codex", "label": "Codex", "path": str(CODEX_PATH),
            "exists": True, "state": "ok" if ok else "warn", "action": "snippet",
            "detail": detail}


# ---------------------------------------------------------------- 对外
def status():
    rows = [_dsh_status(), _zcode_status(), _claude_status(), _codex_status()]
    return {"ok": True, "rows": rows,
            "need": sum(1 for r in rows if r["state"] != "ok")}


def preview(key, add_missing=False):
    if key == "dsh":
        p = _dsh_plan(DSH_PATH, add_missing=add_missing)
        for k in ("lines", "edits", "inserts", "newline"):
            p.pop(k, None)
        return p
    if key == "zcode":
        return _zcode_plan(ZCODE_PATH)
    if key == "claude":
        return {"ok": True, "manual": True, "target": str(CLAUDE_PATH),
                "snippet": _claude_snippet(),
                "notes": ["写入 settings.json 的 env；模型名按网关 /v1/models 调整",
                          "会覆盖现有 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN；请自行备份"]}
    if key == "codex":
        return {"ok": True, "manual": True, "target": str(CODEX_PATH),
                "snippet": _codex_snippet(),
                "notes": ["Codex 的 wire_api 只支持 responses；relay 已内置 /v1/responses 翻译层",
                          "模型名按网关 /v1/models 调整；写前请自行备份 config.toml"]}
    return {"ok": False, "message": "未知客户端：%s" % key}


def apply(key, add_missing=False):
    if key == "dsh":
        return _apply_dsh(add_missing=add_missing)
    if key == "zcode":
        return _apply_zcode()
    if key in ("claude", "codex"):
        p = preview(key)
        return {"ok": False, "manual": True,
                "message": "Claude Code / Codex 只提供片段，不自动写入",
                "snippet": p.get("snippet"), "notes": p.get("notes")}
    return {"ok": False, "message": "未知客户端：%s" % key}
