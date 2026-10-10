# 配置参考

[English](CONFIG.md) | 简体中文

AgentGuard 的配置是纯 JSON（零依赖的代价与底线）。所有字符串在加载时做环境变量展开：Windows 风格的 `%USERPROFILE%`、`$HOME`（Windows 上也支持）、`~` 均可使用。

顶层结构：

```json
{
  "$schema": "schema/agentguard.schema.json",
  "state_dir": "%LOCALAPPDATA%\\AgentGuard",
  "log_max_mb": 5,
  "watchdog": { "task_name": "AgentGuard-Watchdog", "interval_minutes": 3, "logon": true, "startup_shim": false },
  "guards": [ ... ]
}
```

## 顶层字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `state_dir` | string | `%LOCALAPPDATA%\AgentGuard`（Windows）/ `~/.local/state/agentguard` | 锁、guard 状态、disarm 标记、AgentGuard 日志的存放目录 |
| `log_max_mb` | number | `5` | AgentGuard 自身日志（`state_dir/agentguard.log`）轮转阈值，超过后转存 `.1` |
| `watchdog.task_name` | string | `AgentGuard-Watchdog` | 计划任务名 |
| `watchdog.interval_minutes` | int 1-1439 | `3` | 重复触发间隔 |
| `watchdog.logon` | bool | `true` | 是否附加「登录时」触发器 |
| `watchdog.startup_shim` | bool | `false` | `watchdog install` 时是否额外写 Startup 文件夹 VBS 垫片 |
| `watchdog.description` | string | - | 计划任务描述 |

## Guard 通用字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `name` | string | 必填 | `^[A-Za-z0-9._-]{1,64}$`，用于锁/状态/日志；全局唯一 |
| `description` | string | - | 人类可读说明 |
| `enabled` | bool | `true` | false 时看门狗直接跳过 |
| `observe_only` | bool | `false` | 只报告不改动（无需 start 命令，永不杀进程） |
| `identity` | object | - | 进程认领规则，见下 |
| `health` | object | 必填 | 健康判定，见下 |
| `requires` | array | `[]` | 启动前的依赖探针（含可选 autostart） |
| `start` | object | - | 启动动作（managed 模式必需，除非 observe_only 或 delegated） |
| `stop` | object | - | 停止动作：`kill`（默认 true，managed 下按 identity 杀）+ 可选 `command` |
| `delegate` | object | - | 委托自愈（存在即为 delegated 模式） |
| `heal` | object | - | 重试策略 |
| `rotate` | array | `[]` | 被保护服务自身日志的轮转规则 |

### identity（进程认领）

```json
{ "process_names": ["node.exe"], "cmdline_contains": ["desktop-commander"] }
```

- `process_names`：镜像名（大小写不敏感）全匹配；
- `cmdline_contains`：命令行必须**全部包含**这些子串；未提供时仅按镜像名认领；
- **fail-closed**：命令行不可读（权限不足、提权进程）时，提供过 `cmdline_contains` 的进程一律**不认领**——宁可不管，绝不误杀。提权服务的守护请用 `delegated` 交给它自己的机制。

### health（健康判定）

```json
{
  "mode": "all",
  "young_grace_sec": 120,
  "probes": [ ... ]
}
```

- `mode`：`all`（全部探针通过）或 `any`（任一通过）。RDC 用 `any`（已建立连接 **或** 日志 online）；OpenChamber 用 `all`（UI 与 API 两个端口都要活）。
- `young_grace_sec`：认领到的进程「出生」不足该秒数时，一律视为 `starting`，不杀不重启——对抗「启动慢但没死」的误判。

### 探针类型

| type | 字段 | 说明 |
|---|---|---|
| `tcp` | `host`(默认 127.0.0.1), `port`, `timeout_sec`(2) | 端口可连接即健康 |
| `http` | `url`, `timeout_sec`(3), `status`（状态码白名单）, `status_lt`（状态码 < N）, `expect_json`（响应 JSON 必须包含的键值对） | 本地请求强制绕过系统代理 |
| `log` | `path`, `window_bytes`(4MiB), `since`(`window`/`last-start`), `markers`(状态→子串列表), `healthy`/`starting`/`failed`/`offline`(状态名列表), `empty_state`(`unknown`) | 状态机：扫描窗口内最后命中的 marker 获胜；`last-start` 从上次启动前记录的字节偏移开始读 |
| `established` | `timeout_sec`(15) | 认领进程持有 ESTABLISHED TCP 连接（RDC 存活实证） |
| `cmd` | `command`, `timeout_sec`(30), `exit_codes`([0]) | 命令退出码判定 |
| `process` | `identity` | 认领进程存在即健康（stdio MCP 的常用判定） |

### requires（依赖门控）

依赖探针可用 `tcp` / `http` / `cmd` / `process`（不允许 `established`），附加字段：

```json
{
  "name": "proxy-10808",
  "type": "tcp",
  "host": "127.0.0.1",
  "port": 10808,
  "wait_sec": 40,
  "autostart": { "command": ["...\\v2rayN.exe"], "hidden": true }
}
```

- 依赖不健康时：有 `autostart` 就先启动它，然后每 1 秒轮询，最长 `wait_sec` 秒；
- 仍不健康 → 本轮放弃启动主服务（`dependency-unavailable`），等下一轮看门狗重试，绝不带病启动。

### 动作对象（start / stop.command / autostart / delegate.*_command 共用形态）

```json
{ "command": ["cmd", "/c", "路径\\脚本.bat"], "hidden": true, "cwd": null, "stdout_log": null }
```

- `command`：argv 列表，不做 shell 引号游戏；要跑 bat 就写 `["cmd","/c","文件.bat"]`；
- `hidden`：Windows 上附加 `CREATE_NO_WINDOW`（GUI 程序不受影响）；
- `stdout_log`：可选，把被启动进程的 stdout/stderr 落到文件。

### delegate（委托模式）

```json
{
  "ensure_command": ["pythonw.exe", "host.py", "ensure"],
  "start_command":   ["python.exe",  "host.py", "start"],
  "stop_command":    ["python.exe",  "host.py", "stop"],
  "healthy_timeout_sec": 120
}
```

- 健康判定由 AgentGuard 的探针负责；不健康时同步执行 `ensure_command`（超时 = 2×timeout+30s），再用探针确认；
- `stop` 时执行 `stop_command`（不再由 AgentGuard 杀进程）；
- `start` 时清除 disarm 并走 ensure 流程。
- 适用对象：自带幂等 ensure/锁/操作员停止标记的服务——AgentGuard 不重复造它的轮子。

### heal（重试策略）

| 字段 | 默认 | 说明 |
|---|---|---|
| `attempts` | 3 | 单轮自愈的启动尝试次数 |
| `healthy_timeout_sec` | 90 | 每次尝试等待健康的上限（delegate 优先用自己的 `healthy_timeout_sec`） |
| `backoff_sec` | 5 | 尝试间隔 |
| `kill_stale` | true | managed 模式启动前是否清理身份匹配的残留进程 |

### rotate（服务日志轮转）

```json
[{ "path": "%USERPROFILE%\\.local\\bin\\rdc_log.txt", "max_mb": 40, "keep": 1 }]
```

超过 `max_mb` 时按 `path.N` 递推轮转，`keep` 控制保留份数。

## 状态与退出码

`ensure` 输出 JSON：`{"status": "ok"|"degraded", "results": [...]}`，每个 guard 的 `status` 取值：

- 正常：`healthy` / `recovered` / `starting` / `disarmed` / `disabled` / `observe-only` / `other-run` / `stopped`
- 异常（退出码 1）：`failed` / `dependency-unavailable` / `stale` / `no-action`

其他命令：`status` 有异常退出 1；配置错误退出 2，并列出全部校验错误。

## 环境与运行细节

- 状态文件：`state_dir/guards/<name>.state.json`（记录 `last_healthy` 与日志偏移量）；
- disarm 标记：`state_dir/guards/<name>.disarmed`；
- 锁：`state_dir/ensure.lock`（全局）、`state_dir/locks/<name>.lock`（每 guard）；
- 日志：`state_dir/agentguard.log`（`guard.py logs` 查看）。
