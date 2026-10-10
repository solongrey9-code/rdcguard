"""Health probes and process discovery.

Every probe returns a dict: {"ok": bool, "state": str, "detail": str}.

Design constraints learned in production:
- localhost probes must bypass any system/HTTP proxy (a Node 24 ``fetch``
  ignored ``HTTP_PROXY`` until ``NODE_USE_ENV_PROXY=1`` was set; the same class
  of bug hits urllib for 127.0.0.1 if environment proxies leak through);
- process ownership is verified by exact command line when available, and the
  code fails closed (treats the process as not-owned) when it cannot verify;
- log-state probes mirror the original rdc_ensure.ps1 logic: scan a bounded
  tail window or from a recorded byte offset, then take the last state marker.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request

from . import util


class ProcessInfo:
    __slots__ = ("pid", "name", "cmdline", "created")

    def __init__(self, pid, name, cmdline="", created=None):
        self.pid = int(pid)
        self.name = name or ""
        self.cmdline = cmdline or ""
        self.created = created


def probe_tcp(host="127.0.0.1", port=None, timeout_sec=2):
    if not port:
        return {"ok": False, "state": "down", "detail": "no port configured"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(float(timeout_sec))
            code = sock.connect_ex((host, int(port)))
            if code == 0:
                return {"ok": True, "state": "up", "detail": "tcp %s:%s reachable" % (host, port)}
        return {"ok": False, "state": "down", "detail": "tcp %s:%s refused/closed" % (host, port)}
    except OSError as exc:
        return {"ok": False, "state": "down", "detail": "tcp %s:%s error: %s" % (host, port, exc)}


def probe_http(url, timeout_sec=3, status=None, status_lt=None, expect_json=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "AgentGuard/1.0"})
    code = None
    body = b""
    error = ""
    try:
        with opener.open(request, timeout=float(timeout_sec)) as response:
            code = getattr(response, "status", None) or response.getcode()
            if expect_json:
                body = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            if expect_json:
                body = exc.read(1024 * 1024)
        except Exception:  # noqa: BLE001
            body = b""
    except Exception as exc:  # noqa: BLE001 - network stacks raise broadly
        error = str(exc)
    if code is None:
        return {"ok": False, "state": "down", "detail": "http %s no response (%s)" % (url, error or "timeout")}
    if status_lt is not None:
        ok = code < int(status_lt)
    elif status:
        ok = code in [int(item) for item in status]
    else:
        ok = 200 <= code < 400
    detail = "http %s -> %s" % (url, code)
    if ok and expect_json:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return {"ok": False, "state": "bad-json", "detail": "http %s -> %s, body is not JSON" % (url, code)}
        for key, wanted in expect_json.items():
            actual = data.get(key)
            if actual != wanted:
                return {
                    "ok": False,
                    "state": "bad-json",
                    "detail": "http %s -> %s, %s=%r (want %r)" % (url, code, key, actual, wanted),
                }
        detail += " (json ok)"
    return {"ok": ok, "state": "up" if ok else "bad-status", "detail": detail}


def probe_cmd(command, timeout_sec=30, exit_codes=(0,)):
    code, out, err = util.run_capture(command, timeout=timeout_sec)
    ok = code in [int(item) for item in exit_codes]
    detail = "cmd exit=%s" % ("timeout" if code is None else code)
    tail = (err or out).strip()
    if not ok and tail:
        last_line = tail.splitlines()[-1][:160]
        detail += " (%s)" % last_line
    return {"ok": ok, "state": "up" if ok else "down", "detail": detail}


def read_log_state(probe, since_offset=None):
    path = probe.get("path", "")
    if not path or not os.path.isfile(path):
        return {"ok": False, "state": "missing", "detail": "log missing: %s" % path}
    size = os.path.getsize(path)
    start = None
    if probe.get("since") == "last-start" and since_offset is not None:
        try:
            since_offset = int(since_offset)
            if 0 <= since_offset <= size:
                start = since_offset
        except (TypeError, ValueError):
            start = None
    if start is None:
        window = int(probe.get("window_bytes", 4 * 1024 * 1024))
        start = max(0, size - window)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read()
    except OSError as exc:
        return {"ok": False, "state": "missing", "detail": "log unreadable: %s" % exc}
    text = data.decode("utf-8", "replace")
    markers = probe.get("markers", {})
    state = ""
    detail = ""
    for line in text.splitlines():
        lowered = line.lower()
        matched = None
        for candidate, patterns in markers.items():
            if any(pattern.lower() in lowered for pattern in patterns):
                matched = candidate
                break
        if matched is not None:
            state = matched
            detail = line.strip()[:200]
    healthy = state in set(probe.get("healthy", []))
    if not state:
        state = probe.get("empty_state", "unknown")
    return {"ok": healthy, "state": state, "detail": detail or "no marker in scanned window"}


def list_processes(names=None):
    """Snapshot processes (pid, name, cmdline, creation time) via CIM.

    Returns None when the lookup itself failed, so callers can fall back or
    fail closed instead of assuming "no process".
    """
    if os.name != "nt":
        return _list_processes_posix(names)
    filter_expr = ""
    if names:
        clauses = " or ".join("Name='%s'" % name for name in names)
        filter_expr = " -Filter \"(%s)\"" % clauses
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$rows=Get-CimInstance Win32_Process" + filter_expr + " | "
        "ForEach-Object { $c=''; if($_.CommandLine){$c=$_.CommandLine -replace \"[`r`n`t]\",' '}; "
        "$t=''; if($_.CreationDate){$t=$_.CreationDate.ToString('o')}; "
        "('{0}|{1}|{2}|{3}' -f $_.ProcessId,$_.Name,$t,$c) };"
        "$rows -join \"`n\""
    )
    code, out, err = util.run_capture(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=25)
    if code is None:
        return None
    processes = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0].strip())
        except ValueError:
            continue
        created = util.parse_iso(parts[2].strip()) if len(parts) > 2 else None
        cmdline = parts[3].strip() if len(parts) > 3 else ""
        processes.append(ProcessInfo(pid, parts[1].strip(), cmdline, created))
    return processes


def _list_processes_posix(names=None):
    code, out, _ = util.run_capture(["ps", "-eo", "pid=,comm=,args="], timeout=15)
    if code is None:
        return None
    processes = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        name = os.path.basename(parts[1])
        if names and name not in names:
            continue
        processes.append(ProcessInfo(pid, name, parts[2]))
    return processes


def tasklist_processes(names):
    """Fallback snapshot without command lines; only pid/name are trustworthy."""
    code, out, _ = util.run_capture(["tasklist", "/FO", "CSV", "/NH"], timeout=20)
    if code is None:
        return None
    wanted = {name.lower() for name in names} if names else None
    processes = []
    for line in out.splitlines():
        cells = [cell.strip('"') for cell in line.split('","')]
        if len(cells) < 2:
            continue
        name = cells[0].strip('"')
        if wanted and name.lower() not in wanted:
            continue
        try:
            pid = int(cells[1].strip('"'))
        except ValueError:
            continue
        processes.append(ProcessInfo(pid, name))
    return processes


def normalize(text):
    return (text or "").lower().replace("\\", "/")


def match_identity(identity, processes=None):
    """Return owned processes: name matches AND every cmdline needle is present.

    When the command line cannot be read (fallback snapshot) but the identity
    requires needles, the process is treated as NOT owned - fail closed, never
    kill or adopt a process we cannot prove we launched.
    """
    identity = identity or {}
    names = [name.lower() for name in identity.get("process_names", [])]
    needles = [normalize(needle) for needle in identity.get("cmdline_contains", [])]
    if not names:
        return []
    snapshot = processes
    if snapshot is None:
        snapshot = list_processes(names)
        if snapshot is None:
            snapshot = tasklist_processes(names)
        if snapshot is None:
            return []
    owned = []
    for proc in snapshot:
        if proc.name.lower() not in names:
            continue
        if needles:
            cmdline = normalize(proc.cmdline)
            if not cmdline or not all(needle in cmdline for needle in needles):
                continue
        owned.append(proc)
    return owned


def probe_established(owned_pids, timeout_sec=15):
    """True when any owned pid holds an established TCP connection.

    This is the liveness trick from the original RDC watchdog: once a remote
    MCP device is subscribed it keeps an established socket, and that is a far
    more reliable signal than scraping a log that huge tool outputs can flood.
    """
    if not owned_pids:
        return {"ok": False, "state": "no-process", "detail": "no owned pid to inspect"}
    pid_set = set(int(pid) for pid in owned_pids)
    if os.name == "nt":
        code, out, _ = util.run_capture(["netstat", "-ano", "-p", "tcp"], timeout=timeout_sec)
        if code is None:
            return {"ok": False, "state": "unknown", "detail": "netstat failed"}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "ESTABLISHED":
                try:
                    if int(parts[4]) in pid_set:
                        return {"ok": True, "state": "connected", "detail": "established socket for pid %s" % parts[4]}
                except ValueError:
                    continue
        return {"ok": False, "state": "idle", "detail": "no established socket for owned pids"}
    code, out, _ = util.run_capture(["ss", "-tnp", "state", "established"], timeout=timeout_sec)
    if code is None:
        return {"ok": False, "state": "unknown", "detail": "ss failed"}
    for pid in pid_set:
        if ("pid=%s," % pid) in out or ("pid=%s" % pid) in out:
            return {"ok": True, "state": "connected", "detail": "established socket for pid %s" % pid}
    return {"ok": False, "state": "idle", "detail": "no established socket for owned pids"}


def run_probe(probe, owned_pids=None, since_offset=None, processes=None):
    ptype = probe.get("type")
    if ptype == "tcp":
        return probe_tcp(probe.get("host", "127.0.0.1"), probe.get("port"), probe.get("timeout_sec", 2))
    if ptype == "http":
        return probe_http(
            probe.get("url"),
            probe.get("timeout_sec", 3),
            probe.get("status"),
            probe.get("status_lt"),
            probe.get("expect_json"),
        )
    if ptype == "log":
        return read_log_state(probe, since_offset)
    if ptype == "cmd":
        return probe_cmd(probe.get("command"), probe.get("timeout_sec", 30), probe.get("exit_codes", [0]))
    if ptype == "established":
        return probe_established(owned_pids or [], probe.get("timeout_sec", 15))
    if ptype == "process":
        owned = match_identity(probe.get("identity", {}), processes)
        return {
            "ok": bool(owned),
            "state": "running" if owned else "absent",
            "detail": "owned processes: %s" % (",".join(str(p.pid) for p in owned) or "none"),
        }
    return {"ok": False, "state": "unknown", "detail": "unsupported probe type: %s" % ptype}


def wait_tcp(host, port, wait_sec, interval=1.0):
    deadline = time.monotonic() + float(wait_sec)
    while True:
        result = probe_tcp(host, port, timeout_sec=1)
        if result["ok"]:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
