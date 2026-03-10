"""
Jarvis Web Dashboard - HTTP + WebSocket server for the control panel.

Runs inside the daemon process on WEB_PORT (9743).
Provides:
  - GET /              → Dashboard HTML
  - GET /api/status    → Full daemon status
  - GET /api/sessions/{id}/output → Full output buffer
  - POST /api/spawn    → Spawn a new monitored session
  - POST /api/broadcast → Broadcast message to all sessions
  - POST /api/inject/{id} → Inject message into one session
  - POST /api/coordinate → Trigger coordination analysis
  - GET /ws            → WebSocket for real-time events
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .daemon import JarvisDaemon

log = logging.getLogger("jarvis.web")

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


def create_web_app(daemon: "JarvisDaemon") -> web.Application:
    app = web.Application()
    app["daemon"] = daemon

    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/sessions/{sid}/output", handle_session_output)
    app.router.add_post("/api/spawn", handle_spawn)
    app.router.add_post("/api/broadcast", handle_broadcast)
    app.router.add_post("/api/inject/{sid}", handle_inject)
    app.router.add_post("/api/coordinate", handle_coordinate)
    app.router.add_get("/ws", handle_ws)

    # Orchestrator endpoints
    app.router.add_post("/api/task", handle_task_submit)
    app.router.add_get("/api/tasks", handle_tasks_list)
    app.router.add_get("/api/tasks/{tid}", handle_task_detail)
    app.router.add_post("/api/tasks/{tid}/cancel", handle_task_cancel)

    return app


async def handle_dashboard(request: web.Request) -> web.Response:
    html = DASHBOARD_PATH.read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


async def handle_status(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    status = daemon.get_status()
    status["coordination_log"] = list(daemon.coordination_log)
    return web.json_response(status)


async def handle_session_output(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    sid = request.match_info["sid"]
    sess = daemon.sessions.get(sid)
    if not sess:
        return web.json_response({"error": "Session not found"}, status=404)

    tail = int(request.query.get("tail", "0"))
    output = sess.output_buffer
    if tail > 0:
        output = output[-tail:]

    return web.Response(text=output, content_type="text/plain")


async def handle_spawn(request: web.Request) -> web.Response:
    data = await request.json()
    cmd = data.get("cmd", "").strip()
    cwd = data.get("cwd", os.getcwd())

    if not cmd:
        return web.json_response({"error": "No command provided"}, status=400)

    try:
        from .protocol import IS_WINDOWS
        jarvis_root = Path(__file__).parent.parent

        if IS_WINDOWS:
            CREATE_NEW_CONSOLE = 0x00000010
            proc = subprocess.Popen(
                [sys.executable, "-m", "jarvis", "spawn"] + cmd.split(),
                cwd=cwd,
                creationflags=CREATE_NEW_CONSOLE,
            )
        else:
            # On Unix, open in a new terminal if possible
            proc = subprocess.Popen(
                [sys.executable, "-m", "jarvis", "spawn"] + cmd.split(),
                cwd=cwd,
                start_new_session=True,
            )

        return web.json_response({
            "ok": True,
            "pid": proc.pid,
            "cmd": cmd,
        })

    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_broadcast(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    data = await request.json()
    message = data.get("message", "").strip()

    if not message:
        return web.json_response({"error": "No message provided"}, status=400)

    formatted = f"\033[1;35m[JARVIS]\033[0m {message}"
    await daemon.broadcast(formatted)

    # Also notify dashboard WS clients
    await daemon.notify_ws({
        "event": "coordination",
        "message": message,
        "targets": "all",
        "severity": "info",
        "timestamp": time.time(),
    })

    return web.json_response({"ok": True, "delivered_to": daemon.get_status()["active_count"]})


async def handle_inject(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    sid = request.match_info["sid"]
    data = await request.json()
    message = data.get("message", "").strip()

    if not message:
        return web.json_response({"error": "No message provided"}, status=400)

    formatted = f"\033[1;35m[JARVIS]\033[0m {message}"
    ok = await daemon.inject_message(sid, formatted)

    if ok:
        return web.json_response({"ok": True})
    else:
        return web.json_response({"error": "Session not found or not active"}, status=404)


async def handle_coordinate(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    await daemon._analyze_and_coordinate()
    return web.json_response({
        "ok": True,
        "coordination_log": list(daemon.coordination_log[-5:]),
    })


async def handle_task_submit(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    data = await request.json()
    prompt = data.get("prompt", "").strip()
    cwd = data.get("cwd", os.getcwd())
    max_parallel = int(data.get("max_parallel", 4))

    if not prompt:
        return web.json_response({"error": "No prompt provided"}, status=400)

    task = await daemon.orchestrator.submit_task(prompt, cwd, max_parallel)
    return web.json_response({"ok": True, "task": task.to_dict()})


async def handle_tasks_list(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    tasks = daemon.orchestrator.get_all_tasks()
    return web.json_response({"tasks": tasks})


async def handle_task_detail(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    tid = request.match_info["tid"]
    task = daemon.orchestrator.get_task(tid)
    if not task:
        return web.json_response({"error": "Task not found"}, status=404)
    return web.json_response({"task": task})


async def handle_task_cancel(request: web.Request) -> web.Response:
    daemon = request.app["daemon"]
    tid = request.match_info["tid"]
    ok = await daemon.orchestrator.cancel_task(tid)
    if ok:
        return web.json_response({"ok": True})
    return web.json_response({"error": "Task not found"}, status=404)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    daemon = request.app["daemon"]
    daemon.ws_clients.add(ws)
    log.info(f"Dashboard WebSocket connected ({len(daemon.ws_clients)} clients)")

    # Send initial status snapshot
    status = daemon.get_status()
    status["coordination_log"] = list(daemon.coordination_log)
    await ws.send_json({"event": "status", **status})

    try:
        async for msg in ws:
            pass  # Dashboard is read-only via WS; actions go through REST
    except Exception:
        pass
    finally:
        daemon.ws_clients.discard(ws)
        log.info(f"Dashboard WebSocket disconnected ({len(daemon.ws_clients)} clients)")

    return ws
