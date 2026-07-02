#!/usr/bin/env python3
"""
Watchdog / auto-restart supervisor for the stat-arb bot.

Runs `python app.py` and restarts it if the process **dies** OR its **heartbeat
goes stale**. The engine writes a heartbeat every ~10s from its tick loop
(app.on_tick_callback), so a stale heartbeat means the async loop hung or market
data stopped flowing — e.g. the loop-starvation / CPU-throttling storm that a
plain crash-only restart can't catch. Keeps the bot alive unattended so its
software stop/exit logic can actually run.

Run this INSTEAD of `python app.py`:
    python scripts/watchdog.py

On Windows EC2, wrap this in a Windows Service (NSSM) or a Task Scheduler task
set to "restart on failure" + "run whether logged on or not" so it survives
reboots and RDP logoff.

Env:
    HEARTBEAT_FILE        path to the heartbeat file (must match app.py; default <repo>/heartbeat.txt)
    HEARTBEAT_STALE_SEC   restart if heartbeat older than this (default 120)
    WATCHDOG_GRACE_SEC    time after (re)start before staleness is enforced (default 90)
    WATCHDOG_CHECK_SEC    poll interval (default 15)
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEARTBEAT_FILE = os.getenv("HEARTBEAT_FILE", os.path.join(ROOT, "heartbeat.txt"))
STALE_SEC = float(os.getenv("HEARTBEAT_STALE_SEC", "120"))
GRACE_SEC = float(os.getenv("WATCHDOG_GRACE_SEC", "90"))
CHECK_SEC = float(os.getenv("WATCHDOG_CHECK_SEC", "15"))


def _log(msg):
    print(f"[watchdog] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _heartbeat_age():
    """Seconds since the heartbeat file was last written, or None if missing."""
    try:
        return time.time() - os.path.getmtime(HEARTBEAT_FILE)
    except OSError:
        return None


def _launch():
    # Drop a stale heartbeat so the grace window is measured from a clean slate.
    try:
        os.remove(HEARTBEAT_FILE)
    except OSError:
        pass
    _log(f"launching: {sys.executable} app.py  (cwd={ROOT})")
    return subprocess.Popen([sys.executable, "app.py"], cwd=ROOT)


def _restart(proc):
    """Hard-stop a (possibly hung) process, then relaunch."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            _log("terminate timed out — killing")
            proc.kill()
    except Exception as e:  # noqa: BLE001
        _log(f"error stopping process: {e}")
    return _launch()


def main():
    _log(f"heartbeat={HEARTBEAT_FILE}  stale>{STALE_SEC}s  grace={GRACE_SEC}s  check={CHECK_SEC}s")
    proc = _launch()
    started = time.time()
    while True:
        time.sleep(CHECK_SEC)

        # 1) Process crashed/exited?
        if proc.poll() is not None:
            _log(f"process exited (code={proc.returncode}) — restarting")
            proc = _launch()
            started = time.time()
            continue

        # 2) Hung? (heartbeat stale) — only enforced after the grace window so a
        #    slow boot / WS reconnect doesn't trigger a false restart.
        if time.time() - started < GRACE_SEC:
            continue
        age = _heartbeat_age()
        if age is None or age > STALE_SEC:
            _log(f"heartbeat stale (age={'missing' if age is None else f'{age:.0f}s'} "
                 f"> {STALE_SEC:.0f}s) — process alive but hung; restarting")
            proc = _restart(proc)
            started = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("watchdog stopped by operator")
