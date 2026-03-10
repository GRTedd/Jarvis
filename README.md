# Jarvis — Claude Code Orchestrator

A daemon that takes your prompts, decomposes them into subtasks, spawns parallel Claude Code CLI workers, and orchestrates them — all using your **Claude Max subscription**. No API key needed.

**Cross-platform:** Works on Linux, macOS, and Windows.

## What it does

1. **You give Jarvis a task** — from the web dashboard or CLI
2. **Jarvis decomposes it** — uses `claude -p` to break it into subtasks with dependencies
3. **Spawns parallel workers** — each subtask gets its own `claude -p` instance
4. **Orchestrates execution** — respects dependency ordering, runs independent work in parallel
5. **Auto-rotates context** — when a worker hits 50% context usage, Jarvis asks it to write a checkpoint, then spawns a fresh CLI to continue
6. **Coordinates sessions** — detects conflicts, duplicated work, and error cascades across all running sessions

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                   Jarvis Daemon                      │
│            (Python asyncio + aiohttp)                │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │
│  │ Orchestrator  │  │ Coordinator  │  │    Web    │  │
│  │              │  │    Brain     │  │ Dashboard │  │
│  │ Decomposes   │  │              │  │           │  │
│  │ tasks, spawns │  │ Analyzes all │  │ Real-time │  │
│  │ workers,     │  │ sessions via │  │ control   │  │
│  │ rotates ctx  │  │ claude -p    │  │ panel     │  │
│  └──────┬───────┘  └──────────────┘  └───────────┘  │
│         │                                            │
└─────────┼────────────────────────────────────────────┘
          │
   ┌──────┴──────────────────────────────┐
   │      │           │          │       │
┌──┴──┐ ┌─┴───┐ ┌────┴──┐ ┌────┴──┐ ┌──┴───┐
│ W#1 │ │ W#2 │ │ W#3  │ │ W#4  │ │ W#5  │
│     │ │     │ │      │ │      │ │      │
│claude│ │claude│ │claude │ │claude │ │claude │
│ -p  │ │ -p  │ │  -p  │ │  -p  │ │  -p  │
└─────┘ └─────┘ └──────┘ └──────┘ └──────┘
  All workers use your Claude Max subscription
```

## Quick Start

```bash
# Install
cd Jarvis
pip install -e .

# On Windows, also install:
pip install pywinpty

# Start the daemon (background)
jarvis start -d
# Or: python -m jarvis start -d

# Open the dashboard
# http://127.0.0.1:9743

# Submit a task from CLI
jarvis task "Build a REST API with user auth in this project"
jarvis task --cwd /path/to/project "Add unit tests for all endpoints"

# Or type your task in the dashboard prompt bar
```

## Features

### Task Orchestration
- Submit a high-level prompt, Jarvis breaks it into subtasks
- Parallel execution with configurable concurrency (default: 4 workers)
- Dependency tracking — subtask B waits for subtask A if needed
- Progress tracking with live output streaming on the dashboard

### Context Window Rotation
- Monitors each worker's context usage
- At 50% context, the worker is asked to create a checkpoint file
- A fresh `claude -p` instance picks up from the checkpoint
- Up to 5 rotations per subtask (safety limit)
- No work is lost — checkpoints include files modified, decisions made, and next steps

### Session Monitoring
You can also use Jarvis to monitor and coordinate manual sessions:

```bash
# Spawn monitored sessions (in separate terminals)
jarvis spawn claude        # AI coding agent
jarvis spawn bash          # regular shell
jarvis spawn npm run dev   # dev server

# Check status
jarvis status
```

### Coordination Brain
Analyzes all active sessions every 30 seconds using `claude -p` and detects:
- File conflicts (two sessions editing the same files)
- Port collisions (multiple dev servers on the same port)
- Error cascades (one session's changes breaking another)
- Parallel git operations that could cause merge conflicts
- Duplicated work across sessions

Falls back to fast rules-based pattern matching if `claude` CLI is unavailable.

### Web Dashboard
Real-time control panel at `http://127.0.0.1:9743`:
- Task submission with prompt bar
- Live subtask progress with expandable output
- Session cards with terminal output preview
- Coordination log sidebar
- Broadcast messages to all sessions
- Spawn new sessions from the UI

## CLI Reference

```
jarvis start [-d]          Start daemon (foreground, or -d for background)
jarvis stop                Stop the daemon
jarvis status              Show all monitored sessions
jarvis task "<prompt>"     Submit a task for orchestration
jarvis spawn <cmd> [args]  Run a command under Jarvis monitoring
jarvis logs                Tail the daemon log
```

## How it works under the hood

**Everything runs on `claude -p`** (Claude Code CLI in non-interactive mode):

| Component | What it does | How |
|---|---|---|
| Task decomposition | Breaks prompt into subtasks | `claude -p "decompose this..."` |
| Workers | Execute each subtask | `claude -p "do this subtask..."` |
| Checkpoints | Save state for context rotation | `claude -p "write checkpoint..."` |
| Coordination | Analyze sessions for conflicts | `claude -p "analyze these sessions..."` |
| Daemon | Routes, orchestrates, serves UI | Plain Python (no LLM) |

No API keys. No Anthropic SDK. Just `claude` CLI + your Max subscription.

## Platform details

| | Linux/macOS | Windows |
|---|---|---|
| IPC | Unix domain socket (`/tmp/jarvis.sock`) | TCP `127.0.0.1:9742` |
| PTY | Native `pty` + `fork` | `pywinpty` (ConPTY) |
| Background daemon | `fork` + `setsid` | `subprocess` + `CREATE_NO_WINDOW` |
| Temp files | `/tmp/jarvis*` | `%TEMP%\jarvis\*` |

## Environment variables

Inside monitored sessions (`jarvis spawn`), the child process gets:

```
JARVIS_ACTIVE=1              # Flag: "you're being watched"
JARVIS_SESSION_ID=a1b2c3d4   # This session's unique ID
JARVIS_SOCKET=/tmp/jarvis.sock  # Socket path, or 127.0.0.1:9742 on Windows
```

AI coding agents can read these to become Jarvis-aware and coordinate with siblings.
