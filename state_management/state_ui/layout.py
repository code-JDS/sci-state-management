"""Deterministic layered layout for the task dependency graph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


NODE_WIDTH = 248
NODE_HEIGHT = 88
HORIZONTAL_GAP = 72
VERTICAL_GAP = 112
CANVAS_PADDING = 56
MIN_CANVAS_WIDTH = 760
MIN_CANVAS_HEIGHT = 560
TASK_ID_RE = re.compile(r"T-(\d+)$")


@dataclass(frozen=True)
class NodePosition:
    left: int
    top: int
    width: int = NODE_WIDTH
    height: int = NODE_HEIGHT
    level: int = 0


@dataclass(frozen=True)
class LayoutResult:
    positions: dict[str, NodePosition]
    edges: tuple[tuple[str, str], ...]
    width: int
    height: int


def task_id_key(task_id: str) -> tuple[int, int | str]:
    match = TASK_ID_RE.fullmatch(task_id)
    return (0, int(match.group(1))) if match else (1, task_id)


def _task_mapping(tasks: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("each task must have a non-empty string task_id")
        if task_id in result:
            raise ValueError(f"duplicate task ID: {task_id}")
        result[task_id] = task
    return result


def _dependencies(task: dict[str, Any], known_ids: set[str]) -> list[str]:
    raw = task.get("dependencies", [])
    if not isinstance(raw, list):
        return []
    return sorted(
        {item for item in raw if isinstance(item, str) and item in known_ids},
        key=task_id_key,
    )


def compute_layout(tasks: Iterable[dict[str, Any]]) -> LayoutResult:
    """Lay out a DAG from top to bottom using longest dependency depth.

    Missing dependency records are kept out of the drawable edge set. Cycles are
    rejected because no top-to-bottom dependency layout can represent them.
    """

    by_id = _task_mapping(tasks)
    if not by_id:
        return LayoutResult({}, (), MIN_CANVAS_WIDTH, MIN_CANVAS_HEIGHT)

    known_ids = set(by_id)
    dependencies = {
        task_id: _dependencies(task, known_ids) for task_id, task in by_id.items()
    }
    levels: dict[str, int] = {}
    visiting: set[str] = set()

    def level_for(task_id: str) -> int:
        if task_id in levels:
            return levels[task_id]
        if task_id in visiting:
            raise ValueError(f"cyclic task dependency involving {task_id}")
        visiting.add(task_id)
        predecessors = dependencies[task_id]
        level = 0 if not predecessors else max(level_for(item) + 1 for item in predecessors)
        visiting.remove(task_id)
        levels[task_id] = level
        return level

    for task_id in sorted(by_id, key=task_id_key):
        level_for(task_id)

    grouped: dict[int, list[str]] = {}
    for task_id, level in levels.items():
        grouped.setdefault(level, []).append(task_id)

    largest_layer = max(len(task_ids) for task_ids in grouped.values())
    stride = NODE_WIDTH + HORIZONTAL_GAP
    content_width = largest_layer * NODE_WIDTH + (largest_layer - 1) * HORIZONTAL_GAP
    width = max(MIN_CANVAS_WIDTH, CANVAS_PADDING * 2 + content_width)
    max_level = max(grouped)
    height = max(
        MIN_CANVAS_HEIGHT,
        CANVAS_PADDING * 2 + (max_level + 1) * NODE_HEIGHT + max_level * VERTICAL_GAP,
    )

    positions: dict[str, NodePosition] = {}
    for level in range(max_level + 1):
        task_ids = grouped.get(level, [])

        def barycenter(task_id: str) -> tuple[float, tuple[int, int | str]]:
            predecessor_centers = [
                positions[item].left + NODE_WIDTH / 2
                for item in dependencies[task_id]
                if item in positions
            ]
            average = (
                sum(predecessor_centers) / len(predecessor_centers)
                if predecessor_centers
                else width / 2
            )
            return average, task_id_key(task_id)

        task_ids.sort(key=barycenter)
        layer_width = len(task_ids) * NODE_WIDTH + max(0, len(task_ids) - 1) * HORIZONTAL_GAP
        start_left = max(CANVAS_PADDING, round((width - layer_width) / 2))
        top = CANVAS_PADDING + level * (NODE_HEIGHT + VERTICAL_GAP)
        for index, task_id in enumerate(task_ids):
            positions[task_id] = NodePosition(
                left=start_left + index * stride,
                top=top,
                level=level,
            )

    edges = tuple(
        sorted(
            (
                (dependency, task_id)
                for task_id in by_id
                for dependency in dependencies[task_id]
            ),
            key=lambda edge: (task_id_key(edge[1]), task_id_key(edge[0])),
        )
    )
    return LayoutResult(positions, edges, width, height)
