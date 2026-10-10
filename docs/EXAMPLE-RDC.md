# Example: keeping a remote MCP device (RDC) online

English | [简体中文](EXAMPLE-RDC.zh-CN.md)

This is the end-to-end walkthrough of `examples/rdc.json` - the config that
protects a Desktop Commander remote device (Remote MCP at
`mcp.desktopcommander.app`) the way a hand-written PowerShell watchdog used to
in production.

If you protect *any* long-running remote-device or MCP-style service, the
pattern here (`identity` + `health` + `requires` + `start` + `heal` + `rotate`)
is the one to copy.

## Background: the two failure modes

The original mechanism (`rdc_ensure.ps1` + a Task Scheduler watchdog running
at logon and every 3 minutes) worked, but kept hitting two problems:

1. **Boot race.** The device and the local proxy both start at logon with no
   ordering guarantee. When the device connected before the proxy was
   listening, its core exited with `Device startup failed: fetch failed` and
   nothing ever restarted it - the web UI just showed the device offline.
2. **Log flooding.** A single ChatGPT tool call can write hundreds of KB
   (base64 images) into `rdc_log.txt`, which grows past tens of MB. State
   markers like `Device ready:` get buried and naive log scraping reports the
   wrong state.

AgentGuard fixes both structurally:

- **boot race** -> `requires` with `autostart` and `wait_sec`: the device is
  simply never started before its dependency is verified reachable;
- **log flooding** -> a bounded scan window, a byte offset recorded before
  every start (`since: "last-start"`), and an `established` probe that proves
  liveness independently of the log.

## What you need

- Windows with Python 3.8+ (the watchdog works without admin rights).
- Node.js on `PATH` and the desktop-commander CLI:

  ```powershell
  npm install -g @wonderwhy-er/desktop-commander
  ```

- A proxy if your network needs one (the example assumes `127.0.0.1:10808`;
  adapt or remove the `requires` block accordingly).

## Step 1 - the launcher

Copy `examples/rdc-launcher.bat` to `%USERPROFILE%\.local\bin\start_desktop_commander.bat`
and adjust `PROXY`/paths if needed. Two things in that template matter:

- `NODE_USE_ENV_PROXY=1` - Node's `fetch()` ignores `HTTP_PROXY`/`HTTPS_PROXY`
  without it. This is the single most common reason a remote MCP device dies
  with `fetch failed` while `curl` through the same proxy works fine.
- The single-instance check - if a `desktop-commander` node process is already
  alive, the launcher exits without starting a second one.

## Step 2 - first-run authorization (once)

Run the launcher manually once and complete the browser login flow:

```powershell
%USERPROFILE%\.local\bin\start_desktop_commander.bat
Start-Process "https://mcp.desktopcommander.app/"
```

The device credentials are cached in `%USERPROFILE%\.desktop-commander-device\device.json`.
Treat that file like a password: it can execute commands on your machine.
Never copy it into a repository, log or chat. Once the device shows as online
in the web UI, you can close the window.

## Step 3 - the config

Take `examples/rdc.json` as-is when your layout matches (`node.exe` +
`desktop-commander`, proxy on 10808, logs under `%USERPROFILE%\.local\bin\`),
or adjust:

- `identity.cmdline_contains` - keep this precise; it is what makes killing
  safe. AgentGuard refuses to touch any process it cannot prove it owns.
- `requires[0].autostart.command` - path to your proxy client, or delete
  `autostart` to only *wait* for an externally managed proxy.
- `start.command` - the launcher from step 1.
- `rotate` - `rdc_log.txt` capped at 40 MB.

Validate before trusting it:

```powershell
python guard.py validate --config examples\rdc.json
```

## Step 4 - see the truth

```powershell
python guard.py status --config examples\rdc.json
```

Captured from a real machine (trimmed):

```
NAME   KIND      STATE    HEALTHY  PROCESS                 DETAIL
rdc    managed   healthy  yes      pid 30948,31476 age=5959  established socket for pid 30948
```

And one healing round - safe to run any time, it is idempotent:

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

If the device is actually down, `ensure` restarts it and reports
`"status": "recovered"` with the attempt number.

## Step 5 - make it survive boots

```powershell
python guard.py watchdog install --config examples\rdc.json
```

This registers a Task Scheduler job (logon + every 3 minutes, `IgnoreNew`)
that runs `pythonw.exe guard.py ensure` hidden. A few practical notes:

- Each config should use its own `state_dir` if you run several watchdogs -
  locks and guard state live there.
- If you previously used the hand-written `rdc_ensure.ps1` watchdog task for
  the same target, remove/disable it when adopting AgentGuard. Both are
  idempotent, but running two watchdogs over the same device is redundant.
- Watch the journal when in doubt:

  ```powershell
  python guard.py logs rdc -n 20
  ```

## What each part buys you

| Config | Replaces this from the original script | Why it matters |
|---|---|---|
| `identity: node.exe + desktop-commander` | `Get-RdcProc` exact-cmdline match | Only provably-owned processes are ever killed |
| `established` probe | liveness fallback via `Get-NetTCPConnection` | The subscribed device holds a TCP connection; log floods cannot fake death |
| `log` probe with `since: last-start` | `Get-LastState` with byte offset | A stale `Device ready` from the previous run cannot mask a fresh failure |
| `young_grace_sec: 120` | "connecting and age < 90s -> do not restart" | Slow boot is not death |
| `requires` + `autostart` | `Ensure-Proxy` polling loop | No more boot race, by construction |
| `heal` (3 attempts / 90s / backoff) | `Wait-Online` + retry loop | Flaky network gets retried, not abandoned |
| `rotate` | log rotation at 40 MB | Disk stays sane; scans stay fast |
| watchdog task | `RDC-Watchdog` (logon + 3 min) | Boot autostart + self-healing in one |
| `stop` / disarm marker | manual kill | Operator intent is durable, not one-shot |

## Troubleshooting

- **`fetch failed` right after boot**: confirm `NODE_USE_ENV_PROXY=1` is in
  the launcher and that the proxy port passes
  `python guard.py status` (the `requires` probe shows it).
- **Process alive but the tool says `down`**: run `status --json` and look at
  the `log` probe's `state`/`detail` - the window may need to be bigger, or
  the service changed its wording (add the new marker).
- **Web UI shows offline while everything is green locally**: that is a
  remote-side or proxy-egress issue; test the proxy endpoint the device uses
  and watch `rdc_log.txt` for `Channel subscription timed out`.
- **Two devices fighting**: keep exactly one `start` path (launcher or manual)
  and one watchdog.
