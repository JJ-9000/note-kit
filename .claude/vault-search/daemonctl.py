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

Exit status: start/stop/restart return 0 on success, non-zero on failure.
status returns 0 when the daemon is running (healthy or warming) and 3 when down.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
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


def _health(host: str, port: int, timeout: float = 2.0) -> tuple[str, object]:
    """Probe GET /health. Returns one of 'healthy' (200), 'warming' (503 or other
    HTTP code), or 'down' (no connection), plus the status code for detail."""
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            code = resp.getcode()
            return ("healthy", code) if code == 200 else ("warming", code)
    except urllib.error.HTTPError as exc:        # 503 while warming, etc.
        return "warming", exc.code
    except (urllib.error.URLError, OSError):     # refused / unreachable
        return "down", None


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
            print(f"vault-search: up and healthy at http://{host}:{port}/")
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
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True,
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(10):          # up to ~10s for a graceful stop
                if not _pid_alive(pid):
                    break
                time.sleep(1.0)
            if _pid_alive(pid):
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start, stop, or check the note-kit vault-search daemon."
    )
    parser.add_argument(
        "action", choices=["status", "start", "stop", "restart"],
        help="status: health check; start/stop/restart: control the background daemon.",
    )
    args = parser.parse_args()
    host, port = _read_host_port()
    return {
        "status": cmd_status,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
    }[args.action](host, port)


if __name__ == "__main__":
    sys.exit(main())
