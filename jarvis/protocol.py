"""
Protocol: JSON-newline messages over Unix domain sockets (Linux/macOS)
or TCP localhost (Windows).

Session -> Daemon:
  REGISTER   { session_id, pid, cmd, cwd, env_snapshot }
  OUTPUT     { session_id, data, timestamp }
  INPUT      { session_id, data, timestamp }
  EXIT       { session_id, exit_code, timestamp }

Daemon -> Session:
  ACK        { session_id, jarvis_port }
  INJECT     { session_id, message, style }
  COORDINATE { session_id, instruction, context }
"""

import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

# --- Platform-adaptive paths ---
WEB_HOST = "127.0.0.1"
WEB_PORT = 9743

if IS_WINDOWS:
    _JARVIS_DIR = os.path.join(os.environ.get("TEMP", r"C:\Temp"), "jarvis")
    SOCKET_PATH = None  # Not used on Windows
    TCP_HOST = "127.0.0.1"
    TCP_PORT = 9742
    PID_FILE = os.path.join(_JARVIS_DIR, "jarvis.pid")
    LOG_FILE = os.path.join(_JARVIS_DIR, "jarvis_daemon.log")
    STATUS_FILE = os.path.join(_JARVIS_DIR, "jarvis_status.json")
    LOG_DIR = _JARVIS_DIR
else:
    SOCKET_PATH = "/tmp/jarvis.sock"
    TCP_HOST = None
    TCP_PORT = None
    PID_FILE = "/tmp/jarvis.pid"
    LOG_FILE = "/tmp/jarvis_daemon.log"
    STATUS_FILE = "/tmp/jarvis_status.json"
    LOG_DIR = "/tmp/jarvis_logs"


def ensure_jarvis_dir():
    """Create the Jarvis temp directory on Windows."""
    if IS_WINDOWS:
        os.makedirs(_JARVIS_DIR, exist_ok=True)


BANNER = """
\033[1;36m╔══════════════════════════════════════════════╗
║  JARVIS is watching this session              ║
║  Session: {session_id_short}                          ║
║  Run \033[1;33mjarvis status\033[1;36m to see all active sessions  ║
╚══════════════════════════════════════════════╝\033[0m
"""


class MsgType(str, Enum):
    # Session -> Daemon
    REGISTER = "REGISTER"
    OUTPUT = "OUTPUT"
    INPUT = "INPUT"
    EXIT = "EXIT"
    # Daemon -> Session
    ACK = "ACK"
    INJECT = "INJECT"
    COORDINATE = "COORDINATE"


@dataclass
class Message:
    type: MsgType
    session_id: str
    timestamp: float = field(default_factory=time.time)
    data: Optional[str] = None
    pid: Optional[int] = None
    cmd: Optional[str] = None
    cwd: Optional[str] = None
    exit_code: Optional[int] = None
    message: Optional[str] = None
    style: Optional[str] = None
    instruction: Optional[str] = None
    context: Optional[dict] = None

    def encode(self) -> bytes:
        d = {k: v for k, v in asdict(self).items() if v is not None}
        return (json.dumps(d) + "\n").encode()

    @classmethod
    def decode(cls, raw: bytes) -> "Message":
        d = json.loads(raw.decode().strip())
        d["type"] = MsgType(d["type"])
        return cls(**d)

    @classmethod
    def decode_line(cls, line: str) -> "Message":
        d = json.loads(line.strip())
        d["type"] = MsgType(d["type"])
        return cls(**d)


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]
