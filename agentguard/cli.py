"""AgentGuard command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, config as config_mod, engine, scheduler, util

OK_STATUSES = {
    "healthy",
    "recovered",
    "starting",
    "disarmed",
    "disabled",
    "observe-only",
    "other-run",
    "stopped",
}

FAILURE_STATUSES = {"failed", "dependency-unavailable", "stale", "no-action"}


def _program_path():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(root, "guard.py")
    if os.path.isfile(candidate):
        return candidate
    return os.path.abspath(sys.argv[0])


def _load(args):
    return config_mod.load_config(config_mod.find_config(getattr(args, "config", None)))


def _journal(cfg):
    return util.Journal(os.path.join(cfg["_state_dir"], "agentguard.log"), cfg.get("log_max_mb", 5))


def _print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _is_failure(status):
    if status.get("healthy"):
        return False
    if status.get("status") in OK_STATUSES:
        return False
    if status.get("disarmed") or status.get("enabled") is False or status.get("observe_only"):
        return False
    return True


def _wants_json(args):
    return bool(getattr(args, "json", False))


def _select(cfg, names):
    guards = config_mod.select_guards(cfg, names)
    known = {guard.get("name") for guard in cfg.get("guards", [])}
    missing = [name for name in names if name not in known] if names else []
    return guards, missing


def cmd_ensure(args):
    cfg = _load(args)
    guards, missing = _select(cfg, args.names)
    if missing:
        _print_json({"status": "unknown-guard", "missing": missing})
        return 2
    journal = _journal(cfg)
    results = engine.ensure_all(cfg, guards, journal)
    failed = [item for item in results if _is_failure(item)]
    _print_json(
        {
            "status": "ok" if not failed else "degraded",
            "checked_at": util.now_iso(),
            "results": [_public(item) for item in results],
        }
    )
    return 1 if failed else 0


def cmd_status(args):
    cfg = _load(args)
    guards, missing = _select(cfg, args.names)
    if missing:
        _print_json({"status": "unknown-guard", "missing": missing})
        return 2
    results = [engine.status_guard(cfg, guard) for guard in guards]
    if _wants_json(args):
        _print_json({"generated_at": util.now_iso(), "config": cfg["_path"], "guards": [_public(item) for item in results]})
    else:
        _print_status_table(cfg, results)
    return 1 if any(_is_failure(item) for item in results) else 0


def _print_status_table(cfg, results):
    print("config: %s" % cfg["_path"])
    header = "%-16s %-10s %-12s %-8s %-24s %s" % ("NAME", "KIND", "STATE", "HEALTHY", "PROCESS", "DETAIL")
    print(header)
    print("-" * len(header))
    for item in results:
        process = "%s proc(s)" % item.get("process", {}).get("count", 0)
        pids = item.get("process", {}).get("pids") or []
        if pids:
            process = "pid %s" % ",".join(str(pid) for pid in pids[:3])
        age = item.get("process", {}).get("age_sec")
        if age is not None:
            process += " age=%.0fs" % age
        detail = ""
        for probe in item.get("probes", []):
            if not probe.get("ok"):
                detail = probe.get("detail", "")
                break
        if not detail and item.get("probes"):
            detail = item["probes"][0].get("detail", "")
        if item.get("error"):
            detail = item["error"]
        print(
            "%-16s %-10s %-12s %-8s %-24s %s"
            % (
                item.get("name", "?"),
                item.get("mode_kind", "-"),
                item.get("status") or item.get("state", "-"),
                "yes" if item.get("healthy") else "no",
                process[:24],
                detail[:90],
            )
        )


def cmd_start(args):
    cfg = _load(args)
    guards, missing = _select(cfg, args.names)
    if missing:
        _print_json({"status": "unknown-guard", "missing": missing})
        return 2
    journal = _journal(cfg)
    results = []
    for guard in guards:
        name = guard.get("name", "?")
        try:
            with util.FileLock(engine.lock_path(cfg, name)):
                results.append(engine.start_guard(cfg, guard, journal))
        except util.LockBusy:
            results.append({"name": name, "status": "other-run", "healthy": True})
    failed = [item for item in results if _is_failure(item)]
    _print_json({"status": "ok" if not failed else "degraded", "results": [_public(item) for item in results]})
    return 1 if failed else 0


def cmd_stop(args):
    cfg = _load(args)
    guards, missing = _select(cfg, args.names)
    if missing:
        _print_json({"status": "unknown-guard", "missing": missing})
        return 2
    journal = _journal(cfg)
    results = []
    for guard in guards:
        name = guard.get("name", "?")
        try:
            with util.FileLock(engine.lock_path(cfg, name)):
                results.append(engine.stop_guard(cfg, guard, journal))
        except util.LockBusy:
            engine.set_disarmed(cfg, name)
            journal.write("disarm marker written while another operation was running", guard=name)
            results.append({"name": name, "status": "disarm-recorded", "healthy": False, "disarmed": True})
    _print_json({"status": "ok", "results": [_public(item) for item in results]})
    return 0


def cmd_restart(args):
    cfg = _load(args)
    guards, missing = _select(cfg, args.names)
    if missing:
        _print_json({"status": "unknown-guard", "missing": missing})
        return 2
    journal = _journal(cfg)
    results = []
    for guard in guards:
        name = guard.get("name", "?")
        try:
            with util.FileLock(engine.lock_path(cfg, name)):
                engine.stop_guard(cfg, guard, journal)
                results.append(engine.start_guard(cfg, guard, journal))
        except util.LockBusy:
            results.append({"name": name, "status": "other-run", "healthy": True})
    failed = [item for item in results if _is_failure(item)]
    _print_json({"status": "ok" if not failed else "degraded", "results": [_public(item) for item in results]})
    return 1 if failed else 0


def cmd_list(args):
    cfg = _load(args)
    rows = []
    for guard in cfg.get("guards", []):
        kind = "delegated" if guard.get("delegate") else ("observe" if guard.get("observe_only") else "managed")
        rows.append(
            {
                "name": guard.get("name"),
                "kind": kind,
                "enabled": guard.get("enabled", True),
                "disarmed": engine.is_disarmed(cfg, guard.get("name", "")),
                "description": guard.get("description", ""),
            }
        )
    if _wants_json(args):
        _print_json({"guards": rows})
    else:
        for row in rows:
            print(
                "%-16s %-10s %-9s %-9s %s"
                % (
                    row["name"],
                    row["kind"],
                    "enabled" if row["enabled"] else "disabled",
                    "disarmed" if row["disarmed"] else "armed",
                    row["description"],
                )
            )
    return 0


def cmd_validate(args):
    try:
        cfg = _load(args)
    except config_mod.ConfigError as exc:
        _print_json({"status": "invalid", "errors": exc.errors})
        return 2
    _print_json({"status": "valid", "config": cfg["_path"], "guards": [guard.get("name") for guard in cfg.get("guards", [])]})
    return 0


def cmd_logs(args):
    cfg = _load(args)
    journal = _journal(cfg)
    lines = journal.tail(5000)
    if args.name:
        needle = "guard=%s " % args.name
        lines = [line for line in lines if needle in line]
    for line in lines[-int(args.lines):]:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")
    return 0


INIT_TEMPLATE = {
    "state_dir": "%LOCALAPPDATA%\\AgentGuard",
    "log_max_mb": 5,
    "watchdog": {
        "task_name": "AgentGuard-Watchdog",
        "interval_minutes": 3,
        "logon": True,
        "startup_shim": False,
        "description": "Keep AgentGuard-protected services online",
    },
    "guards": [
        {
            "name": "example-http",
            "description": "Observe-only demo: watch a local HTTP service (edit or delete me)",
            "enabled": False,
            "observe_only": True,
            "health": {
                "mode": "all",
                "probes": [
                    {
                        "type": "http",
                        "url": "http://127.0.0.1:11434/api/tags",
                        "status": [200],
                    }
                ],
            },
        },
        {
            "name": "example-service",
            "description": "Managed demo: replace start.command with your service command",
            "enabled": False,
            "identity": {"process_names": ["your-service.exe"], "cmdline_contains": ["your-service"]},
            "health": {
                "mode": "any",
                "young_grace_sec": 120,
                "probes": [{"type": "tcp", "host": "127.0.0.1", "port": 9999}],
            },
            "start": {"command": ["cmd", "/c", "echo replace-me"], "hidden": True},
            "stop": {"kill": True},
            "heal": {"attempts": 3, "healthy_timeout_sec": 90, "backoff_sec": 5, "kill_stale": True},
            "rotate": [],
        },
    ],
}


def cmd_init(args):
    out = os.path.abspath(util.expand(args.out or config_mod.DEFAULT_CONFIG_NAME))
    if os.path.exists(out) and not args.force:
        _print_json({"status": "exists", "config": out, "hint": "use --force to overwrite"})
        return 1
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(INIT_TEMPLATE, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    _print_json({"status": "created", "config": out})
    return 0


def cmd_watchdog(args):
    cfg = _load(args)
    watchdog = cfg.get("watchdog", {})
    task_name = watchdog.get("task_name", "AgentGuard-Watchdog")
    if args.action == "status":
        _print_json(scheduler.task_status(task_name))
        return 0
    if args.action == "remove":
        _print_json(scheduler.remove_task(task_name))
        return 0
    result = scheduler.install_task(
        task_name,
        watchdog.get("description", "Keep AgentGuard-protected services online"),
        _program_path(),
        watchdog.get("interval_minutes", 3),
        watchdog.get("logon", True),
    )
    output = {"watchdog": result}
    if watchdog.get("startup_shim"):
        output["startup_shim"] = scheduler.install_startup_shim(_program_path(), task_name.replace(" ", "-"))
    _print_json(output)
    return 0


def cmd_startup(args):
    name = "AgentGuard"
    if args.action == "status":
        _print_json(scheduler.startup_status(name))
        return 0
    if args.action == "remove":
        _print_json(scheduler.remove_startup_shim(name))
        return 0
    _print_json(scheduler.install_startup_shim(_program_path(), name))
    return 0


def _public(item):
    clean = dict(item)
    clean.pop("log_offsets", None)
    return clean


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", help="path to agentguard.json (default: auto-discover)")

    parser = argparse.ArgumentParser(
        prog="guard",
        description="AgentGuard: boot autostart and self-healing watchdog for local agent/MCP services.",
    )
    parser.add_argument("--version", action="version", version="AgentGuard %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ensure", parents=[common], help="health-check and heal guards (watchdog entry point)")
    p.add_argument("names", nargs="*", help="guard names; default: all")
    p.add_argument("--json", action="store_true", help="accepted for symmetry; ensure always prints JSON")
    p.set_defaults(func=cmd_ensure)

    p = sub.add_parser("status", parents=[common], help="read-only health report")
    p.add_argument("names", nargs="*")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("start", parents=[common], help="clear disarm marker and bring guard(s) online")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", parents=[common], help="disarm guard(s) and stop the service")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("restart", parents=[common], help="stop then start guard(s)")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_restart)

    p = sub.add_parser("list", parents=[common], help="list configured guards")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("validate", parents=[common], help="validate the configuration file")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("logs", parents=[common], help="tail the AgentGuard journal")
    p.add_argument("name", nargs="?", help="filter by guard name")
    p.add_argument("-n", "--lines", type=int, default=50)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("init", help="write a starter agentguard.json")
    p.add_argument("--out", metavar="PATH")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("watchdog", parents=[common], help="install/remove/status the Task Scheduler watchdog")
    p.add_argument("action", choices=["install", "remove", "status"])
    p.set_defaults(func=cmd_watchdog)

    p = sub.add_parser("startup", parents=[common], help="install/remove/status the Startup-folder shim")
    p.add_argument("action", choices=["install", "remove", "status"])
    p.set_defaults(func=cmd_startup)

    return parser


def main(argv=None):
    util.ensure_ascii_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config_mod.ConfigError as exc:
        _print_json({"status": "error", "error": "config", "errors": exc.errors})
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard for pythonw runs
        _print_json({"status": "error", "error": str(exc)})
        return 1
