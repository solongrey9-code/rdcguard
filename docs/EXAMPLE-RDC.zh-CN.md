# 实战范例：让远程 MCP 设备（RDC）永不掉线

[English](EXAMPLE-RDC.md) | 简体中文

本文是 `examples/rdc.json` 的端到端走读——它守护的是真实的 Desktop Commander 远程设备（Remote MCP，`mcp.desktopcommander.app`），也就是原版手写 PowerShell 看门狗在生产里守护的那个东西。

如果你要守护**任何**长时间运行的远程设备 / MCP 类服务，这里的分层套路（`identity` + `health` + `requires` + `start` + `heal` + `rotate`）就是可以照抄的模板。

## 背景：两个反复踩的坑

原版机制（`rdc_ensure.ps1` + 登录/每 3 分钟的计划任务）能用，但一直有两类问题：

1. **开机竞态**：设备与本地代理都在登录时启动，顺序无保证。设备在代理监听之前连远程，核心以 `Device startup failed: fetch failed` 退出，之后再无人重启——网页端只显示设备离线。
2. **日志淹没**：单次 ChatGPT 工具调用就能往 `rdc_log.txt` 写几百 KB（base64 图片），日志轻松几十 MB；`Device ready:` 这类状态标记被埋，粗糙的日志扫描会报错误状态。

AgentGuard 从结构上消灭这两类问题：

- **开机竞态** → `requires` + `autostart` + `wait_sec`：依赖未验证可达之前，主服务根本不会启动；
- **日志淹没** → 有界扫描窗口 + 每次启动前记录字节偏移（`since: "last-start"`）+ `established` 探针独立证明存活。

## 前置条件

- Windows + Python 3.8+（看门狗无需管理员权限）。
- Node.js 在 `PATH` 上，并安装 desktop-commander CLI：

  ```powershell
  npm install -g @wonderwhy-er/desktop-commander
  ```

- 需要代理的网络环境（范例假设 `127.0.0.1:10808`，按需改或删掉 `requires` 段）。

## 第 1 步 —— 启动器

把 `examples/rdc-launcher.bat` 复制到 `%USERPROFILE%\.local\bin\start_desktop_commander.bat`，并按机器改 `PROXY`/路径。模板里两件事最关键：

- `NODE_USE_ENV_PROXY=1`：没有它，Node 的 `fetch()` 会无视 `HTTP_PROXY`/`HTTPS_PROXY`。这是「curl 走同一代理完全正常、设备却 `fetch failed`」的最常见根因。
- 单实例检查：已有 `desktop-commander` 的 node 进程存活时，启动器直接退出，不会起第二个。

## 第 2 步 —— 首次授权（只做一次）

手动跑一次启动器，完成浏览器登录流程：

```powershell
%USERPROFILE%\.local\bin\start_desktop_commander.bat
Start-Process "https://mcp.desktopcommander.app/"
```

凭据缓存在 `%USERPROFILE%\.desktop-commander-device\device.json`。请把它当密码对待：它能代表本机执行命令。**禁止**复制进任何仓库、日志或聊天。网页端看到设备在线后即可关掉窗口。

## 第 3 步 —— 配置

如果机器布局与范例一致（`node.exe` + `desktop-commander`、代理 10808、日志在 `%USERPROFILE%\.local\bin\`），`examples/rdc.json` 直接可用；否则按需调整：

- `identity.cmdline_contains`：保持精确——这是「敢杀」的前提，AgentGuard 不碰任何证明不了归属的进程；
- `requires[0].autostart.command`：代理客户端路径；若代理由其他机制管理，删掉 `autostart` 只保留等待；
- `start.command`：第 1 步的启动器；
- `rotate`：`rdc_log.txt` 上限 40 MB。

先校验再信任：

```powershell
python guard.py validate --config examples\rdc.json
```

## 第 4 步 —— 看真相

```powershell
python guard.py status --config examples\rdc.json
```

真实机器抓取（截断）：

```
NAME   KIND      STATE    HEALTHY  PROCESS                 DETAIL
rdc    managed   healthy  yes      pid 30948,31476 age=5959  established socket for pid 30948
```

手动跑一轮自愈（幂等，随时可跑）：

```powershell
python guard.py ensure --config examples\rdc.json
```

```json
{
  "status": "ok",
  "results": [
    { "name": "rdc", "status": "healthy", "healthy": true, "state": "healthy",
      "probes": [
        { "type": "established", "ok": true, "detail": "established socket for pid 30948" },
        { "type": "log", "ok": true, "state": "online", "detail": "? Device marked as online" }
      ] }
  ]
}
```

设备真的掉了时，`ensure` 会把它拉起来并报 `"status": "recovered"`（含第几次尝试成功）。

## 第 5 步 —— 让它扛过重启

```powershell
python guard.py watchdog install --config examples\rdc.json
```

这会注册登录触发 + 每 3 分钟重复的计划任务（`IgnoreNew`），以隐藏的 `pythonw.exe guard.py ensure` 运行。几点实务提醒：

- 多个配置各自跑看门狗时，应为每个配置用**独立的 `state_dir`**——锁与 guard 状态都在那里；
- 如果你此前用原版 `rdc_ensure.ps1` 看门狗任务守护同一目标，切换后请移除/禁用它。两者都幂等，但两个看门狗管同一设备是多余；
- 有疑问先看 journal：

  ```powershell
  python guard.py logs rdc -n 20
  ```

## 每一块配置换来了什么

| 配置 | 替换了原脚本的 | 意义 |
|---|---|---|
| `identity: node.exe + desktop-commander` | `Get-RdcProc` 精确命令行匹配 | 只杀「证明得了归属」的进程 |
| `established` 探针 | `Get-NetTCPConnection` 存活兜底 | 已订阅设备会长期持有 TCP 连接，日志淹没伪造不了死亡 |
| `log` 探针 + `since: last-start` | `Get-LastState` 带字节偏移 | 上一轮残留的 `Device ready` 不会掩盖新一轮失败 |
| `young_grace_sec: 120` | 「connecting 且年龄 < 90s → 不重启」 | 启动慢 ≠ 死亡 |
| `requires` + `autostart` | `Ensure-Proxy` 轮询 | 由构造消灭开机竞态 |
| `heal`（3 次 / 90s / 退避） | `Wait-Online` + 重试循环 | 网络抖动被重试，而不是被放弃 |
| `rotate` | 40 MB 日志轮转 | 磁盘健康，扫描快速 |
| 计划任务 | `RDC-Watchdog`（登录 + 3 分钟） | 开机自启 + 自愈一体 |
| `stop` / disarm 标记 | 手动杀进程 | 操作员意图是持久的，不是一次性的 |

## 排障

- **开机后立刻 `fetch failed`**：确认启动器里有 `NODE_USE_ENV_PROXY=1`，且代理端口能过 `python guard.py status`（`requires` 探针会显示）；
- **进程活着但工具报 `down`**：`status --json` 看 `log` 探针的 `state`/`detail`——可能需要加大窗口，或服务改了措辞（补一条新 marker）；
- **本地全绿但网页端离线**：属于远端或代理出口问题；测设备实际使用的出口，并观察 `rdc_log.txt` 是否出现 `Channel subscription timed out`；
- **两个设备打架**：确保只有一条 `start` 路径（启动器或手动）和一个看门狗。
