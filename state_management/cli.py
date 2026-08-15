"""File-backed state-management CLI described by the project design."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator

from .state_ui import render_project_state


SCHEMA_VERSION = 1
STATES = {
    "pending",
    "ready",
    "running",
    "submitted",
    "completed",
    "blocked",
    "failed",
}
TASK_ID_RE = re.compile(r"T-(\d+)$")
RUN_ID_RE = re.compile(r"R-(\d+)$")
ARTIFACT_ID_RE = re.compile(r"A-(\d+)$")


class StateError(Exception):
    """A command error that must be returned as JSON without a traceback."""

    def __init__(self, error: str, task_name: str | None = None):
        super().__init__(error)
        self.error = error
        self.task_name = task_name


class JsonArgumentParser(argparse.ArgumentParser):
    """Make parser errors follow the CLI's JSON-only interface."""

    def error(self, message: str) -> None:
        raise StateError(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes) -> None:
    """Write, fsync, and atomically replace a file in its own directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".tmp-", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write(path, json_bytes(value))


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"expected a JSON object in {path}")
    return value


def read_utf8_file(path: Path, purpose: str, *, nonempty: bool = True) -> str:
    try:
        if not path.is_file():
            raise StateError(f"{purpose} file does not exist: {path}")
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise StateError(f"{purpose} file must use UTF-8: {path}") from exc
    except OSError as exc:
        raise StateError(f"cannot read {purpose} file {path}: {exc}") from exc
    if nonempty and not text:
        raise StateError(f"{purpose} file must not be empty: {path}")
    return text


def validate_task_name(task_name: str) -> None:
    if (
        not task_name
        or task_name in {".", ".."}
        or "/" in task_name
        or "\\" in task_name
        or "\x00" in task_name
    ):
        raise StateError("invalid task name")


def state_root_from(start: Path, *, required: bool) -> Path | None:
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / ".state-management"
        if candidate.is_symlink():
            raise StateError(".state-management must not be a symbolic link")
        if candidate.exists():
            if not candidate.is_dir():
                raise StateError(".state-management is not a directory")
            return candidate
    if required:
        raise StateError(".state-management was not found in this project")
    return None


def resolve_task_root(task_name: str | None) -> tuple[str, Path, Path]:
    if task_name is None:
        raise StateError("TASK_NAME_REQUIRED")
    validate_task_name(task_name)
    state_root = state_root_from(Path.cwd(), required=True)
    assert state_root is not None
    task_root = state_root / task_name
    if task_root.is_symlink() or not task_root.is_dir():
        raise StateError("TASK_NOT_FOUND")
    if task_root.resolve().parent != state_root.resolve():
        raise StateError("TASK_NOT_FOUND")
    for name in ("meta.json", "tasks", "runs", "artifacts", ".locks", "views"):
        child = task_root / name
        if child.is_symlink():
            raise StateError("overall task state must not contain symbolic links")
        if name == "meta.json" and not child.is_file():
            raise StateError("overall task metadata is missing")
        if name != "meta.json" and not child.is_dir():
            raise StateError(f"overall task directory is missing: {name}")
    return task_name, task_root, state_root


def path_inside_task(raw: str, task_root: Path, purpose: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = task_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise StateError(f"{purpose} file does not exist: {raw}") from exc
    try:
        resolved.relative_to(task_root.resolve())
    except ValueError as exc:
        raise StateError(f"{purpose} path escapes the overall task directory") from exc
    if not resolved.is_file():
        raise StateError(f"{purpose} path is not a file: {raw}")
    return resolved


@contextmanager
def file_lock(task_root: Path, name: str) -> Iterator[None]:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise StateError("invalid lock name")
    lock_path = task_root / ".locks" / name
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o644)
    except OSError as exc:
        raise StateError(f"cannot open task lock: {name}") from exc
    with os.fdopen(descriptor, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def task_lock(task_root: Path, task_id: str) -> Iterator[None]:
    task_path(task_root, task_id)
    with file_lock(task_root, f"{task_id}.lock"):
        yield


def task_path(task_root: Path, task_id: str) -> Path:
    if not TASK_ID_RE.fullmatch(task_id):
        raise StateError(f"invalid task ID: {task_id}")
    return task_root / "tasks" / f"{task_id}.json"


def run_path(task_root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise StateError(f"invalid run ID: {run_id}")
    directory = task_root / "runs" / run_id
    if directory.is_symlink():
        raise StateError(f"invalid run directory: {run_id}")
    return directory / "run.json"


def get_task(task_root: Path, task_id: str) -> dict[str, Any]:
    path = task_path(task_root, task_id)
    if path.is_symlink() or not path.is_file():
        raise StateError(f"task not found: {task_id}")
    return load_json(path)


def get_run(task_root: Path, run_id: str) -> dict[str, Any]:
    path = run_path(task_root, run_id)
    if path.is_symlink() or not path.is_file():
        raise StateError(f"run not found: {run_id}")
    return load_json(path)


def all_task_paths(task_root: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in (task_root / "tasks").glob("T-*.json")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: int(TASK_ID_RE.fullmatch(path.stem).group(1))
        if TASK_ID_RE.fullmatch(path.stem)
        else sys.maxsize,
    )


def all_tasks(task_root: Path) -> list[dict[str, Any]]:
    return [load_json(path) for path in all_task_paths(task_root)]


def validate_required_skills(value: Any, project_root: Path) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise StateError("required_skills must be an array of strings")
    if len(set(value)) != len(value):
        raise StateError("required_skills contains duplicate paths")

    resolved_project_root = project_root.resolve()
    resolved_skills: set[Path] = set()
    for item in value:
        path = Path(item)
        if not item or path.is_absolute() or PureWindowsPath(item).is_absolute():
            raise StateError("required_skills paths must be repository-relative")
        if ".." in path.parts:
            raise StateError("required_skills paths must not contain ..")
        if path.name != "SKILL.md":
            raise StateError("required_skills paths must name SKILL.md")
        try:
            resolved = (resolved_project_root / path).resolve(strict=True)
            resolved.relative_to(resolved_project_root)
        except (OSError, ValueError) as exc:
            raise StateError(
                f"required skill does not exist inside the repository: {item}"
            ) from exc
        if not resolved.is_file():
            raise StateError(f"required skill is not a file: {item}")
        if resolved in resolved_skills:
            raise StateError("required_skills contains duplicate paths")
        resolved_skills.add(resolved)
    return value


def validate_description(value: Any, project_root: Path) -> dict[str, Any]:
    required_fields = {"objective", "background", "acceptance_criteria"}
    allowed_fields = required_fields | {"required_skills"}
    if (
        not isinstance(value, dict)
        or not required_fields.issubset(value)
        or not set(value).issubset(allowed_fields)
    ):
        raise StateError(
            "description must contain objective, background, acceptance_criteria, "
            "and optional required_skills only"
        )
    objective = value["objective"]
    background = value["background"]
    criteria = value["acceptance_criteria"]
    if not isinstance(objective, str) or not objective.strip():
        raise StateError("objective must be a non-empty string")
    if not isinstance(background, str):
        raise StateError("background must be a string")
    if (
        not isinstance(criteria, list)
        or not criteria
        or any(not isinstance(item, str) or not item.strip() for item in criteria)
    ):
        raise StateError("acceptance_criteria must contain non-empty strings")
    if "required_skills" in value:
        validate_required_skills(value["required_skills"], project_root)
    return value


def validate_task_description_fields(task: dict[str, Any], task_root: Path) -> None:
    description = {
        "objective": task.get("objective"),
        "background": task.get("background"),
        "acceptance_criteria": task.get("acceptance_criteria"),
    }
    if "required_skills" in task:
        description["required_skills"] = task["required_skills"]
    validate_description(description, task_root.parent.parent)


def parse_dependencies(raw: str | None) -> list[str]:
    if raw is None or raw == "":
        return []
    dependencies = [item.strip() for item in raw.split(",")]
    if any(not TASK_ID_RE.fullmatch(item) for item in dependencies):
        raise StateError("depends-on must be a comma-separated list of task IDs")
    if len(set(dependencies)) != len(dependencies):
        raise StateError("depends-on contains duplicate task IDs")
    return dependencies


def next_number(paths: Iterator[Path], pattern: re.Pattern[str]) -> int:
    numbers: list[int] = []
    for path in paths:
        match = pattern.fullmatch(path.stem)
        if match:
            numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def new_run_id(task_root: Path) -> tuple[str, Path]:
    runs_root = task_root / "runs"
    while True:
        number = next_number(iter(runs_root.iterdir()), RUN_ID_RE)
        run_id = f"R-{number:03d}"
        directory = runs_root / run_id
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        (directory / "workspace").mkdir()
        fsync_directory(runs_root)
        return run_id, directory


def artifact_records(task_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    runs_root = task_root / "runs"
    for directory in sorted(runs_root.glob("R-*")):
        if not directory.is_dir() or directory.is_symlink():
            continue
        path = directory / "run.json"
        if path.is_symlink() or not path.is_file():
            continue
        try:
            run = load_json(path)
        except StateError:
            continue
        artifacts = run.get("artifacts", [])
        if isinstance(artifacts, list):
            records.extend(item for item in artifacts if isinstance(item, dict))
    return records


def find_artifact(task_root: Path, artifact_id: str) -> dict[str, Any]:
    if not ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise StateError(f"invalid artifact ID: {artifact_id}")
    matches = [
        record
        for record in artifact_records(task_root)
        if record.get("artifact_id") == artifact_id
    ]
    if len(matches) != 1:
        raise StateError(f"artifact not found: {artifact_id}")
    return matches[0]


def stored_artifact_path(task_root: Path, record: dict[str, Any]) -> Path:
    raw = record.get("stored_path")
    if not isinstance(raw, str):
        raise StateError("artifact stored_path is invalid")
    state_relative = Path(raw)
    state_root = task_root.parent
    path = state_root.parent / state_relative
    resolved = path.resolve()
    try:
        resolved.relative_to(task_root.resolve())
    except ValueError as exc:
        raise StateError("artifact path escapes the overall task directory") from exc
    return resolved


def verify_artifact(task_root: Path, record: dict[str, Any]) -> None:
    path = stored_artifact_path(task_root, record)
    if not path.is_file():
        raise StateError(f"artifact file is missing: {record.get('artifact_id')}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != record.get("sha256"):
        raise StateError(f"artifact hash mismatch: {record.get('artifact_id')}")


def markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def rebuild_views(task_root: Path) -> None:
    with file_lock(task_root, "PROJECT_STATE.lock"):
        meta = load_json(task_root / "meta.json")
        tasks = all_tasks(task_root)
        lines = [
            f"# Project State: {meta['task_name']}",
            "",
            "## Goal",
            "",
            str(meta["goal"]),
            "",
            "## Tasks",
            "",
            "| Task | Title | Status | Dependencies | Required Skills |",
            "| --- | --- | --- | --- | --- |",
        ]
        for task in tasks:
            dependencies = ", ".join(task.get("dependencies", [])) or "—"
            required_skills = ", ".join(task.get("required_skills", [])) or "—"
            lines.append(
                "| {task_id} | {title} | {status} | {dependencies} | {required_skills} |".format(
                    task_id=markdown_cell(task.get("task_id", "")),
                    title=markdown_cell(task.get("title", "")),
                    status=markdown_cell(task.get("status", "")),
                    dependencies=markdown_cell(dependencies),
                    required_skills=markdown_cell(required_skills),
                )
            )
        submitted = [task for task in tasks if isinstance(task.get("summary"), str)]
        if submitted:
            lines.extend(["", "## Submitted Results", ""])
            for task in submitted:
                lines.extend(
                    [
                        f"### {task['task_id']}: {task['title']}",
                        "",
                        task["summary"],
                        "",
                        f"Artifact: `{task.get('artifact_id', '')}`",
                        "",
                    ]
                )
        markdown = "\n".join(lines).encode("utf-8")
        html = render_project_state(meta, tasks).encode("utf-8")
        atomic_write(task_root / "views" / "PROJECT_STATE.md", markdown)
        atomic_write(task_root / "views" / "ui" / "PROJECT_STATE.html", html)


def compact_task(task: dict[str, Any]) -> dict[str, Any]:
    result = {
        "task_id": task.get("task_id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "dependencies": task.get("dependencies"),
    }
    for field in (
        "required_skills",
        "run_id",
        "summary",
        "artifact_id",
        "accepted_run_id",
        "reason",
    ):
        if field in task:
            result[field] = task[field]
    return result


def incomplete_runs(task_root: Path, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    task_by_id = {
        task["task_id"]: task
        for task in tasks
        if isinstance(task.get("task_id"), str)
    }
    result: list[dict[str, Any]] = []
    for run_directory in sorted((task_root / "runs").glob("R-*")):
        if not run_directory.is_dir() or run_directory.is_symlink():
            continue
        run_json = run_directory / "run.json"
        if not run_json.is_file() or run_json.is_symlink():
            result.append(
                {
                    "run_id": run_directory.name,
                    "task_id": None,
                    "status": None,
                    "run_directory": str(run_directory.relative_to(task_root)),
                    "workspace": str((run_directory / "workspace").relative_to(task_root)),
                }
            )
            continue
        run = load_json(run_json)
        run_task_id = run.get("task_id")
        task = task_by_id.get(run_task_id) if isinstance(run_task_id, str) else None
        accepted = (
            task is not None
            and task.get("status") == "completed"
            and task.get("accepted_run_id") == run.get("run_id")
        )
        if not accepted and run.get("status") not in {"blocked", "failed"}:
            result.append(
                {
                    "run_id": run.get("run_id"),
                    "task_id": run.get("task_id"),
                    "status": run.get("status"),
                    "run_directory": str(run_json.parent.relative_to(task_root)),
                    "workspace": str((run_json.parent / "workspace").relative_to(task_root)),
                }
            )
    return result


def recompute_ready(task_root: Path) -> list[str]:
    tasks = all_tasks(task_root)
    completed = {
        task.get("task_id") for task in tasks if task.get("status") == "completed"
    }
    changed: list[str] = []
    for task in tasks:
        if task.get("status") != "pending":
            continue
        dependencies = task.get("dependencies")
        if isinstance(dependencies, list) and all(item in completed for item in dependencies):
            task["status"] = "ready"
            atomic_write_json(task_path(task_root, task["task_id"]), task)
            changed.append(task["task_id"])
    return changed


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    validate_task_name(args.task_name)
    goal_path = Path(args.goal_file)
    if not goal_path.is_absolute():
        goal_path = Path.cwd() / goal_path
    goal = read_utf8_file(goal_path, "goal")

    project_root = Path.cwd().resolve()
    state_root = project_root / ".state-management"
    if state_root.is_symlink():
        raise StateError(".state-management must not be a symbolic link")
    if state_root.exists() and not state_root.is_dir():
        raise StateError(".state-management is not a directory")
    task_root = state_root / args.task_name
    if task_root.exists() or task_root.is_symlink():
        raise StateError("overall task already exists")

    state_root.mkdir(exist_ok=True)
    task_root.mkdir()
    for directory in ("tasks", "runs", "artifacts", ".locks", "views"):
        (task_root / directory).mkdir()
    atomic_write_json(
        task_root / "meta.json",
        {
            "schema_version": SCHEMA_VERSION,
            "task_name": args.task_name,
            "goal": goal,
        },
    )
    rebuild_views(task_root)
    return {
        "ok": True,
        "task_name": args.task_name,
        "task_root": str(task_root),
        "schema_version": SCHEMA_VERSION,
    }


def command_project_list(_args: argparse.Namespace) -> dict[str, Any]:
    state_root = state_root_from(Path.cwd(), required=False)
    if state_root is None:
        return {"ok": True, "task_names": []}
    task_names = sorted(
        path.name
        for path in state_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    return {"ok": True, "task_names": task_names}


def command_project_resume(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    meta = load_json(task_root / "meta.json")
    if meta.get("schema_version") != SCHEMA_VERSION:
        raise StateError("incompatible schema version", task_name)
    recompute_ready(task_root)
    rebuild_views(task_root)
    tasks = all_tasks(task_root)
    return {
        "ok": True,
        "task_name": task_name,
        "goal": meta.get("goal"),
        "tasks": [compact_task(task) for task in tasks],
        "incomplete_runs": incomplete_runs(task_root, tasks),
    }


def command_task_create(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    if not isinstance(args.title, str) or not args.title.strip():
        raise StateError("title must be a non-empty string", task_name)
    description_path = Path(args.description_file)
    if not description_path.is_absolute():
        description_path = Path.cwd() / description_path
    try:
        description_value = json.loads(read_utf8_file(description_path, "description"))
    except json.JSONDecodeError as exc:
        raise StateError(f"description file is invalid JSON: {exc}", task_name) from exc
    description = validate_description(description_value, task_root.parent.parent)
    dependencies = parse_dependencies(args.depends_on)
    for dependency in dependencies:
        get_task(task_root, dependency)

    number = next_number(iter((task_root / "tasks").glob("T-*.json")), TASK_ID_RE)
    while True:
        task_id = f"T-{number:03d}"
        with task_lock(task_root, task_id):
            path = task_path(task_root, task_id)
            if path.exists():
                number += 1
                continue
            completed = {
                task.get("task_id")
                for task in all_tasks(task_root)
                if task.get("status") == "completed"
            }
            status = "ready" if all(item in completed for item in dependencies) else "pending"
            task = {
                "task_id": task_id,
                "title": args.title,
                "objective": description["objective"],
                "background": description["background"],
                "acceptance_criteria": description["acceptance_criteria"],
                "dependencies": dependencies,
                "status": status,
            }
            if "required_skills" in description:
                task["required_skills"] = description["required_skills"]
            atomic_write_json(path, task)
            break
    rebuild_views(task_root)
    result = {
        "ok": True,
        "task_name": task_name,
        "task_id": task_id,
        "status": status,
        "dependencies": dependencies,
    }
    if "required_skills" in description:
        result["required_skills"] = description["required_skills"]
    return result


def command_task_ready(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    tasks = [
        {"task_id": task["task_id"], "title": task["title"]}
        for task in all_tasks(task_root)
        if task.get("status") == "ready"
    ]
    return {"ok": True, "task_name": task_name, "tasks": tasks}


def command_task_claim(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    with task_lock(task_root, args.task_id):
        task = get_task(task_root, args.task_id)
        if task.get("status") != "ready":
            error = (
                "TASK_ALREADY_CLAIMED"
                if task.get("status") in {"running", "submitted"}
                else "task is not ready"
            )
            raise StateError(error, task_name)
        run_id, directory = new_run_id(task_root)
        claimed_at = now()
        run = {
            "run_id": run_id,
            "task_id": args.task_id,
            "status": "running",
            "claimed_at": claimed_at,
            "artifacts": [],
        }
        atomic_write_json(directory / "run.json", run)
        for field in ("summary", "artifact_id", "accepted_run_id", "completed_at", "reason"):
            task.pop(field, None)
        task["status"] = "running"
        task["run_id"] = run_id
        atomic_write_json(task_path(task_root, args.task_id), task)
    rebuild_views(task_root)
    result = {
        "ok": True,
        "task_name": task_name,
        "task_id": args.task_id,
        "run_id": run_id,
        "status": "running",
    }
    return result


def require_current_run(
    task_root: Path,
    task_id: str,
    run_id: str,
    allowed_states: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    task = get_task(task_root, task_id)
    if task.get("run_id") != run_id:
        raise StateError("run_id does not match the task's current run")
    if task.get("status") not in allowed_states:
        raise StateError(f"task status must be one of: {', '.join(sorted(allowed_states))}")
    run = get_run(task_root, run_id)
    if run.get("task_id") != task_id or run.get("run_id") != run_id:
        raise StateError("task and run records are inconsistent")
    return task, run


def command_task_show(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    task, _ = require_current_run(
        task_root, args.task_id, args.run_id, {"running", "submitted", "completed"}
    )
    dependency_results: list[dict[str, Any]] = []
    for dependency_id in task.get("dependencies", []):
        dependency = get_task(task_root, dependency_id)
        result = {
            "task_id": dependency_id,
            "status": dependency.get("status"),
        }
        for field in ("summary", "artifact_id"):
            if field in dependency:
                result[field] = dependency[field]
        dependency_results.append(result)
    result = {
        "ok": True,
        "task_name": task_name,
        "task_id": args.task_id,
        "objective": task.get("objective"),
        "background": task.get("background"),
        "acceptance_criteria": task.get("acceptance_criteria"),
        "dependencies": dependency_results,
        "workspace": str((task_root / "runs" / args.run_id / "workspace").resolve()),
    }
    if "required_skills" in task:
        result["required_skills"] = task["required_skills"]
    return result


def command_artifact_add(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    source = path_inside_task(args.path, task_root, "artifact")
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    with task_lock(task_root, args.task_id):
        task, run = require_current_run(
            task_root, args.task_id, args.run_id, {"running", "submitted"}
        )
        for record in run.get("artifacts", []):
            if record.get("sha256") == digest:
                verify_artifact(task_root, record)
                return {
                    "ok": True,
                    "task_name": task_name,
                    "artifact_id": record["artifact_id"],
                    "stored_path": record["stored_path"],
                }
        if task.get("status") != "running":
            raise StateError("new artifacts can only be registered while the task is running")

        with file_lock(task_root, "artifacts.lock"):
            used = [
                int(match.group(1))
                for path in (task_root / "artifacts").iterdir()
                if (match := re.match(r"A-(\d+)(?:\..*)?$", path.name))
            ]
            number = max(used, default=0) + 1
            artifact_id = f"A-{number:03d}"
            suffix = source.suffix
            destination = task_root / "artifacts" / f"{artifact_id}{suffix}"
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            fsync_directory(destination.parent)

        stored_path = str(destination.relative_to(task_root.parent.parent))
        record = {
            "artifact_id": artifact_id,
            "stored_path": stored_path,
            "sha256": digest,
            "task_id": args.task_id,
            "run_id": args.run_id,
        }
        artifacts = run.setdefault("artifacts", [])
        artifacts.append(record)
        atomic_write_json(run_path(task_root, args.run_id), run)
    return {
        "ok": True,
        "task_name": task_name,
        "artifact_id": artifact_id,
        "stored_path": stored_path,
    }


def command_task_submit(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    summary_path = path_inside_task(args.summary_file, task_root, "summary")
    summary = read_utf8_file(summary_path, "summary")
    with task_lock(task_root, args.task_id):
        task, run = require_current_run(
            task_root, args.task_id, args.run_id, {"running", "submitted"}
        )
        record = find_artifact(task_root, args.artifact_id)
        if record.get("task_id") != args.task_id or record.get("run_id") != args.run_id:
            raise StateError("artifact was not registered by this task and run", task_name)
        verify_artifact(task_root, record)
        if task.get("status") == "submitted":
            if task.get("summary") != summary or task.get("artifact_id") != args.artifact_id:
                raise StateError("task was already submitted with a different result", task_name)
            return {"ok": True, "task_name": task_name, "status": "submitted"}
        run["status"] = "submitted"
        run["summary"] = summary
        run["artifact_id"] = args.artifact_id
        atomic_write_json(run_path(task_root, args.run_id), run)
        task["status"] = "submitted"
        task["summary"] = summary
        task["artifact_id"] = args.artifact_id
        atomic_write_json(task_path(task_root, args.task_id), task)
    rebuild_views(task_root)
    return {"ok": True, "task_name": task_name, "status": "submitted"}


def command_task_accept(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    with task_lock(task_root, args.task_id):
        task = get_task(task_root, args.task_id)
        if task.get("status") == "completed" and task.get("accepted_run_id") == args.run_id:
            return {
                "ok": True,
                "task_name": task_name,
                "status": "completed",
                "unlocked_tasks": [],
            }
        task, run = require_current_run(
            task_root, args.task_id, args.run_id, {"submitted"}
        )
        validate_task_description_fields(task, task_root)
        summary = task.get("summary")
        artifact_id = task.get("artifact_id")
        if not isinstance(summary, str) or not summary:
            raise StateError("submitted summary is incomplete", task_name)
        if not isinstance(artifact_id, str):
            raise StateError("submitted artifact is incomplete", task_name)
        if run.get("summary") != summary or run.get("artifact_id") != artifact_id:
            raise StateError("task and run results are inconsistent", task_name)
        record = find_artifact(task_root, artifact_id)
        if record.get("task_id") != args.task_id or record.get("run_id") != args.run_id:
            raise StateError("submitted artifact has the wrong source", task_name)
        verify_artifact(task_root, record)

        completed_at = now()
        run["status"] = "completed"
        run["completed_at"] = completed_at
        atomic_write_json(run_path(task_root, args.run_id), run)
        task["status"] = "completed"
        task["accepted_run_id"] = args.run_id
        task["completed_at"] = completed_at
        atomic_write_json(task_path(task_root, args.task_id), task)

    unlocked = recompute_ready(task_root)
    rebuild_views(task_root)
    return {
        "ok": True,
        "task_name": task_name,
        "status": "completed",
        "unlocked_tasks": unlocked,
    }


def command_task_block(args: argparse.Namespace) -> dict[str, Any]:
    return finish_unsuccessful(args, "blocked", retryable=False)


def command_task_unblock(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    with task_lock(task_root, args.task_id):
        task, run = require_current_run(
            task_root, args.task_id, args.run_id, {"blocked"}
        )
        if run.get("status") != "blocked":
            raise StateError("task and run blocked states are inconsistent", task_name)
        task["status"] = "ready"
        for field in (
            "run_id",
            "reason",
            "summary",
            "artifact_id",
            "accepted_run_id",
            "completed_at",
        ):
            task.pop(field, None)
        atomic_write_json(task_path(task_root, args.task_id), task)
    rebuild_views(task_root)
    return {
        "ok": True,
        "task_name": task_name,
        "task_id": args.task_id,
        "run_id": args.run_id,
        "status": "ready",
    }


def command_task_fail(args: argparse.Namespace) -> dict[str, Any]:
    retryable = parse_boolean(args.retryable)
    return finish_unsuccessful(args, "failed", retryable=retryable)


def finish_unsuccessful(
    args: argparse.Namespace, run_status: str, *, retryable: bool
) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    reason_path = path_inside_task(args.reason_file, task_root, "reason")
    reason = read_utf8_file(reason_path, "reason")
    with task_lock(task_root, args.task_id):
        task, run = require_current_run(
            task_root, args.task_id, args.run_id, {"running", "submitted"}
        )
        run["status"] = run_status
        run["reason"] = reason
        atomic_write_json(run_path(task_root, args.run_id), run)
        task["status"] = "ready" if run_status == "failed" and retryable else run_status
        task["reason"] = reason
        if task["status"] == "ready":
            task.pop("summary", None)
            task.pop("artifact_id", None)
        atomic_write_json(task_path(task_root, args.task_id), task)
    rebuild_views(task_root)
    return {
        "ok": True,
        "task_name": task_name,
        "task_id": args.task_id,
        "run_id": args.run_id,
        "status": task["status"],
    }


def command_task_status(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    task = get_task(task_root, args.task_id)
    return {"ok": True, "task_name": task_name, **compact_task(task)}


def command_task_list(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    requested: set[str] | None = None
    if args.statuses:
        requested = {item.strip() for item in args.statuses.split(",")}
        if not requested or "" in requested or not requested.issubset(STATES):
            raise StateError("status contains an invalid task state", task_name)
    tasks = [
        compact_task(task)
        for task in all_tasks(task_root)
        if requested is None or task.get("status") in requested
    ]
    return {"ok": True, "task_name": task_name, "tasks": tasks}


def command_artifact_show(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    record = find_artifact(task_root, args.artifact_id)
    verify_artifact(task_root, record)
    return {"ok": True, "task_name": task_name, **record}


def command_artifact_list(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    if args.task_id is not None and not TASK_ID_RE.fullmatch(args.task_id):
        raise StateError(f"invalid task ID: {args.task_id}", task_name)
    records = [
        record
        for record in artifact_records(task_root)
        if args.task_id is None or record.get("task_id") == args.task_id
    ]
    def artifact_number(record: dict[str, Any]) -> int:
        artifact_id = record.get("artifact_id")
        match = ARTIFACT_ID_RE.fullmatch(artifact_id) if isinstance(artifact_id, str) else None
        return int(match.group(1)) if match else sys.maxsize

    records.sort(key=artifact_number)
    return {"ok": True, "task_name": task_name, "artifacts": records}


def dependency_issues(tasks: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            issues.append(f"invalid task ID in task record: {task_id!r}")
            continue
        by_id[task_id] = task
    for task in tasks:
        task_id = task.get("task_id")
        dependencies = task.get("dependencies")
        if not isinstance(dependencies, list):
            issues.append(f"{task_id}: dependencies is not a list")
            continue
        for dependency in dependencies:
            if not isinstance(dependency, str) or dependency not in by_id:
                issues.append(f"{task_id}: invalid dependency {dependency}")

    visiting: set[Any] = set()
    visited: set[Any] = set()

    def visit(task_id: Any) -> None:
        if task_id in visiting:
            issues.append(f"cyclic dependency involving {task_id}")
            return
        if task_id in visited or task_id not in by_id:
            return
        visiting.add(task_id)
        dependencies = by_id[task_id].get("dependencies", [])
        if isinstance(dependencies, list):
            for dependency in dependencies:
                if isinstance(dependency, str):
                    visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)
    return list(dict.fromkeys(issues))


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    task_name, task_root, _ = resolve_task_root(args.task_name)
    issues: list[str] = []
    try:
        meta = load_json(task_root / "meta.json")
        if meta.get("schema_version") != SCHEMA_VERSION:
            issues.append("incompatible schema version")
    except StateError as exc:
        issues.append(exc.error)

    tasks: list[dict[str, Any]] = []
    for path in all_task_paths(task_root):
        try:
            task = load_json(path)
            tasks.append(task)
            if task.get("task_id") != path.stem or not TASK_ID_RE.fullmatch(path.stem):
                issues.append(f"{path.name}: task ID is invalid")
            if task.get("status") not in STATES:
                issues.append(f"{path.stem}: invalid status")
            try:
                validate_task_description_fields(task, task_root)
            except StateError as exc:
                issues.append(f"{path.stem}: {exc.error}")
        except StateError as exc:
            issues.append(exc.error)
    issues.extend(dependency_issues(tasks))
    task_by_id = {
        task["task_id"]: task
        for task in tasks
        if isinstance(task.get("task_id"), str)
        and TASK_ID_RE.fullmatch(task["task_id"])
    }

    run_by_id: dict[Any, dict[str, Any]] = {}
    for directory in sorted((task_root / "runs").glob("R-*")):
        if directory.is_symlink() or not directory.is_dir():
            issues.append(f"{directory.name}: invalid run directory")
            continue
        path = directory / "run.json"
        if not path.is_file() or path.is_symlink():
            issues.append(f"{directory.name}: run.json is missing")
            continue
        try:
            run = load_json(path)
            run_id = run.get("run_id")
            if isinstance(run_id, str):
                run_by_id[run_id] = run
            if run_id != directory.name or not RUN_ID_RE.fullmatch(directory.name):
                issues.append(f"{directory.name}: run ID is invalid")
            if run.get("status") not in STATES:
                issues.append(f"{directory.name}: invalid run status")
            run_task_id = run.get("task_id")
            task = task_by_id.get(run_task_id) if isinstance(run_task_id, str) else None
            if task is None:
                issues.append(f"{path.parent.name}: task is missing")
            elif task.get("run_id") == run.get("run_id"):
                if task.get("status") != run.get("status") and not (
                    task.get("status") == "ready" and run.get("status") == "failed"
                ):
                    issues.append(f"{path.parent.name}: task and run status differ")
        except StateError as exc:
            issues.append(exc.error)

    for task in tasks:
        run_id = task.get("run_id")
        if run_id is not None:
            run = run_by_id.get(run_id) if isinstance(run_id, str) else None
            if run is None:
                issues.append(f"{task.get('task_id')}: current run is missing")
            elif run.get("task_id") != task.get("task_id"):
                issues.append(f"{task.get('task_id')}: current run belongs to another task")
        if task.get("status") == "completed" and task.get("accepted_run_id") != run_id:
            issues.append(f"{task.get('task_id')}: accepted run is inconsistent")

    for record in artifact_records(task_root):
        try:
            record_task_id = record.get("task_id")
            record_run_id = record.get("run_id")
            if not isinstance(record_task_id, str) or record_task_id not in task_by_id:
                issues.append(f"{record.get('artifact_id')}: source task is missing")
            run = run_by_id.get(record_run_id) if isinstance(record_run_id, str) else None
            if run is None or run.get("task_id") != record_task_id:
                issues.append(f"{record.get('artifact_id')}: source run is inconsistent")
            verify_artifact(task_root, record)
        except StateError as exc:
            issues.append(exc.error)

    for path in task_root.rglob(".tmp-*.tmp"):
        issues.append(f"temporary file remains: {path.relative_to(task_root)}")
    try:
        for run in incomplete_runs(task_root, tasks):
            issues.append(f"unfinished run: {run['run_id']}")
    except StateError as exc:
        issues.append(exc.error)

    return {
        "ok": True,
        "task_name": task_name,
        "issues": list(dict.fromkeys(issues)),
    }


def parse_boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise StateError("retryable must be true or false")


def add_json_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true")


def add_task_name(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-name")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="statemng")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("task_name")
    init.add_argument("--goal-file", required=True)
    add_json_option(init)
    init.set_defaults(handler=command_init)

    project = commands.add_parser("project")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_list = project_commands.add_parser("list")
    add_json_option(project_list)
    project_list.set_defaults(handler=command_project_list)
    project_resume = project_commands.add_parser("resume")
    add_task_name(project_resume)
    add_json_option(project_resume)
    project_resume.set_defaults(handler=command_project_resume)

    task = commands.add_parser("task")
    task_commands = task.add_subparsers(dest="task_command", required=True)

    create = task_commands.add_parser("create")
    add_task_name(create)
    create.add_argument("--title", required=True)
    create.add_argument("--description-file", required=True)
    create.add_argument("--depends-on")
    add_json_option(create)
    create.set_defaults(handler=command_task_create)

    ready = task_commands.add_parser("ready")
    add_task_name(ready)
    add_json_option(ready)
    ready.set_defaults(handler=command_task_ready)

    claim = task_commands.add_parser("claim")
    claim.add_argument("task_id")
    add_task_name(claim)
    add_json_option(claim)
    claim.set_defaults(handler=command_task_claim)

    show = task_commands.add_parser("show")
    show.add_argument("task_id")
    add_task_name(show)
    show.add_argument("--run-id", required=True)
    add_json_option(show)
    show.set_defaults(handler=command_task_show)

    submit = task_commands.add_parser("submit")
    submit.add_argument("task_id")
    add_task_name(submit)
    submit.add_argument("--run-id", required=True)
    submit.add_argument("--summary-file", required=True)
    submit.add_argument("--artifact", dest="artifact_id", required=True)
    add_json_option(submit)
    submit.set_defaults(handler=command_task_submit)

    accept = task_commands.add_parser("accept")
    accept.add_argument("task_id")
    add_task_name(accept)
    accept.add_argument("--run-id", required=True)
    add_json_option(accept)
    accept.set_defaults(handler=command_task_accept)

    block = task_commands.add_parser("block")
    block.add_argument("task_id")
    add_task_name(block)
    block.add_argument("--run-id", required=True)
    block.add_argument("--reason-file", required=True)
    add_json_option(block)
    block.set_defaults(handler=command_task_block)

    unblock = task_commands.add_parser("unblock")
    unblock.add_argument("task_id")
    add_task_name(unblock)
    unblock.add_argument("--run-id", required=True)
    add_json_option(unblock)
    unblock.set_defaults(handler=command_task_unblock)

    fail = task_commands.add_parser("fail")
    fail.add_argument("task_id")
    add_task_name(fail)
    fail.add_argument("--run-id", required=True)
    fail.add_argument("--reason-file", required=True)
    fail.add_argument("--retryable", required=True)
    add_json_option(fail)
    fail.set_defaults(handler=command_task_fail)

    status = task_commands.add_parser("status")
    status.add_argument("task_id")
    add_task_name(status)
    add_json_option(status)
    status.set_defaults(handler=command_task_status)

    task_list = task_commands.add_parser("list")
    add_task_name(task_list)
    task_list.add_argument("--status", dest="statuses")
    add_json_option(task_list)
    task_list.set_defaults(handler=command_task_list)

    artifact = commands.add_parser("artifact")
    artifact_commands = artifact.add_subparsers(dest="artifact_command", required=True)

    artifact_add = artifact_commands.add_parser("add")
    add_task_name(artifact_add)
    artifact_add.add_argument("--task", dest="task_id", required=True)
    artifact_add.add_argument("--run-id", required=True)
    artifact_add.add_argument("--path", required=True)
    add_json_option(artifact_add)
    artifact_add.set_defaults(handler=command_artifact_add)

    artifact_show = artifact_commands.add_parser("show")
    artifact_show.add_argument("artifact_id")
    add_task_name(artifact_show)
    add_json_option(artifact_show)
    artifact_show.set_defaults(handler=command_artifact_show)

    artifact_list = artifact_commands.add_parser("list")
    add_task_name(artifact_list)
    artifact_list.add_argument("--task", dest="task_id")
    add_json_option(artifact_list)
    artifact_list.set_defaults(handler=command_artifact_list)

    doctor = commands.add_parser("doctor")
    add_task_name(doctor)
    add_json_option(doctor)
    doctor.set_defaults(handler=command_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        needs_task_name = bool(arguments) and (
            arguments[0] in {"task", "artifact", "doctor"}
            or arguments[:2] == ["project", "resume"]
        )
        has_task_name = any(
            item == "--task-name" or item.startswith("--task-name=")
            for item in arguments
        )
        if needs_task_name and not has_task_name and not any(
            item in {"-h", "--help"} for item in arguments
        ):
            raise StateError("TASK_NAME_REQUIRED")
        args = build_parser().parse_args(arguments)
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except StateError as exc:
        result: dict[str, Any] = {"ok": False}
        error_task_name = exc.task_name
        if error_task_name is None and args is not None:
            error_task_name = getattr(args, "task_name", None)
        if error_task_name is not None:
            result["task_name"] = error_task_name
        result["error"] = exc.error
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
