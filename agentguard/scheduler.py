"""Windows Task Scheduler and Startup-folder installation.

The task XML mirrors the battle-tested shape of the original RDC watchdog:
logon trigger plus minute repetition, ``IgnoreNew`` so a slow heal never piles
up, ``StartWhenAvailable`` for missed runs, and the interactive user so the
hidden ``pythonw.exe`` action never flashes a console.
"""

from __future__ import annotations

import os
import sys
import tempfile

from . import util


def python_hidden():
    """Prefer pythonw.exe next to the current interpreter (no console window)."""
    exe = os.path.abspath(sys.executable)
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else exe


def user_id():
    code, out, _ = util.run_capture(["whoami"], timeout=10)
    if code == 0 and out.strip():
        return out.strip()
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return ("%s\\%s" % (domain, user)) if domain else user


def build_task_xml(task_name, description, owner, command, arguments, interval_minutes, logon=True, start_boundary=None):
    start_boundary = start_boundary or util.now().replace(microsecond=0).isoformat()
    logon_block = ""
    if logon:
        logon_block = "<LogonTrigger><UserId>%s</UserId></LogonTrigger>" % _escape(owner)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>%s</Description>\n"
        "    <URI>\\%s</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <UserId>%s</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>\n"
        "  </Settings>\n"
        "  <Triggers>\n"
        "    %s\n"
        "    <TimeTrigger>\n"
        "      <StartBoundary>%s</StartBoundary>\n"
        "      <Repetition>\n"
        "        <Interval>PT%dM</Interval>\n"
        "        <StopAtDurationEnd>true</StopAtDurationEnd>\n"
        "      </Repetition>\n"
        "    </TimeTrigger>\n"
        "  </Triggers>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        "      <Command>%s</Command>\n"
        "      <Arguments>%s</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
        % (
            _escape(description),
            _escape(task_name),
            _escape(owner),
            logon_block,
            _escape(start_boundary),
            int(interval_minutes),
            _escape(command),
            _escape(arguments),
        )
    )


def _escape(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def task_exists(task_name):
    code, _, _ = util.run_capture(["schtasks", "/Query", "/TN", task_name], timeout=15)
    return code == 0


def install_task(task_name, description, guard_py, interval_minutes, logon=True):
    if os.name != "nt":
        raise RuntimeError("Task Scheduler install is Windows-only; use cron on this platform")
    if task_exists(task_name):
        return {"status": "already-installed", "task": task_name}
    owner = user_id()
    command = python_hidden()
    arguments = '"%s" ensure' % os.path.abspath(guard_py)
    xml = build_task_xml(task_name, description, owner, command, arguments, interval_minutes, logon)
    handle, path = tempfile.mkstemp(suffix=".xml", prefix="agentguard-task-")
    os.close(handle)
    try:
        with open(path, "w", encoding="utf-16") as fh:
            fh.write(xml)
        code, out, err = util.run_capture(["schtasks", "/Create", "/TN", task_name, "/XML", path], timeout=30)
        if code != 0:
            raise RuntimeError("schtasks create failed: %s" % (err or out).strip())
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return {"status": "installed", "task": task_name, "interval_minutes": int(interval_minutes), "logon": bool(logon)}


def remove_task(task_name):
    if not task_exists(task_name):
        return {"status": "not-installed", "task": task_name}
    code, out, err = util.run_capture(["schtasks", "/Delete", "/TN", task_name, "/F"], timeout=20)
    if code != 0:
        raise RuntimeError("schtasks delete failed: %s" % (err or out).strip())
    return {"status": "removed", "task": task_name}


def task_status(task_name):
    return {"status": "installed" if task_exists(task_name) else "not-installed", "task": task_name}


def startup_dir():
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def startup_shim_path(name="AgentGuard"):
    return os.path.join(startup_dir(), "%s.vbs" % name)


def build_startup_vbs(guard_py, runner=None):
    runner = runner or python_hidden()
    lines = [
        'Set WshShell = CreateObject("WScript.Shell")',
        'WshShell.Run """%s"" ""%s"" ensure", 0, False' % (runner, os.path.abspath(guard_py)),
    ]
    return "\r\n".join(lines) + "\r\n"


def install_startup_shim(guard_py, name="AgentGuard"):
    path = startup_shim_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii", newline="") as fh:
        fh.write(build_startup_vbs(guard_py))
    return {"status": "installed", "shim": path}


def remove_startup_shim(name="AgentGuard"):
    path = startup_shim_path(name)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as exc:
            raise RuntimeError("cannot remove shim: %s" % exc) from exc
        return {"status": "removed", "shim": path}
    return {"status": "not-installed", "shim": path}


def startup_status(name="AgentGuard"):
    path = startup_shim_path(name)
    return {"status": "installed" if os.path.exists(path) else "not-installed", "shim": path}
