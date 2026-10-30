"""Event-Driven HTTP Gateway, Telegram Bot Poller/Webhook, & Zero-Downtime Hot-Reload Control Plane (v0.3.0)."""

from __future__ import annotations

import dataclasses
import http.server
import json
import signal
import threading
import time
from typing import Dict, List, Optional

from ouroboros.loop import OuroborosManagerAgent
from ouroboros.telegram_bot import TelegramBotBridge


@dataclasses.dataclass
class ScheduledRoutine:
    name: str
    interval_seconds: int
    prompt: str
    last_run: float = 0.0


class OuroborosGatewayServer:
    """Single-process hub exposing REST `/v1/tasks`, `/v1/reload`, `/v1/telegram/webhook`, and background polling."""

    def __init__(
        self,
        agent: OuroborosManagerAgent,
        host: str = "127.0.0.1",
        port: int = 8787,
        routines: Optional[List[ScheduledRoutine]] = None,
        enable_telegram_polling: bool = True,
    ) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self.routines = routines or []
        self.enable_telegram_polling = enable_telegram_polling
        self.telegram: TelegramBotBridge = agent.telegram
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def install_signal_handlers(self) -> None:
        """Allows `kill -HUP <pid>` or `systemctl reload ouroboros` to trigger a zero-downtime hot-reload."""
        if hasattr(signal, "SIGHUP"):
            def _on_sighup(signum: int, frame: object) -> None:
                with self._lock:
                    self.agent.hot_reload.sync_all(self.agent, force=True)
            try:
                signal.signal(signal.SIGHUP, _on_sighup)
            except ValueError:
                pass

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
        self.install_signal_handlers()
        if self.enable_telegram_polling:
            self.telegram.start_background_polling()

        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/healthz":
                    gateway.agent.hot_reload.sync_all(gateway.agent, force=False)
                    self._send_json(
                        200,
                        {
                            "status": "ok",
                            "version": "0.3.0",
                            "service": "ouroboros-gateway",
                            "telegram_configured": bool(gateway.telegram.is_configured),
                            "telegram_bot_username": gateway.telegram.bot_username,
                            "telegram_allowed_chat_ids": gateway.telegram.allowed_chat_ids,
                            "telegram_last_error": gateway.telegram.last_error,
                            "loaded_env_files": gateway.agent.config._env_loader.last_loaded_files,
                            "active_skills_count": len(gateway.agent.skills._skills),
                            "active_plugins": gateway.agent.hot_reload.loaded_plugins,
                        },
                    )
                    return
                if self.path == "/v1/state":
                    gateway.agent.hot_reload.sync_all(gateway.agent, force=False)
                    self._send_json(
                        200,
                        {
                            "task_checklist": gateway.agent.memory.read_task_checklist(),
                            "skills": gateway.agent.skills.list_catalog_prompt(),
                            "tools": sorted(gateway.agent.tools._tools.keys()),
                            "plugins": gateway.agent.hot_reload.loaded_plugins,
                            "telegram_configured": bool(gateway.telegram.is_configured),
                        },
                    )
                    return
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                if self.path == "/v1/reload":
                    with gateway._lock:
                        report = gateway.agent.hot_reload.sync_all(gateway.agent, force=True)
                    self._send_json(200, {"status": "reloaded", "report": report})
                    return

                if self.path == "/v1/telegram/webhook":
                    length = int(self.headers.get("Content-Length", "0"))
                    raw_body = self.rfile.read(length)
                    headers_dict = {k: v for k, v in self.headers.items()}
                    code, resp_payload = gateway.telegram.handle_webhook_request(
                        headers_dict, raw_body
                    )
                    self._send_json(code, resp_payload)
                    return

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
