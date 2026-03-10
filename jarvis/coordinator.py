"""
Jarvis Coordinator Brain — session analysis via Claude Code CLI.

Uses `claude -p` (your Max subscription) to analyze sessions.
Falls back to rules-based pattern matching if claude CLI is unavailable.
No API key needed.
"""

import asyncio
import json
import logging
import re
from collections import defaultdict

log = logging.getLogger("jarvis.coordinator")

SYSTEM_PROMPT = (
    "You are Jarvis, a coordination daemon watching multiple CLI terminal sessions.\n\n"
    "You receive snapshots of terminal sessions running simultaneously. Your job:\n\n"
    "1. UNDERSTAND what each session is doing based on its command, cwd, and recent output.\n"
    "2. DETECT issues:\n"
    "   - File conflicts (two sessions editing the same files)\n"
    "   - Duplicated work (sessions doing the same thing independently)\n"
    "   - Error cascades (one session's changes causing errors in another)\n"
    "   - Dependency ordering (one session needs to wait for another)\n"
    "   - Resource conflicts (port collisions, lock files, etc.)\n"
    "3. SUGGEST coordination:\n"
    "   - Warn sessions about potential conflicts\n"
    "   - Tell sessions about relevant work happening elsewhere\n"
    "   - Suggest ordering or sequencing of work\n"
    "   - Flag when one session's output is relevant to another\n\n"
    "Respond with ONLY a JSON array of coordination actions. Each action:\n"
    '{"target_sessions": ["id1"], "message": "Brief message", '
    '"severity": "info"|"warning"|"critical", '
    '"type": "conflict"|"duplicate"|"dependency"|"awareness"|"error_cascade"}\n\n'
    "If no coordination is needed, return: []\n"
    "Be concise. One line per message. Only flag genuinely useful things."
)


async def analyze_sessions_llm(sessions: list[dict]) -> list[dict]:
    """
    Use `claude -p` (Claude Code CLI) to analyze sessions.
    Uses your Max subscription — no API key needed.
    """
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Here are the currently active terminal sessions:\n\n"
        f"{json.dumps(sessions, indent=2)}\n\n"
        f"Analyze and return coordination actions as a JSON array."
    )

    try:
        from .protocol import IS_WINDOWS

        cmd = ["claude", "-p", prompt]

        if IS_WINDOWS:
            CREATE_NO_WINDOW = 0x08000000
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        text = stdout.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            log.warning(f"claude -p returned {proc.returncode}, falling back to rules")
            return analyze_sessions_rules(sessions)

        # Parse JSON — handle potential markdown wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]

        actions = json.loads(text)
        return actions if isinstance(actions, list) else []

    except asyncio.TimeoutError:
        log.warning("claude -p timed out, falling back to rules")
        return analyze_sessions_rules(sessions)
    except FileNotFoundError:
        log.warning("claude CLI not found, falling back to rules")
        return analyze_sessions_rules(sessions)
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"LLM analysis failed: {e}, falling back to rules")
        return analyze_sessions_rules(sessions)


def analyze_sessions_rules(sessions: list[dict]) -> list[dict]:
    """
    Rules-based fallback coordinator. Fast, no CLI call needed.
    """
    actions = []

    if len(sessions) < 2:
        return actions

    # Pattern: Same working directory
    cwd_groups = defaultdict(list)
    for s in sessions:
        cwd_groups[s["cwd"]].append(s)

    for cwd, group in cwd_groups.items():
        if len(group) > 1:
            ids = [s["session_id"] for s in group]
            names = [f"{s['session_id'][:8]}({s['cmd'].split()[0]})" for s in group]
            actions.append({
                "target_sessions": ids,
                "message": f"Heads up: {', '.join(names)} are all working in {cwd}",
                "severity": "warning",
                "type": "conflict",
            })

    # Pattern: Port conflicts
    port_users = defaultdict(list)
    for s in sessions:
        output = s.get("recent_output", "")
        ports = re.findall(r'(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{4,5})', output)
        for port in ports:
            port_users[port].append(s)

    for port, users in port_users.items():
        if len(users) > 1:
            ids = [s["session_id"] for s in users]
            actions.append({
                "target_sessions": ids,
                "message": f"Port {port} referenced by multiple sessions - possible conflict",
                "severity": "warning",
                "type": "conflict",
            })

    # Pattern: One session erroring, another modified files
    error_sessions = []
    modifier_sessions = []
    for s in sessions:
        output = s.get("recent_output", "").lower()
        if any(kw in output for kw in ["error", "traceback", "failed", "exception", "enoent"]):
            error_sessions.append(s)
        if any(kw in output for kw in ["wrote", "saved", "created", "modified", "commit"]):
            modifier_sessions.append(s)

    for err_s in error_sessions:
        for mod_s in modifier_sessions:
            if err_s["session_id"] != mod_s["session_id"]:
                actions.append({
                    "target_sessions": [err_s["session_id"]],
                    "message": (
                        f"Session {mod_s['session_id'][:8]} recently modified files - "
                        f"might be related to your errors"
                    ),
                    "severity": "info",
                    "type": "error_cascade",
                })

    # Pattern: Git operations in parallel
    git_sessions = [
        s for s in sessions
        if "git" in s.get("recent_output", "").lower()
        or "git" in s.get("cmd", "").lower()
    ]
    if len(git_sessions) > 1:
        ids = [s["session_id"] for s in git_sessions]
        actions.append({
            "target_sessions": ids,
            "message": "Multiple sessions doing git operations - coordinate to avoid merge conflicts",
            "severity": "warning",
            "type": "conflict",
        })

    return actions
