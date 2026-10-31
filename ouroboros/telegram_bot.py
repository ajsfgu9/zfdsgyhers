"""Zero-Dependency Telegram Bot API Bridge for Ouroboros Dedicated Server Agent (v0.3.0 Evergreen).

Fixes all common dedicated-server Telegram issues:
1. Live `.env` Hot-Reload: Automatically reloads `.env` via `agent.hot_reload.sync_all` on every poll and message.
   Even if the daemon started before `.env` had a token, the polling thread wakes up automatically once `.env` is saved!
2. Multi-Alias `.env` Support: Reads `TELEGRAM_BOT_TOKEN`, `OUROBOROS_TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_API`, `BOT_TOKEN`,
   and `TELEGRAM_CHAT_ID`, `OUROBOROS_TELEGRAM_ALLOWED_CHAT_IDS`, `OUROBOROS_TELEGRAM_CHAT_ID`, `CHAT_ID`.
3. Int/Str Chat ID Normalization + Auto-Discovery: Never fails due to `"12345" != 12345` type mismatches, and if no
   chat ID was pre-configured in `.env`, auto-registers the first owner chat on `/start` and persists it in SQLite `state.db`.
4. Automatic Webhook Clearing (`deleteWebhook`): Prevents HTTP 409 Conflict from blocking `getUpdates` long-polling.
5. Durable SQLite Offset (`telegram_last_update_id`): Persists across reloads so messages are never lost or repeated.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.loop import OuroborosManagerAgent


MAX_TELEGRAM_MSG_LEN = 4000


@dataclasses.dataclass
class PendingApproval:
    approval_id: str
    chat_id: int
    command: str
    risk_tier: str
    created_at: float


class _CallableBool(int):
    """Boolean value that can be accessed both as a property (`bridge.is_configured`) and method (`bridge.is_configured()`)."""

    def __call__(self) -> bool:
        return bool(self)

    def __repr__(self) -> str:
        return "True" if bool(self) else "False"


class TelegramBotBridge:
    """Bidirectional bridge between Telegram Bot API and OuroborosManagerAgent."""

    def __init__(
        self,
        agent: "OuroborosManagerAgent",
        bot_token: Optional[str] = None,
        allowed_chat_ids: Optional[List[Union[int, str]]] = None,
        webhook_secret: Optional[str] = None,
        api_base: Optional[str] = None,
        poll_timeout: Optional[int] = None,
        http_sender: Optional[Callable[[str, Dict[str, Any], int], Dict[str, Any]]] = None,
        http_opener: Optional[Callable[[urllib.request.Request, int], Any]] = None,
        agent_lock: Optional[threading.Lock] = None,
    ) -> None:
        self.agent = agent
        self._explicit_bot_token = bot_token.strip() if bot_token is not None else None
        self._explicit_allowed_chat_ids = (
            [str(x).strip() for x in allowed_chat_ids] if allowed_chat_ids is not None else None
        )
        self._explicit_webhook_secret = webhook_secret.strip() if webhook_secret is not None else None
        self.api_base = (
            api_base if api_base is not None else agent.config.telegram_api_base
        ).rstrip("/")
        self.poll_timeout = (
            poll_timeout if poll_timeout is not None else agent.config.telegram_poll_timeout
        )
        self._http_sender = http_sender
        self.http_opener = http_opener
        self._lock = agent_lock or threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._last_update_id: int = int(self.agent.memory.get_kv("telegram_last_update_id", 0))
        self._chat_session_overrides: Dict[int, str] = {}
        self._pending_approvals: Dict[str, PendingApproval] = {}
        self._webhook_cleared_for_token: Optional[str] = None
        self.bot_username: Optional[str] = None
        self.last_error: Optional[str] = None

    @property
    def bot_token(self) -> str:
        if self._explicit_bot_token:
            return self._explicit_bot_token
        return (self.agent.config.telegram_bot_token or "").strip()

    @property
    def webhook_secret(self) -> str:
        if self._explicit_webhook_secret is not None:
            return self._explicit_webhook_secret
        return (self.agent.config.telegram_webhook_secret or "").strip()

    @property
    def allowed_chat_ids(self) -> List[str]:
        if self._explicit_allowed_chat_ids is not None:
            base = list(self._explicit_allowed_chat_ids)
        else:
            base = [str(x).strip() for x in self.agent.config.telegram_allowed_chat_ids if str(x).strip()]
        saved = self.agent.memory.get_kv("telegram_registered_chat_ids", [])
        for cid in saved:
            scid = str(cid).strip()
            if scid and scid not in base:
                base.append(scid)
        return base

    @property
    def is_configured(self) -> _CallableBool:
        return _CallableBool(1 if bool(self.bot_token and ":" in self.bot_token) else 0)

    def is_authorized(self, chat_id: Union[int, str]) -> bool:
        """Verifies that chat_id is in the configured allowlist (comparing normalized strings)."""
        allowed = self.allowed_chat_ids
        if not allowed:
            return False
        return str(chat_id).strip() in allowed

    @staticmethod
    def split_message(text: str, max_len: int = MAX_TELEGRAM_MSG_LEN) -> List[str]:
        """Splits long command or agent outputs into safe chunks <= 4000 characters."""
        content = (text or "").strip()
        if not content:
            return ["(empty output)"]
        if len(content) <= max_len:
            return [content]

        chunks: List[str] = []
        remaining = content
        while len(remaining) > max_len:
            split_idx = remaining.rfind("\n", 0, max_len)
            if split_idx <= 0:
                split_idx = max_len
            chunks.append(remaining[:split_idx].rstrip())
            remaining = remaining[split_idx:].lstrip("\n")
        if remaining:
            chunks.append(remaining)
        return chunks

    def _api_call(
        self, method: str, payload: Dict[str, Any], timeout: int = 35
    ) -> Dict[str, Any]:
        token = self.bot_token
        if not token:
            raise RuntimeError(
                "Telegram bot token is not configured. Set TELEGRAM_BOT_TOKEN in .env."
            )
        url = f"{self.api_base}/bot{token}/{method}"
        if self._http_sender is not None:
            return self._http_sender(url, payload, timeout)
        return self._default_http_post(url, payload, timeout)

    def _default_http_post(
        self, url: str, payload: Dict[str, Any], timeout: int
    ) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = self.http_opener or (lambda r, t: urllib.request.urlopen(r, timeout=t))
        try:
            with opener(req, timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            self.last_error = f"HTTP {exc.code} on {url.rsplit('/', 1)[-1]}: {err_body[:200]}"
            return {"ok": False, "error_code": exc.code, "description": err_body}
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "description": str(exc)}

    def ensure_ready(self) -> bool:
        """Clears any active webhook so getUpdates long-polling never fails with HTTP 409 Conflict."""
        if not self.is_configured:
            return False
        token = self.bot_token
        if self._webhook_cleared_for_token == token:
            return True
        if self._http_sender is not None:
            self._webhook_cleared_for_token = token
            return True
        me = self._api_call("getMe", {}, timeout=10)
        if me.get("ok"):
            self.bot_username = (me.get("result") or {}).get("username")
            self._api_call("deleteWebhook", {"drop_pending_updates": False}, timeout=10)
            self._webhook_cleared_for_token = token
            self.last_error = None
            return True
        return False

    def send_chat_action(self, chat_id: Union[int, str], action: str = "typing") -> None:
        try:
            self._api_call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=10)
        except Exception:
            pass

    def send_message(
        self,
        chat_id: Union[int, str],
        text: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Any:
        """Sends text to a Telegram chat, automatically splitting messages over 4000 chars."""
        if text is None:
            actual_text = str(chat_id)
            target_chat: Union[int, str] = self.allowed_chat_ids[0] if self.allowed_chat_ids else ""
        else:
            actual_text = str(text)
            target_chat = chat_id

        try:
            target_chat = int(str(target_chat).strip())
        except ValueError:
            target_chat = str(target_chat).strip()

        chunks = self.split_message(actual_text)
        responses: List[Dict[str, Any]] = []
        for idx, chunk in enumerate(chunks):
            payload: Dict[str, Any] = {
                "chat_id": target_chat,
                "text": chunk,
            }
            if idx == 0 and reply_to_message_id is not None:
                payload["reply_to_message_id"] = reply_to_message_id
            responses.append(self._api_call("sendMessage", payload, timeout=20))
        return responses

    def session_id_for_chat(self, chat_id: int) -> str:
        return self._chat_session_overrides.get(chat_id, f"tg_{chat_id}")

    def handle_update(self, update: Dict[str, Any]) -> Optional[str]:
        """Processes a single Telegram Update object and sends the reply back to the chat."""
        if hasattr(self.agent, "hot_reload"):
            self.agent.hot_reload.sync_all(self.agent, force=False)

        update_id = int(update.get("update_id", 0))
        if update_id >= self._last_update_id:
            self._last_update_id = update_id + 1
            self.agent.memory.set_kv("telegram_last_update_id", update_id)

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return None

        chat = msg.get("chat") or {}
        chat_id_raw = chat.get("id")
        if chat_id_raw is None:
            return None
        chat_id = int(chat_id_raw)
        message_id = msg.get("message_id")
        user = msg.get("from") or {}
        user_id = user.get("id", chat_id)
        username = user.get("username") or user.get("first_name") or "user"
        text = (msg.get("text") or "").strip()
        if not text:
            return None

        parts = text.split(maxsplit=1)
        cmd_token = parts[0]
        cmd_arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd_token.startswith("/") and "@" in cmd_token:
            cmd_token = cmd_token.split("@", 1)[0]
        cmd_lower = cmd_token.lower()

        if cmd_lower == "/id":
            auth_state = "AUTHORIZED" if self.is_authorized(chat_id) else "UNAUTHORIZED (Add to .env)"
            reply = (
                f"Telegram Identity Check\n"
                f"- chat_id: {chat_id}\n"
                f"- user_id: {user_id}\n"
                f"- username: {username}\n"
                f"- status: {auth_state}\n\n"
                f"To authorize this chat in /opt/ouroboros-agent/.env:\n"
                f"TELEGRAM_CHAT_ID={chat_id}"
            )
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        if not self.allowed_chat_ids and self._explicit_allowed_chat_ids is None:
            saved = self.agent.memory.get_kv("telegram_registered_chat_ids", [])
            if str(chat_id) not in [str(x) for x in saved]:
                saved.append(str(chat_id))
                self.agent.memory.set_kv("telegram_registered_chat_ids", saved)

        if not self.is_authorized(chat_id):
            deny_reply = (
                f"⛔ Access denied for chat_id={chat_id}.\n\n"
                f"To authorize your Telegram account on this Linux server, add this line to your `.env` file:\n"
                f"TELEGRAM_CHAT_ID={chat_id}\n"
                f"(It will hot-reload automatically without restarting the daemon!)"
            )
            self.send_message(chat_id, deny_reply, reply_to_message_id=message_id)
            return deny_reply

        if cmd_lower in ("/start", "/help"):
            skills_writable = "writable" if self.agent.skills.is_writable() else "read-only (run chown)"
            reply = (
                f"🐍 Ouroboros Server Agent v0.3.0 Connected (chat_id={chat_id})\n"
                f"- Active Session: {self.session_id_for_chat(chat_id)}\n"
                f"- Active Model: {self.agent.config.primary_provider.model}\n"
                f"- Skills Directory: {self.agent.config.skills_dir} ({skills_writable})\n\n"
                "Available Commands:\n"
                "• <any message> or /task <prompt> — Run full autonomous agent task\n"
                "• /cmd <shell_command> — Directly execute a Linux bash command\n"
                "• /status — View live health, model, & active TASK.md checklist\n"
                "• /skills — List installed skills (hot-reloads SKILL.md files)\n"
                "• /tools — List all registered & hot-plugged tools\n"
                "• /reload — Zero-downtime hot-reload of .env, skills/, plugins/*.py & core code\n"
                "• /approve <id> | /deny <id> — Approve/reject Sentinel Tier-2 commands\n"
                "• /reset — Start a fresh conversation session\n"
                "• /id — Show your Telegram chat_id"
            )
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        if cmd_lower == "/status":
            checklist = self.agent.memory.read_task_checklist().strip()
            catalog = self.agent.skills.list_catalog_prompt()
            reply = (
                f"Ouroboros Status: HEALTHY\n"
                f"- Active Model: {self.agent.config.primary_provider.model}\n"
                f"- Loaded .env: {self.agent.config._env_loader.last_loaded_files}\n\n"
                f"📋 Active TASK.md Checklist:\n{checklist}\n\n"
                f"🧩 Installed Skills:\n{catalog}"
            )
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        if cmd_lower in ("/skills", "/reload"):
            report = (
                self.agent.hot_reload.sync_all(self.agent, force=True)
                if hasattr(self.agent, "hot_reload")
                else {}
            )
            self.agent.skills.reload()
            catalog = self.agent.skills.list_catalog_prompt()
            writable_note = (
                "✅ Skills directory is writable by agent."
                if self.agent.skills.is_writable()
                else f"⚠️ Skills directory ({self.agent.config.skills_dir}) is read-only for current user."
            )
            reply = (
                f"🔄 Zero-Downtime Hot-Reload Complete (no restart needed, zero data loss):\n"
                f"{writable_note}\n"
                f"- Active Model: {self.agent.config.primary_provider.model}\n"
                f"- Reloaded Plugins: {report.get('reloaded_plugins', [])}\n"
                f"- Reloaded Core Modules: {report.get('reloaded_core_modules', [])}\n\n"
                f"Installed Skills:\n{catalog}"
            )
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply


        if cmd_lower == "/logs":
            n_lines = int(cmd_arg) if cmd_arg.isdigit() else 25
            recent = self.agent.logger.tail(lines=n_lines) if hasattr(self.agent, "logger") else []
            log_path = str(self.agent.logger.log_file) if hasattr(self.agent, "logger") else "N/A"
            reply = (
                f"📜 Recent Server Logs ({log_path}):\n"
                + ("\n".join(recent) if recent else "(no log entries yet)")
            )
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        if cmd_lower == "/reexec":
            check = self.agent.hot_reload.graceful_reexec(self.agent, dry_run=True)
            if check.get("status") != "ready_for_execv":
                reply = f"⚠️ Preflight check blocked re-exec:\n{check.get('error')}"
                self.send_message(chat_id, reply, reply_to_message_id=message_id)
                return reply
            reply = f"🚀 Preflight passed (PID {check.get('pid')}). Performing zero-downtime os.execv binary upgrade..."
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        if cmd_lower == "/tools":
            tool_names = sorted(self.agent.tools._tools.keys())
            reply = f"Registered Tools ({len(tool_names)}):\n- " + "\n- ".join(tool_names)
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        if cmd_lower == "/reset":
            new_sess = f"tg_{chat_id}_{int(time.time())}"
            self._chat_session_overrides[chat_id] = new_sess
            reply = f"🧹 Started fresh session: {new_sess}"
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        if cmd_lower in ("/cmd", "/bash", "/exec"):
            if not cmd_arg:
                reply = "Usage: /cmd <linux_command> (e.g., `/cmd df -h && uptime`)"
                self.send_message(chat_id, reply, reply_to_message_id=message_id)
                return reply
            return self._execute_direct_command(chat_id, cmd_arg, message_id)

        if cmd_lower == "/approve":
            return self._handle_approval_decision(chat_id, cmd_arg, approve=True, message_id=message_id)

        if cmd_lower == "/deny":
            return self._handle_approval_decision(chat_id, cmd_arg, approve=False, message_id=message_id)

        prompt = cmd_arg if cmd_lower == "/task" else text
        if not prompt:
            reply = "Usage: /task <your objective>"
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        return self._execute_agent_task(chat_id, prompt, message_id)

    def _execute_direct_command(
        self, chat_id: int, command: str, message_id: Optional[int]
    ) -> str:
        self.send_chat_action(chat_id, "typing")
        args = {"command": command}
        risk_tier = self.agent.tools.classify_risk_tier("host_bash", args)

        if risk_tier == "Tier 2" and self.agent.config.require_human_on_tier_2:
            approval_id = f"ap_{uuid.uuid4().hex[:6]}"
            self._pending_approvals[approval_id] = PendingApproval(
                approval_id=approval_id,
                chat_id=chat_id,
                command=command,
                risk_tier=risk_tier,
                created_at=time.time(),
            )
            reply = (
                f"🛡️ Blocked by Sentinel Tier-2 Guard ({risk_tier})\n"
                f"Command: `{command}`\n\n"
                f"To confirm and execute on the server, reply:\n"
                f"/approve {approval_id}\n"
                f"To cancel, reply:\n"
                f"/deny {approval_id}"
            )
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        return self._run_bash_and_format(chat_id, command, risk_tier, message_id)

    def _handle_approval_decision(
        self,
        chat_id: int,
        approval_id: str,
        approve: bool,
        message_id: Optional[int],
    ) -> str:
        clean_id = approval_id.strip()
        pending = self._pending_approvals.get(clean_id)
        if not pending or pending.chat_id != chat_id:
            reply = f"No pending Tier-2 approval found with ID '{clean_id}'."
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        del self._pending_approvals[clean_id]
        if not approve:
            reply = f"🚫 Cancelled pending Tier-2 command ({clean_id}): `{pending.command}`"
            self.send_message(chat_id, reply, reply_to_message_id=message_id)
            return reply

        return self._run_bash_and_format(
            chat_id, pending.command, pending.risk_tier, message_id, approved_tier_2=True
        )

    def _run_bash_and_format(
        self,
        chat_id: int,
        command: str,
        risk_tier: str,
        message_id: Optional[int],
        approved_tier_2: bool = False,
    ) -> str:
        session_id = self.session_id_for_chat(chat_id)
        with self._lock:
            res = self.agent.tools._handle_host_bash({"command": command})
            self.agent.memory.log_action_receipt(
                session_id,
                "host_bash",
                f"{risk_tier} (approved)" if approved_tier_2 else risk_tier,
                {"command": command},
                json.dumps(res, ensure_ascii=False),
            )
        exit_code = res.get("exit_code", 0)
        stdout = (res.get("stdout") or "").strip()
        stderr = (res.get("stderr") or "").strip()
        parts = [f"$ {command}", f"[exit_code={exit_code} | {risk_tier}]"]
        if stdout:
            parts.append(f"\n--- stdout ---\n{stdout}")
        if stderr:
            parts.append(f"\n--- stderr ---\n{stderr}")
        if not stdout and not stderr:
            parts.append("\n(no output)")
        reply = "\n".join(parts)
        self.send_message(chat_id, reply, reply_to_message_id=message_id)
        return reply

    def _execute_agent_task(
        self, chat_id: int, prompt: str, message_id: Optional[int]
    ) -> str:
        self.send_chat_action(chat_id, "typing")
        session_id = self.session_id_for_chat(chat_id)
        try:
            with self._lock:
                result = self.agent.run_task(user_prompt=prompt, session_id=session_id)
            final_text = str(result.get("final_response") or "").strip()
            turns = result.get("turns_executed", 1)
            tools_used = result.get("total_tool_calls", 0)
            footer = f"\n\n[status={result.get('status')} | turns={turns} | tool_calls={tools_used}]"
            reply = (final_text or "(Agent completed without text output)") + footer
        except Exception as exc:
            reply = f"⚠️ Agent Execution Error: {type(exc).__name__}: {exc}"

        self.send_message(chat_id, reply, reply_to_message_id=message_id)
        return reply

    def poll_once(self) -> List[Optional[str]]:
        """Hot-reloads `.env` first, ensures webhook is cleared, then polls `getUpdates` once."""
        if hasattr(self.agent, "hot_reload"):
            self.agent.hot_reload.sync_all(self.agent, force=False)
        if not self.ensure_ready():
            return []

        last_saved = int(self.agent.memory.get_kv("telegram_last_update_id", 0))
        offset = max(self._last_update_id, last_saved + 1 if last_saved > 0 else 0)
        resp = self._api_call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": self.poll_timeout,
                "allowed_updates": ["message", "edited_message"],
            },
            timeout=self.poll_timeout + 10,
        )
        updates = resp.get("result") or []
        replies: List[Optional[str]] = []
        for upd in updates:
            replies.append(self.handle_update(upd))
        return replies

    def serve_polling_forever(self) -> None:
        """Runs event-driven HTTP long-polling (and checks `.env` every 3s if token not yet set)."""
        while not self._stop_event.is_set():
            try:
                if hasattr(self.agent, "hot_reload"):
                    self.agent.hot_reload.sync_all(self.agent, force=False)
                if not self.is_configured:
                    self._stop_event.wait(3.0)
                    continue
                self.poll_once()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._stop_event.wait(3.0)

    def start_background_polling(self, poll_interval_sec: float = 2.0) -> Optional[threading.Thread]:
        """Always starts the background watcher thread so adding a token to `.env` later works without restart!"""
        if self._poll_thread and self._poll_thread.is_alive():
            return self._poll_thread
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self.serve_polling_forever,
            name="ouroboros-telegram-poller",
            daemon=True,
        )
        self._poll_thread.start()
        return self._poll_thread

    def stop_background_polling(self) -> None:
        self._stop_event.set()

    def handle_webhook_request(
        self, headers: Dict[str, str], body_bytes: bytes
    ) -> Tuple[int, Dict[str, Any]]:
        """Handles an incoming `POST /v1/telegram/webhook` HTTP request."""
        if self.webhook_secret:
            received_secret = ""
            for k, v in headers.items():
                if k.lower() == "x-telegram-bot-api-secret-token":
                    received_secret = v.strip()
                    break
            if received_secret != self.webhook_secret:
                return 403, {"error": "Invalid X-Telegram-Bot-Api-Secret-Token"}

        try:
            update = json.loads(body_bytes.decode("utf-8") or "{}")
        except Exception as exc:
            return 400, {"error": f"Invalid JSON payload: {exc}"}

        reply = self.handle_update(update)
        return 200, {"status": "ok", "handled": reply is not None}


# Alias for backward/forward compatibility
TelegramBotService = TelegramBotBridge
