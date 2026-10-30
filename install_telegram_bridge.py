#!/usr/bin/env python3
"""In-Place Telegram Bridge Patcher & Permission Repairer for Ouroboros Agent.

Run this script on your Linux server to upgrade an existing `/opt/ouroboros-agent` directory
in-place, wire up Telegram Bot API support, and ensure valid syntax on Python 3.10, 3.11, and 3.12.

Usage:
    sudo python3 install_telegram_bridge.py /opt/ouroboros-agent
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import time

CONFIG_PY = '''"""Configuration management for Ouroboros Recursive Manager Agent."""

from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Dict, List


def _parse_chat_ids(raw: str) -> List[int]:
    ids: List[int] = []
    for part in raw.split(","):
        clean = part.strip()
        if not clean:
            continue
        try:
            ids.append(int(clean))
        except ValueError:
            continue
    return ids


@dataclasses.dataclass
class ModelProviderConfig:
    """Configuration for primary and fallback LLM endpoints."""

    name: str = "openai_compatible"
    api_mode: str = "chat_completions"
    base_url: str = dataclasses.field(
        default_factory=lambda: os.environ.get("OUROBOROS_API_BASE", "https://api.openai.com/v1")
    )
    api_key_env: str = "OUROBOROS_API_KEY"
    model: str = dataclasses.field(
        default_factory=lambda: os.environ.get("OUROBOROS_MODEL", "gpt-4.1")
    )
    max_context_tokens: int = 128_000
    timeout_seconds: int = 120


@dataclasses.dataclass
class AgentConfig:
    """Runtime, Telegram bridge, and governance configuration for the recursive loop."""

    workspace_root: pathlib.Path = dataclasses.field(
        default_factory=lambda: pathlib.Path(
            os.environ.get("OUROBOROS_WORKSPACE", "./workspace_state")
        ).resolve()
    )
    skills_dir: pathlib.Path = dataclasses.field(
        default_factory=lambda: pathlib.Path(
            os.environ.get("OUROBOROS_SKILLS_DIR", "./skills")
        ).resolve()
    )
    max_turns: int = 100
    subagent_max_turns: int = 30
    max_recursion_depth: int = 3
    parallel_tool_workers: int = 8
    compression_trigger_ratio: float = 0.50
    compression_protect_last_n: int = 12
    skill_learning_min_tool_calls: int = 5
    auto_approve_tier_0: bool = True
    auto_approve_tier_1: bool = True
    require_human_on_tier_2: bool = True
    primary_provider: ModelProviderConfig = dataclasses.field(
        default_factory=ModelProviderConfig
    )
    fallback_providers: List[ModelProviderConfig] = dataclasses.field(default_factory=list)
    telegram_bot_token: str = dataclasses.field(
        default_factory=lambda: os.environ.get("OUROBOROS_TELEGRAM_BOT_TOKEN", "").strip()
    )
    telegram_allowed_chat_ids: List[int] = dataclasses.field(
        default_factory=lambda: _parse_chat_ids(
            os.environ.get("OUROBOROS_TELEGRAM_ALLOWED_CHAT_IDS", "")
        )
    )
    telegram_webhook_secret: str = dataclasses.field(
        default_factory=lambda: os.environ.get("OUROBOROS_TELEGRAM_WEBHOOK_SECRET", "").strip()
    )
    telegram_poll_timeout: int = dataclasses.field(
        default_factory=lambda: int(os.environ.get("OUROBOROS_TELEGRAM_POLL_TIMEOUT", "25"))
    )
    telegram_api_base: str = dataclasses.field(
        default_factory=lambda: os.environ.get(
            "OUROBOROS_TELEGRAM_API_BASE", "https://api.telegram.org"
        ).rstrip("/")
    )
    tier_2_sensitive_patterns: List[str] = dataclasses.field(
        default_factory=lambda: [
            "rm -rf /",
            "mkfs",
            "dd if=",
            "iptables -F",
            "/etc/sudoers",
            "/etc/ssh/sshd_config",
            "CODEOWNERS",
            "REVIEW.md",
            "AGENTS.md",
            ".github/workflows/",
        ]
    )

    def ensure_directories(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "planner").mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "worktrees").mkdir(parents=True, exist_ok=True)
        (self.workspace_root / "logs").mkdir(parents=True, exist_ok=True)
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
'''

SKILLS_PY = '''"""Dynamic Skill Registry and Hermes-style Closed Learning Loop (`SKILL.md`)."""

from __future__ import annotations

import dataclasses
import os
import pathlib
import re
from typing import Any, Dict, List, Optional


FRONTMATTER_RE = re.compile(r"^---\\s*\\n(.*?)\\n---\\s*\\n(.*)$", re.DOTALL)


@dataclasses.dataclass
class SkillMetadata:
    name: str
    description: str
    skill_dir: pathlib.Path
    body: str
    resource_files: List[str]
    enabled: bool = True


class SkillRegistry:
    """Discovers, loads, and dynamically saves reusable `SKILL.md` capabilities."""

    def __init__(self, skills_dir: pathlib.Path) -> None:
        self.skills_dir = skills_dir
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass
        self._skills: Dict[str, SkillMetadata] = {}
        self.reload()

    def is_writable(self) -> bool:
        """Checks whether the current Linux service user has write permission on skills_dir."""
        return os.access(self.skills_dir, os.W_OK)

    def reload(self) -> None:
        self._skills.clear()
        if not self.skills_dir.exists():
            return
        for skill_md in sorted(self.skills_dir.glob("*/SKILL.md")):
            try:
                meta = self._parse_skill_md(skill_md)
            except OSError:
                continue
            if meta and meta.enabled:
                self._skills[meta.name.lower()] = meta

    def _parse_skill_md(self, path: pathlib.Path) -> Optional[SkillMetadata]:
        raw = path.read_text(encoding="utf-8")
        match = FRONTMATTER_RE.match(raw)
        name = path.parent.name
        description = "No description provided."
        enabled = True
        body = raw
        if match:
            fm_text, body = match.group(1), match.group(2)
            for line in fm_text.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    k, v = k.strip().lower(), v.strip().strip("\'\\"")
                    if k == "name" and v:
                        name = v
                    elif k == "description" and v:
                        description = v
                    elif k == "enabled":
                        enabled = v.lower() not in ("false", "0", "no")
        resources = [
            str(p.relative_to(path.parent))
            for p in path.parent.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        ]
        return SkillMetadata(
            name=name,
            description=description,
            skill_dir=path.parent,
            body=body.strip(),
            resource_files=sorted(resources),
            enabled=enabled,
        )

    def list_catalog_prompt(self) -> str:
        """Returns compact skill descriptions for system prompt injection (Gemini Task / OpenClaw style)."""
        self.reload()
        if not self._skills:
            return "No custom skills installed yet."
        lines = []
        for meta in self._skills.values():
            lines.append(f"- **{meta.name}**: {meta.description}")
        return "\\n".join(lines)

    def lookup_skills(self, skill_names: List[str]) -> Dict[str, Any]:
        """Loads full SKILL.md instructions on demand without bloating initial context."""
        self.reload()
        found = []
        missing = []
        for requested in skill_names:
            clean = requested.lower().replace("user:", "").replace("system:", "").strip()
            meta = self._skills.get(clean)
            if not meta:
                candidates = [
                    m
                    for k, m in self._skills.items()
                    if clean in k or clean in m.description.lower()
                ]
                if len(candidates) == 1:
                    meta = candidates[0]
            if meta:
                found.append(
                    {
                        "name": meta.name,
                        "description": meta.description,
                        "resources": meta.resource_files,
                        "instructions": meta.body,
                    }
                )
            else:
                missing.append(requested)
        return {"loaded_skills": found, "not_found": missing}

    def get_skill_resources(self, skill_name: str, filenames: List[str]) -> Dict[str, str]:
        self.reload()
        clean = skill_name.lower().replace("user:", "").replace("system:", "").strip()
        meta = self._skills.get(clean)
        if not meta:
            return {"error": f"Skill not found: {skill_name}"}
        output: Dict[str, str] = {}
        for fname in filenames:
            target = (meta.skill_dir / fname).resolve()
            if not str(target).startswith(str(meta.skill_dir.resolve())) or not target.is_file():
                output[fname] = f"Error: Resource {fname} not found inside skill {skill_name}"
            else:
                output[fname] = target.read_text(encoding="utf-8", errors="replace")
        return output

    def save_skill(
        self,
        name: str,
        description: str,
        instructions_markdown: str,
        scripts: Optional[Dict[str, str]] = None,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Saves or updates a reusable skill (Hermes closed learning loop)."""
        slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
        target_dir = self.skills_dir / slug
        if target_dir.exists() and not overwrite:
            return {
                "status": "already_exists",
                "name": slug,
                "message": f"Skill \'{slug}\' already exists. Pass overwrite=True after user confirmation.",
            }
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            skill_md_content = (
                f"---\\nname: {slug}\\ndescription: {description}\\nenabled: true\\n---\\n\\n"
                f"{instructions_markdown.strip()}\\n"
            )
            (target_dir / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
            if scripts:
                for rel_path, code in scripts.items():
                    script_file = (target_dir / rel_path).resolve()
                    if str(script_file).startswith(str(target_dir.resolve())):
                        script_file.parent.mkdir(parents=True, exist_ok=True)
                        script_file.write_text(code, encoding="utf-8")
                        if script_file.suffix in (".sh", ".py"):
                            script_file.chmod(0o755)
        except PermissionError as exc:
            return {
                "status": "permission_denied",
                "name": slug,
                "skills_dir": str(self.skills_dir),
                "error": str(exc),
                "remediation": (
                    f"The Linux user running Ouroboros lacks write permission on \'{self.skills_dir}\'. "
                    f"Fix on host: sudo chown -R $(id -un):$(id -gn) {self.skills_dir} && sudo chmod -R u+rwX {self.skills_dir}"
                ),
            }
        self.reload()
        return {"status": "created", "name": slug, "path": str(target_dir)}
'''

GATEWAY_PY = '''"""Event-Driven HTTP Gateway, Telegram Webhook/Polling Hub & Zero-Polling Cron Scheduler."""

from __future__ import annotations

import dataclasses
import http.server
import json
import threading
import time
from typing import Callable, Dict, List, Optional

from ouroboros.loop import OuroborosManagerAgent
from ouroboros.telegram_bot import TelegramBotBridge


@dataclasses.dataclass
class ScheduledRoutine:
    name: str
    interval_seconds: int
    prompt: str
    last_run: float = 0.0


class OuroborosGatewayServer:
    """Single-process hub exposing REST `/v1/tasks`, `/v1/telegram/webhook`, `/v1/reload`, and scheduled wakeups."""

    def __init__(
        self,
        agent: OuroborosManagerAgent,
        host: str = "127.0.0.1",
        port: int = 8787,
        routines: Optional[List[ScheduledRoutine]] = None,
        telegram_bridge: Optional[TelegramBotBridge] = None,
        enable_telegram_polling: bool = True,
    ) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self.routines = routines or []
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.telegram = telegram_bridge or TelegramBotBridge(
            agent=self.agent, agent_lock=self._lock
        )
        self.enable_telegram_polling = enable_telegram_polling

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
        if self.enable_telegram_polling and self.telegram.is_configured:
            self.telegram.start_background_polling()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/healthz":
                    self._send_json(
                        200,
                        {
                            "status": "ok",
                            "service": "ouroboros-gateway",
                            "telegram_configured": gateway.telegram.is_configured,
                            "skills_writable": gateway.agent.skills.is_writable(),
                        },
                    )
                    return
                if self.path == "/v1/state":
                    self._send_json(
                        200,
                        {
                            "task_checklist": gateway.agent.memory.read_task_checklist(),
                            "skills": gateway.agent.skills.list_catalog_prompt(),
                            "skills_writable": gateway.agent.skills.is_writable(),
                            "telegram_configured": gateway.telegram.is_configured,
                            "telegram_allowed_chat_ids": gateway.telegram.allowed_chat_ids,
                        },
                    )
                    return
                self._send_json(404, {"error": "not_found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length) if length > 0 else b"{}"

                if self.path == "/v1/telegram/webhook":
                    headers_dict = {k: v for k, v in self.headers.items()}
                    status_code, resp_payload = gateway.telegram.handle_webhook_request(
                        headers_dict, raw_body
                    )
                    self._send_json(status_code, resp_payload)
                    return

                if self.path == "/v1/reload":
                    with gateway._lock:
                        gateway.agent.skills.reload()
                    self._send_json(
                        200,
                        {
                            "status": "reloaded",
                            "skills_dir": str(gateway.agent.config.skills_dir),
                            "skills_writable": gateway.agent.skills.is_writable(),
                            "skills": gateway.agent.skills.list_catalog_prompt(),
                        },
                    )
                    return

                if self.path != "/v1/tasks":
                    self._send_json(404, {"error": "not_found"})
                    return

                body = json.loads(raw_body.decode("utf-8") or "{}")
                prompt = body.get("prompt", "")
                session_id = body.get("session_id")
                if not prompt:
                    self._send_json(400, {"error": "Missing \'prompt\' field"})
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
'''

MAIN_PY = '''#!/usr/bin/env python3
"""CLI, Telegram Bot, & Server Daemon Entrypoint for Ouroboros Recursive Autonomous Manager Agent."""

from __future__ import annotations

import argparse
import json
import sys

from ouroboros.config import AgentConfig
from ouroboros.gateway import OuroborosGatewayServer, ScheduledRoutine
from ouroboros.loop import OuroborosManagerAgent
from ouroboros.telegram_bot import TelegramBotBridge


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ouroboros: Recursive Autonomous Manager Agent for Dedicated Servers"
    )
    parser.add_argument(
        "--mode",
        choices=["task", "chat", "daemon", "telegram"],
        default="task",
        help="Execution mode: single task, interactive REPL chat, HTTP+Telegram daemon, or standalone Telegram bot.",
    )
    parser.add_argument("--prompt", help="Task prompt when running in --mode task.")
    parser.add_argument("--session-id", default=None, help="Optional session ID to resume.")
    parser.add_argument("--host", default="127.0.0.1", help="Gateway bind address.")
    parser.add_argument("--port", type=int, default=8787, help="Gateway bind port.")
    parser.add_argument(
        "--no-telegram-poll",
        action="store_true",
        help="Disable background Telegram long-polling in --mode daemon (use when receiving webhooks via /v1/telegram/webhook).",
    )
    args = parser.parse_args()

    config = AgentConfig()
    agent = OuroborosManagerAgent(config=config)

    if args.mode == "task":
        if not args.prompt:
            parser.error("--prompt is required when --mode is \'task\'")
        result = agent.run_task(user_prompt=args.prompt, session_id=args.session_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.mode == "chat":
        session_id = args.session_id or "interactive_cli"
        print(f"Ouroboros Interactive Manager Shell (session={session_id}). Type \'exit\' to quit.")
        while True:
            try:
                user_in = input("ouroboros> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if user_in.lower() in ("exit", "quit"):
                break
            if not user_in:
                continue
            res = agent.run_task(user_prompt=user_in, session_id=session_id)
            print("\\n" + str(res["final_response"]) + "\\n")
        return 0

    if args.mode == "telegram":
        bridge = TelegramBotBridge(agent=agent)
        if not bridge.is_configured:
            print(
                "Error: OUROBOROS_TELEGRAM_BOT_TOKEN is not set in environment or .env.",
                file=sys.stderr,
            )
            return 1
        print(
            f"Ouroboros Telegram Bot long-polling active (allowed_chats={bridge.allowed_chat_ids})."
        )
        bridge.serve_polling_forever()
        return 0

    routines = [
        ScheduledRoutine(
            name="hourly_self_heal_audit",
            interval_seconds=3600,
            prompt="Load the self-healer skill and verify disk usage and failed services.",
        )
    ]
    gateway = OuroborosGatewayServer(
        agent=agent,
        host=args.host,
        port=args.port,
        routines=routines,
        enable_telegram_polling=not args.no_telegram_poll,
    )
    print(f"Ouroboros Gateway listening on http://{args.host}:{args.port}")
    if gateway.telegram.is_configured:
        mode_label = "Webhook-Only" if args.no_telegram_poll else "Background Long-Polling + Webhook"
        print(
            f"Telegram Bot Bridge active ({mode_label}, allowed_chats={gateway.telegram.allowed_chat_ids})"
        )
    gateway.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def apply_upgrade(target_dir: pathlib.Path) -> None:
    print(f"[1/4] Upgrading Ouroboros installation in: {target_dir}")
    (target_dir / "ouroboros").mkdir(parents=True, exist_ok=True)
    (target_dir / "skills" / "telegram-bot-ops").mkdir(parents=True, exist_ok=True)

    local_tg = pathlib.Path(__file__).parent / "telegram_bot.py"
    if local_tg.exists():
        shutil.copy2(local_tg, target_dir / "ouroboros" / "telegram_bot.py")
        print("  + Installed ouroboros/telegram_bot.py")

    (target_dir / "ouroboros" / "config.py").write_text(CONFIG_PY, encoding="utf-8")
    (target_dir / "ouroboros" / "skills.py").write_text(SKILLS_PY, encoding="utf-8")
    (target_dir / "ouroboros" / "gateway.py").write_text(GATEWAY_PY, encoding="utf-8")
    (target_dir / "main.py").write_text(MAIN_PY, encoding="utf-8")
    (target_dir / "main.py").chmod(0o755)
    print("  + Installed clean ouroboros/config.py, ouroboros/skills.py, ouroboros/gateway.py, and main.py")

    env_path = target_dir / ".env"
    if env_path.exists():
        backup = target_dir / f".env.bak.{int(time.time())}"
        shutil.copy2(env_path, backup)
        print(f"[2/4] Backed up existing .env to {backup.name}")
    else:
        env_path.write_text("", encoding="utf-8")

    env_text = env_path.read_text(encoding="utf-8")
    added = []
    for key, default_val in [
        ("OUROBOROS_TELEGRAM_BOT_TOKEN", ""),
        ("OUROBOROS_TELEGRAM_ALLOWED_CHAT_IDS", ""),
        ("OUROBOROS_TELEGRAM_WEBHOOK_SECRET", ""),
        ("OUROBOROS_TELEGRAM_POLL_TIMEOUT", "25"),
    ]:
        if f"{key}=" not in env_text:
            env_text = env_text.rstrip("\n") + f"\n{key}={default_val}\n"
            added.append(key)
    if added:
        env_path.write_text(env_text, encoding="utf-8")
        print(f"  + Added {', '.join(added)} to {env_path}")

    print("[3/4] Checking skills/ and workspace_state/ permissions...")
    skills_dir = target_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        subprocess.run(
            ["chown", "-R", "ouroboros:ouroboros", str(skills_dir)],
            check=False,
        )
        subprocess.run(["chmod", "-R", "u+rwX", str(skills_dir)], check=False)
        print(f"  + Repaired ownership and write permissions on {skills_dir}")
    else:
        print(
            f"  ! Run as root to fix skill permissions: sudo chown -R $(whoami) {skills_dir} && sudo chmod -R u+rwX {skills_dir}"
        )

    print("[4/4] Running unit tests...")
    test_run = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=str(target_dir),
        env={**os.environ, "PYTHONPATH": str(target_dir)},
        capture_output=True,
        text=True,
    )
    print(test_run.stdout)
    if test_run.stderr:
        print(test_run.stderr)
    if test_run.returncode == 0:
        print("✅ All unit tests passed cleanly!")


if __name__ == "__main__":
    target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/ouroboros-agent").resolve()
    apply_upgrade(target)
