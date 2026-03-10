"""
Session Wrapper - PTY proxy that makes a child process visible to Jarvis.

Usage: jarvis spawn <command> [args...]

How it works:
  1. Connects to the Jarvis daemon via Unix socket (or TCP on Windows)
  2. Registers a new session
  3. Forks a child process inside a pseudo-terminal (PTY)
  4. User interacts with the child normally (full TTY: colors, cursor, etc.)
  5. All output is teed to the daemon in real-time
  6. Listens for injected messages from the daemon and renders them

Platform support:
  - Linux/macOS: native pty + fork + Unix socket
  - Windows: pywinpty (ConPTY) + TCP socket
"""

import asyncio
import json
import os
import socket
import sys
import time
from threading import Thread

from .protocol import (
    IS_WINDOWS, BANNER, SOCKET_PATH, TCP_HOST, TCP_PORT,
    Message, MsgType, new_session_id,
)


def connect_to_daemon() -> socket.socket | None:
    """Try to connect to the Jarvis daemon. Returns None if not running."""
    try:
        if IS_WINDOWS:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((TCP_HOST, TCP_PORT))
        else:
            if not os.path.exists(SOCKET_PATH):
                return None
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(SOCKET_PATH)
        sock.setblocking(False)
        return sock
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        return None


def _send_msg(sock: socket.socket, msg: Message):
    """Send a message to the daemon, swallowing errors."""
    try:
        sock.setblocking(True)
        sock.sendall(msg.encode())
        sock.setblocking(False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Windows implementation using pywinpty
# ---------------------------------------------------------------------------

def _spawn_windows(cmd: list[str]):
    """Windows PTY proxy using pywinpty (ConPTY)."""
    try:
        from winpty import PtyProcess
    except ImportError:
        print(
            "\033[1;31m[JARVIS]\033[0m pywinpty is required on Windows.\n"
            "Install it with: pip install pywinpty",
            file=sys.stderr,
        )
        sys.exit(1)

    import msvcrt
    import ctypes

    session_id = new_session_id()
    cwd = os.getcwd()
    cmd_str = " ".join(cmd)

    # Connect to daemon
    daemon_sock = connect_to_daemon()
    if daemon_sock is None:
        print(
            "\033[1;31m[JARVIS]\033[0m Daemon not running. "
            "Start it with: jarvis start",
            file=sys.stderr,
        )
        print(
            "\033[1;33m[JARVIS]\033[0m Running command without coordination...\n",
            file=sys.stderr,
        )
        # Run the command directly without monitoring
        import subprocess
        result = subprocess.run(cmd)
        sys.exit(result.returncode)

    # Set environment variables so the child knows about Jarvis
    env = os.environ.copy()
    env["JARVIS_SESSION_ID"] = session_id
    env["JARVIS_SOCKET"] = f"{TCP_HOST}:{TCP_PORT}"
    env["JARVIS_ACTIVE"] = "1"

    # Get terminal size
    try:
        size = os.get_terminal_size()
        cols, rows = size.columns, size.lines
    except OSError:
        cols, rows = 120, 30

    # Spawn the child in a ConPTY
    # PtyProcess.spawn takes a single command string
    proc = PtyProcess.spawn(cmd_str)

    # Register with daemon
    reg = Message(
        type=MsgType.REGISTER,
        session_id=session_id,
        pid=proc.pid,
        cmd=cmd_str,
        cwd=cwd,
    )
    try:
        daemon_sock.setblocking(True)
        daemon_sock.sendall(reg.encode())
        # Wait for ACK
        daemon_sock.settimeout(2.0)
        daemon_sock.recv(4096)
        daemon_sock.setblocking(False)
    except Exception as e:
        print(f"\033[1;31m[JARVIS]\033[0m Failed to register: {e}", file=sys.stderr)
        daemon_sock = None

    # Show banner
    banner = BANNER.format(session_id_short=session_id[:8])
    sys.stdout.write(banner)
    sys.stdout.flush()

    # Enable Windows virtual terminal processing for ANSI
    kernel32 = ctypes.windll.kernel32
    STD_OUTPUT_HANDLE = -11
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    mode = ctypes.c_ulong()
    kernel32.GetConsoleMode(handle, ctypes.byref(mode))
    kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)

    # --- Input thread: read from real stdin, write to PTY + daemon ---
    stop_event = False

    def input_thread():
        nonlocal daemon_sock, stop_event
        # On Windows, read from console handle directly
        stdin_handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        while not stop_event and proc.isalive():
            try:
                # Use msvcrt for non-blocking key reads
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    # Convert special keys
                    if ch == '\r':
                        data = '\r\n'
                    elif ch == '\x03':  # Ctrl+C
                        data = '\x03'
                    elif ch == '\x00' or ch == '\xe0':
                        # Extended key - read second byte
                        ch2 = msvcrt.getwch()
                        # Map arrow keys etc.
                        key_map = {
                            'H': '\x1b[A',  # Up
                            'P': '\x1b[B',  # Down
                            'M': '\x1b[C',  # Right
                            'K': '\x1b[D',  # Left
                            'G': '\x1b[H',  # Home
                            'O': '\x1b[F',  # End
                            'I': '\x1b[5~', # Page Up
                            'Q': '\x1b[6~', # Page Down
                            'S': '\x1b[3~', # Delete
                            'R': '\x1b[2~', # Insert
                        }
                        data = key_map.get(ch2, '')
                    else:
                        data = ch

                    if data:
                        proc.write(data)
                        # Tee to daemon
                        if daemon_sock:
                            msg = Message(
                                type=MsgType.INPUT,
                                session_id=session_id,
                                data=data,
                            )
                            _send_msg(daemon_sock, msg)
                else:
                    time.sleep(0.01)
            except Exception:
                time.sleep(0.05)

    input_t = Thread(target=input_thread, daemon=True)
    input_t.start()

    # --- Output thread: read from daemon for injected messages ---
    def daemon_listen_thread():
        nonlocal daemon_sock, stop_event
        buf = b""
        while not stop_event and daemon_sock:
            try:
                daemon_sock.setblocking(True)
                daemon_sock.settimeout(0.5)
                chunk = daemon_sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = Message.decode_line(line.decode())
                        if msg.type == MsgType.INJECT and msg.message:
                            rendered = (
                                f"\r\n\033[1;35m[JARVIS]\033[0m "
                                f"{msg.message}\r\n"
                            )
                            sys.stdout.write(rendered)
                            sys.stdout.flush()
                    except Exception:
                        pass
            except socket.timeout:
                continue
            except Exception:
                break

    if daemon_sock:
        daemon_t = Thread(target=daemon_listen_thread, daemon=True)
        daemon_t.start()

    # --- Main loop: read child output, display + tee to daemon ---
    try:
        while proc.isalive():
            try:
                data = proc.read()
                if data:
                    text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    # Tee to daemon
                    if daemon_sock:
                        msg = Message(
                            type=MsgType.OUTPUT,
                            session_id=session_id,
                            data=text,
                        )
                        _send_msg(daemon_sock, msg)
                else:
                    time.sleep(0.01)
            except EOFError:
                break
            except Exception:
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event = True
        exit_code = proc.exitstatus if hasattr(proc, 'exitstatus') else -1
        if exit_code is None:
            exit_code = -1

        # Notify daemon
        if daemon_sock:
            exit_msg = Message(
                type=MsgType.EXIT,
                session_id=session_id,
                exit_code=exit_code,
            )
            _send_msg(daemon_sock, exit_msg)
            try:
                daemon_sock.close()
            except Exception:
                pass

        sys.stdout.write(
            f"\n\033[1;36m[JARVIS]\033[0m Session {session_id[:8]} ended.\n"
        )


# ---------------------------------------------------------------------------
# Unix implementation using native pty + fork
# ---------------------------------------------------------------------------

def _spawn_unix(cmd: list[str]):
    """Unix PTY proxy using native pty/fork."""
    import fcntl
    import pty
    import select
    import signal
    import termios
    import tty

    session_id = new_session_id()
    cwd = os.getcwd()
    cmd_str = " ".join(cmd)

    # Connect to daemon
    daemon_sock = connect_to_daemon()
    if daemon_sock is None:
        print(
            "\033[1;31m[JARVIS]\033[0m Daemon not running. "
            "Start it with: jarvis start",
            file=sys.stderr,
        )
        print(
            "\033[1;33m[JARVIS]\033[0m Running command without coordination...\n",
            file=sys.stderr,
        )
        # Fall through - run the command anyway, just without monitoring
        os.execvp(cmd[0], cmd)
        return

    # Set environment variables so the child knows about Jarvis
    env = os.environ.copy()
    env["JARVIS_SESSION_ID"] = session_id
    env["JARVIS_SOCKET"] = SOCKET_PATH
    env["JARVIS_ACTIVE"] = "1"

    def set_window_size(fd):
        """Copy the terminal window size to the child PTY."""
        if os.isatty(sys.stdin.fileno()):
            size = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\x00" * 8)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, size)

    # Create PTY
    master_fd, slave_fd = pty.openpty()

    # Fork
    pid = os.fork()

    if pid == 0:
        # === CHILD PROCESS ===
        os.close(master_fd)
        os.setsid()

        # Set the slave as the controlling terminal
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        # Redirect stdio to the PTY slave
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)

        os.execvpe(cmd[0], cmd, env)
        # If exec fails
        sys.exit(127)

    # === PARENT PROCESS (proxy) ===
    os.close(slave_fd)

    # Match child PTY size to our terminal
    set_window_size(master_fd)

    # Forward SIGWINCH (terminal resize) to child
    def handle_winch(signum, frame):
        set_window_size(master_fd)
        os.kill(pid, signal.SIGWINCH)

    signal.signal(signal.SIGWINCH, handle_winch)

    # Register with daemon
    reg = Message(
        type=MsgType.REGISTER,
        session_id=session_id,
        pid=pid,
        cmd=cmd_str,
        cwd=cwd,
    )
    try:
        daemon_sock.setblocking(True)
        daemon_sock.sendall(reg.encode())

        # Wait for ACK (with timeout)
        daemon_sock.settimeout(2.0)
        ack_data = daemon_sock.recv(4096)
        daemon_sock.setblocking(False)
    except Exception as e:
        print(f"\033[1;31m[JARVIS]\033[0m Failed to register: {e}", file=sys.stderr)
        daemon_sock = None

    # Show banner
    banner = BANNER.format(session_id_short=session_id[:8])
    sys.stdout.write(banner)
    sys.stdout.flush()

    # Save original terminal settings
    old_tty = None
    if os.isatty(sys.stdin.fileno()):
        old_tty = termios.tcgetattr(sys.stdin)
        tty.setraw(sys.stdin.fileno())

    try:
        while True:
            # Poll: stdin (user input), master_fd (child output), daemon socket
            fds = [sys.stdin.fileno(), master_fd]
            if daemon_sock:
                fds.append(daemon_sock.fileno())

            try:
                rlist, _, _ = select.select(fds, [], [], 0.1)
            except (ValueError, OSError):
                break

            # --- User input -> child + daemon ---
            if sys.stdin.fileno() in rlist:
                try:
                    data = os.read(sys.stdin.fileno(), 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(master_fd, data)
                # Tee to daemon
                if daemon_sock:
                    msg = Message(
                        type=MsgType.INPUT,
                        session_id=session_id,
                        data=data.decode("utf-8", errors="replace"),
                    )
                    try:
                        daemon_sock.sendall(msg.encode())
                    except Exception:
                        daemon_sock = None

            # --- Child output -> user + daemon ---
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
                # Tee to daemon
                if daemon_sock:
                    msg = Message(
                        type=MsgType.OUTPUT,
                        session_id=session_id,
                        data=data.decode("utf-8", errors="replace"),
                    )
                    try:
                        daemon_sock.sendall(msg.encode())
                    except Exception:
                        daemon_sock = None

            # --- Daemon messages -> render in terminal ---
            if daemon_sock and daemon_sock.fileno() in rlist:
                try:
                    raw = daemon_sock.recv(4096)
                    if raw:
                        for line in raw.decode().strip().split("\n"):
                            if not line:
                                continue
                            try:
                                msg = Message.decode_line(line)
                                if msg.type == MsgType.INJECT and msg.message:
                                    # Render the injected message above the current line
                                    rendered = (
                                        f"\r\n\033[1;35m[JARVIS]\033[0m "
                                        f"{msg.message}\r\n"
                                    )
                                    os.write(sys.stdout.fileno(), rendered.encode())
                            except Exception:
                                pass
                except Exception:
                    daemon_sock = None

            # Check if child is still alive
            result = os.waitpid(pid, os.WNOHANG)
            if result[0] != 0:
                # Child exited - drain remaining output
                try:
                    while True:
                        rlist, _, _ = select.select([master_fd], [], [], 0.1)
                        if master_fd not in rlist:
                            break
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        os.write(sys.stdout.fileno(), data)
                except Exception:
                    pass

                exit_code = os.WEXITSTATUS(result[1]) if os.WIFEXITED(result[1]) else -1

                # Notify daemon
                if daemon_sock:
                    exit_msg = Message(
                        type=MsgType.EXIT,
                        session_id=session_id,
                        exit_code=exit_code,
                    )
                    try:
                        daemon_sock.sendall(exit_msg.encode())
                    except Exception:
                        pass

                break

    except KeyboardInterrupt:
        # Forward Ctrl+C to child
        try:
            os.kill(pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    finally:
        # Restore terminal
        if old_tty is not None:
            termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, old_tty)
        os.close(master_fd)
        if daemon_sock:
            daemon_sock.close()

        sys.stdout.write(
            f"\n\033[1;36m[JARVIS]\033[0m Session {session_id[:8]} ended.\n"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def spawn(cmd: list[str]):
    """
    Spawn a command inside a PTY, tee all I/O to the Jarvis daemon.
    The user experience is identical to running the command directly.
    Dispatches to platform-specific implementation.
    """
    if IS_WINDOWS:
        _spawn_windows(cmd)
    else:
        _spawn_unix(cmd)


def main():
    if len(sys.argv) < 2:
        print("Usage: jarvis spawn <command> [args...]")
        sys.exit(1)
    spawn(sys.argv[1:])
