"""Unit and integration tests for Ouroboros Recursive Manager Agent & Governance."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from typing import Any, Dict, List

from ouroboros.config import AgentConfig, ModelProviderConfig
from ouroboros.gateway import OuroborosGatewayServer, ScheduledRoutine
from ouroboros.loop import OuroborosManagerAgent
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
                # Fan out 5 parallel tool calls in Turn 1 (update checklist, lookup skill, record memory, write file, run bash)
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
                # Model tries to finish without saving a skill; loop will inject the closed learning loop nudge
                return {
                    "role": "assistant",
                    "content": "Initial execution complete.",
                }
            if step == 3:
                # Responds to closed learning loop nudge by saving a reusable skill
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
            # If running inside child subagent, return immediately
            if "Specialist Sub-Agent Role:" in messages[0]["content"]:
                return {"role": "assistant", "content": "Sub-agent finished log analysis."}
            # If parent hasn't delegated yet, call delegate_subagent
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

        # When breaker trips, Tier 0 is automatically elevated to Tier 1
        tier0_elevated = classify_diff_paths(["docs/arch.md"], breaker_tripped=True)
        self.assertEqual(tier0_elevated["risk_tier"], "Tier 1")
        self.assertEqual(tier0_elevated["required_human_approvals"], 1)

        tier2 = classify_diff_paths(["REVIEW.md", "ouroboros/loop.py"], breaker_tripped=False)
        self.assertEqual(tier2["risk_tier"], "Tier 2")
        self.assertEqual(tier2["required_human_approvals"], 2)


if __name__ == "__main__":
    unittest.main()
