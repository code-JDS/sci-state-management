#!/usr/bin/env python3
"""Install the stable machine-local launcher used by Codex Desktop."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path


STATE_MANAGEMENT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = STATE_MANAGEMENT_ROOT.parent
PROJECT_NAME = PROJECT_ROOT.name
SERVER_PATH = STATE_MANAGEMENT_ROOT / "mcp_server.py"
LAUNCHER_RELATIVE_PATH = Path(".codex") / "statemng" / PROJECT_NAME / "mcp"


def launcher_path() -> Path:
    return Path.home() / LAUNCHER_RELATIVE_PATH


def launcher_content(
    python_executable: Path | None = None, server_path: Path = SERVER_PATH
) -> str:
    executable = (python_executable or Path(sys.executable)).absolute()
    server = server_path.resolve()
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"exec {shlex.quote(str(executable))} {shlex.quote(str(server))}\n"
    )


def validate_sources(
    python_executable: Path | None = None, server_path: Path = SERVER_PATH
) -> None:
    executable = (python_executable or Path(sys.executable)).absolute()
    server = server_path.resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"Python executable is unavailable: {executable}")
    if not server.is_file():
        raise RuntimeError(f"statemng MCP server is unavailable: {server}")


def check_installed(path: Path | None = None) -> tuple[bool, str]:
    destination = path or launcher_path()
    if not destination.is_file():
        return False, f"launcher is missing: {destination}"
    if destination.read_text(encoding="utf-8") != launcher_content():
        return False, f"launcher does not target this checkout: {destination}"
    mode = destination.stat().st_mode
    if not mode & stat.S_IXUSR:
        return False, f"launcher is not executable: {destination}"
    try:
        validate_sources()
    except RuntimeError as exc:
        return False, str(exc)
    return True, "launcher is ready"


def install(path: Path | None = None) -> Path:
    validate_sources()
    destination = path or launcher_path()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = launcher_content().encode("utf-8")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".mcp-", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o700)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="validate without changing the launcher"
    )
    arguments = parser.parse_args()
    try:
        if arguments.check:
            ok, message = check_installed()
            payload = {"ok": ok, "launcher": str(launcher_path()), "message": message}
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if ok else 1
        destination = install()
        print(
            json.dumps(
                {"ok": True, "launcher": str(destination), "message": "launcher installed"},
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, RuntimeError) as exc:
        print(
            json.dumps(
                {"ok": False, "launcher": str(launcher_path()), "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
