#!/usr/bin/env python3
"""
Dev runner — starts the API server and Dramatiq worker together.

Usage:
    python run.py            # start both
    python run.py api        # API only
    python run.py worker     # worker only
    python run.py restart    # kill existing processes and start fresh

Both processes share stdout/stderr with a colored prefix so you can
follow both logs in one terminal. Ctrl+C stops both cleanly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).parent
PYTHON = str(ROOT / ".venv312" / "bin" / "python3.12")

COMMANDS = {
    "api": [
        PYTHON, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--workers", "1",
        "--log-level", "info",
    ],
    "worker": [
        PYTHON, "-m", "dramatiq", "app.jobs.actors",
        "--processes", "1",
        "--threads", "4",
        "--queues", "scans", "webhooks",
    ],
}

# ANSI colours for log prefixes
COLOURS = {"api": "\033[36m", "worker": "\033[33m"}  # cyan / yellow
RESET = "\033[0m"


def stream_output(proc: subprocess.Popen, label: str) -> None:
    """Forward a process's stdout+stderr to our stdout with a coloured label."""
    colour = COLOURS.get(label, "")
    prefix = f"{colour}[{label}]{RESET} "
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip("\n")
        print(f"{prefix}{line}", flush=True)


def kill_existing() -> None:
    """Kill any uvicorn / dramatiq processes already running from this project."""
    procs = [
        ["pkill", "-f", "uvicorn app.main:app"],
        ["pkill", "-f", "dramatiq app.jobs.actors"],
    ]
    for cmd in procs:
        subprocess.run(cmd, capture_output=True)


def run(targets: list[str]) -> None:
    procs: dict[str, subprocess.Popen] = {}
    threads: list[threading.Thread] = []

    def shutdown(signum=None, frame=None) -> None:
        print("\nStopping…", flush=True)
        for name, p in procs.items():
            try:
                p.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    for name in targets:
        cmd = COMMANDS[name]
        p = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        procs[name] = p
        t = threading.Thread(target=stream_output, args=(p, name), daemon=True)
        t.start()
        threads.append(t)
        print(f"Started {name} (pid {p.pid})", flush=True)

    # Wait — exit if any process dies unexpectedly
    while True:
        for name, p in list(procs.items()):
            ret = p.poll()
            if ret is not None:
                print(f"{name} exited with code {ret}", flush=True)
                shutdown()
        threading.Event().wait(1)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "both"

    if arg == "restart":
        print("Killing existing processes…")
        kill_existing()
        import time; time.sleep(1)
        arg = "both"

    if arg == "both":
        targets = ["api", "worker"]
    elif arg in COMMANDS:
        targets = [arg]
    else:
        print(f"Usage: python run.py [api|worker|both|restart]")
        sys.exit(1)

    run(targets)
