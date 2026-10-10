# 设计与实战教训

[English](DESIGN.md) | 简体中文

AgentGuard 不是凭空设计的框架，而是两台真实机制（RDC 看门狗 / OpenChamber 原生宿主）在长期运行中踩坑、修复、再抽象的结果。本文记录这些教训如何变成通用设计。

## 1. 为什么需要「守护的守护」

被守护的服务常见的死法不是崩溃，而是**无声地死去**：

- 开机时与依赖竞态：服务在代理就绪前启动，连接失败后自行退出，之后无人重启（RDC 的 `Device startup failed: fetch failed` 即为典型）；
- 一次性启动脚本只在登录时跑一次，之后进程死掉就再也没人管；
- 服务有自带日志，但没人看；界面显示离线时才知道。

所以 AgentGuard 的核心循环是：**周期性检查 → 不健康就治 → 治不好如实上报**，并用计划任务把「周期」做成系统级承诺（登录触发 + 每 N 分钟，`IgnoreNew`，`StartWhenAvailable`）。

## 2. 教训清单（以及它在 AgentGuard 里的落点）

### 2.1 启动竞态必须用「依赖门控」消除，而不是靠重试碰运气

RDC 的核心教训：v2rayN 与 RDC 同属 HKCU Run，无顺序保证；RDC 启动即连远程 MCP，代理没就绪就 `fetch failed` 自杀。

**落点**：`requires` + `autostart` + `wait_sec`。依赖不健康就绝不启动主服务，并在必要时先拉起依赖。重试是兜底，不是策略。

### 2.2 Node 24 的 `fetch()` 默认无视 `HTTP_PROXY`

实测：设虚假代理 env 后 `fetch()` 仍直连成功；必须 `NODE_USE_ENV_PROXY=1` 才生效。症状是日志里 `fetch failed` / `Channel subscription timed out` 反复出现，而 `curl -x` 同一代理完全正常。排查时极易误判为「代理挂了」。

**落点**：
- 启动脚本（bat）里保证 `NODE_USE_ENV_PROXY=1`（示例 `start_desktop_commander.bat` 的做法）；
- AgentGuard 自己的本地 HTTP 探针**强制绕过系统代理**（`ProxyHandler({})`），避免系统代理抖动影响健康判定。

### 2.3 日志状态判定必须防「旧状态冒充新状态」

`rdc_log.txt` 可能几十 MB，且单条工具输出（如 base64 图片）能到 300KB+，扫描窗口太小会完全错过状态标记。

**落点**：
- 日志探针默认 4MiB 窗口；
- `since: "last-start"`：每次启动前记录文件偏移量，只认之后的新行；
- 状态机取「最后一条命中的 marker」，并把 `Device ready` 与 `Device marked as online` 都映射为 `online`（服务更新措辞不会误伤）；
- 提供 `established` 探针做实证：进程持有已建立 TCP 连接比在日志里翻找可靠得多——这也是原版 rdc_ensure.ps1 的 liveness fallback。

### 2.4 「不健康」不等于「该杀」

服务可能只是启动慢/网络抖动。原始脚本用「进程年龄 < 90s 且处于 connecting → 只等不杀」对抗误判。

**落点**：`young_grace_sec` + 日志状态机的 `starting` 列表。宽限期内无论探针怎么说，都不杀不重启。

### 2.5 Fail-closed：宁可不认领，绝不误杀

原始 RDC 脚本用命令行精确匹配认领 node 进程；OpenChamber host 用 pid 文件 + 命令行双重校验才敢 taskkill。

**落点**：
- `identity` 双条件（镜像名 + 命令行子串全部命中）；
- 命令行不可读时视为「不是自己的进程」；
- 端口被未知进程占用时拒绝替换（OpenChamber host 的「refusing to touch an unknown process」）；
- 操作员 `stop` 用 disarm 标记，而不是靠杀一次算数（OpenChamber host 的 `operator-stop.marker`）。

### 2.6 pythonw / 计划任务下没有 stdout

计划任务里跑 `pythonw` 没有控制台，`print()` 会直接抛错——一个纯输出语句就能让看门狗殉职。旧版 Windows 控制台还是 GBK 代码页，服务日志里混进来的 emoji（如 `🔌 Device marked as online`）会让 JSON 打印直接 UnicodeEncodeError。

**落点**：
- CLI 启动时把 `sys.stdout/stderr` 缺失接到 `os.devnull`；
- 对控制台流 `reconfigure(errors="replace")`，任何字符都能安全打印；
- 日志文件一律显式 UTF-8。

### 2.7 看门狗自己不能堆积、竞争与自我打架

- 计划任务层面：`MultipleInstancesPolicy=IgnoreNew`；
- 进程层面：全局 `ensure.lock`（msvcrt/flock 非阻塞）防两轮看门狗重叠；
- guard 层面：每 guard 锁，让 `stop`/`start` 与看门狗互斥；
- 自愈中再触发：拿不到锁就本轮跳过，下轮再来。

### 2.8 自愈系统不应互相抢管子

OpenChamber host.py 自带 `ensure`（含 pid 所有权校验、端口 fail-closed、操作员标记）。再写一套平行的杀/拉逻辑只会打架。

**落点**：`delegated` 模式——AgentGuard 只做健康判定与触发，把生命周期留给目标自己的机制。RDC 这类没有现成 ensure 的用 `managed`。

### 2.9 计划任务的结构是经过实战的

RDC-Watchdog 的任务 XML（登录触发 + `PT3M` 重复、`IgnoreNew`、`StartWhenAvailable`、`DisallowStartIfOnBatteries=false`、交互用户、`pythonw` 隐藏执行）被完整移植为 `scheduler.build_task_xml`。

## 3. 双模式为何不合并

`managed` 与 `delegated` 表达的是**所有权归属**：

- managed：AgentGuard 拥有生命周期 → 它必须证明进程归属、必须依赖门控、必须清理残骸；
- delegated：目标拥有生命周期 → AgentGuard 仅观察与触发，越权即风险。

两种模式共享同一套探针与等待逻辑，但 kill/stop 语义完全不同，合并只会让「谁有权杀谁」变得含糊——这正是此类工具最危险的地方。

## 4. 不做什么

- 不做服务管理器：不抢 Windows SCM，不注册系统服务；
- 不做编排器：不做依赖图拓扑排序，只做启动前的就绪门控；
- 不做遥测：无网络上报，无秘密回显，日志只记状态与命令名；
- 不做自动「修复」未知进程：任何不确定，一律转人工。
