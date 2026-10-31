"""Unit and integration tests for Ouroboros Recursive Manager Agent, Telegram Bridge & Governance."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from typing import Any, Dict, List

from ouroboros.config import AgentConfig, ModelProviderConfig
from ouroboros.gateway import OuroborosGatewayServer, ScheduledRoutine
from ouroboros.loop import OuroborosManagerAgent
from ouroboros.telegram_bot import TelegramBotBridge
from scripts.circuit_breaker import (
    classify_diff_paths,
    evaluate_circuit_breakers,
    score_incident_evals,
)


class TestOuroborosAgentAndGovernance(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp_dir.name)
        self.config = AgentConfig(
            workspace_root=self.root / "workspace",
            skills_dir=self.root / "skills",
            max_turns=15,
            subagent_max_turns=5,
            max_recursion_depth=2,
            skill_learning_min_tool_calls=5,
            telegram_bot_token="123456:TEST_BOT_TOKEN",
            telegram_allowed_chat_ids=[777888],
            telegram_webhook_secret="sec_token_123",
        )
        self.config.ensure_directories()
        # Copy starter skill
        (self.config.skills_dir / "self-healer").mkdir(parents=True, exist_ok=True)
        (self.config.skills_dir / "self-healer" / "SKILL.md").write_text(
            "---\nname: self-healer\ndescription: Diagnose server health.\n---\nRun df -h.",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_recursive_loop_parallel_tools_and_closed_learning_loop(self) -> None:
        """Verifies Phase-2 TASK.md updates, parallel tool fan-out, and >=5 tool call skill learning."""
        call_counter = {"step": 0}

        def fake_llm(
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]],
            provider: ModelProviderConfig,
        ) -> Dict[str, Any]:
            self.assertTrue(OuroborosManagerAgent.validate_message_alternation(messages))
            call_counter["step"] += 1
            step = call_counter["step"]

            if step == 1:
                return {
                    "role": "assistant",
                    "content": "Decomposing task and executing 5 parallel actions.",
                    "tool_calls": [
                        {
                            "id": "tc_1",
                            "function": {
                                "name": "update_task_checklist",
                                "arguments": json.dumps(
                                    {
                                        "checklist_markdown": "# TASK.md\n- [x] Step 1: Audit\n- [/] Step 2: Build"
                                    }
                                ),
                            },
                        },
                        {
                            "id": "tc_2",
                            "function": {
                                "name": "lookup_skills",
                                "arguments": json.dumps({"skill_names": ["self-healer"]}),
                            },
                        },
                        {
                            "id": "tc_3",
                            "function": {
                                "name": "record_memory",
                                "arguments": json.dumps({"fact": "Server uses port 8787."}),
                            },
                        },
                        {
                            "id": "tc_4",
                            "function": {
                                "name": "file_ops",
                                "arguments": json.dumps(
                                    {
                                        "action": "write",
                                        "path": str(self.config.workspace_root / "hello.txt"),
                                        "content": "ouroboros-ready",
                                    }
                                ),
                            },
                        },
                        {
                            "id": "tc_5",
                            "function": {
                                "name": "host_bash",
                                "arguments": json.dumps({"command": "echo server_ok"}),
                            },
                        },
                    ],
                }
            if step == 2:
                return {
                    "role": "assistant",
                    "content": "Initial execution complete.",
                }
            if step == 3:
                return {
                    "role": "assistant",
                    "content": "Saving learned skill.",
                    "tool_calls": [
                        {
                            "id": "tc_6",
                            "function": {
                                "name": "save_skill",
                                "arguments": json.dumps(
                                    {
                                        "name": "health-audit",
                                        "description": "Automated server health check.",
                                        "instructions_markdown": "# Health Audit\nRun echo server_ok.",
                                    }
                                ),
                            },
                        }
                    ],
                }
            return {
                "role": "assistant",
                "content": "All milestones completed and health-audit skill saved.",
            }

        agent = OuroborosManagerAgent(config=self.config, llm_callable=fake_llm)
        result = agent.run_task("Audit the server and create a health check workflow.")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["total_tool_calls"], 6)
        self.assertIn("Step 1: Audit", result["task_checklist"])
        self.assertTrue((self.config.skills_dir / "health-audit" / "SKILL.md").exists())

    def test_subagent_delegation_and_recursion_cap(self) -> None:
        """Verifies Chief-of-Staff sub-agent spawning and max_recursion_depth guard."""

        def fake_llm(
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]],
            provider: ModelProviderConfig,
        ) -> Dict[str, Any]:
            if "Specialist Sub-Agent Role:" in messages[0]["content"]:
                return {"role": "assistant", "content": "Sub-agent finished log analysis."}
            has_tool_result = any(m.get("role") == "tool" for m in messages)
            if not has_tool_result:
                return {
                    "role": "assistant",
                    "content": "Delegating to log-specialist.",
                    "tool_calls": [
                        {
                            "id": "sub_1",
                            "function": {
                                "name": "delegate_subagent",
                                "arguments": json.dumps(
                                    {
                                        "role": "log-specialist",
                                        "objective": "Check system logs.",
                                        "max_turns": 3,
                                    }
                                ),
                            },
                        }
                    ],
                }
            return {"role": "assistant", "content": "Parent synthesized sub-agent report."}

        agent = OuroborosManagerAgent(config=self.config, llm_callable=fake_llm)
        res = agent.run_task("Delegate log inspection.")
        self.assertEqual(res["status"], "completed")
        self.assertIn("Parent synthesized", res["final_response"])

    def test_sentinel_tier_2_blocking(self) -> None:
        """Verifies that destructive or governance-modifying commands are blocked without human approval."""
        agent = OuroborosManagerAgent(config=self.config, approval_callback=lambda *_: False)
        results = agent.tools.execute_tool_calls(
            "sess_sec",
            [
                {
                    "id": "sec_1",
                    "function": {
                        "name": "host_bash",
                        "arguments": json.dumps({"command": "rm -rf / --no-preserve-root"}),
                    },
                }
            ],
        )
        payload = json.loads(results[0]["content"])
        self.assertEqual(payload["status"], "blocked_by_sentinel_tier_2")
        self.assertEqual(payload["risk_tier"], "Tier 2")

    def test_context_compression_preserves_tool_pairs(self) -> None:
        """Verifies context compaction never splits assistant tool_calls from tool results."""
        self.config.compression_protect_last_n = 2
        agent = OuroborosManagerAgent(config=self.config)
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "First user message " * 50},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "host_bash", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "name": "host_bash", "content": "ok"},
            {"role": "assistant", "content": "Done step 1"},
        ]
        compressed = agent.compress_context_if_needed(messages, max_tokens=50)
        self.assertTrue(OuroborosManagerAgent.validate_message_alternation(compressed))

    def test_governance_circuit_breakers_and_diff_classifier(self) -> None:
        """Verifies @code-review-governance risk tiers and automated circuit breaker triggers."""
        healthy = {
            "merged_prs_7d": 50,
            "reverted_prs_7d": 0,
            "baseline_30d_revert_rate": 0.02,
            "sev1_sev2_escapes_from_tier0_tier1": 0,
            "mean_rounds_to_pass": 1.2,
            "max_pr_rounds_observed": 2,
            "module_hotfixes_14d": {"ouroboros/loop.py": 1},
        }
        rep_ok = evaluate_circuit_breakers(healthy)
        self.assertEqual(rep_ok["status"], "HEALTHY")

        tier0 = classify_diff_paths(["docs/arch.md", "tests/test_ouroboros.py"], breaker_tripped=False)
        self.assertEqual(tier0["risk_tier"], "Tier 0")
        self.assertEqual(tier0["required_human_approvals"], 0)

        tier0_elevated = classify_diff_paths(["docs/arch.md"], breaker_tripped=True)
        self.assertEqual(tier0_elevated["risk_tier"], "Tier 1")
        self.assertEqual(tier0_elevated["required_human_approvals"], 1)

        tier2 = classify_diff_paths(["REVIEW.md", "ouroboros/loop.py"], breaker_tripped=False)
        self.assertEqual(tier2["risk_tier"], "Tier 2")
        self.assertEqual(tier2["required_human_approvals"], 2)

    def test_multi_agent_role_isolation_and_holdout_data_firewall(self) -> None:
        """Verifies that Scout/Sweeper have isolated memory/workspaces and are blocked from Holdout data, while Skeptic is allowed."""
        holdout_file = self.config.workspace_root / "data" / "holdout" / "oos_bars.csv"
        develop_file = self.config.workspace_root / "data" / "develop" / "in_sample_bars.csv"
        ledger_file = self.config.workspace_root / "ledger" / "research_ledger.jsonl"
        holdout_file.parent.mkdir(parents=True, exist_ok=True)
        develop_file.parent.mkdir(parents=True, exist_ok=True)
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        holdout_file.write_text("2026-10-01,100.5\n", encoding="utf-8")
        develop_file.write_text("2025-01-01,95.0\n", encoding="utf-8")
        ledger_file.write_text("", encoding="utf-8")

        def fake_llm(
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]],
            provider: ModelProviderConfig,
        ) -> Dict[str, Any]:
            sys_prompt = messages[0]["content"]
            has_tool_response = any(m.get("role") == "tool" for m in messages)

            if "Isolated Specialist Sub-Agent Role: sweeper" in sys_prompt:
                if not has_tool_response:
                    return {
                        "role": "assistant",
                        "content": "Sweeper recording private note and attempting holdout read.",
                        "tool_calls": [
                            {
                                "id": "sw_mem",
                                "function": {
                                    "name": "record_memory",
                                    "arguments": json.dumps({"fact": "Sweeper private bias note"}),
                                },
                            },
                            {
                                "id": "sw_illegal_holdout",
                                "function": {
                                    "name": "file_ops",
                                    "arguments": json.dumps(
                                        {"action": "read", "path": str(holdout_file)}
                                    ),
                                },
                            },
                            {
                                "id": "sw_legal_dev",
                                "function": {
                                    "name": "file_ops",
                                    "arguments": json.dumps(
                                        {"action": "read", "path": str(develop_file)}
                                    ),
                                },
                            },
                        ],
                    }
                tool_msgs = [m for m in messages if m.get("role") == "tool"]
                return {
                    "role": "assistant",
                    "content": json.dumps([json.loads(m["content"]) if m["content"].startswith("{") else m["content"] for m in tool_msgs]),
                }

            if "Isolated Specialist Sub-Agent Role: skeptic" in sys_prompt:
                self.assertNotIn("Sweeper private bias note", sys_prompt)
                if not has_tool_response:
                    return {
                        "role": "assistant",
                        "content": "Skeptic reading untouched holdout slice.",
                        "tool_calls": [
                            {
                                "id": "sk_holdout",
                                "function": {
                                    "name": "file_ops",
                                    "arguments": json.dumps(
                                        {"action": "read", "path": str(holdout_file)}
                                    ),
                                },
                            },
                            {
                                "id": "sk_illegal_ledger_write",
                                "function": {
                                    "name": "file_ops",
                                    "arguments": json.dumps(
                                        {
                                            "action": "write",
                                            "path": str(ledger_file),
                                            "content": "tamper",
                                        }
                                    ),
                                },
                            },
                        ],
                    }
                tool_msgs = [m for m in messages if m.get("role") == "tool"]
                return {
                    "role": "assistant",
                    "content": json.dumps([json.loads(m["content"]) if m["content"].startswith("{") else m["content"] for m in tool_msgs]),
                }

            return {"role": "assistant", "content": "Root done"}

        lead_agent = OuroborosManagerAgent(config=self.config, llm_callable=fake_llm)

        sweeper_res = lead_agent._spawn_child_agent(
            role="sweeper",
            objective="Run parameter sweep on develop slice.",
            max_turns=4,
            handoff_payload={"experiment_id": "2026-10-30-001", "grid": [10, 20]},
        )
        sweeper_outputs = json.loads(sweeper_res["summary"])
        self.assertEqual(sweeper_outputs[1]["status"], "blocked_by_role_firewall")
        self.assertIn("2025-01-01,95.0", sweeper_outputs[2]["content"])

        skeptic_res = lead_agent._spawn_child_agent(
            role="skeptic",
            objective="Audit survivor on holdout slice.",
            max_turns=4,
            handoff_payload={"experiment_id": "2026-10-30-001", "frozen_param": 20},
        )
        skeptic_outputs = json.loads(skeptic_res["summary"])
        self.assertIn("2026-10-01,100.5", skeptic_outputs[0]["content"])
        self.assertEqual(skeptic_outputs[1]["status"], "blocked_by_role_firewall")
        self.assertNotEqual(sweeper_res["isolated_workspace"], skeptic_res["isolated_workspace"])

    def test_telegram_bot_bridge_commands_tier2_approval_and_hot_reload(self) -> None:
        """Verifies Telegram command execution, unauthorized blocking, Tier-2 /approve, and /reload skill hot-reload."""
        sent_messages: List[Dict[str, Any]] = []

        def fake_http(url: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
            if url.endswith("/sendMessage"):
                sent_messages.append(payload)
            return {"ok": True, "result": {}}

        def fake_llm(
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]],
            provider: ModelProviderConfig,
        ) -> Dict[str, Any]:
            return {"role": "assistant", "content": "Telegram task executed cleanly."}

        agent = OuroborosManagerAgent(config=self.config, llm_callable=fake_llm)
        bridge = TelegramBotBridge(agent=agent, http_sender=fake_http)

        # 1. Unauthorized chat should be blocked
        unauth_reply = bridge.handle_update(
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 999999}, "text": "/cmd whoami"},
            }
        )
        self.assertIn("Access denied", unauth_reply or "")

        # 2. Authorized direct command (/cmd)
        cmd_reply = bridge.handle_update(
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 777888},
                    "text": "/cmd echo telegram_ping_ok",
                },
            }
        )
        self.assertIn("telegram_ping_ok", cmd_reply or "")
        self.assertIn("exit_code=0", cmd_reply or "")

        # 3. Tier-2 sensitive command (/cmd cat /etc/sudoers) -> blocked + queued for /approve
        tier2_reply = bridge.handle_update(
            {
                "update_id": 3,
                "message": {
                    "message_id": 12,
                    "chat": {"id": 777888},
                    "text": "/cmd echo checking CODEOWNERS",
                },
            }
        )
        self.assertIn("Blocked by Sentinel Tier-2 Guard", tier2_reply or "")
        # Extract approval ID and approve it
        approval_id = [
            tok for tok in (tier2_reply or "").split() if tok.startswith("ap_")
        ][0]
        approved_reply = bridge.handle_update(
            {
                "update_id": 4,
                "message": {
                    "message_id": 13,
                    "chat": {"id": 777888},
                    "text": f"/approve {approval_id}",
                },
            }
        )
        self.assertIn("checking CODEOWNERS", approved_reply or "")

        # 4. Hot-reload new skill via /reload without restarting agent
        (self.config.skills_dir / "live-skill").mkdir(parents=True, exist_ok=True)
        (self.config.skills_dir / "live-skill" / "SKILL.md").write_text(
            "---\nname: live-skill\ndescription: Added while server running.\n---\nBody.",
            encoding="utf-8",
        )
        reload_reply = bridge.handle_update(
            {
                "update_id": 5,
                "message": {"message_id": 14, "chat": {"id": 777888}, "text": "/reload"},
            }
        )
        self.assertIn("live-skill", reload_reply or "")

        # 5. Webhook secret validation and natural-language task execution
        code_bad, _ = bridge.handle_webhook_request(
            {"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            b'{"update_id": 6}',
        )
        self.assertEqual(code_bad, 403)

        code_ok, body_ok = bridge.handle_webhook_request(
            {"X-Telegram-Bot-Api-Secret-Token": "sec_token_123"},
            json.dumps(
                {
                    "update_id": 7,
                    "message": {
                        "message_id": 15,
                        "chat": {"id": 777888},
                        "text": "Check system status and summarize",
                    },
                }
            ).encode("utf-8"),
        )
        self.assertEqual(code_ok, 200)
        self.assertTrue(body_ok["handled"])
        self.assertIn("Telegram task executed cleanly.", sent_messages[-1]["text"])



    def test_telegram_message_splitting_and_skill_permission_diagnostics(self) -> None:
        """Verifies >4000 char output chunking and actionable Linux chown hints when skills_dir is read-only."""
        long_output = "\n".join([f"line_{i}: " + ("x" * 80) for i in range(100)])
        chunks = TelegramBotBridge.split_message(long_output, max_len=4000)
        self.assertGreater(len(chunks), 2)
        for ch in chunks:
            self.assertLessEqual(len(ch), 4000)

        # Simulate read-only skills directory (e.g., owned by root on Linux server)
        self.config.skills_dir.chmod(0o555)
        try:
            agent = OuroborosManagerAgent(config=self.config)
            res = agent.skills.save_skill(
                name="locked-skill",
                description="Should return permission_denied instead of crashing.",
                instructions_markdown="# Locked",
            )
            if not agent.skills.is_writable():
                self.assertEqual(res["status"], "permission_denied")
                self.assertIn("chown -R", res["remediation"])
        finally:
            self.config.skills_dir.chmod(0o755)



    def test_live_dotenv_hot_reload_and_telegram_bot_integration(self) -> None:
        """Verifies that editing .env while the agent is already running dynamically activates Telegram and processes commands/tasks without restart."""
        env_file = self.root / ".env"
        env_file.write_text("OUROBOROS_MODEL=gpt-4.1-initial\n", encoding="utf-8")
        self.config.telegram_bot_token = ""
        self.config.telegram_allowed_chat_ids = []
        self.config.env_file = env_file
        self.config.__post_init__()

        def fake_llm(
            messages: List[Dict[str, Any]],
            tools: List[Dict[str, Any]],
            provider: ModelProviderConfig,
        ) -> Dict[str, Any]:
            return {
                "role": "assistant",
                "content": f"Telegram task handled by model={provider.model}",
            }

        agent = OuroborosManagerAgent(config=self.config, llm_callable=fake_llm)
        self.assertFalse(agent.telegram.is_configured())

        # Now simulate the user editing .env while the agent is already running (NO restart!)
        import time as _time
        _time.sleep(0.01)
        env_file.write_text(
            "OUROBOROS_MODEL=gpt-4.1-hot-swapped\n"
            "TELEGRAM_BOT_TOKEN=999888777:AAH_fake_test_token\n"
            "TELEGRAM_CHAT_ID=5551234\n",
            encoding="utf-8",
        )

        # Mock Telegram HTTP endpoint
        sent_messages: List[Dict[str, Any]] = []
        updates_queue: List[Dict[str, Any]] = [
            {
                "update_id": 1001,
                "message": {"chat": {"id": 5551234}, "text": "/status"},
            },
            {
                "update_id": 1002,
                "message": {"chat": {"id": 5551234}, "text": "Check server disk and report."},
            },
        ]

        class _FakeResp:
            def __init__(self, payload: Dict[str, Any]) -> None:
                self._raw = json.dumps(payload).encode("utf-8")
            def read(self) -> bytes:
                return self._raw
            def __enter__(self) -> "_FakeResp":
                return self
            def __exit__(self, *args: object) -> None:
                pass

        def fake_tg_http(req: object, timeout: int) -> _FakeResp:
            url = getattr(req, "full_url", "")
            body = json.loads(getattr(req, "data", b"{}").decode("utf-8"))
            if url.endswith("/getMe"):
                return _FakeResp({"ok": True, "result": {"username": "ouroboros_test_bot"}})
            if url.endswith("/deleteWebhook"):
                return _FakeResp({"ok": True, "result": True})
            if url.endswith("/sendChatAction"):
                return _FakeResp({"ok": True, "result": True})
            if url.endswith("/getUpdates"):
                batch = list(updates_queue)
                updates_queue.clear()
                return _FakeResp({"ok": True, "result": batch})
            if url.endswith("/sendMessage"):
                sent_messages.append(body)
                return _FakeResp({"ok": True, "result": {"message_id": len(sent_messages)}})
            return _FakeResp({"ok": True})

        agent.telegram.http_opener = fake_tg_http

        # Poll once: this should automatically detect the modified .env, configure Telegram, clear webhook,
        # process /status and the natural language task using the hot-swapped model, and persist offset 1002 in SQLite!
        processed = agent.telegram.poll_once()
        self.assertTrue(agent.telegram.is_configured())
        self.assertEqual(agent.config.primary_provider.model, "gpt-4.1-hot-swapped")
        self.assertEqual(len(processed), 2)
        self.assertEqual(agent.memory.get_kv("telegram_last_update_id"), 1002)
        self.assertTrue(any("Ouroboros Status: HEALTHY" in m["text"] for m in sent_messages))
        self.assertTrue(any("model=gpt-4.1-hot-swapped" in m["text"] for m in sent_messages))

    def test_zero_downtime_plugin_hot_loading_and_fault_isolation(self) -> None:
        """Verifies that dropping a new .py tool into plugins/ registers it live without restart, and syntax errors in plugins never crash the agent."""
        self.config.plugins_dir = self.root / "plugins"
        self.config.plugins_dir.mkdir(parents=True, exist_ok=True)
        agent = OuroborosManagerAgent(config=self.config)

        # Drop a new live plugin file while agent is already running
        plugin_file = self.config.plugins_dir / "custom_ping.py"
        plugin_file.write_text(
            "from ouroboros.tools import ToolSpec\n"
            "def register_plugins(reg):\n"
            "    reg.register(ToolSpec(name='custom_ping', description='Ping tool', parameters={'type':'object','properties':{}}, handler=lambda a: {'pong': True}))\n",
            encoding="utf-8",
        )
        report = agent.hot_reload.sync_all(agent, force=False)
        self.assertIn("custom_ping.py", report["reloaded_plugins"])
        self.assertIn("custom_ping", agent.tools._tools)

        # Execute the newly hot-plugged tool
        res = agent.tools.execute_tool_calls(
            "sess_plug",
            [{"id": "p1", "function": {"name": "custom_ping", "arguments": "{}"}}],
        )
        self.assertEqual(json.loads(res[0]["content"])["pong"], True)

        # Now write a broken plugin with a SyntaxError and verify the agent stays alive and keeps custom_ping working!
        bad_plugin = self.config.plugins_dir / "broken_plugin.py"
        bad_plugin.write_text("def register_plugins(reg):\n    syntax error here !!!\n", encoding="utf-8")
        report2 = agent.hot_reload.sync_all(agent, force=False)
        self.assertTrue(len(report2["errors"]) >= 1)
        self.assertIn("custom_ping", agent.tools._tools)



    def test_industrial_cache_atomic_plugin_swap_glob_firewall_and_logger(self) -> None:
        """Verifies v0.4.0 Multi-Tier Cache hit rates, Prompt-Prefix alignment, Atomic Plugin Swap (including deletion), Glob Firewall, and Rotating Logger."""
        agent = OuroborosManagerAgent(config=self.config)

        # 1. Prompt-Prefix Cache Alignment: Static Skill Catalog must precede volatile TASK.md checklist
        prompt1 = agent.build_system_prompt()
        prompt2 = agent.build_system_prompt()
        self.assertEqual(prompt1, prompt2)
        self.assertLess(
            prompt1.index("Available On-Demand Skills"),
            prompt1.index("Active Milestone Checklist"),
        )
        metrics = agent.memory.cache_metrics()
        self.assertGreaterEqual(metrics["file_cache"]["hits"], 3)
        self.assertGreaterEqual(agent.skills.cache_hits, 1)

        # 2. Hardened Glob & Path-Normalization Firewall on RoleSandboxPolicy
        sweeper_policy = agent.role_policies_registry["sweeper"]
        self.assertIsNotNone(sweeper_policy.check_bash_command("cat data/hold*/*.csv"))
        self.assertIsNotNone(sweeper_policy.check_bash_command("cat data//holdout/oos.csv"))
        self.assertIsNotNone(sweeper_policy.check_bash_command("cd data && head holdout/oos.csv"))

        # 3. Atomic Plugin Registry Swap: when a plugin file is deleted, its tools are removed (no ghost tools)
        self.config.plugins_dir = self.root / "plugins_atomic"
        self.config.plugins_dir.mkdir(parents=True, exist_ok=True)
        agent.hot_reload.plugins_dir = self.config.plugins_dir
        pfile = self.config.plugins_dir / "ephemeral_tool.py"
        pfile.write_text(
            "from ouroboros.tools import ToolSpec\n"
            "def register_plugins(reg):\n"
            "    reg.register(ToolSpec(name='temp_alpha', description='t', parameters={'type':'object','properties':{}}, handler=lambda a: {'ok':1}))\n",
            encoding="utf-8",
        )
        agent.hot_reload.sync_all(agent, force=False)
        self.assertIn("temp_alpha", agent.tools._tools)

        # Delete the plugin file and sync: temp_alpha must be unregistered!
        pfile.unlink()
        agent.hot_reload.sync_all(agent, force=False)
        self.assertNotIn("temp_alpha", agent.tools._tools)

        # 4. Preflight-verified graceful_reexec dry-run and Rotating Logger (/logs)
        reexec_check = agent.hot_reload.graceful_reexec(agent, dry_run=True)
        self.assertEqual(reexec_check["status"], "ready_for_execv")
        agent.logger.info("adversarial_audit_verified", round=2)
        tail_lines = agent.logger.tail(lines=10)
        self.assertTrue(any("adversarial_audit_verified" in line for line in tail_lines))


if __name__ == "__main__":
    unittest.main()
