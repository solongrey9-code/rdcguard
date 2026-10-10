# AgentGuard

**Windows 开机自启 + 自愈守护框架，面向一切需要保护的本地机制：agent、MCP 服务器、本地桥接、代理、模型服务、后台守护进程……**

[English](README.en.md) | 简体中文

AgentGuard 把一个朴素的诉求做成通用工具：**「这东西必须一直在线，掉了我自己不知道，谁来管？」**

它由两条真实战场里打磨出来的机制泛化而来：

- **RDC 看门狗**（PowerShell）：让 Desktop Commander 远程设备在开机竞态（代理未就绪 → `fetch failed` 自杀）后仍能自动恢复；
- **OpenChamber 原生宿主**（Python）：让隔离的 OpenCode V2 + OpenChamber 服务被逐分钟检查、fail-closed 自愈。

这两套逻辑各自可靠，但都只为单一目标写死。AgentGuard 把它们抽象为**声明式配置 + 通用探针 + 统一自愈引擎**：任何「需要被保护的东西」写进 JSON 就能获得同样的开机自启与看门狗待遇。

## 特性

- **零依赖**：纯 Python 标准库（3.8+），Windows 优先，Linux/macOS 亦可运行自愈逻辑。
- **声明式**：一份 `agentguard.json` 描述所有被保护对象；`%USERPROFILE%` 等环境变量会在加载时展开，公开配置不泄露用户名。
- **通用探针**：TCP 端口、HTTP（含状态码与 JSON 字段断言）、日志状态机（最后一根 marker 决定状态）、已建立连接（进程存活实证）、命令退出码、进程身份（镜像名 + 命令行子串）。
- **两种守护模式**：
  - `managed`：AgentGuard 全权管理生命周期（检查 → 只杀「能证明是自己启动的」残留进程 → 依赖门控 → 启动 → 等健康 → 重试退避）；
  - `delegated`：目标自带幂等 `ensure`（如 OpenChamber `host.py ensure`、WLC `START.cmd`）时，交给它自愈，AgentGuard 只做健康判定与触发。
- **依赖门控**：RDC 依赖 10808 代理？把代理写成 `requires`（甚至可 `autostart`），代理就绪前绝不启动，从根上消除开机竞态。
- **Fail-closed 安全**：命令行读不到就不认领、不杀；端口被未知进程占用就拒绝替换；操作员 `stop` 后 disarm，直到显式 `start`。
- **单实例与并发安全**：全局 ensure 锁 + 每 guard 锁，看门狗永不堆积。
- **日志轮转**：被保护服务自己的大日志（如动辄几十 MB 的 `rdc_log.txt`）也会按 `rotate` 规则轮转。
- **开机自启安装器**：一条命令注册「登录触发 + 每 N 分钟重复」的计划任务（pythonw 隐藏运行），可选写 Startup 文件夹 VBS 垫片。
- **人类与机器双输出**：`status` 给人看表格，`--json` 给监控/脚本；`ensure` 恒输出 JSON，退出码 0/1 可直接接监控。

## 快速开始

```powershell
# 1) 生成启动配置
python guard.py init --out agentguard.json

# 2) 校验配置
python guard.py validate

# 3) 看当前健康（只读）
python guard.py status

# 4) 手动跑一轮自愈（看门狗入口，可反复执行）
python guard.py ensure

# 5) 安装看门狗计划任务（登录自启 + 每 3 分钟）
python guard.py watchdog install
```

`examples/` 里是与本机真实机制一一对应的成品配置：

| 文件 | 模式 | 保护对象 |
|---|---|---|
| `examples/rdc.json` | managed | Desktop Commander 远程设备（含 10808 代理依赖 + 日志状态机 + 已建立连接探针） |
| `examples/openchamber.json` | delegated | OpenCode V2 + OpenChamber 原生宿主（委托 `host.py ensure`） |
| `examples/windows-local-commander.json` | delegated | ChatGPT 原生 MCP 桥（8765，healthz JSON 断言） |
| `examples/mcp-stdio.json` | managed | 任意 stdio MCP 服务器模板（进程身份探针） |
| `examples/ollama.json` | managed | 本地模型服务 |
| `examples/proxy.json` | managed | 代理客户端（作为一等公民保护） |
| `examples/observe-only.json` | observe | 只观察不触碰，切换 managed 前的安全第一步 |

## 工作方式

```
watchdog 触发 (登录 / 每 N 分钟)
        │
        ▼
  全局锁 ──▶ 逐个 guard ──▶ 每 guard 锁
        │                        │
        │                        ▼
        │              ┌─ 健康？ ──▶ 是 ──▶ 记录 last_healthy，完
        │              │
        │              ├─ 新进程且有宽限期？ ──▶ 标记 starting，不杀
        │              │
        │              ├─ 轮转日志
        │              │
        │              ├─ managed：杀「身份匹配」的残留 → 检查依赖（必要时 autostart + 等待）
        │              │  delegated：直接调到 delegate
        │              │
        │              ├─ 启动 / 触发 ensure
        │              │
        │              ▼
        │        等健康（廉价探针 2s 一轮 + 全量探针周期复查）
        │              │
        │              ├─ 成功 ──▶ recovered（记录本次启动的日志偏移量）
        │              └─ 失败 ──▶ 清理 + 退避重试 ×N，仍失败则 failed
```

**状态判定细节**

- 日志探针维护一个状态机：扫描 `window_bytes` 尾部窗口（或自上次启动的字节偏移），**最后一条命中的 marker 决定状态**；`healthy` 列表判定健康，`starting` 状态的进程在宽限期内不会被误杀。
- `established` 探针复刻 RDC 的存活实证：设备订阅后长期持有 TCP 连接，比在可能被海量工具输出淹没的日志里找 `online` 更可靠。
- `since: "last-start"` 会记录每次启动前的文件偏移，重启后的残留旧状态（例如上一轮的 `Device ready`）不会冒充新状态。

## CLI 一览

| 命令 | 说明 |
|---|---|
| `guard.py status [names] [--json]` | 只读健康报告；有异常时退出码 1 |
| `guard.py ensure [names]` | 检查并按需自愈（计划任务的入口），恒 JSON |
| `guard.py start / stop / restart <names>` | 人工干预；`stop` 写 disarm 标记，`start` 清除并拉起 |
| `guard.py list` | 列出 guard 与 enable/armed 状态 |
| `guard.py validate` | 校验配置，错误逐条列出 |
| `guard.py logs [name] [-n N]` | 查看 AgentGuard 日志（`state_dir/agentguard.log`） |
| `guard.py watchdog install\|remove\|status` | 计划任务（登录 + 每 N 分钟） |
| `guard.py startup install\|remove\|status` | Startup 文件夹隐藏 VBS 垫片 |
| `guard.py init --out PATH [--force]` | 生成启动模板 |

## 计划任务与开机自启

`watchdog install` 生成的计划任务与 RDC 看门狗同款结构：

- 触发器：**登录时** + **每 N 分钟重复**（`MultipleInstancesPolicy=IgnoreNew`，慢自愈不会堆积）；
- 动作：`pythonw.exe "<项目路径>\guard.py" ensure`，无窗口；
- 运行身份：当前交互用户（无需管理员）；
- 可选 `watchdog.startup_shim: true`：额外在 Startup 文件夹写一个 `AgentGuard.vbs`，作为计划任务的双保险。

## 安全设计（fail-closed）

1. **只杀证明得了的进程**：镜像名 + 命令行子串全部匹配才认领；命令行不可读（如提权进程）时宁可不认领。
2. **不抢未知端口**：目标端口被非我方进程占用时，拒绝停止/替换，转人工。
3. **disarm 优先**：`guard.py stop` 先写 disarm 标记再动手；看门狗见到标记立即放过，直到显式 `start`。
4. **不接管已有自愈系统**：目标自带锁/pid 校验/操作员标记（如 OpenChamber host）时用 `delegated`，AgentGuard 不越权。
5. **秘密不落盘**：配置与日志中不应出现 token；AgentGuard 从不回显被保护服务的输出（journal 只记状态与命令名）。

## FAQ

- **为什么我的服务在被守护后仍然被杀？** 检查 `identity` 是否精确；AgentGuard 只对「身份匹配且不健康」的进程动手。观察期建议先用 `observe_only: true`。
- **pythonw 下脚本会不会因无 stdout 崩溃？** 不会：AgentGuard 启动时会把无控制台的 stdout/stderr 接到 `os.devnull`，并在控制台代码集（GBK 等）无法编码时降级为 `?`，看门狗绝不因打印而殉职。
- **Node 服务明明设了 `HTTP_PROXY` 却直连？** Node 24 的 `fetch()` 默认无视代理环境变量，启动脚本里需要 `NODE_USE_ENV_PROXY=1`——见 `docs/DESIGN.md` 的实战教训。
- **日志巨大导致状态误判？** `window_bytes` 与 `since: last-start` 就是为此设计；也可以加 `established` 探针做实证。
- **需要管理员权限吗？** 计划任务以当前用户运行即可；被保护服务需要提权时（如 WLC），用 `delegated` 交给它自己的提权守护。

## 文档

- `docs/CONFIG.md` — 配置字段完整参考
- `docs/DESIGN.md` — 设计与实战教训（RDC / OpenChamber 泛化过程）
- `schema/agentguard.schema.json` — 编辑器补全用 JSON Schema

## License

MIT
