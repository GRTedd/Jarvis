#!/usr/bin/env python3
"""
Jarvis CLI - manage the coordination daemon and spawn monitored sessions.

Commands:
  jarvis start           Start the Jarvis daemon (foreground)
  jarvis start -d        Start the Jarvis daemon (background)
  jarvis stop            Stop the Jarvis daemon
  jarvis spawn <cmd>     Run a command inside a Jarvis-monitored session
  jarvis status          Show all active sessions
  jarvis logs            Tail the daemon log
"""

import json
import os
import signal
import subprocess
import sys
import time

from .protocol import (
    IS_WINDOWS, SOCKET_PATH, PID_FILE, LOG_FILE, STATUS_FILE,
)


def daemon_is_running() -> bool:
    if not os.path.exists(PID_FILE):
        return False
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        if IS_WINDOWS:
            # On Windows, os.kill(pid, 0) doesn't work the same way.
            # Use tasklist to check.
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)  # Check if process exists
            return True
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        return False


def get_daemon_pid() -> int | None:
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


def cmd_start(background: bool = False):
    if daemon_is_running():
        pid = get_daemon_pid()
        print(f"\033[1;36m[JARVIS]\033[0m Already running (PID {pid})")
        return

    if background:
        if IS_WINDOWS:
            # On Windows, use subprocess with CREATE_NO_WINDOW to detach
            import ctypes
            CREATE_NO_WINDOW = 0x08000000
            DETACHED_PROCESS = 0x00000008

            # Redirect output to log file
            log_dir = os.path.dirname(LOG_FILE)
            os.makedirs(log_dir, exist_ok=True)

            with open(LOG_FILE, "w") as log_f:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "jarvis.daemon"],
                    stdout=log_f,
                    stderr=log_f,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
                    cwd=os.getcwd(),
                )

            time.sleep(0.5)
            if daemon_is_running():
                print(f"\033[1;36m[JARVIS]\033[0m Daemon started (PID {proc.pid})")
            else:
                print("\033[1;31m[JARVIS]\033[0m Failed to start daemon")
                print(f"Check log: {LOG_FILE}")
            return
        else:
            # Unix: fork to background
            pid = os.fork()
            if pid > 0:
                # Parent
                time.sleep(0.5)
                if daemon_is_running():
                    print(f"\033[1;36m[JARVIS]\033[0m Daemon started (PID {pid})")
                else:
                    print("\033[1;31m[JARVIS]\033[0m Failed to start daemon")
                return
            else:
                # Child - become session leader
                os.setsid()
                devnull = os.open(os.devnull, os.O_RDWR)
                os.dup2(devnull, 0)
                log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
                os.dup2(log_fd, 1)
                os.dup2(log_fd, 2)
                os.close(devnull)
                os.close(log_fd)

    # Run daemon (this blocks)
    print(f"\033[1;36m[JARVIS]\033[0m Starting daemon (PID {os.getpid()})...")
    from .daemon import main as daemon_main
    daemon_main()


def cmd_stop():
    if not daemon_is_running():
        print("\033[1;36m[JARVIS]\033[0m Not running")
        return

    pid = get_daemon_pid()
    try:
        if IS_WINDOWS:
            # On Windows, use taskkill
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(pid, signal.SIGTERM)

        # Wait for it to die
        for _ in range(20):
            time.sleep(0.1)
            if not daemon_is_running():
                break

        print(f"\033[1;36m[JARVIS]\033[0m Stopped (was PID {pid})")
    except (ProcessLookupError, OSError):
        print(f"\033[1;36m[JARVIS]\033[0m Already stopped")

    # Clean up
    for path in [PID_FILE]:
        if os.path.exists(path):
            os.unlink(path)
    if not IS_WINDOWS and SOCKET_PATH and os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)


def cmd_status():
    if not daemon_is_running():
        print("\033[1;31m[JARVIS]\033[0m Daemon not running. Start with: jarvis start -d")
        return

    pid = get_daemon_pid()
    print(f"\033[1;36m+--------------------------------------+\033[0m")
    print(f"\033[1;36m|  JARVIS Status                       |\033[0m")
    print(f"\033[1;36m+--------------------------------------+\033[0m")
    print(f"  Daemon PID: {pid}")
    if IS_WINDOWS:
        from .protocol import TCP_HOST, TCP_PORT
        print(f"  Listening:   {TCP_HOST}:{TCP_PORT}")
    else:
        print(f"  Socket:      {SOCKET_PATH}")
    print()

    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE) as f:
                status = json.load(f)
            sessions = status.get("sessions", {})
            if not sessions:
                print("  No active sessions.")
            else:
                for sid, info in sessions.items():
                    alive_marker = "[ALIVE]" if info.get("alive") else "[DEAD]"
                    alive_color = "32" if info.get("alive") else "31"
                    uptime = info.get("uptime_seconds", 0)
                    mins = uptime // 60
                    secs = uptime % 60
                    print(f"  \033[1;{alive_color}m{alive_marker}\033[0m [{sid[:8]}] {info.get('cmd', '?')}")
                    print(f"      Dir: {info.get('cwd', '?')}")
                    print(f"      Uptime: {mins}m{secs}s")
                    tail = info.get("output_tail", "").strip()
                    if tail:
                        last_line = tail.split("\n")[-1][:80]
                        print(f"      Last: {last_line}")
                    print()
        except Exception as e:
            print(f"  Error reading status: {e}")
    else:
        print("  Status file not available yet.")
        print("  (Sessions will appear after they register)")


def cmd_spawn(args: list[str]):
    if not args:
        print("Usage: jarvis spawn <command> [args...]")
        sys.exit(1)

    from .session import spawn
    spawn(args)


def cmd_logs():
    if os.path.exists(LOG_FILE):
        if IS_WINDOWS:
            # Windows: use powershell Get-Content -Wait (like tail -f)
            subprocess.run(
                ["powershell", "-Command", f"Get-Content '{LOG_FILE}' -Wait -Tail 50"],
            )
        else:
            os.execvp("tail", ["tail", "-f", LOG_FILE])
    else:
        print("\033[1;36m[JARVIS]\033[0m No log file found. Is daemon running in background?")


def print_help():
    print("""
\033[1;36mJARVIS - CLI Session Coordinator\033[0m

\033[1mUsage:\033[0m
  jarvis start [-d]          Start daemon (foreground, or -d for background)
  jarvis stop                Stop the daemon
  jarvis status              Show all monitored sessions
  jarvis spawn <cmd> [args]  Run a command under Jarvis monitoring
  jarvis logs                Tail the daemon log

\033[1mHow it works:\033[0m
  1. Start the daemon:     jarvis start -d
  2. Spawn sessions:       jarvis spawn claude
                           jarvis spawn bash
                           jarvis spawn npm run dev
  3. Jarvis watches all sessions and coordinates them.

\033[1mEnvironment:\033[0m
  Inside monitored sessions, these env vars are set:
    JARVIS_ACTIVE=1
    JARVIS_SESSION_ID=<id>
    JARVIS_SOCKET=<socket_path_or_host:port>
""")


def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        print_help()
        return

    cmd = args[0]

    if cmd == "start":
        background = "-d" in args or "--daemon" in args
        cmd_start(background=background)
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "status":
        cmd_status()
    elif cmd == "spawn":
        cmd_spawn(args[1:])
    elif cmd == "logs":
        cmd_logs()
    else:
        # Assume it's a command to spawn
        print(f"\033[1;33m[JARVIS]\033[0m Unknown command '{cmd}'. Did you mean: jarvis spawn {cmd}?")
        print_help()


if __name__ == "__main__":
    main()
