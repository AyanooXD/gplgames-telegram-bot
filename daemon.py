"""
Daemon runner — keeps the bot alive across crashes via an infinite
restart loop. Forks into the background (Unix only).

Usage:
    python3 daemon.py
    # tail -f /tmp/bot_daemon.log
    # kill $(cat /tmp/bot_daemon.pid)  # to stop
"""

import os
import sys
import subprocess
import time
import signal
from pathlib import Path

# Resolve project directory relative to this file so the daemon works
# regardless of where it was launched from.
PROJECT_DIR = Path(__file__).resolve().parent
LOG_FILE = "/tmp/bot_daemon.log"
PID_FILE = "/tmp/bot_daemon.pid"


def _write_pid() -> None:
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def _cleanup_pid(signum=None, frame=None) -> None:
    try:
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)
    except Exception:
        pass
    if signum is not None:
        sys.exit(0)


def main() -> None:
    # Double fork to fully daemonize (Unix only)
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)

    _write_pid()
    signal.signal(signal.SIGTERM, _cleanup_pid)
    signal.signal(signal.SIGINT, _cleanup_pid)

    # Redirect stdio — keep file objects alive so descriptors stay valid
    # for the lifetime of the daemon (closing them would break child output).
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r") as si, open(LOG_FILE, "a") as logf:
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(logf.fileno(), sys.stdout.fileno())
        os.dup2(logf.fileno(), sys.stderr.fileno())

        os.chdir(str(PROJECT_DIR))

        while True:
            proc = subprocess.Popen(
                [sys.executable, "-u", "bot.py"],
                stdout=sys.stdout,
                stderr=sys.stderr,
                cwd=str(PROJECT_DIR),
            )
            proc.wait()
            with open(LOG_FILE, "a") as f:
                f.write(
                    f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"Bot exited with code {proc.returncode}, restarting in 3s...\n"
                )
            time.sleep(3)


if __name__ == "__main__":
    main()
