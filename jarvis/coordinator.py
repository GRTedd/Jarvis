"""
Jarvis Coordinator Brain - LLM-powered session analysis.

This module provides the intelligence layer. It periodically receives
snapshots of all active sessions and uses Claude to:
  1. Understand what each session is doing
  2. Detect conflicts, duplicated work, or missed opportunities
  3. Generate coordination messages to inject into sessions

Can run in two modes:
  - "llm" mode: Calls the Anthropic API for deep analysis
  - "rules" mode: Uses pattern matching (no API key needed)
"""

import json
import logging
import os
from typing import Optional

log = logging.getLogger("jarvis.coordinator")

SYSTEM_PROMPT = """You are Jarvis, a coordination daemon watching multiple CLI terminal sessions.

You receive snapshots of terminal sessions that are running simultaneously. Your job is to:

1. UNDERSTAND what each session is doing based on its command, working directory, and recent output.
2. DETECT issues:
   - File conflicts (two sessions editing the same files)
   - Duplicated work (sessions doing the same thing independently)
   - Error cascades (one session's changes causing errors in another)
   - Dependency ordering (one session needs to wait for another)
   - Resource conflicts (port collisions, lock files, etc.)
3. SUGGEST coordination:
   - Warn sessions about potential conflicts
   - Tell sessions about relevant work happening elsewhere
   - Suggest ordering or sequencing of work
   - Flag when one session's output is relevant to another

Respond with a JSON array of coordination actions. Each action:
{
  "target_sessions": ["session_id1", "session_id2"],
  "message": "Brief, actionable message to display in the terminal",
  "severity": "info" | "warning" | "critical",
  "type": "conflict" | "duplicate" | "dependency" | "awareness" | "error_cascade"
}

If no coordination is needed, return an empty array: []

Be concise. Terminal messages should be one line, max two. Developers are busy.
Only flag things that are genuinely useful — don't be noisy."""


async def analyze_sessions_llm(sessions: list[dict]) -> list[dict]:
    """
    Use the Anthropic API to analyze sessions and generate coordination insights.
    Requires ANTHROPIC_API_KEY environment variable.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("No ANTHROPIC_API_KEY set, falling back to rules-based analysis")
        return analyze_sessions_rules(sessions)

    try:
        import httpx
    except ImportError:
        log.warning("httpx not installed, falling back to rules-based analysis")
        return analyze_sessions_rules(sessions)

    prompt = f"""Here are the currently active terminal sessions:

{json.dumps(sessions, indent=2)}

Analyze these sessions and return coordination actions as JSON."""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 1024,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=15.0,
            )

            if response.status_code != 200:
                log.error(f"API error: {response.status_code} {response.text}")
                return analyze_sessions_rules(sessions)

            data = response.json()
            text = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block["text"]

            # Parse JSON from response
            # Handle potential markdown wrapping
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]

            actions = json.loads(text)
            return actions if isinstance(actions, list) else []

    except Exception as e:
        log.error(f"LLM analysis failed: {e}")
        return analyze_sessions_rules(sessions)


def analyze_sessions_rules(sessions: list[dict]) -> list[dict]:
    """
    Rules-based fallback coordinator. Fast, no API needed.
    Catches common patterns.
    """
    actions = []

    if len(sessions) < 2:
        return actions

    # Pattern: Same working directory
    from collections import defaultdict
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

    # Pattern: Port conflicts (common in dev)
    port_users = defaultdict(list)
    for s in sessions:
        output = s.get("recent_output", "")
        # Detect common port patterns
        import re
        ports = re.findall(r'(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{4,5})', output)
        for port in ports:
            port_users[port].append(s)

    for port, users in port_users.items():
        if len(users) > 1:
            ids = [s["session_id"] for s in users]
            actions.append({
                "target_sessions": ids,
                "message": f"Port {port} referenced by multiple sessions — possible conflict",
                "severity": "warning",
                "type": "conflict",
            })

    # Pattern: One session has errors, another modified files
    error_sessions = []
    modifier_sessions = []
    for s in sessions:
        output = s.get("recent_output", "").lower()
        if any(kw in output for kw in ["error", "traceback", "failed", "exception", "enoent"]):
            error_sessions.append(s)
        if any(kw in output for kw in ["wrote", "saved", "created", "modified", "✓", "commit"]):
            modifier_sessions.append(s)

    for err_s in error_sessions:
        for mod_s in modifier_sessions:
            if err_s["session_id"] != mod_s["session_id"]:
                actions.append({
                    "target_sessions": [err_s["session_id"]],
                    "message": (
                        f"Session {mod_s['session_id'][:8]} recently modified files — "
                        f"might be related to your errors"
                    ),
                    "severity": "info",
                    "type": "error_cascade",
                })

    # Pattern: Git operations happening in parallel
    git_sessions = [
        s for s in sessions
        if "git" in s.get("recent_output", "").lower()
        or "git" in s.get("cmd", "").lower()
    ]
    if len(git_sessions) > 1:
        ids = [s["session_id"] for s in git_sessions]
        actions.append({
            "target_sessions": ids,
            "message": "Multiple sessions doing git operations — coordinate to avoid merge conflicts",
            "severity": "warning",
            "type": "conflict",
        })

    return actions
