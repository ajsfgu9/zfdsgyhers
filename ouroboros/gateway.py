"""Event-Driven HTTP Gateway & Zero-Polling Cron Scheduler (OpenClaw Control Plane Pattern)."""

from __future__ import annotations

import dataclasses
import http.server
import json
import threading
import time
from typing import Callable, Dict, List, Optional

from ouroboros.loop import OuroborosManagerAgent


@dataclasses.dataclass
class ScheduledRoutine:
    name: str
    interval_seconds: int
    prompt: str
    last_run: float = 0.0


class OuroborosGatewayServer:
    """Single-process hub exposing REST `/v1/tasks` and event-driven scheduled wakeups."""

    def __init__(
        self,
        agent: OuroborosManagerAgent,
        host: str = "127.0.0.1",
        port: int = 8787,
        routines: Optional[List[ScheduledRoutine]] = None,
    ) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self.routines = routines or []
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def run_due_routines_once(self, now: Optional[float] = None) -> List[Dict[str, object]]:
        now = now if now is not None else time.time()
        results = []
        for routine in self.routines:
            if now - routine.last_run >= routine.interval_seconds:
                with self._lock:
                    res = self.agent.run_task(
                        user_prompt=f"[Scheduled Routine: {routine.name}] {routine.prompt}",
                        session_id=f"cron_{routine.name}",
                    )
                routine.last_run = now
                results.append({"routine": routine.name, "result": res})
        return results

    def serve_forever(self) -> None:
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/healthz":
                    self._send_json(200, {"status": "ok", "service": "ouroboros-gateway"})
                    return
                if self.path == "/v1/state":
                    self._send_json(
                        200,
                        {
                            "task_checklist": gateway.agent.memory.read_task_checklist(),
                            "skills": gateway.agent.skills.list_catalog_prompt(),
                        },
                    )
                    return
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if self.path != "/v1/tasks":
                    self._send_json(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                prompt = body.get("prompt", "")
                session_id = body.get("session_id")
                if not prompt:
                    self._send_json(400, {"error": "Missing 'prompt' field"})
                    return
                with gateway._lock:
                    result = gateway.agent.run_task(user_prompt=prompt, session_id=session_id)
                self._send_json(200, result)

            def _send_json(self, code: int, payload: Dict[str, object]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format: str, *args: object) -> None:
                return

        httpd = http.server.ThreadingHTTPServer((self.host, self.port), Handler)
        httpd.serve_forever()
