# Grok Register + Live Panel（可运行包）

基于 [AaronL725/grok-register](https://github.com/AaronL725/grok-register)（MIT）的 Grok 批量注册机 + Web 监控面板。

批量注册 Grok 账号（Camoufox 反检测浏览器）→ 邮箱 OTP → 资料页 → Turnstile → SSO → Device/OAuth → 写入 `cpa_auth/` 与 `grok2api_auth/`，附带 Web Live 面板（:8787）做启停、并发、成功率、黑名单、代理池与邮箱域名池管理。

> **声明：** 仅供自动化流程研究、自有环境联调与个人学习。请遵守 xAI / 邮箱 / 代理服务商条款与当地法律，勿用于未授权批量滥用。

---

## 本包说明

这是一个 **脱敏后的可运行包**，与原仓库相比：

- **已剔除全部敏感/运行时数据**：`accounts/`、`cpa_auth/`、`grok2api_auth/` 真实账号、`log/` 运行日志、`xai_capture.jsonl` 抓包、`.grokcli_imported.json`、`config.json`（真实配置）等一律不包含，目录以 `.gitkeep` 占位。
- **配置只保留模板**：根目录只有 `config.example.json`，首次运行需复制为 `config.json` 并填入你自己的凭据。
- **代码为当前可用版本**：含浏览器指纹锁（强制 Windows 1600×900 / 8 核，规避 Linux 沙箱指纹不一致）、IPv6 loopback 出口适配、网页加载重试、批处理监督与孤儿浏览器清理脚本等近期修复。
- 未包含 `plugin_egress.so`（无代码引用的历史产物）与 `.git` 目录。

详细功能说明见 `docs/README.full.md`。

---

## 环境要求

- Python 3.10+
- 能访问注册页、临时邮箱 API、`auth.x.ai` 的网络
- Linux 容器需保留 procfs（默认 `/proc` 挂载），面板依赖它读取并安全停止任务进程

平台支持：

| 平台 | 支持状态 |
|------|----------|
| Linux | 正式支持；无显示时自动调用 `xvfb-run` |
| macOS | 正式支持；直接使用本机显示会话 |
| Windows | 实验性；批处理链路需自行验证 |

## 快速开始

```bash
# 1. 建虚拟环境并装依赖
python3 -m venv .venv
source .venv/bin/activate        # Linux / macOS
# Windows PowerShell: .venv\Scripts\Activate.ps1

pip install -r requirements.txt
python -m camoufox fetch         # 必须：下载浏览器引擎（约数百 MB）

# 2. 生成配置文件并编辑
cp config.example.json config.json
# 编辑 config.json：邮箱 provider、proxy、cpa_auth_dir、outlook_pool_file 等
```

> `pip install` 只装 Python 依赖；**不执行 `camoufox fetch` 无法启动浏览器**。

### 核心配置字段（`config.json`）

| 字段 | 说明 |
|------|------|
| `email_provider` | `cloudflare` / `duckmail` / `yyds` / `mailnest` / `cloudmail` / `moemail` / `outlook` |
| `defaultDomains` | 临时邮域名（如二级 CF 域） |
| `outlook_pool_file` | Outlook 账号池文件路径（`user----pw----rt----cid` 格式）；使用其它 provider 可忽略 |
| `proxy` | 默认 HTTP 代理，如 `http://127.0.0.1:17890` |
| `register_workers` | 并发浏览器数（建议先 2～3） |
| `register_count` | 单次目标数量 |
| `cpa_auto_add` | 是否 SSO→OAuth 并写入 auth |
| `cpa_auth_dir` | 本地 CPA 目录（`xai-*.json`） |
| `grok2api_auth_dir` | Grok2API 风格 auth 目录 |
| `cpa_remote_url` / `cpa_management_key` | 远程 CPA Management API（可选，留空不启用） |
| `moemail_api_base` / `moemail_api_key` | MoeMail OpenAPI 配置（如使用） |

面板相关配置通过环境变量注入（详见下文）。

## 启动方式

### A. Web 面板（推荐）

```bash
export MONITOR_TOKEN='你的长随机串'   # 必设，否则点启动会 401
export MONITOR_HOST=127.0.0.1          # 仅本机；局域网改具体网卡 IP
export MONITOR_PORT=8787
export CPA_AUTH_DIR=./cpa_auth

python webui/monitor.py
# 浏览器打开 http://127.0.0.1:8787/
```

1. 页面顶部**面板 Token** 填入与 `MONITOR_TOKEN` 相同的字符串
2. 设模式 / workers / batch 数量 / 再跑 N → **启动**
3. 需要多出口时打开顶部**代理池**，导入代理并等待检测
4. 打开顶部**邮箱服务**，选 provider 并保存、执行连接测试

### B. 命令行单批

```bash
# Linux 无头
xvfb-run -a python -u run_batch_headless.py 20 3
#                        数量↑        并发↑
# Linux 有显示 / macOS / Windows
python -u run_batch_headless.py 20 3
```

单批由独立监督进程运行，驱动崩溃或连续超时无输出时自动重建并按原子进度文件继续；已写入的账号/SSO/CPA 不回滚。

### C. 编排器（多轮直到目标 CPA）

```bash
python -u run_until_100.py
```

### D. GUI

```bash
python grok_register_ttk.py
```

## 面板关键约定

- 写接口（start/stop/control/blacklist/代理池/邮箱池）一律要求 `Authorization: Bearer <MONITOR_TOKEN>`；未设 token 时返回 `unauthorized`，属预期行为。
- 默认 `PANEL_INCLUDE_TAIL=0`，状态接口不附带原始日志尾（防泄密）。
- 代理池、邮箱密钥通过接口/页面只返回脱敏视图，不回显账号密码。
- 运行时生成的数据文件权限 `0600`、父目录 `0700`（见 `scripts/harden_runtime_permissions.py`）。

## 目录结构

```text
.
├── grok_register_ttk.py       # GUI + CLI 主程序
├── register_flow.py           # 注册页流程 / Turnstile
├── browser_session.py         # 会话、出口探测、ASN 黑名单
├── sso_to_auth_json.py        # SSO → OAuth / 写 CPA
├── camoufox_adapter.py · connectivity.py
├── batch_supervisor.py        # 批处理监督 / 自动重建
├── run_batch_headless.py      # 无头批量
├── run_until_100.py           # 编排器
├── push_to_grokcli_watcher.py # （可选）auth 自动推送 grokcli
├── email_providers/           # 多邮箱后端适配
├── webui/                     # Live 面板（monitor.py + stores）
├── scripts/                   # xvfb、权限加固、孤儿浏览器清理等
├── tests/                     # 单元测试
├── accounts/  cpa_auth/  grok2api_auth/  log/   # 运行时数据目录（空）
├── config.example.json
├── requirements.txt · requirements.lock.txt
├── deploy/                    # systemd / env 示例
├── docs/README.full.md        # 原详细文档
├── LICENSE · NOTICE
└── README.md
```

## 常见问题

- **点启动报 `unauthorized: set MONITOR_TOKEN...`？** 服务端已启用写接口鉴权。启动 monitor 时 `export MONITOR_TOKEN=...`，浏览器面板 Token 填同一串。
- **全是「无法解析出口 IP」？** 打开顶部代理池查看异常/冷却原因，重新检测后只启用健康条目。
- **`Address already in use` / 面板打不开？** 8787 被其它进程占用，换 `MONITOR_PORT` 或释放端口；绑定失败不会自动改绑 `0.0.0.0`。
- **启动任务报 `No such file or directory: '/proc'`？** Linux 容器没挂载 procfs，恢复默认 `/proc` 挂载后重启面板。
- **Windows？** 面板已适配 `.venv\Scripts\python.exe`、进程发现和停止；Camoufox 批处理链路仍标为实验性，生产优先 Linux/macOS。

## 自检

```bash
PYTHON_BIN=.venv/bin/python scripts/run_tests.sh   # 运行发布检查
.venv/bin/python -m pip check
.venv/bin/python scripts/harden_runtime_permissions.py .
```

## License

[MIT](LICENSE) — 见 [NOTICE](NOTICE) 对 AaronL725/grok-register 的归属说明。面板内嵌 Geist 字体使用 [SIL OFL 1.1](LICENSES/OFL-1.1-Geist.txt)。
