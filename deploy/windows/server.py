"""Single-instance loopback supervisor. No configuration, messages or tokens in logs."""
import argparse
import json
import msvcrt
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "storage" / "windows"
RUN = STATE / "run"
LOGS = ROOT / "logs" / "windows"


def supervise(mode):
    for directory in (RUN, LOGS, *(STATE / name for name in ("storage", "documents", "index", "models"))):
        directory.mkdir(parents=True, exist_ok=True)
    lock = (RUN / "server.lock").open("a+b")
    lock.seek(0)
    if not lock.read(1):
        lock.write(b"0")
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit("A supervisor is already running.")
    stop = RUN / "stop"
    stop.unlink(missing_ok=True)
    info = RUN / "server.json"
    pending = info.with_suffix(".tmp")
    environment = {**os.environ, "SMTP_ENABLED": "0", "REGLAMENT_MODE": mode,
                   "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    # Sandbox bootstrap settings must never become part of the deployed service.
    environment.pop("PYTHONPATH", None)
    command = [sys.executable, "-B", "-m", "uvicorn", "deploy.windows.app:create_application",
               "--factory", "--host", "127.0.0.1", "--port", "8000", "--workers", "1",
               "--no-access-log", "--no-proxy-headers"]
    child = None
    failures = 0
    try:
        while not stop.exists():
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind(("127.0.0.1", 8000))
                except OSError:
                    raise SystemExit("Port 8000 is occupied; refusing to replace another service.")
            log_path = LOGS / "server.log"
            if log_path.exists() and log_path.stat().st_size > 5_000_000:
                log_path.replace(LOGS / "server.previous.log")
            started = time.monotonic()
            with log_path.open("ab", buffering=0) as log:
                child = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=log,
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                pending.write_text(json.dumps({"supervisor_pid": os.getpid(), "child_pid": child.pid,
                                               "mode": mode, "url": "http://127.0.0.1:8000"}), encoding="utf-8")
                pending.replace(info)
                while child.poll() is None and not stop.exists():
                    time.sleep(0.25)
                if stop.exists():
                    break
            if time.monotonic() - started > 60:
                failures = 0
            failures += 1
            if failures >= 5:
                raise SystemExit("Five startup/process failures; inspect logs before restarting.")
            for _ in range(min(4 * 2 ** failures, 120)):
                if stop.exists():
                    break
                time.sleep(0.25)
    finally:
        if child is not None and child.poll() is None:
            # SQLite WAL is crash safe. Backups are allowed only after process exit.
            # Windows venv python.exe may be a redirector with a real interpreter
            # child. Terminating only the redirector leaves Uvicorn listening.
            subprocess.run(["taskkill.exe", "/PID", str(child.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        info.unlink(missing_ok=True)
        stop.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("disabled", "demo-offline"), required=True)
    supervise(parser.parse_args().mode)
