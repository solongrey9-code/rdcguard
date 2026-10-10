"""Configuration loading, environment expansion and validation.

The config file is plain JSON so a zero-dependency project stays honest.
``%VAR%`` (Windows) and ``$VAR`` / ``~`` (POSIX, and Windows too) are expanded
everywhere in the tree, which keeps public example configs free of usernames.
"""

from __future__ import annotations

import json
import os
import re

from . import util

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PROBE_TYPES = {"tcp", "http", "log", "established", "cmd", "process"}
ALLOWED_HEALTH_MODES = {"all", "any"}
DEFAULT_CONFIG_NAME = "agentguard.json"


class ConfigError(Exception):
    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def find_config(cli_path=None, cwd=None):
    candidates = []
    if cli_path:
        candidates.append(cli_path)
    env_path = os.environ.get("AGENTGUARD_CONFIG")
    if env_path:
        candidates.append(env_path)
    here = cwd or os.getcwd()
    candidates.append(os.path.join(here, DEFAULT_CONFIG_NAME))
    script_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(script_dir)
    candidates.append(os.path.join(root_dir, DEFAULT_CONFIG_NAME))
    for candidate in candidates:
        if candidate and os.path.isfile(util.expand(candidate)):
            return os.path.abspath(util.expand(candidate))
    if cli_path:
        raise ConfigError(["config file not found: %s" % cli_path])
    raise ConfigError(
        [
            "no %s found (looked in cwd, project root, %%AGENTGUARD_CONFIG%%); "
            "pass --config PATH or run `guard init`" % DEFAULT_CONFIG_NAME
        ]
    )


def load_config(path):
    path = os.path.abspath(util.expand(path))
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError(["cannot read config %s: %s" % (path, exc)]) from exc
    config = util.expand_tree(raw)
    errors = validate_config(config)
    if errors:
        raise ConfigError(errors)
    config["_path"] = path
    config["_dir"] = os.path.dirname(path)
    config.setdefault("state_dir", util.default_state_dir())
    config["_state_dir"] = config["state_dir"]
    watchdog = config.setdefault("watchdog", {})
    watchdog.setdefault("task_name", "AgentGuard-Watchdog")
    watchdog.setdefault("interval_minutes", 3)
    watchdog.setdefault("logon", True)
    watchdog.setdefault("startup_shim", False)
    watchdog.setdefault("description", "Keep AgentGuard-protected services online")
    config.setdefault("log_max_mb", 5)
    return config


def guard_names(config):
    return [guard.get("name") for guard in config.get("guards", [])]


def select_guards(config, names):
    guards = config.get("guards", [])
    if not names or "all" in names:
        return guards
    wanted = set(names)
    return [guard for guard in guards if guard.get("name") in wanted]


def validate_config(config):
    errors = []
    if not isinstance(config, dict):
        return ["config root must be a JSON object"]

    state_dir = config.get("state_dir")
    if state_dir is not None and not isinstance(state_dir, str):
        errors.append("state_dir must be a string")

    log_max = config.get("log_max_mb", 5)
    if not isinstance(log_max, (int, float)) or log_max <= 0:
        errors.append("log_max_mb must be a positive number")

    watchdog = config.get("watchdog", {})
    if watchdog and not isinstance(watchdog, dict):
        errors.append("watchdog must be an object")
        watchdog = {}
    interval = watchdog.get("interval_minutes", 3)
    if not isinstance(interval, int) or isinstance(interval, bool) or not 1 <= interval <= 1439:
        errors.append("watchdog.interval_minutes must be an integer between 1 and 1439")
    task_name = watchdog.get("task_name", "AgentGuard-Watchdog")
    if not isinstance(task_name, str) or not task_name.strip():
        errors.append("watchdog.task_name must be a non-empty string")
    if "logon" in watchdog and not isinstance(watchdog["logon"], bool):
        errors.append("watchdog.logon must be a boolean")
    if "startup_shim" in watchdog and not isinstance(watchdog["startup_shim"], bool):
        errors.append("watchdog.startup_shim must be a boolean")

    guards = config.get("guards")
    if not isinstance(guards, list) or not guards:
        errors.append("guards must be a non-empty list")
        return errors

    seen = set()
    for index, guard in enumerate(guards):
        label = "guards[%d]" % index
        if not isinstance(guard, dict):
            errors.append("%s must be an object" % label)
            continue
        name = guard.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append("%s.name must match %s" % (label, NAME_RE.pattern))
            name = label
        elif name in seen:
            errors.append("duplicate guard name: %s" % name)
        else:
            seen.add(name)
        errors.extend(_validate_guard(guard, name))

    return errors


def _validate_guard(guard, name):
    errors = []
    prefix = "guard '%s'" % name

    for flag in ("enabled", "observe_only"):
        if flag in guard and not isinstance(guard[flag], bool):
            errors.append("%s.%s must be a boolean" % (prefix, flag))

    health = guard.get("health")
    if not isinstance(health, dict):
        errors.append("%s.health must be an object" % prefix)
        health = {}
    mode = health.get("mode", "all")
    if mode not in ALLOWED_HEALTH_MODES:
        errors.append("%s.health.mode must be one of %s" % (prefix, sorted(ALLOWED_HEALTH_MODES)))
    grace = health.get("young_grace_sec", 0)
    if not isinstance(grace, (int, float)) or isinstance(grace, bool) or grace < 0:
        errors.append("%s.health.young_grace_sec must be a non-negative number" % prefix)
    probes = health.get("probes")
    if not isinstance(probes, list) or not probes:
        errors.append("%s.health.probes must be a non-empty list" % prefix)
        probes = []
    for pindex, probe in enumerate(probes):
        errors.extend(_validate_probe(probe, "%s.health.probes[%d]" % (prefix, pindex)))

    identity = guard.get("identity")
    if identity is not None:
        if not isinstance(identity, dict):
            errors.append("%s.identity must be an object" % prefix)
        else:
            names = identity.get("process_names")
            if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
                errors.append("%s.identity.process_names must be a non-empty list of process names" % prefix)
            needles = identity.get("cmdline_contains", [])
            if not isinstance(needles, list) or not all(isinstance(n, str) and n for n in needles):
                errors.append("%s.identity.cmdline_contains must be a list of strings" % prefix)

    requires = guard.get("requires", [])
    if not isinstance(requires, list):
        errors.append("%s.requires must be a list" % prefix)
        requires = []
    for rindex, dep in enumerate(requires):
        label = "%s.requires[%d]" % (prefix, rindex)
        if not isinstance(dep, dict):
            errors.append("%s must be an object" % label)
            continue
        if dep.get("type") not in PROBE_TYPES:
            errors.append("%s.type must be one of %s" % (label, sorted(PROBE_TYPES)))
            continue
        errors.extend(_validate_probe(dep, label, dependency=True))
        if "wait_sec" in dep and (not isinstance(dep["wait_sec"], (int, float)) or isinstance(dep["wait_sec"], bool) or dep["wait_sec"] < 0):
            errors.append("%s.wait_sec must be a non-negative number" % label)
        autostart = dep.get("autostart")
        if autostart is not None:
            errors.extend(_validate_action(autostart, "%s.autostart" % label, require_command=True))

    start = guard.get("start")
    if start is not None:
        errors.extend(_validate_action(start, "%s.start" % prefix, require_command=True))
    stop = guard.get("stop")
    if stop is not None:
        if not isinstance(stop, dict):
            errors.append("%s.stop must be an object" % prefix)
        else:
            if "kill" in stop and not isinstance(stop["kill"], bool):
                errors.append("%s.stop.kill must be a boolean" % prefix)
            if "command" in stop:
                errors.extend(_validate_action(stop, "%s.stop" % prefix, require_command=True))

    delegate = guard.get("delegate")
    if delegate is not None:
        if not isinstance(delegate, dict):
            errors.append("%s.delegate must be an object" % prefix)
        else:
            for key in ("ensure_command", "start_command", "stop_command"):
                if key in delegate:
                    errors.extend(
                        _validate_action({"command": delegate[key]}, "%s.delegate.%s" % (prefix, key), require_command=True)
                    )
            if "ensure_command" not in delegate:
                errors.append("%s.delegate.ensure_command is required" % prefix)
            timeout = delegate.get("healthy_timeout_sec")
            if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0):
                errors.append("%s.delegate.healthy_timeout_sec must be a positive number" % prefix)

    heal = guard.get("heal", {})
    if heal and not isinstance(heal, dict):
        errors.append("%s.heal must be an object" % prefix)
        heal = {}
    attempts = heal.get("attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 10:
        errors.append("%s.heal.attempts must be an integer between 1 and 10" % prefix)
    timeout = heal.get("healthy_timeout_sec", 90)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 5 <= timeout <= 3600:
        errors.append("%s.heal.healthy_timeout_sec must be between 5 and 3600" % prefix)
    backoff = heal.get("backoff_sec", 5)
    if not isinstance(backoff, (int, float)) or isinstance(backoff, bool) or not 0 <= backoff <= 300:
        errors.append("%s.heal.backoff_sec must be between 0 and 300" % prefix)
    if "kill_stale" in heal and not isinstance(heal["kill_stale"], bool):
        errors.append("%s.heal.kill_stale must be a boolean" % prefix)

    rotations = guard.get("rotate", [])
    if not isinstance(rotations, list):
        errors.append("%s.rotate must be a list" % prefix)
        rotations = []
    for rindex, entry in enumerate(rotations):
        label = "%s.rotate[%d]" % (prefix, rindex)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry.get("path"):
            errors.append("%s.path is required" % label)
            continue
        max_mb = entry.get("max_mb", 40)
        if not isinstance(max_mb, (int, float)) or isinstance(max_mb, bool) or max_mb <= 0:
            errors.append("%s.max_mb must be a positive number" % label)
        keep = entry.get("keep", 1)
        if not isinstance(keep, int) or isinstance(keep, bool) or keep < 1:
            errors.append("%s.keep must be a positive integer" % label)

    if not guard.get("observe_only") and delegate is None:
        if not (isinstance(start, dict) and start.get("command")):
            errors.append("%s needs start.command, delegate.ensure_command, or observe_only=true" % prefix)

    return errors


def _validate_probe(probe, label, dependency=False):
    errors = []
    if not isinstance(probe, dict):
        return ["%s must be an object" % label]
    ptype = probe.get("type")
    if ptype not in PROBE_TYPES:
        return ["%s.type must be one of %s" % (label, sorted(PROBE_TYPES))]
    if ptype == "tcp":
        port = probe.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            errors.append("%s.port must be an integer between 1 and 65535" % label)
        host = probe.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host:
            errors.append("%s.host must be a non-empty string" % label)
    elif ptype == "http":
        url = probe.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            errors.append("%s.url must start with http:// or https://" % label)
        timeout = probe.get("timeout_sec", 3)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            errors.append("%s.timeout_sec must be a positive number" % label)
        status = probe.get("status")
        if status is not None and (not isinstance(status, list) or not all(isinstance(s, int) and not isinstance(s, bool) for s in status)):
            errors.append("%s.status must be a list of HTTP status codes" % label)
        status_lt = probe.get("status_lt")
        if status_lt is not None and (not isinstance(status_lt, int) or isinstance(status_lt, bool)):
            errors.append("%s.status_lt must be an integer" % label)
        expect_json = probe.get("expect_json")
        if expect_json is not None and (not isinstance(expect_json, dict) or not all(isinstance(key, str) for key in expect_json)):
            errors.append("%s.expect_json must be an object of key: expected-value" % label)
    elif ptype == "log":
        path = probe.get("path")
        if not isinstance(path, str) or not path:
            errors.append("%s.path is required" % label)
        markers = probe.get("markers")
        if not isinstance(markers, dict) or not markers:
            errors.append("%s.markers must map states to marker strings" % label)
        else:
            for state, patterns in markers.items():
                if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
                    errors.append("%s.markers.%s must be a non-empty list of strings" % (label, state))
        healthy = probe.get("healthy")
        if not isinstance(healthy, list) or not healthy:
            errors.append("%s.healthy must be a non-empty list of states" % label)
        window = probe.get("window_bytes", 4 * 1024 * 1024)
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            errors.append("%s.window_bytes must be a positive integer" % label)
        since = probe.get("since", "window")
        if since not in ("window", "last-start"):
            errors.append("%s.since must be 'window' or 'last-start'" % label)
    elif ptype == "cmd":
        command = probe.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
            errors.append("%s.command must be a non-empty argv list" % label)
        timeout = probe.get("timeout_sec", 30)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            errors.append("%s.timeout_sec must be a positive number" % label)
        codes = probe.get("exit_codes", [0])
        if not isinstance(codes, list) or not all(isinstance(c, int) and not isinstance(c, bool) for c in codes):
            errors.append("%s.exit_codes must be a list of integers" % label)
    if dependency and ptype == "established":
        errors.append("%s: 'established' cannot be a dependency probe (no pid context)" % label)
    return errors


def _validate_action(action, label, require_command=False):
    errors = []
    if not isinstance(action, dict):
        return ["%s must be an object" % label]
    command = action.get("command")
    if require_command and (not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command)):
        errors.append("%s.command must be a non-empty argv list" % label)
    if "hidden" in action and not isinstance(action["hidden"], bool):
        errors.append("%s.hidden must be a boolean" % label)
    if "cwd" in action and not isinstance(action["cwd"], str):
        errors.append("%s.cwd must be a string" % label)
    return errors
