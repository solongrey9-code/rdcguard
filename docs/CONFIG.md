# Configuration reference

English | [简体中文](CONFIG.zh-CN.md)

An AgentGuard config is plain JSON (the price and the guarantee of a
zero-dependency project). Every string is environment-expanded at load time:
Windows-style `%USERPROFILE%`, `$HOME` (also on Windows) and `~` all work.

Top-level shape:

```json
{
  "$schema": "schema/agentguard.schema.json",
  "state_dir": "%LOCALAPPDATA%\\AgentGuard",
  "log_max_mb": 5,
  "watchdog": { "task_name": "AgentGuard-Watchdog", "interval_minutes": 3, "logon": true, "startup_shim": false },
  "guards": [ ... ]
}
```

## Top-level fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `state_dir` | string | `%LOCALAPPDATA%\AgentGuard` (Windows) / `~/.local/state/agentguard` | Where locks, guard state, disarm markers and the AgentGuard journal live |
| `log_max_mb` | number | `5` | Rotation threshold for the AgentGuard journal (`state_dir/agentguard.log`), rotated to `.1` |
| `watchdog.task_name` | string | `AgentGuard-Watchdog` | Scheduled task name |
| `watchdog.interval_minutes` | int 1-1439 | `3` | Repetition interval |
| `watchdog.logon` | bool | `true` | Also add a logon trigger |
| `watchdog.startup_shim` | bool | `false` | `watchdog install` additionally writes a Startup-folder VBS shim |
| `watchdog.description` | string | - | Scheduled task description |

## Common guard fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | `^[A-Za-z0-9._-]{1,64}$`, used for locks/state/logs; must be unique |
| `description` | string | - | Human-readable note |
| `enabled` | bool | `true` | `false` = watchdog skips the guard entirely |
| `observe_only` | bool | `false` | Report only, never change anything (no `start` command needed, never kills) |
| `identity` | object | - | Process ownership rule, see below |
| `health` | object | required | Health decision, see below |
| `requires` | array | `[]` | Dependency probes checked before start (optional `autostart`) |
| `start` | object | - | Start action (required in managed mode unless `observe_only` or `delegate`) |
| `stop` | object | - | Stop action: `kill` (default true, managed mode kills by identity) + optional `command` |
| `delegate` | object | - | Delegated healing (its presence selects delegated mode) |
| `heal` | object | - | Retry policy |
| `rotate` | array | `[]` | Rotation rules for the protected service's own logs |

### identity (process ownership)

```json
{ "process_names": ["node.exe"], "cmdline_contains": ["desktop-commander"] }
```

- `process_names`: image names (case-insensitive), must all match;
- `cmdline_contains`: the command line must contain **every** needle; when
  omitted, the image name alone decides ownership;
- **fail-closed**: if the command line cannot be read (permissions, elevated
  processes) and needles were configured, the process is **not** adopted -
  better to leave something unmanaged than to kill the wrong thing. Protect
  elevated services with `delegated` and let their own mechanism own them.

### health (health decision)

```json
{
  "mode": "all",
  "young_grace_sec": 120,
  "probes": [ ... ]
}
```

- `mode`: `all` (every probe must pass) or `any` (one is enough). The RDC
  example uses `any` (established socket **or** log state `online`);
  OpenChamber uses `all` (both UI and API ports must be alive).
- `young_grace_sec`: an owned process younger than this is always treated as
  `starting` - never killed, never restarted. Defends against "slow to boot,
  not dead".

### Probe types

| type | Fields | Meaning |
|---|---|---|
| `tcp` | `host`(127.0.0.1), `port`, `timeout_sec`(2) | Healthy when the port accepts a connection |
| `http` | `url`, `timeout_sec`(3), `status` (allow-list), `status_lt` (status < N), `expect_json` (key: value pairs the JSON body must contain) | Local requests always bypass system proxies |
| `log` | `path`, `window_bytes`(4 MiB), `since`(`window`/`last-start`), `markers`(state -> substring list), `healthy`/`starting`/`failed`/`offline`(state lists), `empty_state`(`unknown`) | State machine: the last matched marker inside the scanned region wins; `last-start` reads from the byte offset recorded before the previous start |
| `established` | `timeout_sec`(15) | An owned process holds an ESTABLISHED TCP connection (the RDC proof of life) |
| `cmd` | `command`, `timeout_sec`(30), `exit_codes`([0]) | Exit-code based decision |
| `process` | `identity` | Healthy when an owned process exists (common for stdio MCP servers) |

### requires (dependency gating)

Dependency probes may use `tcp` / `http` / `cmd` / `process` (`established` is
not allowed), plus:

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

- When a dependency is unhealthy: `autostart` (if present) is launched, then
  the probe is polled every second up to `wait_sec`;
- still unhealthy -> the main service is **not** started this round
  (`dependency-unavailable`) and the next watchdog tick retries. Never start
  a service with an unready dependency.

### Action object (shared by start / stop.command / autostart / delegate.*_command)

```json
{ "command": ["cmd", "/c", "path\\script.bat"], "hidden": true, "cwd": null, "stdout_log": null }
```

- `command`: an argv list, no shell quoting games; to run a bat use
  `["cmd","/c","file.bat"]`;
- `hidden`: adds `CREATE_NO_WINDOW` on Windows (GUI apps are unaffected);
- `stdout_log`: optional file capturing the started process's stdout/stderr.

### delegate (delegated mode)

```json
{
  "ensure_command": ["pythonw.exe", "host.py", "ensure"],
  "start_command":   ["python.exe",  "host.py", "start"],
  "stop_command":    ["python.exe",  "host.py", "stop"],
  "healthy_timeout_sec": 120
}
```

- Health judgement stays with AgentGuard's probes; when unhealthy,
  `ensure_command` is run synchronously (timeout = 2x timeout + 30 s) and the
  probes re-verify;
- `stop` runs `stop_command` instead of killing processes;
- `start` clears the disarm marker and rides the ensure flow.
- Use it for targets that already ship an idempotent ensure with their own
  locks / operator-stop marker - do not rebuild what they already have.

### heal (retry policy)

| Field | Default | Meaning |
|---|---|---|
| `attempts` | 3 | Start attempts per healing round |
| `healthy_timeout_sec` | 90 | Max wait per attempt (delegate uses its own `healthy_timeout_sec` when set) |
| `backoff_sec` | 5 | Pause between attempts |
| `kill_stale` | true | In managed mode, clean identity-matched leftovers before starting |

### rotate (service log rotation)

```json
[{ "path": "%USERPROFILE%\\.local\\bin\\rdc_log.txt", "max_mb": 40, "keep": 1 }]
```

Beyond `max_mb`, files shift to `path.N`; `keep` controls how many copies are kept.

## Status and exit codes

`ensure` prints JSON: `{"status": "ok"|"degraded", "results": [...]}`. Each
guard's `status` is one of:

- OK: `healthy` / `recovered` / `starting` / `disarmed` / `disabled` /
  `observe-only` / `other-run` / `stopped`
- Failure (exit code 1): `failed` / `dependency-unavailable` / `stale` /
  `no-action`

Other commands: `status` exits 1 when anything is unhealthy; config errors
exit 2 and list every validation error.

## Runtime layout

- Guard state: `state_dir/guards/<name>.state.json` (`last_healthy` and log offsets)
- Disarm marker: `state_dir/guards/<name>.disarmed`
- Locks: `state_dir/ensure.lock` (global), `state_dir/locks/<name>.lock` (per guard)
- Journal: `state_dir/agentguard.log` (read with `guard.py logs`)
