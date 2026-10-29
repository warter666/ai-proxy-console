# AI Proxy Console（AI 反代控制台）

本机 AI agent 的**运行底座控制台**：把四路本地反代（AutoClaw / WorkBuddy / Qoder / 豆包）
的启停、端口、占位 token、健康度和客户端接入收在一个面板里。

| 层面 | 归谁管 | 本仓库角色 |
|---|---|---|
| **agent 用什么模型** | 本仓库（四路本地反代） | 网关启停 / 端口 / 占位 token / 健康度 / 配置接入 |
| **agent 会什么** | `AgentHub`（可选，另一个仓库） | 只读检测 + 调它的脚本，不重复实现 |

> **仓库边界**（2026-10-29 起独立成仓）：
> - **本仓库** `ai-proxy-console`：面板、控制台、WorkBuddy 适配器、签到与接入脚本。
> - **三路引擎**（AutoClaw / Qoder / 豆包）在独立的 `autoclaw-switch` 仓库，
>   用环境变量 `A_SWITCH_REPO` 指向本地克隆；未设置时按相邻目录 `..\autoclaw-switch` 探测。
> - **AgentHub** 是可选的共享底座（技能 / 记忆 / 项目规则），另有仓库；
>   不装它也能单独使用本面板。
> - 旧路径 `D:\Code\Projects\AgentHub\AI_Proxy` 只是指向本仓库的兼容 junction，
>   不是另一份代码；计划任务与快捷方式已改指本仓库。

## 目录结构

```text
ai-proxy-console/
├─ ai_hub.py / gateway_ui.py / gateway_ui.html   # 控制台 + 面板（入口，留根目录）
├─ gw_specs.py / agent_setup.py / onboard.py / env_check.py / daily_checkin.py
├─ workbuddy-proxy.js / clients.example.json / install_checkin_task.ps1
├─ *.bat / *.exe                                # 桌面入口（EXE 回落路径写死根目录）
├─ tools/
│  ├─ launcher/    # EXE 启动器源码（Launcher.cs.template + build-launchers.ps1）
│  ├─ doubao/      # 豆包辅助：sync / session / switch / 只读探针（自成一簇）
│  ├─ agent-hub/   # skill_import.py（技能包导入）
│  └─ compat/      # all_in_one.py（旧 bat 兼容薄壳，转发 ai_hub.py）
├─ assets/         # 图标
└─ _legacy/        # 已归档旧入口（gitignore）
```

> 入口脚本留在根目录是刻意设计：桌面 EXE 启动器和 `gateway_ui.py` /
> `ai_hub.py` 都按“自身所在目录”定位同伴文件。其余独立脚本按用途收进 `tools/`。

## 从零搭建（新机器）

本仓库只是控制台，四路网关还依赖本机已登录的客户端和引擎仓库：

1. **系统**：Windows 10/11，Python 3.10+，Node.js 22+（豆包同步用到全局 `WebSocket`），
   PowerShell 5.1。安装依赖：`pip install -r requirements.txt`。
2. **引擎**：本地克隆 `autoclaw-switch`（独立仓库），路径写进环境变量
   `A_SWITCH_REPO`；未设置时按相邻目录 `..\autoclaw-switch` 探测。
3. **登客户端**：AutoClaw / WorkBuddy / Qoder / 豆包，至少登录你要用的那几家。
   真实凭据只留在客户端和代理进程里，本仓库不含任何 key。
4. **起面板**：`python gateway_ui.py`（或双击 `AI反代面板.bat`）。打开后点右上角
   「一键接入」：补占位 token → 启动所需网关 → 写入 DSH / ZCode 配置 → 复检。
   Claude Code / Codex 在结果里给可复制片段。想无控制台闪窗可编译 EXE 启动器（可选，见下）。
5. **自检**：面板「环境自检」逐项列出缺什么；某一路缺依赖不影响其他网关。

本机路径都可以用环境变量覆盖，不设时按默认探测：

| 变量 | 用途 | 默认 |
|---|---|---|
| `A_SWITCH_REPO` | 引擎仓库（AutoClaw / Qoder / 豆包） | `..\autoclaw-switch` |
| `AGENTHUB_DIR` | AgentHub 共享底座（可选） | `..\AgentHub` |
| `AI_PROXY_PYTHON` | 启动器 / BAT 使用的 python.exe | 仓库 `.venv` → `venv` → PATH |
| `DOUBAO_CONSUMER_EXE` | 消费版豆包客户端 | `clients.local.json` 的 `doubao_consumer` → 常见安装位置 |
| `WB_PROXY_DIR` | WorkBuddy 适配器目录 | 本仓库 |
| `AUTOCLAW_SWITCH` | `a_switch.py` 完整路径 | `A_SWITCH_REPO\a_switch.py` |

> WorkBuddy 的令牌信封需要本机密钥（`wb-key-payload.json`）。生成密钥的
> `scan_mem.py`（读取本机进程内存）**不随仓库分发**，请自行保留本地副本；
> 没有它时 WorkBuddy 网关不会启动，其余三家不受影响。

## 快速开始

    python gateway_ui.py        # 或双击 AI反代面板.bat

面板里有六块：

- **一键接入** — 检测环境、拉起所需网关、写 DSH / ZCode 配置并复检（详见下节）
- **网关卡片** — 四路反代的启停、端口、PID、延迟、占位 token
- **今日消耗** — 请求量与输出规模（收起态只显示汇总）
- **AgentHub** — 共享底座状态：技能库规模、五个来源的联接是否完好、记忆入口是否接入
- **客户端** — 一键打开本机装好的 agent

### 一键接入

面板右上角「一键接入」把新机器的配置收成一次点击：

1. 检查客户端安装、网关状态、占位 token 环境变量；
2. 缺的 `*_LOCAL_KEY` 写入 HKCU 用户环境变量（只写占位值，真实凭据不经过面板）；
3. 启动所选客户端需要的网关；
4. DSH / ZCode 的 provider 直接写入（写前备份 `*.bak-ai-proxy-<时间戳>`），
   DSH 缺 provider 时按网关 `/v1/models` 自动补齐；
5. 重新跑环境自检，逐项列出结果。

Claude Code / Codex 不自动改配置，只在结果里给可复制的片段。任务在后台线程执行，
关掉对话框不会中断（退出面板会）。浏览器打开 `gateway_ui.html?demo=1` 可无后端预览。

## 网关

四路反代，全部只监听 `127.0.0.1`，真实凭据只在代理进程里：

| 网关 | 端口 | 占位 token | 协议 |
|---|---|---|---|
| AutoClaw relay | 18766 | `autoclaw-local` | Anthropic + OpenAI |
| WorkBuddy proxy | 8787 | `workbuddy-local` | OpenAI |
| Qoder proxy | 8790 | `qoder-local` | OpenAI |
| DoubaoWork relay | 18770 | `doubao-local-key` | OpenAI |

把**端口 + 占位 token** 填进任意 OpenAI 兼容客户端即可使用。

## 与 AgentHub 的关系

`AgentHub` 是共享底座（技能 / 记忆 / 项目规则），默认按仓库同级目录探测，本仓库是网关控制台。**不是 fork，是外部依赖**：

- 只通过 `agent-hub.ps1` 调用其能力，代码不共享、无版本关联
- 变更操作（link-skills / point-memory / init-project）**全部交给 AgentHub 自己的脚本**，
  面板不重复实现——否则两套逻辑必然演化脱节
- 路径可用环境变量覆盖：`AGENTHUB_DIR`

## 日常命令

在仓库根目录执行：

    python ai_hub.py menu        # 交互式开关：编号开/关单个网关（省内存）
    python ai_hub.py status      # 一屏看清所有代理（端口/pid/健康）
    python ai_hub.py start       # 拉起全部启用的代理（幂等，可反复点）
    python ai_hub.py start wb    # 只拉 WorkBuddy
    python ai_hub.py start auto  # 只拉 AutoClaw（含凭证刷新+ZCode注册+冒烟）
    python ai_hub.py start qoder # 只拉 Qoder（含账号导入+ZCode注册）
    python ai_hub.py stop all    # 全部停止
    python ai_hub.py smoke all   # 推理冒烟
    python ai_hub.py list        # 列注册表

桌面入口：`AI反代面板.exe` / `AI反代面板.bat` 打开可视面板（带单实例保护，重复打开只会聚焦已有窗口）。需要命令行菜单时运行 `python ai_hub.py menu`。

## EXE 启动器（可选）

不想每次开控制台窗口时，可以把桌面入口编译成原生 EXE（约 35 KB，GUI 入口无控制台闪窗，
带图标和版本信息）。它们只是薄启动器，仍然调用本目录的 Python / PowerShell 脚本；
改脚本不需要重编 EXE。

| EXE | 等价 BAT | 行为 |
|---|---|---|
| `AI反代面板.exe` | `AI反代面板.bat` | 无控制台，直接打开可视面板 |
| `安装每日签到任务.exe` | `安装每日签到任务.bat` | 控制台注册计划任务（沿用当前时间，默认 09:00） |
| `<AGENTHUB_DIR>\scripts\agent-hub.exe` | `agent-hub.bat` | 控制台，参数透传给 `agent-hub.ps1`（AgentHub 仓库提供） |

新克隆默认只有 `.bat` 和 `python gateway_ui.py` 两种入口，EXE 是本机编译产物（已 gitignore）。

`AI反代开关` / `每日签到` / `查看签到状态` 三组入口已于 2026-10-29 归档到
`_legacy\2026-10-29-launchers\`：面板已覆盖网关开关和签到，不再需要单独双击。
`daily_checkin.py` 仍保留，面板的签到按钮和定时签到计划任务都在调用它。

EXE 优先用自身目录定位脚本；从桌面或其他目录双击时，回落到编译时记录的绝对路径
（编译脚本按仓库当前位置写入）。重新编译（在仓库根目录执行）：

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\launcher\build-launchers.ps1

AgentHub 不在默认位置时加 `-SkipAgentHub`，或先设 `AGENTHUB_DIR`。
源码在 `tools\launcher\Launcher.cs.template`，用系统自带 .NET Framework 4.x 的 `csc.exe`
编译，不需要 PyInstaller。EXE 是生成产物，已加入 `.gitignore`。

## 每日签到

WorkBuddy + 豆包（消费版）每日自动签到，已签到会自动跳过，重复运行不会重复领。

面板里点「签到」即可手动签。顶部「全部签到」旁边的「定时 HH:mm」胶囊直接显示
当前计划任务时间，点开可改每日时间、保存或移除；改时间只会更新同一个任务，不会新建第二份。

命令行（在仓库根目录执行）：

    python daily_checkin.py              # 两个平台都签
    python daily_checkin.py --status     # 只看状态
    python daily_checkin.py --workbuddy  # 只签 WorkBuddy
    python daily_checkin.py --doubao     # 只签豆包
    python daily_checkin.py --json       # JSON 摘要

计划任务 `AI每日签到` 默认每天 09:00 运行，电脑当时关机或休眠会在恢复后尽快补跑。
「安装签到任务」注册的就是这个 Windows 计划任务；已有任务时双击安装器会沿用当前
时间，不会重置回 09:00。重装 / 改时间：

    双击仓库根目录的 安装每日签到任务.exe（或 .bat）
    或在面板「全部签到」旁点「定时」，改时间后保存

脚本也可以直接带参数（`-At` 格式 HH:mm，`-Status` 只读，`-Remove` 卸载）：

    powershell -NoProfile -ExecutionPolicy Bypass -File install_checkin_task.ps1 -At 08:30
    powershell -NoProfile -ExecutionPolicy Bypass -File install_checkin_task.ps1 -Status
    powershell -NoProfile -ExecutionPolicy Bypass -File install_checkin_task.ps1 -Remove

卸载等价于：

    powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName 'AI每日签到' -Confirm:$false"

实现说明：

- WorkBuddy 复用本机登录态（`workbuddy-desktop.info` + `wb-key-payload.json`），先查
  `checkin-activity-status`，仅在 `today_checked_in=false` 时调用 `daily-checkin`。
- 豆包签到对应消费版客户端 `D:\APP\tool\Doubao` 的活动 `pc_incentive_task_mvp_v3`，
  接口本身幂等。cookie 存在 `~/.doubao-relay/cookies-consumer.json`，设备参数存在
  `doubao-consumer-config.json`。cookie 失效时脚本会尝试用 CDP 9223 同步；若豆包正
  在运行但没开调试端口，会重启一次客户端（加 `--no-restart` 可禁止）。
- 日志在 `logs/checkin-YYYY-MM-DD.log`，只记录状态和额度，不写令牌/cookie。
- 豆包工作（DoubaoWork）当前账号的 `ug_campaign_info` 为空，没有签到活动，因此不参与；
  这里签的是消费版豆包。


## 现役四家

| key | 名称 | 端口 | 协议 | 占位 token | 客户端 env | baseUrl |
|-----|------|------|------|-----------|-----------|---------|
| auto | AutoClaw relay | 18766 | Anthropic + OpenAI | `autoclaw-local` | `AUTOCLAW_LOCAL_KEY` | http://127.0.0.1:18766 |
| wb   | WorkBuddy proxy | 8787 | OpenAI | `workbuddy-local` | `WB_LOCAL_KEY` | http://127.0.0.1:8787/v1 |
| qoder | Qoder proxy | 8790 | OpenAI | `qoder-local` | `QODER_LOCAL_KEY` | http://127.0.0.1:8790/v1 |
| doubao | DoubaoWork relay | 18770 | OpenAI (cookie) | `doubao-local-key` | 无（ZCode 内置） | http://127.0.0.1:18770/v1 |

占位 token 不是真实凭据，真实凭证只留在代理进程/账号池里。Qoder 网关当前
`api_key_required: false`（本机回环，不强制校验），但客户端仍应填 `qoder-local`，
因为 DSH/ZCode 的 provider 配置需要一个非空 key。

Qoder 网页面板：<http://127.0.0.1:8790/>（默认面板密码 `admin`，与 API key 独立）。

## DSH 接入

DSH 的 `cordis.patch.yml` 用 `apiKeyEnv` 读取上面三个环境变量。环境变量已写进用户环境，
但 **DSH 必须完整退出（所有 DSH Desktop 进程）再启动** 才能读到；只关窗口不够。
改完 provider 或 token 后同理。Qoder 的模型列表以网关实时 `/v1/models` 为准，DSH 里的
静态列表需手动同步，未知模型会落到 `auto`。

DSH 走 `qoder/deepseek-flash` 时若出现「回答一半输出 `[assistant 请求调用工具]` + JSON」或
`</｜｜DSML｜｜ …>` 残片，是网关侧旧版工具历史文本化 + 回读失效导致，**v1.2.18 已根治**
（DeepSeek 默认结构化工具历史 + 回读容错 + 暂存上限放宽）。重启 Qoder 网关（8790）即可，
不必换 provider 或压缩工具调用体积；该行为源自网关侧旧版工具历史文本化 + 回读失效，已在 v1.2.18 修复。

## 可视面板

`python gateway_ui.py`（或双击 `AI反代面板.exe` / `.bat`）用 pywebview 打开一个桌面窗口，界面是“配线台”式布局：

- 四路横向通道：大写端口号、协议、运行状态、PID、健康耗时
- 占位 token（一键复制）与启动/停止键；Qoder 运行时额外有“面板”按钮
- 右上角「一键接入」向导：逐步显示补 token / 启动网关 / 写配置 / 复检的进度与结果
- 「客户端」区一键打开 Qoder / DSH / ZCode / WorkBuddy / AutoClaw / 豆包工作
- 底部深色运行日志（启动/停止过程、错误原因）

客户端路径不在仓库里：`gw_specs.py` 只保留可移植候选（如 Qoder 的 `%LOCALAPPDATA%` 路径），
本机安装位置写在 `clients.local.json`（已 gitignore，格式见 `clients.example.json`）；
也可以在面板里点「添加客户端」用原生选择器挑 exe，写入 `clients.json`。
签到用的消费版豆包走同一个文件的 `doubao_consumer`（或环境变量 `DOUBAO_CONSUMER_EXE`）。
按顺序探测第一个存在的候选，找不到的按钮置灰，不会误报。

面板只读缓存状态，点按钮后后台线程执行，不会因为某个网关健康检查超时卡住。
关闭面板不会停止网关；重复打开会尝试把已有窗口拉到前台。旧版本留下的同名窗口
会被新面板发现并关闭替换，避免“点开还是旧界面”。

注意：BAT 回退入口必须保存为 **UTF-8 无 BOM + CRLF**。LF-only 会让 cmd
把一行拆成多条半截命令（典型报错：`'ual' 不是内部或外部命令`）。EXE 是编译产物，
不受此限制；日常启动也可以直接用 `python gateway_ui.py`。

## WSL 里用（pi agent）

Windows 侧反代全部只听 127.0.0.1，WSL2 的 localhost 转发天然可达，无需任何配置。
WSL 的 `~/.pi/agent/models.json` 只注册三家反代 provider（workbuddy / autoclaw / doubao），
默认 `workbuddy` + `glm-5.3-flash`（2026-10-26 起已实测可用）：

    wsl -e bash -ic "pi --provider doubao --model doubao-think"
    # bash -ic 里的 i 必须带：nvm 的 node 22 只在交互 shell 加载，否则回落 node 12 报语法错
    # bash -ic 会执行 .bashrc 的 cd $HOME；要指定项目先显式 cd 再跑 pi

注意：不要把直连上游的 provider（带真实 key）写回 `models.json`；pi 只走本地反代。
直连 key 一律留在代理侧。豆包依赖 cookie（`~/.doubao-relay/cookies-cdp.json`，CDP 抓取，
会静默过期），过期就重跑 `node doubao/cdp.js cookies` 刷新。

## 加第四家代理（3 步）

打开 ai_hub.py，找到 PROVIDERS 注册表，照抄 qoder 条目改 4 处：

    "xxx": {
        "name": "XXX proxy", "port": 9000, "enabled": True,
        "url": "http://127.0.0.1:9000", "proto": "OpenAI",
        "start": xxx_start, "status": xxx_status,
        "stop": lambda _p: stop_by_port(9000, "XXX"),
        "smoke": None,          # 可选
    },

再写两个小函数：`xxx_start`（幂等拉起，返回 (ok, 说明)）和 `xxx_status`（返回 (健康bool, 备注)）。
qoder 条目就是活示例：`enabled=False` 时不进 `start all`，但 status/stop 仍可用。

## 架构约定

- 每家代理独立 try/except：一家凭证坏了不阻断其他，结尾汇总表说话。
- 幂等：在跑的代理不重复拉；stop 按端口找 pid。
- AutoClaw 的”先拉客户端再刷凭证”链路原样保留（vault 24h 票，客户端不跑就导出陈票）。
- WorkBuddy 的信封密钥 keyId 不匹配时自动重扫客户端内存。
- 原理上不碰各代理本体（workbuddy-proxy.js / server.mjs），这里只做编排。

## 公开范围与安全边界

- 仓库里只有占位 token（`autoclaw-local` / `workbuddy-local` / `qoder-local` / `doubao-local-key`），
  真实 key、cookie、账号密码、设备指纹一律不入库，由 `.gitignore` 拦截。
- 不发布本机凭据工具：`scan_mem.py`、`wb_decrypt.py`、`read_proc_env.py` 及
  `wb-key-payload.json`、`custom_gateways.json`、`doubao-consumer-config.json` 均被忽略，
  历史记录里也没有这些文件。
- 四个网关固定只监听 `127.0.0.1`，不对外暴露端口。
- 公开版本的历史做过脱敏：账号名、UID、本机用户名等标识已替换为 `<account-*>` / `<uid-*>` /
  `<user>` 占位符。

## 上下文窗口与压缩

不同模型的上下文窗口差异很大：DeepSeek-V4.1-Flash 约 32k tokens，超过会报 `context overflow`。
**本仓库只负责网关启停与占位 token，不负责上下文压缩**。每个 agent 用自己的插件/配置来处理。

### 用户侧方案

| Agent | 压缩方式 | 说明 |
|---|---|---|
| **DSH** | Cordis 插件 | 在 `cordis.patch.yml` 挂载自定义插件，hook `onSendMessage`，达到阈值时触发摘要 |
| **ZCode** | Harness MCP | 通过 MCP 工具或 provider 配置控制 |
| **Claude Code** | 手动策略 | 编辑提示词，减少历史轮数 |
| **Codex** | 手动策略 | 同上，或调整编辑器上下文范围 |

示例：DSH 的 rolling summary 插件（挂载在 `~/.dsh/profiles/web/config`）：

```yaml
- id: llm-pi-ai
  plugins:
    - name: context-summarizer
      path: “./node_modules/context-summarizer”
      enabled: true
      params:
        thresholdTokens: 25000   # 超过此值触发摘要
        keepRecent: 6            # 保留最近 6 轮对话
        summaryMaxLength: 2000   # 压缩后的历史摘要字数上限
```

插件逻辑：估算当前消息总字符数 → 近似 tokens（~4 字符 = 1 token）→ 超限后调用摘要接口 → 
返回 `[system + 历史摘要 + 最近 N 轮]` 的新消息序列。

### 本仓库兜底方案

本仓库不内置摘要服务。如果需要一个快速验证环境，可以自行实现一个轻量 HTTP 中间件
提供 `/summarize` 接口（示例契约：`POST http://127.0.0.1:18789/summarize`，接受
`{user_prompt: “完整对话文本”}`，返回 `{“summary”: “中文摘要”}`，内部转调 Qoder 网关的
`deepseek-v4.1-flash`）。

**注意**：这是思路示例，生产环境建议直接用对应 agent 的原生插件能力。本仓库不做通用
“harness 兜底”机制，因为不同 agent 的插件系统互不相通，无法用单一层统一处理。
