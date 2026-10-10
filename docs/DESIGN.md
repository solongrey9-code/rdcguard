# Design notes and battle lessons

English | [简体中文](DESIGN.zh-CN.md)

AgentGuard was not designed on a whiteboard. It is the abstraction of two
mechanisms (the RDC watchdog and the OpenChamber native host) that ran for
months, broke in specific ways, got fixed, and taught their lessons. This
document maps each lesson to where it lives in the design.

## 1. Why a watchdog needs a watchdog

Services rarely die by crashing loudly. They die **silently**:

- a boot-time race with a dependency: the service starts first, can't
  connect, exits on its own, and nobody restarts it (the RDC story:
  `Device startup failed: fetch failed`);
- a start-once-at-logon script with no supervision afterwards;
- the service keeps its own log, nobody reads it, and "offline" is only
  noticed in a web UI.

So the core loop is: **check periodically -> heal when unhealthy -> report
honestly when healing fails**, with the schedule itself guaranteed by Task
Scheduler (logon trigger + repetition, `IgnoreNew`, `StartWhenAvailable`).

## 2. Lessons, and where they land in AgentGuard

### 2.1 Boot races are eliminated by gating, not by retrying harder

The RDC and its proxy both live in `HKCU\...\Run` with no ordering guarantee;
the device connected before the proxy was up and killed itself with
`fetch failed`.

**Landing:** `requires` + `autostart` + `wait_sec`. Never start the service
before its dependency is provably reachable; start the dependency first when
allowed. Retries are a fallback, not a strategy.

### 2.2 Node 24's `fetch()` ignores `HTTP_PROXY` by default

Measured directly: with a fake proxy in the environment, `fetch()` still went
direct; only `NODE_USE_ENV_PROXY=1` makes it honor the env proxy. The symptom
is endless `fetch failed` / `Channel subscription timed out` lines while
`curl -x` through the same proxy works perfectly - a notorious false alarm
for "the proxy is down".

**Landing:**
- the launcher keeps `NODE_USE_ENV_PROXY=1` (see `examples/rdc-launcher.bat`);
- AgentGuard's own local HTTP probes force-bypass system proxies
  (`ProxyHandler({})`) so proxy glitches never distort health decisions.

### 2.3 Log state must not let old runs impersonate new ones

`rdc_log.txt` can grow to tens of MB, and a single tool output (base64 images)
can exceed 300 KB - a small scan window can miss every state marker.

**Landing:**
- the log probe defaults to a 4 MiB window;
- `since: "last-start"` records a byte offset before every start and reads
  only newer lines;
- the state machine takes the *last* matched marker, and maps both
  `Device ready:` and `Device marked as online` to `online` so wording
  changes do not break detection;
- the `established` probe provides independent, log-independent proof of
  life (inherited straight from the original script's liveness fallback).

### 2.4 "Unhealthy" does not mean "kill it"

A service may simply be slow to boot or hit a network blip. The original
script refused to restart a process younger than 90 s stuck in `connecting`.

**Landing:** `young_grace_sec` + the log state machine's `starting` list.
Inside the grace window, no matter what probes say, nothing is killed.

### 2.5 Fail-closed: never adopt (or kill) what you cannot prove

The original RDC script claimed processes by exact command-line match; the
OpenChamber host required pid file + command-line verification before any
`taskkill`.

**Landing:**
- `identity` requires image name AND every command-line needle;
- an unreadable command line means "not ours";
- a port held by an unknown process stops the heal ("refusing to touch an
  unknown process");
- operator `stop` writes a durable disarm marker instead of "killing once
  and hoping" (the OpenChamber host's `operator-stop.marker` idea made generic).

### 2.6 No stdout under pythonw / Task Scheduler

A `pythonw` run has no console and `print()` raises - a pure output statement
can kill a watchdog. Legacy Windows consoles are also GBK, and a stray emoji
from a service log (`Device marked as online`) crashed JSON printing with a
`UnicodeEncodeError`.

**Landing:**
- missing `sys.stdout/stderr` are wired to `os.devnull`;
- console streams are `reconfigure(errors="replace")` so any character prints;
- all log files are explicit UTF-8.

### 2.7 Watchdogs must not pile up, race, or fight each other

- scheduler level: `MultipleInstancesPolicy=IgnoreNew`;
- process level: a global non-blocking `ensure.lock` (msvcrt/flock);
- guard level: per-guard locks so `stop`/`start` are mutually exclusive with
  the watchdog;
- a heal that is still running simply makes the next tick skip.

### 2.8 Self-healing systems should not steal each other's controls

The OpenChamber host ships its own `ensure` (pid ownership checks, port
fail-closed, operator marker). Writing a parallel kill/start pipeline would
only fight it.

**Landing:** `delegated` mode - AgentGuard judges health and triggers the
target's ensure; the lifecycle stays with its owner. Targets with no ensure
of their own (like RDC) use `managed` mode.

### 2.9 The scheduler shape is battle-tested

The RDC-Watchdog task XML (logon trigger + `PT3M` repetition, `IgnoreNew`,
`StartWhenAvailable`, `DisallowStartIfOnBatteries=false`, interactive user,
hidden `pythonw` action) was ported verbatim into `scheduler.build_task_xml`.

## 3. Why the two modes are not merged

`managed` vs `delegated` encodes **ownership**:

- managed: AgentGuard owns the lifecycle -> it must prove process ownership,
  gate dependencies, clean leftovers;
- delegated: the target owns the lifecycle -> AgentGuard only observes and
  triggers; overstepping here is a risk, not a feature.

Both share the same probes and waiting logic, but the kill/stop semantics are
fundamentally different. Merging them would blur the only question that
matters in this tool category: *who has the right to kill what*.

## 4. What AgentGuard deliberately does not do

- not a service manager: no Windows SCM, no system services;
- not an orchestrator: no dependency-graph scheduling, only pre-start
  readiness gating;
- no telemetry: nothing is uploaded, no secrets are echoed, the journal keeps
  states and command names only;
- never "repairs" unknown processes: any uncertainty escalates to a human.
