"""
Jarvis Orchestrator — Takes a high-level prompt, decomposes it into subtasks
using `claude -p`, then spawns and manages Claude Code workers for each subtask.

Uses your Claude Max subscription via the CLI — no API key needed.
Automatically discovers and forwards all installed plugins, MCP servers,
and skills to spawned workers.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .daemon import JarvisDaemon

log = logging.getLogger("jarvis.orchestrator")


# ---------------------------------------------------------------------------
# Plugin / MCP / Skill discovery
# ---------------------------------------------------------------------------

def _claude_config_dir() -> Path:
    """Return the Claude Code config directory (~/.claude)."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude"


def discover_plugins() -> list[str]:
    """
    Discover all enabled plugin directories from the user's Claude Code config.

    Reads ~/.claude/settings.json → enabledPlugins, then resolves each plugin
    to its latest cached directory under ~/.claude/plugins/cache/<marketplace>/<name>/<version>.
    Returns a list of absolute paths suitable for --plugin-dir flags.
    """
    config_dir = _claude_config_dir()
    settings_file = config_dir / "settings.json"

    if not settings_file.exists():
        log.warning("No Claude settings.json found — workers will run without plugins")
        return []

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Failed to read Claude settings: {e}")
        return []

    enabled = settings.get("enabledPlugins", {})
    cache_root = config_dir / "plugins" / "cache"
    plugin_dirs = []

    for plugin_key, is_enabled in enabled.items():
        if not is_enabled:
            continue

        # plugin_key format: "name@marketplace" e.g. "context7@claude-plugins-official"
        parts = plugin_key.rsplit("@", 1)
        if len(parts) != 2:
            log.debug(f"Skipping malformed plugin key: {plugin_key}")
            continue

        name, marketplace = parts
        plugin_cache = cache_root / marketplace / name

        if not plugin_cache.exists():
            log.debug(f"Plugin cache not found for {plugin_key}: {plugin_cache}")
            continue

        # Pick the latest version directory (most recently modified)
        versions = sorted(plugin_cache.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        if not versions:
            continue

        latest = versions[0]
        if latest.is_dir():
            plugin_dirs.append(str(latest))
            log.debug(f"Discovered plugin: {name} @ {latest}")

    log.info(f"Discovered {len(plugin_dirs)} plugin(s) for worker forwarding")
    return plugin_dirs


def build_plugin_flags() -> list[str]:
    """Build CLI flags to forward all discovered plugins to a claude subprocess."""
    dirs = discover_plugins()
    flags = []
    for d in dirs:
        flags.extend(["--plugin-dir", d])
    return flags


def describe_plugins_for_worker() -> str:
    """
    Generate a human-readable summary of available plugins/skills for the worker preamble.
    Workers should know what tools they have access to.
    """
    config_dir = _claude_config_dir()
    settings_file = config_dir / "settings.json"

    if not settings_file.exists():
        return ""

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ""

    enabled = settings.get("enabledPlugins", {})
    active = [k.rsplit("@", 1)[0] for k, v in enabled.items() if v]

    if not active:
        return ""

    lines = [
        "## Available Plugins & Tools",
        "You have access to the following plugins (with their MCP servers and skills):",
    ]
    for name in sorted(active):
        lines.append(f"  - {name}")

    lines.append("")
    lines.append(
        "Use these plugins when they're relevant to your task. For example, use "
        "playwright for browser testing, firecrawl for web scraping, context7 for "
        "documentation lookup, github for PR/issue operations, etc."
    )
    lines.append("")
    return "\n".join(lines)


class TaskStatus(str, Enum):
    PENDING = "pending"
    DECOMPOSING = "decomposing"
    RUNNING = "running"
    WAITING = "waiting"      # waiting on dependencies
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Subtask:
    id: str
    title: str
    prompt: str
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    session_id: Optional[str] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    output: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # Agent communication
    pending_questions: list = field(default_factory=list)
    _inbox: list = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt[:200] + ("..." if len(self.prompt) > 200 else ""),
            "depends_on": self.depends_on,
            "status": self.status.value,
            "session_id": self.session_id,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "output_tail": self.output[-500:] if self.output else "",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": (
                round((self.finished_at or time.time()) - self.started_at, 1)
                if self.started_at else None
            ),
            "pending_questions": [
                {"qid": q["qid"], "question": q["question"], "subtask_id": q["subtask_id"]}
                for q in self.pending_questions
            ],
        }


@dataclass
class Task:
    id: str
    prompt: str
    cwd: str
    status: TaskStatus = TaskStatus.PENDING
    subtasks: list[Subtask] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    error: Optional[str] = None
    max_parallel: int = 4

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt[:300] + ("..." if len(self.prompt) > 300 else ""),
            "cwd": self.cwd,
            "status": self.status.value,
            "subtasks": [s.to_dict() for s in self.subtasks],
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "max_parallel": self.max_parallel,
            "progress": self._progress(),
        }

    def _progress(self) -> dict:
        total = len(self.subtasks)
        if total == 0:
            return {"total": 0, "completed": 0, "running": 0, "failed": 0, "pending": 0}
        return {
            "total": total,
            "completed": sum(1 for s in self.subtasks if s.status == TaskStatus.COMPLETED),
            "running": sum(1 for s in self.subtasks if s.status == TaskStatus.RUNNING),
            "failed": sum(1 for s in self.subtasks if s.status == TaskStatus.FAILED),
            "pending": sum(1 for s in self.subtasks if s.status in (TaskStatus.PENDING, TaskStatus.WAITING)),
        }


CONTEXT_ROTATION_THRESHOLD = 50  # Rotate at 50% context usage

CHECKPOINT_FILENAME = ".jarvis_checkpoint_{subtask_id}.md"

CHECKPOINT_PROMPT = """You are continuing work from a previous Claude Code session that ran out of context.

Here is the checkpoint created by the previous session:
{checkpoint}

Here is the original task:
{original_task}

Continue where the previous session left off. Do NOT redo work that's already done.
Focus on what still needs to be completed. The checkpoint above was written by the
previous worker to tell you exactly where they left off."""

DECOMPOSE_PROMPT = (
    "You are a task decomposition engine. Given a high-level task, break it into "
    "concrete subtasks that can each be executed by an independent Claude Code CLI instance.\n\n"
    "Rules:\n"
    "- Each subtask should be a self-contained unit of work\n"
    "- Subtasks that can run in parallel should have no dependencies between them\n"
    "- If subtask B needs subtask A to finish first, mark B as depending on A\n"
    "- Each subtask gets its own terminal - they share the filesystem but not state\n"
    "- Be specific in each subtask prompt - include file paths, function names, etc.\n"
    "- Keep subtask count reasonable (2-8 typically)\n"
    "- If the task is simple enough for one worker, return just one subtask\n"
    "- Workers have access to these plugins: {plugins}. Instruct workers to use "
    "relevant plugins when applicable (e.g. playwright for browser testing, "
    "firecrawl for web scraping, context7 for docs lookup, github for PR ops, etc.)\n\n"
    "Working directory: {cwd}\n\n"
    "Return ONLY valid JSON (no markdown, no explanation) in this exact format:\n"
    '{{\n'
    '  "subtasks": [\n'
    '    {{\n'
    '      "id": "1",\n'
    '      "title": "Short title",\n'
    '      "prompt": "Detailed prompt for the Claude Code worker...",\n'
    '      "depends_on": []\n'
    '    }},\n'
    '    {{\n'
    '      "id": "2",\n'
    '      "title": "Short title",\n'
    '      "prompt": "Detailed prompt...",\n'
    '      "depends_on": ["1"]\n'
    '    }}\n'
    '  ]\n'
    '}}\n\n'
    "Task to decompose:\n{task}"
)


WORKER_PREAMBLE = """You are worker #{subtask_id} ("{title}") of task {task_id}, running under Jarvis orchestration.
Jarvis API: {api_url}

## Communication with Jarvis
If you are uncertain, blocked, or need clarification, ask Jarvis:
  curl -s -X POST {api_url}/api/agent/ask \\
    -H 'Content-Type: application/json' \\
    -d '{{"task_id":"{task_id}","subtask_id":"{subtask_id}","question":"your question here"}}'
The response JSON has an "answer" field. This call BLOCKS until a human or Jarvis answers (up to 5 min).

## Messaging sibling workers
To send a message to another worker (e.g. worker #2):
  curl -s -X POST {api_url}/api/agent/message \\
    -H 'Content-Type: application/json' \\
    -d '{{"task_id":"{task_id}","from_id":"{subtask_id}","to_id":"2","message":"your message"}}'

## Checking for messages from Jarvis or siblings
Every few tool calls, check your inbox:
  curl -s {api_url}/api/agent/poll/{task_id}/{subtask_id}
Returns {{"messages": [...]}}. Act on any messages you receive.

{plugins_section}
---
"""


class Orchestrator:
    def __init__(self, daemon: "JarvisDaemon"):
        self.daemon = daemon
        self.tasks: dict[str, Task] = {}
        self._monitor_task: Optional[asyncio.Task] = None

    async def submit_task(self, prompt: str, cwd: str, max_parallel: int = 4) -> Task:
        """Submit a new task for orchestration."""
        task = Task(
            id=uuid.uuid4().hex[:10],
            prompt=prompt,
            cwd=cwd,
            max_parallel=max_parallel,
        )
        self.tasks[task.id] = task

        await self.daemon.notify_ws({
            "event": "task_submitted",
            "task": task.to_dict(),
        })

        # Start decomposition in background
        asyncio.create_task(self._run_task(task))
        return task

    async def cancel_task(self, task_id: str) -> bool:
        """Cancel a running task."""
        task = self.tasks.get(task_id)
        if not task:
            return False

        task.status = TaskStatus.CANCELLED
        for sub in task.subtasks:
            if sub.status in (TaskStatus.PENDING, TaskStatus.WAITING, TaskStatus.RUNNING):
                sub.status = TaskStatus.CANCELLED
        await self._notify_task_update(task)
        return True

    async def message_worker(self, task_id: str, subtask_id: str, message: str) -> bool:
        """Push a message into a worker's inbox for pickup via polling."""
        task = self.tasks.get(task_id)
        if not task:
            return False
        subtask = next((s for s in task.subtasks if s.id == subtask_id), None)
        if not subtask:
            return False
        subtask._inbox.append({
            "from": "jarvis",
            "message": message,
            "timestamp": time.time(),
        })
        await self.daemon.notify_ws({
            "event": "worker_messaged",
            "task_id": task_id,
            "subtask_id": subtask_id,
            "message": message,
            "timestamp": time.time(),
        })
        return True

    async def _run_task(self, task: Task):
        """Main orchestration loop for a task."""
        try:
            # Step 1: Decompose
            task.status = TaskStatus.DECOMPOSING
            await self._notify_task_update(task)

            subtasks = await self._decompose(task)
            if not subtasks:
                task.status = TaskStatus.FAILED
                task.error = "Failed to decompose task into subtasks"
                await self._notify_task_update(task)
                return

            task.subtasks = subtasks
            task.status = TaskStatus.RUNNING
            await self._notify_task_update(task)

            # Step 2: Execute subtasks respecting dependencies
            await self._execute_subtasks(task)

            # Step 3: Mark complete
            failed = [s for s in task.subtasks if s.status == TaskStatus.FAILED]
            if task.status == TaskStatus.CANCELLED:
                pass  # already set
            elif failed:
                task.status = TaskStatus.FAILED
                task.error = f"{len(failed)} subtask(s) failed"
            else:
                task.status = TaskStatus.COMPLETED

            task.finished_at = time.time()
            await self._notify_task_update(task)

        except Exception as e:
            log.error(f"Task {task.id} error: {e}", exc_info=True)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            await self._notify_task_update(task)

    async def _decompose(self, task: Task) -> list[Subtask]:
        """Use `claude -p` to decompose the task into subtasks."""
        # List active plugin names for the decomposition prompt
        try:
            config_dir = _claude_config_dir()
            with open(config_dir / "settings.json", "r", encoding="utf-8") as f:
                _settings = json.load(f)
            _active = [k.rsplit("@", 1)[0] for k, v in _settings.get("enabledPlugins", {}).items() if v]
            plugins_str = ", ".join(sorted(_active)) if _active else "none"
        except Exception:
            plugins_str = "none"

        prompt = DECOMPOSE_PROMPT.format(cwd=task.cwd, task=task.prompt, plugins=plugins_str)

        log.info(f"Decomposing task {task.id}...")
        result = await self._run_claude(prompt, task.cwd)

        if not result:
            log.error(f"Decomposition returned empty result for task {task.id}")
            return []

        try:
            # Try to parse JSON — claude might wrap it in markdown
            text = result.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                text = text.rsplit("```", 1)[0]

            data = json.loads(text)
            subtasks_raw = data.get("subtasks", [])

            subtasks = []
            for s in subtasks_raw:
                subtasks.append(Subtask(
                    id=s["id"],
                    title=s.get("title", f"Subtask {s['id']}"),
                    prompt=s["prompt"],
                    depends_on=s.get("depends_on", []),
                ))

            log.info(f"Task {task.id} decomposed into {len(subtasks)} subtasks")
            return subtasks

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            log.error(f"Failed to parse decomposition: {e}\nRaw: {result[:500]}")
            # Fallback: treat the whole thing as a single subtask
            return [Subtask(
                id="1",
                title="Full task",
                prompt=task.prompt,
            )]

    async def _execute_subtasks(self, task: Task):
        """Execute subtasks respecting dependencies and parallelism."""
        while True:
            if task.status == TaskStatus.CANCELLED:
                break

            # Check what's done, what's runnable
            all_done = True
            any_failed = False
            running_count = 0

            for sub in task.subtasks:
                if sub.status == TaskStatus.RUNNING:
                    running_count += 1
                    all_done = False
                elif sub.status in (TaskStatus.PENDING, TaskStatus.WAITING):
                    all_done = False
                elif sub.status == TaskStatus.FAILED:
                    any_failed = True

            if all_done:
                break

            # Find subtasks ready to run (dependencies met)
            ready = []
            for sub in task.subtasks:
                if sub.status not in (TaskStatus.PENDING, TaskStatus.WAITING):
                    continue

                deps_met = all(
                    any(d.id == dep_id and d.status == TaskStatus.COMPLETED
                        for d in task.subtasks)
                    for dep_id in sub.depends_on
                )

                deps_failed = any(
                    any(d.id == dep_id and d.status == TaskStatus.FAILED
                        for d in task.subtasks)
                    for dep_id in sub.depends_on
                )

                if deps_failed:
                    sub.status = TaskStatus.FAILED
                    sub.output = "Dependency failed"
                    await self._notify_task_update(task)
                    continue

                if deps_met:
                    ready.append(sub)
                else:
                    sub.status = TaskStatus.WAITING

            # Launch ready subtasks up to max_parallel
            slots = task.max_parallel - running_count
            for sub in ready[:slots]:
                asyncio.create_task(self._run_subtask(task, sub))

            # Wait a bit before checking again
            await asyncio.sleep(2)

    async def _run_subtask(self, task: Task, subtask: Subtask, continuation_prompt: str = None):
        """Spawn a Claude Code CLI worker for a subtask, with context rotation."""
        subtask.status = TaskStatus.RUNNING
        subtask.started_at = subtask.started_at or time.time()
        await self._notify_task_update(task)

        rotation_count = 0
        max_rotations = 5  # Safety limit

        while rotation_count <= max_rotations:
            log.info(f"Starting subtask {subtask.id}: {subtask.title} (rotation #{rotation_count})")

            try:
                needs_rotation = await self._run_worker_process(
                    task, subtask, continuation_prompt
                )

                if needs_rotation and rotation_count < max_rotations:
                    # Context limit hit — create checkpoint and rotate
                    rotation_count += 1
                    log.info(f"Subtask {subtask.id} hit context limit, rotating (#{rotation_count})")

                    await self.daemon.notify_ws({
                        "event": "context_rotation",
                        "task_id": task.id,
                        "subtask_id": subtask.id,
                        "context_pct": CONTEXT_ROTATION_THRESHOLD,
                        "rotation": rotation_count,
                        "timestamp": time.time(),
                    })

                    # Read the checkpoint created by the worker itself
                    checkpoint = await self._read_checkpoint(task, subtask)
                    continuation_prompt = CHECKPOINT_PROMPT.format(
                        checkpoint=checkpoint,
                        original_task=subtask.prompt,
                    )

                    # Reset output for the new session (keep history in a separate field)
                    subtask.output += f"\n\n--- CONTEXT ROTATION #{rotation_count} ---\n\n"
                    subtask.pid = None
                    continue
                else:
                    break

            except Exception as e:
                subtask.status = TaskStatus.FAILED
                subtask.output += f"\nError: {e}"
                subtask.finished_at = time.time()
                log.error(f"Subtask {subtask.id} error: {e}")
                break

        await self._notify_task_update(task)

    async def _run_worker_process(self, task: Task, subtask: Subtask,
                                   continuation_prompt: str = None) -> bool:
        """
        Run a claude worker process with context monitoring.
        Uses `claude` in pipe mode (stdin/stdout) so we can send a checkpoint
        instruction when context hits 50%.
        Returns True if context rotation is needed (checkpoint was created).
        """
        from .protocol import IS_WINDOWS

        # Build API URL from daemon's web port
        from .protocol import WEB_PORT
        api_url = f"http://localhost:{WEB_PORT}"

        preamble = WORKER_PREAMBLE.format(
            subtask_id=subtask.id,
            title=subtask.title,
            task_id=task.id,
            api_url=api_url,
            plugins_section=describe_plugins_for_worker(),
        )

        if continuation_prompt:
            prompt = preamble + continuation_prompt
        else:
            prompt = (
                preamble +
                f"Your specific assignment:\n\n{subtask.prompt}\n\n"
                f"Work in the current directory. Be thorough but focused on your specific task."
            )

        checkpoint_file = os.path.join(
            task.cwd,
            CHECKPOINT_FILENAME.format(subtask_id=subtask.id),
        )

        # Use -p for the initial prompt — one-shot mode.
        # For checkpoint-aware rotation, we'll use --output-format=stream-json
        # so we can detect context usage from the streaming metadata.
        # Forward all user plugins so workers have access to MCP servers, skills, etc.
        plugin_flags = build_plugin_flags()
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"] + plugin_flags

        if IS_WINDOWS:
            CREATE_NO_WINDOW = 0x08000000
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=task.cwd,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=task.cwd,
            )

        subtask.pid = proc.pid

        await self.daemon.notify_ws({
            "event": "worker_started",
            "task_id": task.id,
            "subtask": subtask.to_dict(),
        })

        # Stream output and monitor for context usage
        buffer = []
        needs_rotation = False
        context_pct = 0
        session_id = None

        while True:
            line = await proc.stdout.readline()
            if not line:
                break

            raw = line.decode("utf-8", errors="replace")

            # Try to parse stream-json events for context info
            text_to_display = ""
            try:
                event = json.loads(raw)
                # Stream JSON events may contain result with usage info
                etype = event.get("type")
                if etype == "result":
                    session_id = event.get("session_id")
                    model_usage = event.get("modelUsage", {})
                    for _model, usage in model_usage.items():
                        input_tokens = usage.get("inputTokens", 0)
                        cache_read = usage.get("cacheReadInputTokens", 0)
                        cache_creation = usage.get("cacheCreationInputTokens", 0)
                        context_window = usage.get("contextWindow", 200_000)
                        total_used = input_tokens + cache_read + cache_creation
                        if context_window > 0:
                            context_pct = int((total_used / context_window) * 100)
                        break

                elif etype == "assistant":
                    # Initial message, may contain text
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            text_to_display += block.get("text", "")

            except (json.JSONDecodeError, KeyError):
                # Not JSON — raw text output
                text_to_display = raw

            if text_to_display:
                buffer.append(text_to_display)
                subtask.output += text_to_display

                # Keep output bounded
                if len(subtask.output) > 100_000:
                    subtask.output = subtask.output[-100_000:]

            # --- Check if context threshold hit ---
            if context_pct >= CONTEXT_ROTATION_THRESHOLD and not needs_rotation:
                log.warning(
                    f"Subtask {subtask.id} context at {context_pct}% — "
                    f"asking worker to create checkpoint before rotation"
                )
                needs_rotation = True

                # Don't kill yet — instead, let this run finish and then
                # ask a SEPARATE claude -p to create the checkpoint from
                # the worker's output + file state. The current -p run
                # will complete on its own.
                #
                # We flag for rotation and let it finish naturally,
                # or terminate if it's still running after we get the checkpoint.

            # Notify dashboard with batched output
            if len(buffer) >= 5:
                await self.daemon.notify_ws({
                    "event": "worker_output",
                    "task_id": task.id,
                    "subtask_id": subtask.id,
                    "data": "".join(buffer),
                })
                buffer = []

        # Flush remaining
        if buffer:
            await self.daemon.notify_ws({
                "event": "worker_output",
                "task_id": task.id,
                "subtask_id": subtask.id,
                "data": "".join(buffer),
            })

        exit_code = await proc.wait()

        if needs_rotation and session_id:
            checkpoint_prompt = (
                f"Your context window is at {context_pct}%. "
                f"Write a detailed handoff checkpoint to: {checkpoint_file}\n\n"
                f"Include:\n"
                f"1. Everything you completed (specific files, functions, changes made)\n"
                f"2. Any work that was in progress or partially done\n"
                f"3. What still needs to be done to finish the original task\n"
                f"4. Key decisions made and why\n"
                f"5. Exact next steps for the session taking over\n\n"
                f"Write the file now. Be precise — the next session has no memory except this file."
            )
            await self._run_claude_resume(session_id, checkpoint_prompt, task.cwd)
            subtask.output += f"\n\n--- CONTEXT ROTATION (session {session_id}, {context_pct}%) ---\n\n"
            subtask.pid = None
            return True

        subtask.exit_code = exit_code
        subtask.finished_at = time.time()

        # Clean up any leftover checkpoint file from previous rotations
        if os.path.exists(checkpoint_file):
            try:
                os.unlink(checkpoint_file)
            except OSError:
                pass

        if exit_code == 0:
            subtask.status = TaskStatus.COMPLETED
            log.info(f"Subtask {subtask.id} completed successfully")
        else:
            subtask.status = TaskStatus.FAILED
            log.warning(f"Subtask {subtask.id} failed with exit code {exit_code}")

        return False

    async def _run_claude_resume(self, session_id: str, prompt: str, cwd: str) -> str:
        """Resume the same session so the original worker writes its own checkpoint."""
        from .protocol import IS_WINDOWS

        plugin_flags = build_plugin_flags()
        cmd = ["claude", "--resume", session_id, "-p", prompt, "--verbose"] + plugin_flags

        kwargs = dict(
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000

        try:
            proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            result = stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                log.warning(f"claude --resume returned {proc.returncode}: {stderr.decode()[:200]}")
            return result
        except asyncio.TimeoutError:
            log.error(f"claude --resume {session_id} timed out")
            proc.kill()
            return ""

    async def _read_checkpoint(self, task: Task, subtask: Subtask) -> str:
        """
        Read the checkpoint file created by the previous worker.
        Falls back to output tail if no checkpoint file exists.
        """
        checkpoint_file = os.path.join(
            task.cwd,
            CHECKPOINT_FILENAME.format(subtask_id=subtask.id),
        )

        if os.path.exists(checkpoint_file):
            try:
                with open(checkpoint_file, "r", encoding="utf-8") as f:
                    checkpoint = f.read()
                log.info(f"Read checkpoint from {checkpoint_file} ({len(checkpoint)} chars)")
                return checkpoint
            except Exception as e:
                log.warning(f"Failed to read checkpoint file: {e}")

        # Fallback: use raw output tail
        log.warning(f"No checkpoint file found, using output tail")
        return f"Previous session output (tail):\n{subtask.output[-5000:]}"

    async def _run_claude(self, prompt: str, cwd: str) -> str:
        """Run `claude -p` and capture output, with all user plugins forwarded."""
        from .protocol import IS_WINDOWS

        plugin_flags = build_plugin_flags()
        cmd = ["claude", "-p", prompt] + plugin_flags

        try:
            if IS_WINDOWS:
                CREATE_NO_WINDOW = 0x08000000
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    creationflags=CREATE_NO_WINDOW,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            result = stdout.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                log.warning(f"claude -p returned {proc.returncode}: {stderr.decode()[:200]}")

            return result

        except asyncio.TimeoutError:
            log.error("claude -p timed out during decomposition")
            proc.kill()
            return ""
        except FileNotFoundError:
            log.error("'claude' CLI not found. Make sure Claude Code is installed and in PATH.")
            return ""

    async def _notify_task_update(self, task: Task):
        """Push task state to dashboard."""
        await self.daemon.notify_ws({
            "event": "task_update",
            "task": task.to_dict(),
        })

    def get_all_tasks(self) -> list[dict]:
        """Return all tasks for the API."""
        return [t.to_dict() for t in self.tasks.values()]

    def get_task(self, task_id: str) -> Optional[dict]:
        """Return a single task."""
        task = self.tasks.get(task_id)
        return task.to_dict() if task else None
