"""Guard evaluation and healing engine.

Two protection modes:

managed
    AgentGuard owns the whole lifecycle: it checks health, kills only stale
    processes it can prove it owns (identity match), gates on dependencies,
    runs the start command, waits for health, and retries with backoff.

delegated
    The target already ships its own idempotent ``ensure`` (for example the
    OpenChamber native host's ``host.py ensure``, which keeps its own pid
    ownership checks and operator-stop marker). AgentGuard checks health and
    invokes that command when needed, then verifies with the same probes.

Fail-closed rules inherited from the two production mechanisms:
- never kill a process whose command line does not match the configured
  identity, even if the name matches;
- never adopt or replace a process holding a configured port;
- operator stop markers disarm healing until an explicit start;
- one run at a time (global lock) and one operation per guard (per-guard lock).
"""

from __future__ import annotations

import json
import os
import time

from . import probes, util


def _guard_dir(cfg):
    return os.path.join(cfg["_state_dir"], "guards")


def disarm_marker(cfg, name):
    return os.path.join(_guard_dir(cfg), "%s.disarmed" % name)


def state_file(cfg, name):
    return os.path.join(_guard_dir(cfg), "%s.state.json" % name)


def lock_path(cfg, name):
    return os.path.join(cfg["_state_dir"], "locks", "%s.lock" % name)


def global_lock_path(cfg):
    return os.path.join(cfg["_state_dir"], "ensure.lock")


def load_state(cfg, name):
    try:
        with open(state_file(cfg, name), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(cfg, name, data):
    path = state_file(cfg, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def is_disarmed(cfg, name):
    return os.path.exists(disarm_marker(cfg, name))


def set_disarmed(cfg, name):
    path = disarm_marker(cfg, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("disarmed at %s\n" % util.now_iso())


def clear_disarmed(cfg, name):
    try:
        os.remove(disarm_marker(cfg, name))
    except OSError:
        pass


def _derive_state(guard, probe_configs, results):
    for probe, result in zip(probe_configs, results):
        if probe.get("type") == "log" and not result.get("ok"):
            state = result.get("state")
            if state in probe.get("starting", []):
                return "starting"
            if state in probe.get("failed", []):
                return "failed"
            if state in probe.get("offline", []):
                return "offline"
    return "down"


def evaluate(cfg, guard, stored=None):
    """Full health evaluation. Returns (status, owned_processes)."""
    stored = stored if stored is not None else {}
    name = guard.get("name", "?")
    identity = guard.get("identity") or {}
    owned = probes.match_identity(identity) if identity.get("process_names") else []
    ages = [(util.now() - proc.created).total_seconds() for proc in owned if proc.created]
    offsets = stored.get("log_offsets", {})
    probe_configs = (guard.get("health") or {}).get("probes", [])
    results = []
    for probe in probe_configs:
        since_offset = offsets.get(probe.get("path")) if probe.get("type") == "log" else None
        result = probes.run_probe(probe, [proc.pid for proc in owned], since_offset)
        result["type"] = probe.get("type")
        results.append(result)
    mode = (guard.get("health") or {}).get("mode", "all")
    if mode == "any":
        healthy = any(result["ok"] for result in results)
    else:
        healthy = all(result["ok"] for result in results)
    status = {
        "name": name,
        "healthy": bool(healthy),
        "state": "healthy" if healthy else _derive_state(guard, probe_configs, results),
        "mode": mode,
        "probes": results,
        "process": {
            "count": len(owned),
            "pids": [proc.pid for proc in owned],
            "age_sec": round(min(ages), 1) if ages else None,
        },
        "disarmed": is_disarmed(cfg, name),
        "checked_at": util.now_iso(),
    }
    return status, owned


def _evaluate_quick(cfg, guard, stored):
    """Cheap probes only (no process snapshot); returns None when nothing cheap."""
    probe_configs = (guard.get("health") or {}).get("probes", [])
    cheap = [probe for probe in probe_configs if probe.get("type") not in ("process", "established")]
    if not cheap:
        return None
    offsets = stored.get("log_offsets", {})
    results = []
    for probe in cheap:
        since_offset = offsets.get(probe.get("path")) if probe.get("type") == "log" else None
        result = probes.run_probe(probe, None, since_offset)
        result["type"] = probe.get("type")
        results.append(result)
    mode = (guard.get("health") or {}).get("mode", "all")
    healthy = any(result["ok"] for result in results) if mode == "any" else all(result["ok"] for result in results)
    return {
        "name": guard.get("name", "?"),
        "healthy": bool(healthy),
        "state": "healthy" if healthy else _derive_state(guard, cheap, results),
        "mode": mode,
        "probes": results,
        "checked_at": util.now_iso(),
    }


def rotate_guard_logs(cfg, guard, journal):
    for entry in guard.get("rotate", []) or []:
        path = entry.get("path")
        if not path or not os.path.isfile(path):
            continue
        max_bytes = int(float(entry.get("max_mb", 40)) * 1024 * 1024)
        keep = int(entry.get("keep", 1))
        try:
            size = os.path.getsize(path)
            if size <= max_bytes:
                continue
            for index in range(keep, 1, -1):
                older = "%s.%d" % (path, index - 1)
                if os.path.exists(older):
                    os.replace(older, "%s.%d" % (path, index))
            os.replace(path, path + ".1")
            journal.write(
                "rotated %s (%.1fMB -> .1)" % (os.path.basename(path), size / 1024.0 / 1024.0),
                guard=guard.get("name"),
            )
        except OSError as exc:
            journal.write("rotation failed for %s: %s" % (path, exc), guard=guard.get("name"))


def _ensure_dependencies(cfg, guard, journal):
    for dep in guard.get("requires", []) or []:
        ptype = dep.get("type")
        if ptype == "tcp":
            result = probes.probe_tcp(dep.get("host", "127.0.0.1"), dep.get("port"), dep.get("timeout_sec", 2))
        elif ptype == "http":
            result = probes.probe_http(
                dep.get("url"), dep.get("timeout_sec", 3), dep.get("status"), dep.get("status_lt"), dep.get("expect_json")
            )
        elif ptype == "cmd":
            result = probes.probe_cmd(dep.get("command"), dep.get("timeout_sec", 30), dep.get("exit_codes", [0]))
        elif ptype == "process":
            owned = probes.match_identity(dep.get("identity") or {})
            result = {"ok": bool(owned), "detail": "process dep: %s" % len(owned)}
        else:
            result = {"ok": False, "detail": "unsupported dependency type: %s" % ptype}
        if result["ok"]:
            continue
        label = dep.get("name") or dep.get("url") or ("tcp:%s" % dep.get("port"))
        autostart = dep.get("autostart")
        if autostart and autostart.get("command"):
            journal.write("dependency '%s' down; autostarting" % label, guard=guard.get("name"))
            try:
                util.spawn_detached(
                    autostart["command"],
                    cwd=autostart.get("cwd"),
                    hidden=autostart.get("hidden", True),
                    stdout_path=autostart.get("stdout_log"),
                )
            except OSError as exc:
                journal.write("dependency autostart failed: %s" % exc, guard=guard.get("name"))
        elif result.get("detail"):
            journal.write("dependency '%s' down: %s" % (label, result.get("detail")), guard=guard.get("name"))
        wait_sec = float(dep.get("wait_sec", 30))
        deadline = time.monotonic() + wait_sec
        ready = False
        while True:
            time.sleep(1)
            if ptype == "tcp":
                ready = probes.probe_tcp(dep.get("host", "127.0.0.1"), dep.get("port"), timeout_sec=1)["ok"]
            elif ptype == "http":
                ready = probes.probe_http(
                    dep.get("url"), 2, dep.get("status"), dep.get("status_lt"), dep.get("expect_json")
                )["ok"]
            elif ptype == "cmd":
                ready = probes.probe_cmd(dep.get("command"), dep.get("timeout_sec", 30), dep.get("exit_codes", [0]))["ok"]
            elif ptype == "process":
                ready = bool(probes.match_identity(dep.get("identity") or {}))
            if ready or time.monotonic() >= deadline:
                break
        if not ready:
            return "dependency '%s' unavailable after %ss" % (label, wait_sec)
        journal.write("dependency '%s' ready" % label, guard=guard.get("name"))
    return None


def _snapshot_offsets(guard, stored):
    offsets = stored.setdefault("log_offsets", {})
    for probe in (guard.get("health") or {}).get("probes", []):
        if probe.get("type") == "log" and probe.get("since") == "last-start":
            path = probe.get("path")
            try:
                offsets[path] = os.path.getsize(path) if os.path.isfile(path) else 0
            except OSError:
                offsets[path] = 0
    return stored


def _wait_healthy(cfg, guard, stored, timeout_sec):
    deadline = time.monotonic() + float(timeout_sec)
    last = None
    last_full = 0.0
    while True:
        time.sleep(2)
        quick = _evaluate_quick(cfg, guard, stored)
        if quick is not None:
            last = quick
            if quick["healthy"]:
                full, _ = evaluate(cfg, guard, load_state(cfg, guard.get("name", "")))
                if full["healthy"]:
                    return True, full
            elif quick["state"] in ("failed", "offline"):
                return False, quick
        now = time.monotonic()
        if quick is None or now - last_full >= 6:
            full, _ = evaluate(cfg, guard, load_state(cfg, guard.get("name", "")))
            last = full
            last_full = now
            if full["healthy"]:
                return True, full
            if full["state"] in ("failed", "offline"):
                return False, full
        if time.monotonic() >= deadline:
            if last is None:
                last, _ = evaluate(cfg, guard, load_state(cfg, guard.get("name", "")))
            return False, last


def _short(command):
    if not command:
        return "(none)"
    parts = [os.path.basename(str(part)) for part in command[:2]]
    return " ".join(parts)


def ensure_guard(cfg, guard, journal, force=False):
    name = guard.get("name", "?")
    if not guard.get("enabled", True):
        return {"name": name, "status": "disabled", "healthy": False}
    stored = load_state(cfg, name)
    status, owned = evaluate(cfg, guard, stored)
    if is_disarmed(cfg, name) and not force:
        status["status"] = "disarmed"
        return status
    rotate_guard_logs(cfg, guard, journal)
    if status["healthy"]:
        stored["last_healthy"] = util.now_iso()
        save_state(cfg, name, stored)
        status["status"] = "healthy"
        return status

    age = status["process"].get("age_sec")
    grace = float((guard.get("health") or {}).get("young_grace_sec", 0))
    if owned and age is not None and age < grace and not force:
        status["status"] = "starting"
        return status

    if guard.get("observe_only"):
        status["status"] = "observe-only"
        return status

    delegate = guard.get("delegate") or {}
    managed = not delegate
    heal = guard.get("heal", {}) or {}

    if managed and owned:
        if not heal.get("kill_stale", True):
            status["status"] = "stale"
            return status
        pids = [proc.pid for proc in owned]
        journal.write("killing %d stale owned process(es) %s (state=%s)" % (len(pids), ",".join(map(str, pids)), status["state"]), guard=name)
        util.kill_pids(pids)
        util.wait_pids_gone(pids, timeout=10)

    dep_error = _ensure_dependencies(cfg, guard, journal)
    if dep_error:
        status["status"] = "dependency-unavailable"
        status["error"] = dep_error
        journal.write(dep_error, guard=name)
        return status

    attempts = int(heal.get("attempts", 3))
    timeout = float(delegate.get("healthy_timeout_sec") or heal.get("healthy_timeout_sec", 90))
    backoff = float(heal.get("backoff_sec", 5))

    for attempt in range(1, attempts + 1):
        stored = _snapshot_offsets(guard, load_state(cfg, name))
        save_state(cfg, name, stored)
        if delegate:
            action = delegate.get("ensure_command")
            cwd = delegate.get("cwd")
            hidden = True
        else:
            start = guard.get("start") or {}
            action = start.get("command")
            cwd = start.get("cwd")
            hidden = start.get("hidden", True)
        if not action:
            status["status"] = "no-action"
            return status
        journal.write("starting (attempt %d/%d): %s" % (attempt, attempts, _short(action)), guard=name)
        if delegate:
            code, _, err = util.run_capture(action, timeout=timeout * 2 + 30, cwd=cwd)
            journal.write("delegate ensure exit=%s%s" % (code, (" (%s)" % err.strip()[-160:]) if code not in (0,) and err else ""), guard=name)
        else:
            try:
                util.spawn_detached(action, cwd=cwd, hidden=hidden, stdout_path=(guard.get("start") or {}).get("stdout_log"))
            except OSError as exc:
                journal.write("start command failed: %s" % exc, guard=name)
        healed, healed_status = _wait_healthy(cfg, guard, stored, timeout)
        if healed:
            final = load_state(cfg, name)
            final["last_healthy"] = util.now_iso()
            save_state(cfg, name, final)
            healed_status["status"] = "recovered"
            healed_status["attempt"] = attempt
            journal.write("recovered on attempt %d" % attempt, guard=name)
            return healed_status
        journal.write("attempt %d failed (state=%s)" % (attempt, healed_status.get("state")), guard=name)
        if managed:
            leftover = probes.match_identity(guard.get("identity") or {})
            if leftover:
                pids = [proc.pid for proc in leftover]
                util.kill_pids(pids)
                util.wait_pids_gone(pids, timeout=8)
        if attempt < attempts and backoff:
            time.sleep(backoff)

    status, _ = evaluate(cfg, guard, load_state(cfg, name))
    status["status"] = "failed"
    journal.write("NOT recovered after %d attempts" % attempts, guard=name)
    return status


def stop_guard(cfg, guard, journal):
    name = guard.get("name", "?")
    set_disarmed(cfg, name)
    delegate = guard.get("delegate") or {}
    stop = guard.get("stop") or {}
    if delegate.get("stop_command"):
        code, _, err = util.run_capture(delegate["stop_command"], timeout=90)
        journal.write("delegate stop exit=%s%s" % (code, (" (%s)" % err.strip()[-160:]) if err else ""), guard=name)
    elif stop.get("command"):
        code, _, err = util.run_capture(stop["command"], timeout=90)
        journal.write("stop command exit=%s%s" % (code, (" (%s)" % err.strip()[-160:]) if err else ""), guard=name)
    if not delegate and stop.get("kill", True) and guard.get("identity"):
        owned = probes.match_identity(guard.get("identity") or {})
        if owned:
            pids = [proc.pid for proc in owned]
            util.kill_pids(pids)
            util.wait_pids_gone(pids, timeout=10)
            journal.write("stopped and killed owned process(es) %s" % ",".join(map(str, pids)), guard=name)
    status, _ = evaluate(cfg, guard, load_state(cfg, name))
    status["status"] = "stopped"
    return status


def start_guard(cfg, guard, journal):
    name = guard.get("name", "?")
    clear_disarmed(cfg, name)
    journal.write("operator start (disarm cleared)", guard=name)
    return ensure_guard(cfg, guard, journal, force=True)


def status_guard(cfg, guard):
    stored = load_state(cfg, guard.get("name", ""))
    status, owned = evaluate(cfg, guard, stored)
    status["enabled"] = guard.get("enabled", True)
    status["observe_only"] = bool(guard.get("observe_only"))
    status["mode_kind"] = "delegated" if guard.get("delegate") else ("observe" if guard.get("observe_only") else "managed")
    status["last_healthy"] = stored.get("last_healthy")
    status["description"] = guard.get("description", "")
    if not status["enabled"]:
        status["status"] = "disabled"
    elif status["healthy"]:
        status["status"] = "healthy"
    elif status["disarmed"]:
        status["status"] = "disarmed"
    elif status.get("observe_only"):
        status["status"] = "observe-only"
    else:
        status["status"] = status["state"]
    return status


def ensure_all(cfg, guards, journal):
    results = []
    with util.FileLock(global_lock_path(cfg)):
        for guard in guards:
            name = guard.get("name", "?")
            try:
                with util.FileLock(lock_path(cfg, name)):
                    results.append(ensure_guard(cfg, guard, journal))
            except util.LockBusy:
                results.append({"name": name, "status": "other-run", "healthy": True})
    return results
