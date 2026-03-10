# Jarvis — CLI Session Coordinator

A daemon that watches multiple terminal sessions, understands what they're doing, and helps them coordinate.

**Cross-platform:** Works on Linux, macOS, and Windows.

## Architecture

```
┌──────────────────────────────────────────────┐
│              Jarvis Daemon                   │
│  Unix socket (Linux/macOS) or TCP (Windows)  │
│                                              │
│  ┌─────────────┐  ┌───────────────────────┐  │
│  │  Session     │  │  Coordinator Brain    │  │
│  │  Registry    │  │  (LLM or rules-based) │  │
│  │             │  │                       │  │
│  │  sid → state │  │  Analyzes all session │  │
│  │  sid → state │  │  context every 30s    │  │
│  │  sid → state │  │  and injects messages │  │
│  └─────────────┘  └───────────────────────┘  │
└──────────┬───────────────────────────────────┘
           │
    ┌──────┴──────────────────────────┐
    │      │           │              │
┌───┴───┐ ┌┴────────┐ ┌┴──────────┐ ┌┴──────────┐
│Session│ │Session  │ │Session   │ │Session   │
│Proxy  │ │Proxy   │ │Proxy    │ │Proxy    │
│(PTY)  │ │(PTY)   │ │(PTY)    │ │(PTY)    │
│       │ │        │ │         │ │         │
│claude │ │bash    │ │npm dev  │ │pytest   │
└───────┘ └────────┘ └─────────┘ └─────────┘
```

### How it works

1. **Daemon** listens on a Unix domain socket (Linux/macOS) or TCP localhost:9742 (Windows). Maintains a registry of all sessions with their rolling output buffers.

2. **Session Proxy** wraps any command in a pseudo-terminal (PTY). Uses native `pty`/`fork` on Unix, `pywinpty` (ConPTY) on Windows. The user's experience is identical to running the command directly — full color, cursor control, interactive input. All I/O is transparently teed to the daemon.

3. **Coordinator Brain** runs every 30 seconds, examines all session states, and injects coordination messages when it detects conflicts, duplicated work, errors cascading between sessions, or other coordination opportunities.

### Platform details

| | Linux/macOS | Windows |
|---|---|---|
| IPC | Unix domain socket (`/tmp/jarvis.sock`) | TCP `127.0.0.1:9742` |
| PTY | Native `pty` + `fork` | `pywinpty` (ConPTY) |
| Background daemon | `fork` + `setsid` | `subprocess` + `CREATE_NO_WINDOW` |
| Temp files | `/tmp/jarvis*` | `%TEMP%\jarvis\*` |

### Why PTY proxy (not tmux/shell hooks)?

| Approach | I/O Visibility | Workflow Impact | Reliability |
|----------|---------------|-----------------|-------------|
| **PTY proxy** | Full bidirectional stream | Minimal (`jarvis spawn` prefix) | High — we own the pipe |
| tmux parasitism | Lossy (capture-pane polling) | None if already using tmux | Medium — timing-dependent |
| Shell hooks | Command-level only | None | Low — misses streaming output |

The PTY proxy is the only approach that gives us a reliable, real-time, complete picture of every session's I/O. The tradeoff — adding `jarvis spawn` before your command — is trivial and can be aliased away.

### How sessions know about Jarvis

When spawned via `jarvis spawn`, the child process gets these environment variables:

```
JARVIS_ACTIVE=1              # Flag: "you're being watched"
JARVIS_SESSION_ID=a1b2c3d4   # This session's unique ID
JARVIS_SOCKET=/tmp/jarvis.sock  # Unix socket path, or 127.0.0.1:9742 on Windows
```

This means AI coding agents (Claude Code, Aider, etc.) can be made Jarvis-aware. They can:
- Read `JARVIS_ACTIVE` to know they're part of a coordinated swarm
- Connect to `JARVIS_SOCKET` directly to query sibling session state
- Use `JARVIS_SESSION_ID` to identify themselves in coordination messages

## Quick Start

```bash
# Install
cd jarvis
pip install -e .

# On Windows, also install:
pip install pywinpty

# Start the daemon (background)
jarvis start -d
# Or: python -m jarvis start -d

# Spawn monitored sessions (in separate terminals)
jarvis spawn claude        # AI coding agent
jarvis spawn bash          # regular shell
jarvis spawn npm run dev   # dev server

# Check status
jarvis status

# View daemon logs
jarvis logs

# Stop daemon
jarvis stop
```

## Configuration

Set `ANTHROPIC_API_KEY` for LLM-powered coordination analysis (optional — falls back to pattern matching without it):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## What Jarvis detects (prototype)

**Rules-based (no API key):**
- Sessions working in the same directory
- Port conflicts (e.g., two dev servers on :3000)
- Error cascades (session A errors after session B modifies files)
- Parallel git operations

**LLM-powered (with API key):**
- Everything above, plus semantic understanding of what each session is doing
- Intelligent suggestions ("Session B is building the API that Session A needs — wait for it")
- Natural language coordination messages

## Future directions

- **Shared context bus**: Sessions can publish/subscribe to topics (e.g., "I just deployed to staging")
- **Dependency graph**: Declare what each session produces/consumes, auto-sequence work
- **TUI dashboard**: Rich terminal UI showing all sessions side-by-side with Jarvis annotations
- **MCP integration**: Expose Jarvis as an MCP server so Claude Code instances can query it natively
- **Persistent memory**: Track patterns across restarts ("last time these two tasks ran together, X went wrong")
