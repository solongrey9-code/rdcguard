# AgentGuard

**Boot autostart + self-healing watchdog for anything local that must stay alive: agents, MCP servers, local bridges, proxies, model servers, daemons.**

English | [简体中文](README.md)

AgentGuard answers one plain question: *"This thing must stay up. If it dies, nobody notices. Who keeps it alive?"*

It generalizes two mechanisms hardened in production on a Windows machine:

- the **RDC watchdog** (PowerShell) that keeps a Desktop Commander remote device online across the boot race (core dies with `fetch failed` before the proxy is ready);
- the **OpenChamber native host** (Python) that checks an isolated OpenCode V2 + OpenChamber service every minute and heals it fail-closed.

Both were reliable but single-purpose. AgentGuard turns them into a **declarative config + generic probes + one healing engine**: write JSON, get boot autostart and a watchdog for anything.

## Features

- **Zero dependencies** - Python 3.8+ stdlib only. Windows-first; the healing engine also runs on Linux/macOS.
- **Declarative** - one `agentguard.json`; `%USERPROFILE%`-style variables expand at load time so public configs never leak usernames.
- **Generic probes** - TCP, HTTP (status codes + JSON body assertions), log state machine, established-socket liveness, command exit code, process identity (image name + command-line needles).
- **Two modes** - `managed` (AgentGuard owns the lifecycle, kills only processes it can prove it owns) and `delegated` (the target already ships an idempotent `ensure`; AgentGuard verifies health and invokes it).
- **Dependency gating** - if your service needs a proxy or database, declare it in `requires` (optionally with `autostart`) and it will never be started before its dependency is ready. This kills boot races by design.
- **Fail-closed safety** - unreadable command line means "not owned, never kill"; a port held by an unknown process is never replaced; `stop` disarms until an explicit `start`.
- **Concurrency-safe** - a global ensure lock plus per-guard locks; slow heals never pile up.
- **Log rotation** - rotate the protected service's own oversized logs (like a 40 MB `rdc_log.txt`).
- **Autostart installer** - one command registers a logon + every-N-minutes Task Scheduler job running `pythonw.exe guard.py ensure` with no console window; optional Startup-folder VBS shim.
- **Human and machine output** - `status` prints a table; `--json` for tooling; `ensure` always prints JSON and exits 0/1 for monitoring.

## Quick start

```powershell
python guard.py init --out agentguard.json
python guard.py validate
python guard.py status            # read-only
python guard.py ensure            # one healing round (watchdog entry point)
python guard.py watchdog install  # logon + every 3 minutes
```

`examples/` contains real-world configs:

| File | Mode | Protects |
|---|---|---|
| `examples/rdc.json` | managed | Desktop Commander remote device (proxy dependency + log state machine + established-socket probe) |
| `examples/openchamber.json` | delegated | OpenCode V2 + OpenChamber native host (`host.py ensure`) |
| `examples/windows-local-commander.json` | delegated | ChatGPT-native MCP bridge on 8765 (healthz JSON assertion) |
| `examples/mcp-stdio.json` | managed | template for any stdio MCP server (process identity probe) |
| `examples/ollama.json` | managed | local model server |
| `examples/proxy.json` | managed | the proxy client itself, as a first-class guard |
| `examples/observe-only.json` | observe | watch without touching - the safe first step |

## How it works

A watchdog tick (logon / every N minutes):

1. global lock -> per guard lock;
2. full health evaluation (probes + process ownership snapshot);
3. healthy -> record `last_healthy`, done;
4. fresh process inside `young_grace_sec` -> `starting`, never killed;
5. rotate oversized logs;
6. managed: kill stale **owned** processes, gate on `requires`, run `start`, wait for health with retries and backoff;
   delegated: invoke `delegate.ensure_command`, verify with the same probes;
7. still broken after `heal.attempts` -> `failed` in the JSON result (exit 1).

The log probe is a state machine: the *last* marker line inside the scanned window (or since the byte offset recorded before the last start) wins; `healthy`/`starting`/`failed` lists drive decisions, and stale `Device ready` lines from a previous run cannot masquerade as fresh state.

## CLI

| Command | Purpose |
|---|---|
| `guard.py status [names] [--json]` | read-only health report; exit 1 when anything is unhealthy |
| `guard.py ensure [names]` | check-and-heal (watchdog entry); JSON output |
| `guard.py start / stop / restart <names>` | operator control; `stop` disarms, `start` clears the disarm |
| `guard.py list` | configured guards + enable/armed state |
| `guard.py validate` | validate the config, one error per line |
| `guard.py logs [name] [-n N]` | tail the AgentGuard journal |
| `guard.py watchdog install\|remove\|status` | Task Scheduler job (logon + repetition) |
| `guard.py startup install\|remove\|status` | hidden VBS shim in the Startup folder |
| `guard.py init --out PATH [--force]` | starter config |

## Safety

1. **Kill only what is provably ours** - image name AND every command-line needle must match; if the command line cannot be read (elevated processes), the process is not adopted.
2. **Never fight a port** - an unknown process holding a configured port stops the heal and escalates to a human.
3. **Disarm first** - `stop` writes a disarm marker before acting; watchdogs skip the guard until an explicit `start`.
4. **Respect existing self-healing** - when a target already has locks, pid-ownership checks and an operator-stop marker, use `delegated` and do not duplicate it.
5. **No secrets in logs** - the journal records states and command names, never service output or tokens.

## Docs

- `docs/CONFIG.md` - full configuration reference
- `docs/DESIGN.md` - design rationale and battle lessons
- `schema/agentguard.schema.json` - JSON Schema for editor completion

## License

MIT
