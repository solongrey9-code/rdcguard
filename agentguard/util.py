"""Shared helpers: path expansion, file locks, journal logging, spawning, killing."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time

WINDOWS = os.name == "nt"


def expand(value):
    if not isinstance(value, str):
        return value
    return os.path.expandvars(os.path.expanduser(value))


def expand_tree(node):
    if isinstance(node, dict):
        return {key: expand_tree(val) for key, val in node.items()}
    if isinstance(node, list):
        return [expand_tree(val) for val in node]
    return expand(node)


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone()


def now_iso():
    return now().isoformat(timespec="seconds")


def parse_iso(text):
    if not text:
        return None
    value_text = str(text).strip()
    try:
        value = dt.datetime.fromisoformat(value_text)
    except ValueError:
        # .NET "o" timestamps carry 7 fractional digits; fromisoformat on
        # Python <=3.10 only accepts 3 or 6. Trim instead of giving up: a
        # missing creation time would disable the young-process grace window.
        import re

        match = re.match(r"^(.*\.\d{6})\d+(.*)$", value_text)
        if not match:
            return None
        try:
            value = dt.datetime.fromisoformat(match.group(1) + match.group(2))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=now().tzinfo)
    return value


def default_state_dir():
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "state")
        return os.path.join(base, "AgentGuard")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "agentguard")


def ensure_ascii_stdio():
    """pythonw.exe and detached tasks have no console; make print() harmless.

    Legacy Windows consoles run a GBK/ANSI code page and crash on emoji or
    exotic characters that leak in from service logs; switch to
    errors='replace' so a watchdog never dies while reporting.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    elif hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    elif hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(errors="replace")
        except (OSError, ValueError):
            pass


class LockBusy(RuntimeError):
    pass


class FileLock:
    """Non-blocking exclusive lock on a lock file.

    Uses msvcrt.locking on Windows and fcntl.flock elsewhere. A second holder
    gets LockBusy immediately instead of queueing, which keeps watchdogs from
    piling up when a previous run is still healing something.
    """

    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            if WINDOWS:
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise LockBusy("lock busy: %s" % self.path) from exc
        self._fh = fh
        return self

    def release(self):
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if WINDOWS:
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


class Journal:
    """Append-only operational log with size-based rotation (path -> path.1)."""

    def __init__(self, path, max_mb=5):
        self.path = path
        self.max_bytes = int(float(max_mb) * 1024 * 1024)

    def write(self, message, guard=None):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._rotate()
            prefix = ("guard=%s " % guard) if guard else ""
            line = "%s %s%s" % (now_iso(), prefix, message)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line.rstrip() + os.linesep)
            return line
        except OSError:
            return ""

    def tail(self, lines=50):
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.readlines()
            return data[-int(lines):]
        except OSError:
            return []

    def _rotate(self):
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > self.max_bytes:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass


def run_capture(command, timeout=30, cwd=None, env=None):
    """Run a command to completion. Returns (returncode|None, stdout, stderr)."""
    kwargs = {
        "capture_output": True,
        "text": True,
        "errors": "replace",
        "timeout": timeout,
    }
    if cwd:
        kwargs["cwd"] = cwd
    if env:
        kwargs["env"] = env
    if WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(command, **kwargs)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return None, "", "timeout"
    except (OSError, ValueError) as exc:
        return None, "", str(exc)


def spawn_detached(command, cwd=None, hidden=True, stdout_path=None):
    """Start a long-lived process detached from the watchdog. Returns its pid."""
    kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if cwd:
        kwargs["cwd"] = cwd
    if WINDOWS:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if hidden:
            flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        kwargs["creationflags"] = flags
    handle = None
    if stdout_path:
        os.makedirs(os.path.dirname(stdout_path), exist_ok=True)
        handle = open(stdout_path, "ab")
        kwargs["stdout"] = handle
        kwargs["stderr"] = subprocess.STDOUT
    else:
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(command, **kwargs)
        return proc.pid
    finally:
        if handle:
            handle.close()


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_pids(pids, timeout=20):
    killed = []
    for pid in pids:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if WINDOWS:
            run_capture(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=timeout)
        else:
            import signal

            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
        killed.append(pid)
    return killed


def wait_pids_gone(pids, timeout=10, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(pid_alive(pid) for pid in pids):
            return True
        time.sleep(interval)
    return not any(pid_alive(pid) for pid in pids)
