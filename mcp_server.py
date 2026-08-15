#!/usr/bin/env python3
"""Zero-dependency STDIO MCP facade for the repository-local statemng CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


SERVER_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = SERVER_DIRECTORY.parent
CLI = SERVER_DIRECTORY / "statemng"
SUPPORTED_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


def object_schema(
    properties: dict[str, dict[str, Any]], required: tuple[str, ...] = ()
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


STRING = {"type": "string", "minLength": 1}
BOOLEAN = {"type": "boolean"}


def tool(
    name: str,
    description: str,
    schema: dict[str, Any],
    command: Callable[[dict[str, Any]], list[str]],
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema,
        "annotations": {"readOnlyHint": read_only},
        "_command": command,
    }


def option(arguments: list[str], flag: str, value: Any | None) -> None:
    if value is not None:
        arguments.extend([flag, str(value)])


TOOLS = [
    tool(
        "statemng_init",
        "Initialize one persistent research task from a UTF-8 goal file.",
        object_schema({"task_name": STRING, "goal_file": STRING}, ("task_name", "goal_file")),
        lambda a: ["init", a["task_name"], "--goal-file", a["goal_file"]],
    ),
    tool(
        "statemng_project_list",
        "List persistent research task names in the nearest project state root.",
        object_schema({}),
        lambda _a: ["project", "list"],
        read_only=True,
    ),
    tool(
        "statemng_project_resume",
        "Load one research task, repair derived ready states, and rebuild its view.",
        object_schema({"task_name": STRING}, ("task_name",)),
        lambda a: ["project", "resume", "--task-name", a["task_name"]],
    ),
    tool(
        "statemng_task_create",
        "Create a subtask from an exact JSON description file.",
        object_schema(
            {
                "task_name": STRING,
                "title": STRING,
                "description_file": STRING,
                "depends_on": STRING,
            },
            ("task_name", "title", "description_file"),
        ),
        lambda a: _task_create_command(a),
    ),
    tool(
        "statemng_task_ready",
        "List currently claimable subtasks.",
        object_schema({"task_name": STRING}, ("task_name",)),
        lambda a: ["task", "ready", "--task-name", a["task_name"]],
        read_only=True,
    ),
    tool(
        "statemng_task_claim",
        "Atomically claim a ready subtask and create a fresh run workspace.",
        object_schema({"task_name": STRING, "task_id": STRING}, ("task_name", "task_id")),
        lambda a: ["task", "claim", a["task_id"], "--task-name", a["task_name"]],
    ),
    tool(
        "statemng_task_show",
        "Return the exact context and workspace for the current run.",
        object_schema(
            {"task_name": STRING, "task_id": STRING, "run_id": STRING},
            ("task_name", "task_id", "run_id"),
        ),
        lambda a: [
            "task", "show", a["task_id"], "--task-name", a["task_name"],
            "--run-id", a["run_id"],
        ],
        read_only=True,
    ),
    tool(
        "statemng_artifact_add",
        "Register an immutable artifact produced inside the current run.",
        object_schema(
            {"task_name": STRING, "task_id": STRING, "run_id": STRING, "path": STRING},
            ("task_name", "task_id", "run_id", "path"),
        ),
        lambda a: [
            "artifact", "add", "--task-name", a["task_name"], "--task", a["task_id"],
            "--run-id", a["run_id"], "--path", a["path"],
        ],
    ),
    tool(
        "statemng_task_submit",
        "Submit a run summary and one registered artifact for review.",
        object_schema(
            {
                "task_name": STRING,
                "task_id": STRING,
                "run_id": STRING,
                "summary_file": STRING,
                "artifact_id": STRING,
            },
            ("task_name", "task_id", "run_id", "summary_file", "artifact_id"),
        ),
        lambda a: [
            "task", "submit", a["task_id"], "--task-name", a["task_name"],
            "--run-id", a["run_id"], "--summary-file", a["summary_file"],
            "--artifact", a["artifact_id"],
        ],
    ),
    tool(
        "statemng_task_accept",
        "Accept a submitted run and unlock newly satisfied dependents. Main Agent only.",
        object_schema(
            {"task_name": STRING, "task_id": STRING, "run_id": STRING},
            ("task_name", "task_id", "run_id"),
        ),
        lambda a: [
            "task", "accept", a["task_id"], "--task-name", a["task_name"],
            "--run-id", a["run_id"],
        ],
    ),
    tool(
        "statemng_task_block",
        "Record a non-retryable blocked run and its reason.",
        object_schema(
            {"task_name": STRING, "task_id": STRING, "run_id": STRING, "reason_file": STRING},
            ("task_name", "task_id", "run_id", "reason_file"),
        ),
        lambda a: [
            "task", "block", a["task_id"], "--task-name", a["task_name"],
            "--run-id", a["run_id"], "--reason-file", a["reason_file"],
        ],
    ),
    tool(
        "statemng_task_unblock",
        "Return a blocked subtask to ready while preserving its blocked run. Main Agent only.",
        object_schema(
            {"task_name": STRING, "task_id": STRING, "run_id": STRING},
            ("task_name", "task_id", "run_id"),
        ),
        lambda a: [
            "task", "unblock", a["task_id"], "--task-name", a["task_name"],
            "--run-id", a["run_id"],
        ],
    ),
    tool(
        "statemng_task_fail",
        "Record a failed run and choose whether its subtask becomes ready again.",
        object_schema(
            {
                "task_name": STRING,
                "task_id": STRING,
                "run_id": STRING,
                "reason_file": STRING,
                "retryable": BOOLEAN,
            },
            ("task_name", "task_id", "run_id", "reason_file", "retryable"),
        ),
        lambda a: [
            "task", "fail", a["task_id"], "--task-name", a["task_name"],
            "--run-id", a["run_id"], "--reason-file", a["reason_file"],
            "--retryable", "true" if a["retryable"] else "false",
        ],
    ),
    tool(
        "statemng_task_status",
        "Read one subtask's compact current state.",
        object_schema({"task_name": STRING, "task_id": STRING}, ("task_name", "task_id")),
        lambda a: ["task", "status", a["task_id"], "--task-name", a["task_name"]],
        read_only=True,
    ),
    tool(
        "statemng_task_list",
        "List subtasks, optionally filtered by comma-separated states.",
        object_schema({"task_name": STRING, "status": STRING}, ("task_name",)),
        lambda a: _task_list_command(a),
        read_only=True,
    ),
    tool(
        "statemng_artifact_show",
        "Read and verify one artifact record.",
        object_schema(
            {"task_name": STRING, "artifact_id": STRING}, ("task_name", "artifact_id")
        ),
        lambda a: [
            "artifact", "show", a["artifact_id"], "--task-name", a["task_name"]
        ],
        read_only=True,
    ),
    tool(
        "statemng_artifact_list",
        "List artifact records, optionally for one subtask.",
        object_schema({"task_name": STRING, "task_id": STRING}, ("task_name",)),
        lambda a: _artifact_list_command(a),
        read_only=True,
    ),
    tool(
        "statemng_doctor",
        "Check state, dependency, run, and artifact consistency.",
        object_schema({"task_name": STRING}, ("task_name",)),
        lambda a: ["doctor", "--task-name", a["task_name"]],
        read_only=True,
    ),
]

TOOL_BY_NAME = {item["name"]: item for item in TOOLS}


def _task_create_command(arguments: dict[str, Any]) -> list[str]:
    command = [
        "task", "create", "--task-name", arguments["task_name"],
        "--title", arguments["title"], "--description-file", arguments["description_file"],
    ]
    option(command, "--depends-on", arguments.get("depends_on"))
    return command


def _task_list_command(arguments: dict[str, Any]) -> list[str]:
    command = ["task", "list", "--task-name", arguments["task_name"]]
    option(command, "--status", arguments.get("status"))
    return command


def _artifact_list_command(arguments: dict[str, Any]) -> list[str]:
    command = ["artifact", "list", "--task-name", arguments["task_name"]]
    option(command, "--task", arguments.get("task_id"))
    return command


def public_tool(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def validate_arguments(item: dict[str, Any], arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    schema = item["inputSchema"]
    properties = schema["properties"]
    unexpected = sorted(set(arguments) - set(properties))
    if unexpected:
        raise ValueError(f"unexpected argument(s): {', '.join(unexpected)}")
    missing = [name for name in schema.get("required", []) if name not in arguments]
    if missing:
        raise ValueError(f"missing argument(s): {', '.join(missing)}")
    for name, value in arguments.items():
        expected = properties[name]["type"]
        if expected == "string" and (not isinstance(value, str) or not value):
            raise ValueError(f"{name} must be a non-empty string")
        if expected == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
    return arguments


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    item = TOOL_BY_NAME.get(name)
    if item is None:
        raise ValueError(f"unknown tool: {name}")
    checked = validate_arguments(item, arguments)
    try:
        completed = subprocess.run(
            [sys.executable, str(CLI), *item["_command"](checked)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload = {"ok": False, "error": f"cannot execute statemng: {exc}"}
        return tool_result(payload, is_error=True)
    try:
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("CLI returned a non-object JSON value")
    except (json.JSONDecodeError, ValueError) as exc:
        detail = completed.stderr.strip() or completed.stdout.strip() or str(exc)
        payload = {"ok": False, "error": f"statemng returned invalid output: {detail}"}
    return tool_result(
        payload, is_error=completed.returncode != 0 or payload.get("ok") is not True
    )


def tool_result(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": is_error,
    }


def success(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    if method == "initialize":
        params = message.get("params")
        requested_version = (
            params.get("protocolVersion") if isinstance(params, dict) else None
        )
        protocol_version = (
            requested_version
            if requested_version in SUPPORTED_PROTOCOL_VERSIONS
            else PROTOCOL_VERSION
        )
        return success(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "statemng", "version": "1.0.0"},
                "instructions": "Use task_name on every task-scoped call. accept and unblock are main-Agent decisions.",
            },
        )
    if method == "ping":
        return success(request_id, {})
    if method == "tools/list":
        return success(request_id, {"tools": [public_tool(item) for item in TOOLS]})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return error(request_id, -32602, "Invalid tools/call parameters")
        try:
            return success(request_id, call_tool(params["name"], params.get("arguments", {})))
        except ValueError as exc:
            return error(request_id, -32602, str(exc))
    return error(request_id, -32601, "Method not found")


def main() -> int:
    for raw in sys.stdin:
        try:
            message = json.loads(raw)
            response = handle(message)
        except json.JSONDecodeError:
            response = error(None, -32700, "Parse error")
        except Exception as exc:  # keep protocol output valid on unexpected failures
            print(f"statemng MCP internal error: {exc}", file=sys.stderr, flush=True)
            response = error(None, -32603, "Internal error")
        if response is not None:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
