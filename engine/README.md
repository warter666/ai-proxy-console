# engine/ · 内置引擎

从 `apmengzi/autoclaw-switch` 摘出的运行时子集（本机来源：工作分支
`feat/workbuddy-gateway`，含 Qoder 网关接入与 CN 线修复），2026-10-30 内置到本仓库，
面板不再依赖第二个仓库。

| 路径 | 作用 |
|---|---|
| `a_switch.py` | 账号池 / relay 部署 / ZCode 注册 / Qoder 网关封装 |
| `relay/server.mjs` | AutoClaw 本地 relay（部署到 `~/.autoclaw-relay/`） |
| `qoder/` | Qoder CN 网关（账号池在 `~/.qoder-relay/`） |
| `bridge/watch_auth.py` | 客户端 vault → 池文件同步 |
| `tools/import_v2_account.py` | vault 凭证导出到 `accounts/` |
| `doubao/relay.mjs` | 豆包工作 relay（cookie） |

相对上游的本地改动：

- `ACCOUNTS_DIR` 默认改为引擎同级 `accounts/`（可用 `ASWITCH_DIR` 覆盖）。
- `write_identity_set()` 的回滚归档移出客户端活动目录，避免封禁号设备指纹串号。

升级：用上游同路径文件覆盖，保留上面两处改动，再跑
`python ai_hub.py start auto` 复测 `/health`、`/v1/models`、`/v1/chat/completions`、
`/v1/messages`。账号数据（`accounts/`）与 `qoder/accounts/` 都不入库。
