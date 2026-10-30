"""Dedicated Server Tool Registry, Role Sandbox Firewall, Sentinel Risk Classifier, and Parallel Executor."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import pathlib
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

from ouroboros.config import AgentConfig
from ouroboros.memory import MemoryAndStateStore
from ouroboros.multi_agent import RoleSandboxPolicy
from ouroboros.skills import SkillRegistry


@dataclasses.dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Any]
    is_interactive: bool = False
    default_risk_tier: str = "Tier 0"


class ServerToolRegistry:
    """Manages host server tools, role-level data-pollution firewalls, and Sentinel risk-tier checks."""

    def __init__(
        self,
        config: AgentConfig,
        memory: MemoryAndStateStore,
        skills: SkillRegistry,
        subagent_spawner: Optional[Callable[[str, str, int, Optional[Dict[str, Any]]], Dict[str, Any]]] = None,
        approval_callback: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
        role_policy: Optional[RoleSandboxPolicy] = None,
        shared_root: Optional[pathlib.Path] = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.skills = skills
        self.subagent_spawner = subagent_spawner
        self.approval_callback = approval_callback
        self.role_policy = role_policy
        self.shared_root = (shared_root or config.workspace_root).resolve()
        self._tools: Dict[str, ToolSpec] = {}
        self._background_procs: Dict[str, subprocess.Popen[str]] = {}
        self._register_builtin_tools()

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def classify_risk_tier(self, tool_name: str, args: Dict[str, Any]) -> str:
        serialized = json.dumps(args, sort_keys=True)
        for pattern in self.config.tier_2_sensitive_patterns:
            if pattern in serialized:
                return "Tier 2"
        spec = self._tools.get(tool_name)
        if not spec:
            return "Tier 2"
        if tool_name == "host_bash":
            cmd = str(args.get("command", ""))
            if any(tok in cmd for tok in ["systemctl", "docker", "git push", "kill ", "apt", "pip"]):
                return "Tier 1"
        return spec.default_risk_tier

    def get_openai_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas = []
        for spec in self._tools.values():
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
            )
        return schemas

    def execute_tool_calls(
        self, session_id: str, tool_calls: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if not tool_calls:
            return []

        if len(tool_calls) == 1 or any(
            self._tools.get(
                tc.get("function", {}).get("name", ""),
                ToolSpec("", "", {}, lambda _: None),
            ).is_interactive
            for tc in tool_calls
        ):
            return [self._invoke_single(session_id, tc) for tc in tool_calls]

        results_by_idx: Dict[int, Dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(tool_calls), self.config.parallel_tool_workers)
        ) as pool:
            future_to_idx = {
                pool.submit(self._invoke_single, session_id, tc): idx
                for idx, tc in enumerate(tool_calls)
            }
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                results_by_idx[idx] = future.result()

        return [results_by_idx[i] for i in range(len(tool_calls))]

    def _invoke_single(self, session_id: str, tool_call: Dict[str, Any]) -> Dict[str, Any]:
        call_id = tool_call.get("id", f"call_{int(time.time() * 1000)}")
        fn_block = tool_call.get("function", {})
        name = fn_block.get("name", "")
        raw_args = fn_block.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except Exception as exc:
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps({"error": f"Invalid JSON arguments: {exc}"}),
            }

        spec = self._tools.get(name)
        if not spec:
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps({"error": f"Unknown tool: {name}"}),
            }

        risk_tier = self.classify_risk_tier(name, args)
        if risk_tier == "Tier 2" and self.config.require_human_on_tier_2:
            approved = False
            if self.approval_callback:
                approved = self.approval_callback(name, args, risk_tier)
            if not approved:
                msg = {
                    "status": "blocked_by_sentinel_tier_2",
                    "risk_tier": risk_tier,
                    "tool": name,
                    "message": (
                        "Action touches a Tier-2 High-Risk path or command. "
                        "Human confirmation is required before execution."
                    ),
                }
                self.memory.log_action_receipt(session_id, name, risk_tier, args, json.dumps(msg))
                return {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": json.dumps(msg),
                }

        try:
            output = spec.handler(args)
            serialized_out = (
                output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            )
        except Exception as exc:
            serialized_out = json.dumps({"error": f"{type(exc).__name__}: {exc}"})

        self.memory.log_action_receipt(session_id, name, risk_tier, args, serialized_out)
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "name": name,
            "content": serialized_out,
        }

    def _register_builtin_tools(self) -> None:
        self.register(
            ToolSpec(
                name="host_bash",
                description=(
                    "Execute a shell command or manage background daemon tasks on the dedicated server. "
                    "Respects role-based data-pollution path firewalls when running inside a specialist sub-agent."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to run."},
                        "cwd": {"type": "string", "description": "Working directory."},
                        "background": {
                            "type": "boolean",
                            "description": "Run asynchronously as a managed background task.",
                        },
                        "timeout_sec": {"type": "integer", "description": "Timeout in seconds."},
                    },
                    "required": ["command"],
                },
                handler=self._handle_host_bash,
                default_risk_tier="Tier 1",
            )
        )

        self.register(
            ToolSpec(
                name="file_ops",
                description="Read, write, or list files on the server filesystem (enforces role sandbox ACLs).",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["read", "write", "append", "list"],
                        },
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["action", "path"],
                },
                handler=self._handle_file_ops,
                default_risk_tier="Tier 0",
            )
        )

        self.register(
            ToolSpec(
                name="update_task_checklist",
                description="Update the agent's private TASK.md milestone checklist.",
                parameters={
                    "type": "object",
                    "properties": {
                        "checklist_markdown": {
                            "type": "string",
                            "description": "Full updated markdown content for TASK.md.",
                        }
                    },
                    "required": ["checklist_markdown"],
                },
                handler=lambda args: self.memory.update_task_checklist(
                    args["checklist_markdown"]
                ),
                default_risk_tier="Tier 0",
            )
        )

        self.register(
            ToolSpec(
                name="record_memory",
                description="Persist a durable fact to this agent role's private MEMORY.md.",
                parameters={
                    "type": "object",
                    "properties": {"fact": {"type": "string"}},
                    "required": ["fact"],
                },
                handler=lambda args: self.memory.append_memory(args["fact"]),
                default_risk_tier="Tier 0",
            )
        )

        self.register(
            ToolSpec(
                name="lookup_skills",
                description="Load full instructions from one or more installed skills allowed for this role.",
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_names": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["skill_names"],
                },
                handler=self._handle_lookup_skills,
                default_risk_tier="Tier 0",
            )
        )

        self.register(
            ToolSpec(
                name="get_skill_resources",
                description="Fetch bundled template, script, or reference files from an allowed skill.",
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string"},
                        "filenames": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["skill_name", "filenames"],
                },
                handler=self._handle_get_skill_resources,
                default_risk_tier="Tier 0",
            )
        )

        self.register(
            ToolSpec(
                name="save_skill",
                description="Create or update a reusable SKILL.md package in the skills directory.",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "instructions_markdown": {"type": "string"},
                        "scripts": {"type": "object"},
                        "overwrite": {"type": "boolean"},
                    },
                    "required": ["name", "description", "instructions_markdown"],
                },
                handler=lambda args: self.skills.save_skill(
                    name=args["name"],
                    description=args["description"],
                    instructions_markdown=args["instructions_markdown"],
                    scripts=args.get("scripts"),
                    overwrite=bool(args.get("overwrite", False)),
                ),
                default_risk_tier="Tier 1",
            )
        )

        self.register(
            ToolSpec(
                name="delegate_subagent",
                description=(
                    "Spawn a strictly isolated specialist sub-agent (e.g., 'librarian', 'scout', "
                    "'sweeper', 'skeptic') with its own private workspace, private MEMORY.md/TASK.md, "
                    "data-wall path ACLs, and structured JSON handoff packet."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "role": {
                            "type": "string",
                            "description": "Specialist role ('librarian', 'scout', 'sweeper', 'skeptic', or custom).",
                        },
                        "objective": {
                            "type": "string",
                            "description": "Self-contained task instructions and verification criteria.",
                        },
                        "handoff_payload": {
                            "type": "object",
                            "description": "Structured JSON data packet passed across the role boundary (prevents narrative bias).",
                        },
                        "max_turns": {"type": "integer", "description": "Child iteration cap."},
                    },
                    "required": ["role", "objective"],
                },
                handler=self._handle_delegate_subagent,
                default_risk_tier="Tier 1",
            )
        )

    def _handle_lookup_skills(self, args: Dict[str, Any]) -> Dict[str, Any]:
        requested = list(args.get("skill_names", []))
        if self.role_policy and self.role_policy.allowed_skills:
            allowed_set = {s.lower() for s in self.role_policy.allowed_skills}
            blocked = [
                s for s in requested
                if s.lower().replace("user:", "").replace("system:", "") not in allowed_set
            ]
            if blocked:
                return {
                    "error": (
                        f"[ROLE_SKILL_FIREWALL] Role '{self.role_policy.role_name}' is only permitted "
                        f"to load skills {sorted(allowed_set)}; blocked: {blocked}"
                    )
                }
        return self.skills.lookup_skills(requested)

    def _handle_get_skill_resources(self, args: Dict[str, Any]) -> Dict[str, Any]:
        skill_name = str(args.get("skill_name", ""))
        clean = skill_name.lower().replace("user:", "").replace("system:", "")
        if self.role_policy and self.role_policy.allowed_skills:
            allowed_set = {s.lower() for s in self.role_policy.allowed_skills}
            if clean not in allowed_set:
                return {
                    "error": (
                        f"[ROLE_SKILL_FIREWALL] Role '{self.role_policy.role_name}' cannot access "
                        f"resources from skill '{skill_name}'."
                    )
                }
        return self.skills.get_skill_resources(skill_name, args.get("filenames", []))

    def _handle_host_bash(self, args: Dict[str, Any]) -> Dict[str, Any]:
        command = args["command"]
        cwd = args.get("cwd") or str(self.config.workspace_root)
        timeout_sec = int(args.get("timeout_sec", 30))
        background = bool(args.get("background", False))

        if self.role_policy:
            violation = self.role_policy.check_bash_command(command)
            if violation:
                return {"status": "blocked_by_role_firewall", "error": violation}
            cwd_violation = self.role_policy.check_path_access(
                pathlib.Path(cwd), "read", self.shared_root
            )
            if cwd_violation:
                return {"status": "blocked_by_role_firewall", "error": cwd_violation}

        if background:
            task_id = f"bg_{int(time.time() * 1000)}"
            log_file = self.config.workspace_root / "logs" / f"{task_id}.log"
            with log_file.open("w", encoding="utf-8") as lf:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=cwd,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            self._background_procs[task_id] = proc
            return {
                "status": "started_background",
                "task_id": task_id,
                "pid": proc.pid,
                "log_path": str(log_file),
            }

        completed = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }

    def _handle_file_ops(self, args: Dict[str, Any]) -> Dict[str, Any]:
        action = args["action"]
        target = pathlib.Path(args["path"]).expanduser().resolve()

        if self.role_policy:
            violation = self.role_policy.check_path_access(target, action, self.shared_root)
            if violation:
                return {"status": "blocked_by_role_firewall", "error": violation}

        if action == "read":
            if not target.exists():
                return {"error": f"File not found: {target}"}
            return {"path": str(target), "content": target.read_text(encoding="utf-8")[:16000]}
        if action == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content", "")
            target.write_text(content, encoding="utf-8")
            return {"status": "written", "path": str(target), "bytes": len(content.encode("utf-8"))}
        if action == "append":
            target.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content", "")
            with target.open("a", encoding="utf-8") as f:
                f.write(content)
            return {"status": "appended", "path": str(target)}
        if action == "list":
            if not target.exists():
                return {"error": f"Directory not found: {target}"}
            items = [
                {"name": p.name, "type": "dir" if p.is_dir() else "file"}
                for p in sorted(target.iterdir())
            ]
            return {"path": str(target), "entries": items}
        return {"error": f"Unsupported action: {action}"}

    def _handle_delegate_subagent(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self.subagent_spawner:
            return {"error": "Sub-agent spawner is not wired on this instance."}
        role = args["role"]
        objective = args["objective"]
        max_turns = int(args.get("max_turns", self.config.subagent_max_turns))
        handoff_payload = args.get("handoff_payload")
        return self.subagent_spawner(role, objective, max_turns, handoff_payload)
