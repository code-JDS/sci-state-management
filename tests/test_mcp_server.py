from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


STATE_MANAGEMENT = Path(__file__).resolve().parents[1]
SERVER_PATH = STATE_MANAGEMENT / "mcp_server.py"


def load_server_module():
    spec = importlib.util.spec_from_file_location("statemng_mcp_server", SERVER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MCPProcess:
    def __init__(self, server: Path, cwd: Path):
        self.process = subprocess.Popen(
            [sys.executable, str(server)],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.request_id = 0

    def notify(self, method: str, params: dict | None = None) -> None:
        assert self.process.stdin is not None
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.request_id += 1
        message = {"jsonrpc": "2.0", "id": self.request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        response = json.loads(self.process.stdout.readline())
        if response.get("id") != self.request_id:
            raise AssertionError(response)
        return response

    def call(self, name: str, arguments: dict | None = None) -> dict:
        response = self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        if "error" in response:
            raise AssertionError(response)
        return response["result"]

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            return_code = self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            return_code = self.process.wait(timeout=5)
        stderr = self.process.stderr.read() if self.process.stderr is not None else ""
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        if return_code != 0:
            raise AssertionError(f"MCP server exited {return_code}: {stderr}")


class MCPDefinitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = load_server_module()

    def test_complete_tool_surface_and_main_only_descriptions(self) -> None:
        names = [item["name"] for item in self.server.TOOLS]
        self.assertEqual(
            names,
            [
                "statemng_init",
                "statemng_project_list",
                "statemng_project_resume",
                "statemng_task_create",
                "statemng_task_ready",
                "statemng_task_claim",
                "statemng_task_show",
                "statemng_artifact_add",
                "statemng_task_submit",
                "statemng_task_accept",
                "statemng_task_block",
                "statemng_task_unblock",
                "statemng_task_fail",
                "statemng_task_status",
                "statemng_task_list",
                "statemng_artifact_show",
                "statemng_artifact_list",
                "statemng_doctor",
            ],
        )
        for name in ("statemng_task_accept", "statemng_task_unblock"):
            self.assertIn("Main Agent only", self.server.TOOL_BY_NAME[name]["description"])

    def test_schemas_are_closed_and_read_only_annotations_are_accurate(self) -> None:
        read_only = {
            "statemng_project_list",
            "statemng_task_ready",
            "statemng_task_show",
            "statemng_task_status",
            "statemng_task_list",
            "statemng_artifact_show",
            "statemng_artifact_list",
            "statemng_doctor",
        }
        for item in self.server.TOOLS:
            self.assertFalse(item["inputSchema"]["additionalProperties"])
            self.assertEqual(item["annotations"]["readOnlyHint"], item["name"] in read_only)
        retryable = self.server.TOOL_BY_NAME["statemng_task_fail"]["inputSchema"]
        self.assertEqual(retryable["properties"]["retryable"]["type"], "boolean")

    def test_protocol_errors_do_not_reach_the_cli(self) -> None:
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "statemng_init", "arguments": {"task_name": "x"}},
            }
        )
        self.assertEqual(response["error"]["code"], -32602)

    def test_initialize_negotiates_only_supported_protocol_versions(self) -> None:
        for version in self.server.SUPPORTED_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                response = self.server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": version},
                    }
                )
                self.assertEqual(response["result"]["protocolVersion"], version)
        unsupported = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": "2099-01-01"},
            }
        )
        self.assertEqual(
            unsupported["result"]["protocolVersion"], self.server.PROTOCOL_VERSION
        )

    def test_cli_launch_failure_is_returned_as_a_tool_error(self) -> None:
        original = self.server.PROJECT_ROOT
        try:
            self.server.PROJECT_ROOT = Path("/definitely/missing/project")
            result = self.server.call_tool("statemng_project_list", {})
        finally:
            self.server.PROJECT_ROOT = original
        self.assertTrue(result["isError"])
        self.assertFalse(result["structuredContent"]["ok"])
        self.assertIn("cannot execute statemng", result["structuredContent"]["error"])
        response = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "missing", "arguments": {}},
            }
        )
        self.assertEqual(response["error"]["code"], -32602)


class MCPStdioEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        shutil.copytree(STATE_MANAGEMENT, self.project / "state-management")
        self.unrelated_cwd = Path(self.temporary.name) / "unrelated"
        self.unrelated_cwd.mkdir()
        self.client = MCPProcess(
            self.project / "state-management" / "mcp_server.py", self.unrelated_cwd
        )

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def payload(self, result: dict) -> dict:
        text_payload = json.loads(result["content"][0]["text"])
        self.assertEqual(text_payload, result["structuredContent"])
        return result["structuredContent"]

    def call_ok(self, name: str, arguments: dict | None = None) -> dict:
        result = self.client.call(name, arguments)
        self.assertFalse(result["isError"], result)
        payload = self.payload(result)
        self.assertTrue(payload["ok"], payload)
        return payload

    def test_real_stdio_protocol_and_complete_research_lifecycle(self) -> None:
        initialized = self.client.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )["result"]
        self.assertEqual(initialized["protocolVersion"], "2025-11-25")
        self.assertIn("tools", initialized["capabilities"])
        self.client.notify("notifications/initialized")
        listed = self.client.request("tools/list")["result"]["tools"]
        self.assertEqual(len(listed), 18)
        self.assertNotIn("_command", listed[0])

        self.write("goal.md", "# Goal\n\nProduce and verify a result.\n")
        initialized_task = self.call_ok(
            "statemng_init", {"task_name": "study", "goal_file": "goal.md"}
        )
        self.assertEqual(
            Path(initialized_task["task_root"]).resolve(),
            (self.project / ".state-management" / "study").resolve(),
        )
        self.assertEqual(
            self.call_ok("statemng_project_list")["task_names"], ["study"]
        )

        self.write(
            "description.json",
            json.dumps(
                {
                    "objective": "Derive the checked result.",
                    "background": "Use the stated assumptions.",
                    "acceptance_criteria": ["The artifact contains the verified result."],
                }
            ),
        )
        created = self.call_ok(
            "statemng_task_create",
            {
                "task_name": "study",
                "title": "Derive result",
                "description_file": "description.json",
            },
        )
        task_id = created["task_id"]
        ready = self.call_ok("statemng_task_ready", {"task_name": "study"})
        self.assertEqual([item["task_id"] for item in ready["tasks"]], [task_id])
        claimed = self.call_ok(
            "statemng_task_claim", {"task_name": "study", "task_id": task_id}
        )
        run_id = claimed["run_id"]
        shown = self.call_ok(
            "statemng_task_show",
            {"task_name": "study", "task_id": task_id, "run_id": run_id},
        )
        workspace = Path(shown["workspace"])
        self.assertTrue(workspace.is_dir())
        result_file = workspace / "result.md"
        summary_file = workspace / "summary.md"
        result_file.write_text("verified result\n", encoding="utf-8")
        summary_file.write_text("The result was verified.\n", encoding="utf-8")
        artifact = self.call_ok(
            "statemng_artifact_add",
            {
                "task_name": "study",
                "task_id": task_id,
                "run_id": run_id,
                "path": str(result_file),
            },
        )
        artifact_id = artifact["artifact_id"]
        submitted = self.call_ok(
            "statemng_task_submit",
            {
                "task_name": "study",
                "task_id": task_id,
                "run_id": run_id,
                "summary_file": str(summary_file),
                "artifact_id": artifact_id,
            },
        )
        self.assertEqual(submitted["status"], "submitted")
        accepted = self.call_ok(
            "statemng_task_accept",
            {"task_name": "study", "task_id": task_id, "run_id": run_id},
        )
        self.assertEqual(accepted["status"], "completed")
        self.assertEqual(
            self.call_ok(
                "statemng_artifact_show",
                {"task_name": "study", "artifact_id": artifact_id},
            )["sha256"],
            self.call_ok(
                "statemng_artifact_list",
                {"task_name": "study", "task_id": task_id},
            )["artifacts"][0]["sha256"],
        )
        self.assertEqual(
            self.call_ok("statemng_doctor", {"task_name": "study"})["issues"], []
        )
        resumed = self.call_ok("statemng_project_resume", {"task_name": "study"})
        self.assertEqual(resumed["tasks"][0]["status"], "completed")
        self.assertEqual(resumed["incomplete_runs"], [])

    def test_block_unblock_preserves_old_run_and_cli_errors_are_tool_errors(self) -> None:
        self.write("goal.md", "Goal\n")
        self.call_ok("statemng_init", {"task_name": "study", "goal_file": "goal.md"})
        self.write(
            "description.json",
            json.dumps(
                {
                    "objective": "Attempt the task.",
                    "background": "",
                    "acceptance_criteria": ["It works."],
                }
            ),
        )
        task_id = self.call_ok(
            "statemng_task_create",
            {
                "task_name": "study",
                "title": "Blocked task",
                "description_file": "description.json",
            },
        )["task_id"]
        first_run = self.call_ok(
            "statemng_task_claim", {"task_name": "study", "task_id": task_id}
        )["run_id"]
        workspace = (
            self.project / ".state-management" / "study" / "runs" / first_run / "workspace"
        )
        reason = workspace / "reason.md"
        reason.write_text("Required external input is absent.\n", encoding="utf-8")
        blocked = self.call_ok(
            "statemng_task_block",
            {
                "task_name": "study",
                "task_id": task_id,
                "run_id": first_run,
                "reason_file": str(reason),
            },
        )
        self.assertEqual(blocked["status"], "blocked")
        unblocked = self.call_ok(
            "statemng_task_unblock",
            {"task_name": "study", "task_id": task_id, "run_id": first_run},
        )
        self.assertEqual(unblocked["status"], "ready")
        status = self.call_ok(
            "statemng_task_status", {"task_name": "study", "task_id": task_id}
        )
        self.assertNotIn("run_id", status)
        old_run = json.loads(
            (self.project / ".state-management" / "study" / "runs" / first_run / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(old_run["status"], "blocked")
        self.assertEqual(old_run["reason"], "Required external input is absent.\n")
        second_run = self.call_ok(
            "statemng_task_claim", {"task_name": "study", "task_id": task_id}
        )["run_id"]
        self.assertNotEqual(second_run, first_run)

        failed_call = self.client.call(
            "statemng_task_unblock",
            {"task_name": "study", "task_id": task_id, "run_id": first_run},
        )
        self.assertTrue(failed_call["isError"])
        self.assertFalse(self.payload(failed_call)["ok"])


if __name__ == "__main__":
    unittest.main()
