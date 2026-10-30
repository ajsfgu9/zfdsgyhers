"""Dedicated Server Tool Registry, Sentinel Risk-Tier Classifier, and Parallel Executor."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import pathlib
import subprocess
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from ouroboros.config import AgentConfig
from ouroboros.memory import MemoryAndStateStore
from ouroboros.skills import SkillRegistry


@dataclasses.dataclass
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Any]
    is_interactive: bool = False
    default_risk_tier: str = "Tier 0"  # Tier 0 (Low) | Tier 1 (Medium) | Tier 2 (High)


class ServerToolRegistry:
    """Manages host server tools, background daemons, and Sentinel risk-tier checks."""

    def __init__(
        self,
        config: AgentConfig,
        memory: MemoryAndStateStore,
        skills: SkillRegistry,
        subagent_spawner: Optional[Callable[[str, str, int], Dict[str, Any]]] = None,
        approval_callback: Optional[Callable[[str, Dict[str, Any], str], bool]] = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.skills = skills
        self.subagent_spawner = subagent_spawner
        self.approval_callback = approval_callback
        self._tools: Dict[str, ToolSpec] = {}
        self._background_procs: Dict[str, subprocess.Popen[str]] = {}
        self._register_builtin_tools()

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def classify_risk_tier(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Diff/action-driven Risk Tier classification per @code-review-governance."""
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
        """Executes multiple tool calls concurrently via ThreadPoolExecutor while preserving order."""
        if not tool_calls:
            return []

        # If only 1 call or any call is interactive, execute sequentially
        if len(tool_calls) == 1 or any(
            self._tools.get(tc.get("function", {}).get("name", ""), ToolSpec("", "", {}, lambda _: None)).is_interactive
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
                    "Supports synchronous execution with timeout or background daemon spawning."
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
                description="Read, write, or list files on the server filesystem.",
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
                description=(
                    "Update the Gemini-Task style Phase-2 milestone checklist (TASK.md) "
                    "to track pending (- [ ]), in-progress (- [/]), and completed (- [x]) steps."
                ),
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
                description="Persist a durable architectural or operational fact to MEMORY.md.",
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
                description="Load full instructions from one or more installed skills by name.",
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_names": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["skill_names"],
                },
                handler=lambda args: self.skills.lookup_skills(args["skill_names"]),
                default_risk_tier="Tier 0",
            )
        )

        self.register(
            ToolSpec(
                name="get_skill_resources",
                description="Fetch bundled template, script, or reference files from a skill.",
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string"},
                        "filenames": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["skill_name", "filenames"],
                },
                handler=lambda args: self.skills.get_skill_resources(
                    args["skill_name"], args["filenames"]
                ),
                default_risk_tier="Tier 0",
            )
        )

        self.register(
            ToolSpec(
                name="save_skill",
                description=(
                    "Create or update a reusable SKILL.md package in the skills directory "
                    "(Hermes closed learning loop)."
                ),
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
                    "Spawn a recursive child agent with an isolated context window and bounded "
                    "iteration budget (Grok Bot Chief-of-Staff / Hermes delegate_task pattern)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "description": "Specialist role name."},
                        "objective": {
                            "type": "string",
                            "description": "Self-contained task instructions and verification criteria.",
                        },
                        "max_turns": {"type": "integer", "description": "Child iteration cap."},
                    },
                    "required": ["role", "objective"],
                },
                handler=self._handle_delegate_subagent,
                default_risk_tier="Tier 1",
            )
        )

    def _handle_host_bash(self, args: Dict[str, Any]) -> Dict[str, Any]:
        command = args["command"]
        cwd = args.get("cwd") or str(self.config.workspace_root)
        timeout_sec = int(args.get("timeout_sec", 30))
        background = bool(args.get("background", False))

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
        return self.subagent_spawner(role, objective, max_turns)
