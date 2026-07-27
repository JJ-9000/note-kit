#!/usr/bin/env python3
"""
daemonctl.py
============

Start, stop, and check the note-kit vault-search daemon with one short command,
instead of the long venv-python path plus Ctrl-C. Cross-platform, standard-
library only.

The daemon lives at <vault>/.claude/vault-search/. Run install_daemon.py once
first (it builds the venv this script launches). For always-on auto-launch at
login, prefer an OS service (NSSM / launchd / systemd); see vault-search/README.md.
This helper is for the start-it-when-I-want-it case: it launches the daemon in
the background, records its PID, and can stop that instance again.

Usage:
    python daemonctl.py status     # is it up? (health check)
    python daemonctl.py start      # launch it in the background
    python daemonctl.py stop       # stop the instance this helper started
    python daemonctl.py restart    # stop, then start
    python daemonctl.py watchdog   # restart if unhealthy; confirm the live PID

Exit status: start/stop/restart/watchdog return 0 on success, non-zero on
failure. status returns 0 when the daemon is running (healthy or warming) and
3 when down.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# This script lives at <vault>/.claude/vault-search/daemonctl.py.
_DAEMON_DIR = Path(__file__).resolve().parent
_VENV_DIR = _DAEMON_DIR / ".venv"
_SERVER = _DAEMON_DIR / "server.py"
_CONFIG = _DAEMON_DIR / "config.yaml"
_DATA_DIR = _DAEMON_DIR / "data"
_PID_FILE = _DATA_DIR / "daemon.pid"
_OUT_LOG = _DATA_DIR / "daemon.out.log"   # stdout/stderr of the launched process

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765


def _venv_python() -> Path:
    """Path to the daemon's venv interpreter, per OS layout (matches the installer)."""
    if os.name == "nt":
        return _VENV_DIR / "Scripts" / "python.exe"
    return _VENV_DIR / "bin" / "python"


def _read_host_port() -> tuple[str, int]:
    """Read top-level host/port from config.yaml with a minimal scan (no PyYAML).

    Only the unindented top-level `host:` and `port:` are read; that is all this
    helper needs. Falls back to 127.0.0.1:8765 if the file or keys are absent.
    """
    host, port = _DEFAULT_HOST, _DEFAULT_PORT
    if _CONFIG.exists():
        for line in _CONFIG.read_text(encoding="utf-8").splitlines():
            m = re.match(r'host:\s*"?([^"#\s]+)"?', line)
            if m:
                host = m.group(1)
            m = re.match(r'port:\s*"?(\d+)"?', line)
            if m:
                port = int(m.group(1))
    return host, port


def _port_connectable(host: str, port: int, timeout: float = 1.5) -> bool:
    """True if a TCP connection to host:port completes the handshake.

    A listening process — even a hung one that never answers HTTP — accepts the
    connection, so this returns True. A free port does not: it either refuses
    (RST) or, behind a drop-style firewall, has the SYN silently dropped and
    times out. Both non-connect outcomes mean 'nothing is holding the port',
    which is what distinguishes a genuinely-down daemon from a hung orphan far
    more reliably than trying to tell connection-refused from timeout (a free
    port times out rather than refuses on a drop-configured Windows firewall).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _health(host: str, port: int, timeout: float = 2.0) -> tuple[str, object]:
    """Probe the daemon's health. Returns one of 'healthy' (200), 'warming' (503
    or other HTTP code), 'unhealthy' (a process holds the port but does not
    answer /health — a hung orphan), or 'down' (nothing is listening), plus a
    detail value.

    Leads with a raw TCP-connect probe so a free port (refused OR firewall-
    dropped) reads 'down' and only a port a process actually holds can read
    'unhealthy' — the watchdog must never start a second instance into a
    bind failure, nor refuse to start over a genuinely free port."""
    if not _port_connectable(host, port, timeout=min(timeout, 1.5)):
        return "down", None
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            code = resp.getcode()
            return ("healthy", code) if code == 200 else ("warming", code)
    except urllib.error.HTTPError as exc:        # 503 while warming, etc.
        return "warming", exc.code
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
        # The port accepted the connection but did not answer /health — a hung
        # or wedged process is holding it.
        return "unhealthy", "no-response"


def _listening_pid(port: int) -> int | None:
    """Best-effort PID LISTENING on `port`, from netstat (Windows) or lsof
    (POSIX). Returns None if it cannot be resolved."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in out.splitlines():
                parts = line.split()
                if (len(parts) >= 5 and parts[0].upper() == "TCP"
                        and parts[1].endswith(f":{port}")
                        and parts[3].upper() == "LISTENING"):
                    return int(parts[4])
        else:
            out = subprocess.run(
                ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if out:
                return int(out.splitlines()[0])
    except Exception:
        return None
    return None


def _readiness(host: str, port: int, timeout: float = 3.0) -> tuple[bool, int | None]:
    """Read GET /health and return (healthy, reported_pid).

    `healthy` is True only on HTTP 200 (warmed up). `reported_pid` is the daemon
    process's own PID from the /health body — the marker that confirms a probe
    reached the intended live process, not a stale orphan holding the port.
    Returns (False, None) if the daemon is down or the body has no pid.
    """
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:        # warming (503) still has a body
        code = exc.code
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            body = ""
    except (urllib.error.URLError, OSError):
        return False, None
    pid: int | None = None
    try:
        raw_pid = json.loads(body).get("pid")
        pid = int(raw_pid) if raw_pid is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        pid = None
    return code == 200, pid


def _request_shutdown(host: str, port: int, timeout: float = 3.0) -> bool:
    """Ask the daemon to close its store and exit via POST /shutdown.

    The cross-platform graceful path: the daemon closes its SQLite store before
    exiting instead of being force-killed. Returns True if the daemon accepted
    the request. Falls through to a force kill in cmd_stop when it does not.
    """
    url = f"http://{host}:{port}/shutdown"
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode() in (200, 202)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 202)
    except (urllib.error.URLError, OSError):
        return False


def _read_pid() -> int | None:
    if _PID_FILE.exists():
        try:
            return int(_PID_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None
    return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_status(host: str, port: int) -> int:
    state, detail = _health(host, port)
    if state == "healthy":
        print(f"vault-search: running and healthy at http://{host}:{port}/")
        return 0
    if state == "warming":
        print(f"vault-search: starting up (health returned {detail}); give it a moment.")
        return 0
    if state == "unhealthy":
        held = _listening_pid(port)
        print(
            f"vault-search: {host}:{port} is held but not responding ({detail}); "
            f"pid {held if held is not None else 'unresolved'}. Likely a hung orphan — "
            "stop or kill it before starting a new instance."
        )
        return 3
    print(f"vault-search: not running (no response on {host}:{port}).")
    return 3


def cmd_start(host: str, port: int) -> int:
    state, _ = _health(host, port)
    if state != "down":
        print(f"vault-search is already running ({state}). Nothing to do.")
        return 0

    py = _venv_python()
    if not py.exists():
        print(
            f"vault-search: venv interpreter not found at {py}.\n"
            "Run the installer first, from the vault root:\n"
            "    python .claude/vault-search/install_daemon.py",
            file=sys.stderr,
        )
        return 1

    # Clear a stale PID file from a previous instance that has since died.
    old = _read_pid()
    if old and not _pid_alive(old):
        _PID_FILE.unlink(missing_ok=True)

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = open(_OUT_LOG, "a", encoding="utf-8")
    popen_kwargs: dict = dict(
        cwd=str(_DAEMON_DIR),
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=out,
    )
    if os.name == "nt":
        # Detach so the daemon survives this helper exiting and closing its console.
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen([str(py), str(_SERVER)], **popen_kwargs)
    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"vault-search: launched in the background (pid {proc.pid}). Warming up...")

    # First start downloads a ~90 MB model and builds the index, so allow time.
    deadline = time.time() + 90.0
    while time.time() < deadline:
        if proc.poll() is not None:
            print(
                f"vault-search: process exited early (code {proc.returncode}). "
                f"Check {_OUT_LOG} and {_DATA_DIR / 'daemon.log'}.",
                file=sys.stderr,
            )
            _PID_FILE.unlink(missing_ok=True)
            return 1
        if _health(host, port)[0] == "healthy":
            # The pre-launch down-check above guarantees no orphan was serving,
            # so whatever answers now is the process we started. Report the pid
            # /health returns — on Windows the venv launcher (proc.pid) spawns
            # the real server as a child, so the served pid is the authoritative
            # live process, confirmed by the readiness probe.
            _, reported_pid = _readiness(host, port)
            live = reported_pid if reported_pid is not None else proc.pid
            print(f"vault-search: up and healthy at http://{host}:{port}/ (pid {live}).")
            return 0
        time.sleep(2.0)

    print(
        f"vault-search: started (pid {proc.pid}) but not healthy after 90s. "
        f"It may still be downloading the model or building the index; "
        f"re-check with `status`, or read {_DATA_DIR / 'daemon.log'}."
    )
    return 0


def _service_hint() -> str:
    return (
        "If you installed it to start at login, stop it through its service:\n"
        "  Windows (NSSM): C:\\Tools\\nssm\\nssm.exe stop vault-search\n"
        "  macOS:          launchctl unload ~/Library/LaunchAgents/com.note-kit.vault-search.plist\n"
        "  Linux:          systemctl --user stop vault-search"
    )


def cmd_stop(host: str, port: int) -> int:
    pid = _read_pid()
    if pid and _pid_alive(pid):
        print(f"vault-search: stopping pid {pid}...")
        # 1. Graceful: ask the daemon to close its store and exit cleanly. This
        #    is the cross-platform path — on Windows a detached daemon has no
        #    deliverable SIGTERM, so /shutdown is the only clean stop.
        if _request_shutdown(host, port):
            print("vault-search: graceful shutdown requested; waiting for store close...")
            for _ in range(8):
                if not _pid_alive(pid):
                    break
                time.sleep(1.0)
        # 2. POSIX SIGTERM as a second graceful signal if still alive.
        if _pid_alive(pid) and os.name != "nt":
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(5):
                if not _pid_alive(pid):
                    break
                time.sleep(1.0)
        # 3. Force kill only as a last resort.
        if _pid_alive(pid):
            print("vault-search: graceful stop did not complete in time; forcing.",
                  file=sys.stderr)
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True,
                )
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        _PID_FILE.unlink(missing_ok=True)
        print("vault-search: stopped.")
        return 0

    # No live instance recorded by this helper.
    _PID_FILE.unlink(missing_ok=True)
    if _health(host, port)[0] != "down":
        print(
            "vault-search is running, but not from this helper (no PID on file).\n"
            + _service_hint(),
            file=sys.stderr,
        )
        return 1
    print("vault-search: not running.")
    return 0


def cmd_restart(host: str, port: int) -> int:
    cmd_stop(host, port)
    time.sleep(1.0)
    return cmd_start(host, port)


def cmd_watchdog(host: str, port: int) -> int:
    """Health-check the daemon and restart it only when it is genuinely down,
    then confirm the restart reached a live process by its reported PID.

    A liveness-and-recovery entry point for a scheduled check: a healthy or
    warming daemon is left alone; a down one (connection refused) is restarted
    and readiness is reconfirmed against the fresh PID; a port that is held but
    unresponsive (a hung orphan) is reported and left for manual intervention —
    never restarted over, since a second instance would bind-fail.
    Returns 0 when the daemon ends up healthy or is legitimately warming,
    non-zero otherwise.
    """
    state, detail = _health(host, port)
    if state == "healthy":
        _, reported_pid = _readiness(host, port)
        print(f"vault-search: healthy at http://{host}:{port}/ (pid {reported_pid}). No action.")
        return 0
    if state == "warming":
        print(f"vault-search: warming up (health {detail}); giving it time, no restart.")
        return 0
    if state == "unhealthy":
        # A port that accepts the connection but never answers is a hung orphan,
        # not a free port — starting a second instance would bind-fail. Report
        # and refuse rather than restart.
        held = _listening_pid(port)
        print(
            f"vault-search: {host}:{port} is held but not responding ({detail}); "
            f"pid {held if held is not None else 'unresolved'}. Not starting a second "
            "instance over a hung orphan — stop or kill it first, then retry.",
            file=sys.stderr,
        )
        return 1
    # state == "down": nothing is listening — safe to restart.
    print("vault-search: down — restarting via watchdog...")
    cmd_stop(host, port)
    # Confirm the port is actually free before starting, so a restart never
    # ends up confirming a stale orphan that survived the stop.
    for _ in range(8):
        if _health(host, port)[0] == "down":
            break
        time.sleep(1.0)
    if _health(host, port)[0] != "down":
        print(
            f"vault-search: {host}:{port} still answering after stop; not "
            "restarting over a possible orphan. Investigate manually.",
            file=sys.stderr,
        )
        return 1
    rc = cmd_start(host, port)
    if rc != 0:
        return rc
    healthy, reported_pid = _readiness(host, port)
    if healthy and reported_pid is not None:
        print(f"vault-search: watchdog confirmed live pid {reported_pid}.")
        return 0
    print(
        "vault-search: watchdog restart did not reach a healthy pid; "
        f"check {_DATA_DIR / 'daemon.log'}.",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start, stop, or check the note-kit vault-search daemon."
    )
    parser.add_argument(
        "action", choices=["status", "start", "stop", "restart", "watchdog"],
        help="status: health check; start/stop/restart: control the background "
             "daemon; watchdog: restart it if unhealthy and confirm the live PID.",
    )
    args = parser.parse_args()
    host, port = _read_host_port()
    return {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "watchdog": cmd_watchdog,
    }[args.action](host, port)


if __name__ == "__main__":
    sys.exit(main())
