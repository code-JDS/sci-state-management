#!/usr/bin/env python3
"""Install or update the project-local statemng Codex integration."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


STATE_MANAGEMENT_ROOT = Path(__file__).resolve().parent
HOST_PROJECT_ROOT = STATE_MANAGEMENT_ROOT.parent
PROJECT_NAME = HOST_PROJECT_ROOT.name
CODEX_ADAPTER_ROOT = STATE_MANAGEMENT_ROOT / "adapters" / "codex"
TEMPLATE_ROOT = CODEX_ADAPTER_ROOT / "templates"
LAUNCHER_INSTALLER = CODEX_ADAPTER_ROOT / "install_launcher.py"
LAUNCHER_PATH = Path.home() / ".codex" / "statemng" / PROJECT_NAME / "mcp"

AGENTS_BEGIN = "<!-- statemng:begin -->"
AGENTS_END = "<!-- statemng:end -->"
CONFIG_BEGIN = "# statemng:begin"
CONFIG_END = "# statemng:end"
STATE_IGNORE = ".state-management/"


def atomic_write_text(path: Path, content: str) -> bool:
    data = content.encode("utf-8")
    if path.is_file() and path.read_bytes() == data:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), existing_mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def append_block(content: str, block: str) -> str:
    if not content:
        return block + "\n"
    separator = "\n\n" if content.endswith("\n") else "\n\n"
    return content.rstrip("\n") + separator + block + "\n"


def replace_marked_block(
    content: str, begin: str, end: str, replacement: str
) -> str | None:
    begin_count = content.count(begin)
    end_count = content.count(end)
    if begin_count == 0 and end_count == 0:
        return None
    if begin_count != 1 or end_count != 1:
        raise RuntimeError(f"invalid managed block markers: {begin} / {end}")
    start = content.index(begin)
    finish = content.index(end, start) + len(end)
    return content[:start] + replacement + content[finish:]


def replace_legacy_mcp_table(content: str, replacement: str) -> str | None:
    lines = content.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if line.strip() == "[mcp_servers.statemng]"
    ]
    if not starts:
        return None
    if len(starts) != 1:
        raise RuntimeError("multiple [mcp_servers.statemng] tables are present")

    start = starts[0]
    finish = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            finish = index
            break
    prefix = "".join(lines[:start])
    suffix = "".join(lines[finish:])
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + replacement + "\n" + suffix


def render_mcp_block() -> str:
    return (
        f"{CONFIG_BEGIN}\n"
        "[mcp_servers.statemng]\n"
        "enabled = true\n"
        'command = "/bin/sh"\n'
        f'args = ["-c", "exec \\"$HOME/.codex/statemng/{PROJECT_NAME}/mcp\\""]\n'
        "required = false\n"
        'default_tools_approval_mode = "writes"\n'
        f"{CONFIG_END}"
    )


def update_mcp_config(path: Path) -> bool:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    block = render_mcp_block()
    updated = replace_marked_block(content, CONFIG_BEGIN, CONFIG_END, block)
    if updated is None:
        updated = replace_legacy_mcp_table(content, block)
    if updated is None:
        updated = append_block(content, block)
    tomllib.loads(updated)
    return atomic_write_text(path, updated)


def update_agents(path: Path) -> bool:
    block = (TEMPLATE_ROOT / "AGENTS.block.md").read_text(encoding="utf-8").strip("\n")
    if not block.startswith(AGENTS_BEGIN) or not block.endswith(AGENTS_END):
        raise RuntimeError("AGENTS.block.md does not contain the required markers")
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = replace_marked_block(content, AGENTS_BEGIN, AGENTS_END, block)
    if updated is None:
        updated = append_block(content, block)
    return atomic_write_text(path, updated)


def update_gitignore(path: Path) -> bool:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if STATE_IGNORE in {line.strip() for line in content.splitlines()}:
        return False
    updated = content.rstrip("\n")
    if updated:
        updated += "\n"
    updated += STATE_IGNORE + "\n"
    return atomic_write_text(path, updated)


def install_skill(path: Path) -> bool:
    source = TEMPLATE_ROOT / "persistent-scientific-research" / "SKILL.md"
    return atomic_write_text(path, source.read_text(encoding="utf-8"))


def install_worker(path: Path) -> bool:
    source = TEMPLATE_ROOT / "persistent-research-worker.toml"
    template = source.read_text(encoding="utf-8")
    if template.count("__PROJECT_NAME__") != 1:
        raise RuntimeError("worker template must contain one __PROJECT_NAME__ placeholder")
    return atomic_write_text(path, template.replace("__PROJECT_NAME__", PROJECT_NAME))


def validate_environment() -> None:
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required")
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise RuntimeError("only macOS and Linux are supported")
    if STATE_MANAGEMENT_ROOT.name != "state-management":
        raise RuntimeError(
            "the repository must be placed at [HOST_PROJECT]/state-management"
        )
    required = (
        STATE_MANAGEMENT_ROOT / "mcp_server.py",
        STATE_MANAGEMENT_ROOT / "statemng",
        LAUNCHER_INSTALLER,
        TEMPLATE_ROOT / "AGENTS.block.md",
        TEMPLATE_ROOT / "persistent-research-worker.toml",
        TEMPLATE_ROOT / "persistent-scientific-research" / "SKILL.md",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError("required files are missing: " + ", ".join(missing))


def install_launcher() -> Path:
    completed = subprocess.run(
        [sys.executable, str(LAUNCHER_INSTALLER)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"launcher installation failed: {detail}")
    payload = json.loads(completed.stdout)
    if not payload.get("ok"):
        raise RuntimeError(f"launcher installation failed: {payload}")
    launcher = Path(payload["launcher"])
    if launcher != LAUNCHER_PATH:
        raise RuntimeError(f"launcher path mismatch: {launcher} != {LAUNCHER_PATH}")
    return launcher


def check_mcp(launcher: Path) -> None:
    requests = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize",'
        '"params":{"protocolVersion":"2025-11-25","capabilities":{},'
        '"clientInfo":{"name":"statemng-installer","version":"1"}}}\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    completed = subprocess.run(
        [str(launcher)],
        cwd="/",
        input=requests,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"MCP self-check failed: {completed.stderr.strip()}")
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    if len(responses) != 2:
        raise RuntimeError(f"MCP self-check returned unexpected responses: {responses}")
    initialized = responses[0].get("result", {})
    if initialized.get("serverInfo", {}).get("name") != "statemng":
        raise RuntimeError(f"MCP initialize failed: {responses[0]}")
    tools = responses[1].get("result", {}).get("tools", [])
    if "statemng_project_list" not in {item.get("name") for item in tools}:
        raise RuntimeError("MCP tools/list did not expose statemng_project_list")


def install() -> dict[str, object]:
    validate_environment()
    launcher = install_launcher()
    changed: list[str] = []

    destinations = (
        (
            HOST_PROJECT_ROOT
            / ".agents"
            / "skills"
            / "persistent-scientific-research"
            / "SKILL.md",
            install_skill,
        ),
        (
            HOST_PROJECT_ROOT
            / ".codex"
            / "agents"
            / "persistent-research-worker.toml",
            install_worker,
        ),
        (HOST_PROJECT_ROOT / "AGENTS.md", update_agents),
        (HOST_PROJECT_ROOT / ".codex" / "config.toml", update_mcp_config),
        (HOST_PROJECT_ROOT / ".gitignore", update_gitignore),
    )
    for destination, operation in destinations:
        if operation(destination):
            changed.append(str(destination.relative_to(HOST_PROJECT_ROOT)))

    check_mcp(launcher)
    return {
        "ok": True,
        "project_root": str(HOST_PROJECT_ROOT),
        "launcher": str(launcher),
        "changed": changed,
        "restart_required": True,
        "message": "installation complete; start a new Codex Local session",
    }


def update() -> None:
    validate_environment()
    commands = (
        ["git", "-C", str(STATE_MANAGEMENT_ROOT), "fetch", "origin", "main"],
        [
            "git",
            "-C",
            str(STATE_MANAGEMENT_ROOT),
            "checkout",
            "--detach",
            "FETCH_HEAD",
        ],
    )
    for command in commands:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(detail)
    os.execv(sys.executable, [sys.executable, str(STATE_MANAGEMENT_ROOT / "manage.py"), "install"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "update"))
    arguments = parser.parse_args()
    try:
        if arguments.command == "install":
            print(json.dumps(install(), ensure_ascii=False))
        else:
            update()
        return 0
    except (OSError, RuntimeError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
