"""
Jarvis Daemon - the central nervous system.

Runs as a background process, listens on a Unix domain socket (Linux/macOS)
or TCP localhost (Windows). Maintains state for all active sessions,
periodically analyzes cross-session context, and injects coordination messages.

Also runs a web dashboard on WEB_PORT (9743).
"""

import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from aiohttp import web

from .protocol import (
    IS_WINDOWS, SOCKET_PATH, TCP_HOST, TCP_PORT,
    WEB_HOST, WEB_PORT,
    PID_FILE, LOG_DIR, STATUS_FILE,
    Message, MsgType, new_session_id, ensure_jarvis_dir,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [JARVIS] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis")

# How many bytes of output history to keep per session (rolling buffer)
OUTPUT_BUFFER_SIZE = 50_000
# How often to run the coordinator analysis (seconds)
COORDINATION_INTERVAL = 30
# How often to flush batched output to WebSocket clients (seconds)
WS_FLUSH_INTERVAL = 0.25


@dataclass
class SessionState:
    session_id: str
    pid: int
    cmd: str
    cwd: str
    registered_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    output_buffer: str = ""
    input_buffer: str = ""
    exit_code: Optional[int] = None
    alive: bool = True
    writer: Optional[asyncio.StreamWriter] = field(default=None, repr=False)

    @property
    def is_active(self) -> bool:
        return self.alive and self.exit_code is None

    def append_output(self, data: str):
        self.output_buffer += data
        # Rolling window - keep last N bytes
        if len(self.output_buffer) > OUTPUT_BUFFER_SIZE:
            self.output_buffer = self.output_buffer[-OUTPUT_BUFFER_SIZE:]
        self.last_activity = time.time()

    def append_input(self, data: str):
        self.input_buffer += data
        if len(self.input_buffer) > OUTPUT_BUFFER_SIZE:
            self.input_buffer = self.input_buffer[-OUTPUT_BUFFER_SIZE:]
        self.last_activity = time.time()

    def recent_output(self, chars: int = 2000) -> str:
        return self.output_buffer[-chars:]

    def summary_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "cmd": self.cmd,
            "cwd": self.cwd,
            "alive": self.is_active,
            "last_activity": self.last_activity,
            "output_tail": self.recent_output(500),
            "uptime_seconds": int(time.time() - self.registered_at),
        }


class JarvisDaemon:
    def __init__(self):
        self.sessions: dict[str, SessionState] = {}
        self.server: Optional[asyncio.AbstractServer] = None
        self._running = False
        self._coordination_task: Optional[asyncio.Task] = None
        self._ws_flush_task: Optional[asyncio.Task] = None

        # Web dashboard state
        self.ws_clients: set[web.WebSocketResponse] = set()
        self.coordination_log: deque = deque(maxlen=200)
        self._ws_output_pending: dict[str, str] = {}  # batched output per session
        self._web_runner: Optional[web.AppRunner] = None

    async def start(self):
        ensure_jarvis_dir()

        # --- Start session protocol server ---
        if IS_WINDOWS:
            self.server = await asyncio.start_server(
                self._handle_session, TCP_HOST, TCP_PORT,
            )
            listen_addr = f"{TCP_HOST}:{TCP_PORT}"
        else:
            # Clean up stale socket
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
            self.server = await asyncio.start_unix_server(
                self._handle_session, path=SOCKET_PATH,
            )
            os.chmod(SOCKET_PATH, 0o700)
            listen_addr = SOCKET_PATH

        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

        # Write PID file
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

        # --- Start web dashboard server ---
        from .web import create_web_app
        app = create_web_app(self)
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, WEB_HOST, WEB_PORT)
        await site.start()

        self._running = True
        self._write_status_file()
        self._coordination_task = asyncio.create_task(self._coordination_loop())
        self._ws_flush_task = asyncio.create_task(self._ws_output_flush_loop())

        log.info(f"Jarvis daemon started (PID {os.getpid()})")
        log.info(f"Sessions on {listen_addr}")
        log.info(f"Dashboard on http://{WEB_HOST}:{WEB_PORT}")

        # Keep running until stopped
        self._stop_event = asyncio.Event()
        # Start serving sessions in background
        async with self.server:
            await self._stop_event.wait()

    async def stop(self):
        self._running = False
        if hasattr(self, '_stop_event'):
            self._stop_event.set()
        if self._coordination_task:
            self._coordination_task.cancel()
        if self._ws_flush_task:
            self._ws_flush_task.cancel()
        if self._web_runner:
            await self._web_runner.cleanup()
        if self.server:
            self.server.close()
        if not IS_WINDOWS and SOCKET_PATH and os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)
        # Close all WS clients
        for ws in list(self.ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        log.info("Jarvis daemon stopped")

    # --- WebSocket fan-out ---

    async def notify_ws(self, event: dict):
        """Send an event to all connected dashboard WebSocket clients."""
        if not self.ws_clients:
            return
        dead = set()
        data = json.dumps(event, default=str)
        for ws in self.ws_clients:
            try:
                await ws.send_str(data)
            except Exception:
                dead.add(ws)
        self.ws_clients -= dead

    async def _ws_output_flush_loop(self):
        """Batch and flush output to WS clients every 250ms."""
        while self._running:
            await asyncio.sleep(WS_FLUSH_INTERVAL)
            if not self._ws_output_pending or not self.ws_clients:
                continue
            # Grab and clear pending
            pending = self._ws_output_pending
            self._ws_output_pending = {}
            for session_id, data in pending.items():
                # Cap at 4KB per flush per session
                if len(data) > 4096:
                    data = data[-4096:]
                await self.notify_ws({
                    "event": "output",
                    "session_id": session_id,
                    "data": data,
                })

    # --- Session handling ---

    async def _handle_session(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Handle a single session connection."""
        session_id = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break

                try:
                    msg = Message.decode_line(line.decode())
                except (json.JSONDecodeError, ValueError) as e:
                    log.warning(f"Bad message: {e}")
                    continue

                if msg.type == MsgType.REGISTER:
                    session_id = msg.session_id
                    self.sessions[session_id] = SessionState(
                        session_id=session_id,
                        pid=msg.pid or 0,
                        cmd=msg.cmd or "unknown",
                        cwd=msg.cwd or ".",
                        writer=writer,
                    )
                    log.info(
                        f"Session registered: {session_id} "
                        f"(cmd={msg.cmd}, pid={msg.pid})"
                    )
                    # Send ACK
                    ack = Message(
                        type=MsgType.ACK, session_id=session_id
                    )
                    writer.write(ack.encode())
                    await writer.drain()
                    self._write_status_file()

                    # Notify dashboard
                    await self.notify_ws({
                        "event": "registered",
                        "session_id": session_id,
                        "cmd": msg.cmd,
                        "cwd": msg.cwd,
                        "pid": msg.pid,
                        "timestamp": time.time(),
                    })

                elif msg.type == MsgType.OUTPUT and msg.session_id in self.sessions:
                    self.sessions[msg.session_id].append_output(msg.data or "")
                    # Batch for WS (don't send immediately)
                    sid = msg.session_id
                    self._ws_output_pending[sid] = self._ws_output_pending.get(sid, "") + (msg.data or "")

                elif msg.type == MsgType.INPUT and msg.session_id in self.sessions:
                    self.sessions[msg.session_id].append_input(msg.data or "")

                elif msg.type == MsgType.EXIT and msg.session_id in self.sessions:
                    sess = self.sessions[msg.session_id]
                    sess.exit_code = msg.exit_code
                    sess.alive = False
                    log.info(
                        f"Session exited: {session_id} (code={msg.exit_code})"
                    )
                    self._write_status_file()

                    # Flush any pending output for this session
                    if session_id in self._ws_output_pending:
                        data = self._ws_output_pending.pop(session_id)
                        await self.notify_ws({
                            "event": "output",
                            "session_id": session_id,
                            "data": data,
                        })

                    await self.notify_ws({
                        "event": "exited",
                        "session_id": session_id,
                        "exit_code": msg.exit_code,
                        "timestamp": time.time(),
                    })

        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error(f"Session handler error: {e}")
        finally:
            if session_id and session_id in self.sessions:
                self.sessions[session_id].alive = False
                self.sessions[session_id].writer = None
            writer.close()

    async def inject_message(self, session_id: str, message: str, style: str = "info"):
        """Send a message to be displayed in a session's terminal."""
        sess = self.sessions.get(session_id)
        if not sess or not sess.writer or not sess.is_active:
            return False

        msg = Message(
            type=MsgType.INJECT,
            session_id=session_id,
            message=message,
            style=style,
        )
        try:
            sess.writer.write(msg.encode())
            await sess.writer.drain()
            return True
        except Exception as e:
            log.warning(f"Failed to inject into {session_id}: {e}")
            return False

    async def broadcast(self, message: str, exclude: Optional[str] = None):
        """Send a message to all active sessions."""
        for sid, sess in self.sessions.items():
            if sid != exclude and sess.is_active:
                await self.inject_message(sid, message)

    async def _coordination_loop(self):
        """Periodically analyze sessions and suggest coordination."""
        while self._running:
            await asyncio.sleep(COORDINATION_INTERVAL)
            try:
                await self._analyze_and_coordinate()
            except Exception as e:
                log.error(f"Coordination error: {e}")

    async def _analyze_and_coordinate(self):
        """
        Core coordination logic. Analyzes all active sessions and
        identifies opportunities for coordination.
        """
        # Always write status file (even with 0 sessions, for `jarvis status`)
        self._write_status_file()

        active = {
            sid: sess for sid, sess in self.sessions.items() if sess.is_active
        }

        if len(active) < 2:
            return

        log.info(f"Analyzing {len(active)} active sessions for coordination...")

        # Build a context snapshot
        context = []
        for sid, sess in active.items():
            context.append({
                "session_id": sid,
                "cmd": sess.cmd,
                "cwd": sess.cwd,
                "recent_output": sess.recent_output(1000),
                "uptime": int(time.time() - sess.registered_at),
            })

        # Use LLM coordinator if available, else rules-based
        try:
            from .coordinator import analyze_sessions_llm
            actions = await analyze_sessions_llm(context)
        except Exception as e:
            log.warning(f"LLM coordinator unavailable: {e}")
            actions = []
            insights = self._detect_patterns(active)
            for insight in insights:
                for sid in insight.get("notify", []):
                    actions.append({
                        "target_sessions": [sid],
                        "message": insight["message"],
                        "severity": "info",
                        "type": insight["type"],
                    })

        for action in actions:
            severity = action.get("severity", "info")
            color = {"info": "36", "warning": "33", "critical": "31"}.get(severity, "35")
            for sid in action.get("target_sessions", []):
                await self.inject_message(
                    sid,
                    f"\033[1;{color}m[JARVIS]\033[0m {action['message']}",
                    style="coordination",
                )

            # Log and notify dashboard
            entry = {
                "timestamp": time.time(),
                "message": action["message"],
                "severity": severity,
                "type": action.get("type", "unknown"),
                "targets": action.get("target_sessions", []),
            }
            self.coordination_log.append(entry)
            await self.notify_ws({"event": "coordination", **entry})

    def _write_status_file(self):
        """Write current status to a file for `jarvis status` to read."""
        status = self.get_status()
        try:
            ensure_jarvis_dir()
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(status, f, indent=2, default=str)
            os.replace(tmp, STATUS_FILE)
        except Exception as e:
            log.warning(f"Failed to write status file: {e}")

    def _detect_patterns(self, active: dict[str, SessionState]) -> list[dict]:
        """
        Simple pattern detection for the prototype.
        Looks for: duplicate work, file conflicts, error cascades,
        shared dependencies.
        """
        insights = []
        sessions = list(active.values())

        # Detect: sessions working in the same directory
        cwd_groups = defaultdict(list)
        for s in sessions:
            cwd_groups[s.cwd].append(s)
        for cwd, group in cwd_groups.items():
            if len(group) > 1:
                sids = [s.session_id for s in group]
                insights.append({
                    "type": "shared_cwd",
                    "message": (
                        f"Sessions {', '.join(s.session_id[:8] for s in group)} "
                        f"are all working in {cwd}. Watch for file conflicts."
                    ),
                    "notify": sids,
                })

        # Detect: one session erroring on something another session modified
        for s in sessions:
            recent = s.recent_output(2000).lower()
            if "error" in recent or "traceback" in recent or "failed" in recent:
                for other in sessions:
                    if other.session_id == s.session_id:
                        continue
                    other_out = other.recent_output(2000).lower()
                    if any(w in other_out for w in ["wrote", "saved", "modified", "git commit", "git push"]):
                        insights.append({
                            "type": "possible_conflict",
                            "message": (
                                f"Session {s.session_id[:8]} is seeing errors. "
                                f"Session {other.session_id[:8]} recently modified files. "
                                f"These might be related."
                            ),
                            "notify": [s.session_id, other.session_id],
                        })

        # Detect: sessions that might benefit from knowing about each other
        if len(sessions) >= 2 and not insights:
            summaries = []
            for s in sessions:
                summaries.append(f"  [{s.session_id[:8]}] {s.cmd} in {s.cwd}")
            summary_text = "\n".join(summaries)
            insights.append({
                "type": "awareness",
                "message": (
                    f"Active sibling sessions:\n{summary_text}\n"
                    f"Use `jarvis status` for full details."
                ),
                "notify": [s.session_id for s in sessions],
            })

        return insights

    def get_status(self) -> dict:
        return {
            "daemon_pid": os.getpid(),
            "sessions": {
                sid: sess.summary_dict()
                for sid, sess in self.sessions.items()
            },
            "active_count": sum(
                1 for s in self.sessions.values() if s.is_active
            ),
            "total_count": len(self.sessions),
        }


async def run_daemon():
    daemon = JarvisDaemon()

    if not IS_WINDOWS:
        import signal
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.stop()))

    try:
        await daemon.start()
    except asyncio.CancelledError:
        await daemon.stop()
    except KeyboardInterrupt:
        await daemon.stop()


def main():
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
