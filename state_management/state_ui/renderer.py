"""Render the task state and dependency graph as one standalone HTML file."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from .layout import LayoutResult, compute_layout, task_id_key


UI_ROOT = Path(__file__).resolve().parent
TEMPLATE_PATH = UI_ROOT / "templates" / "project_state.html"
CSS_PATH = UI_ROOT / "assets" / "project_state.css"
JS_PATH = UI_ROOT / "assets" / "project_state.js"

STATUS_META = {
    "pending": ("待依赖", "○"),
    "ready": ("待执行", "●"),
    "running": ("执行中", "▶"),
    "submitted": ("待验收", "◆"),
    "completed": ("已完成", "✓"),
    "blocked": ("已阻塞", "!"),
    "failed": ("已失败", "×"),
}


def _text(value: Any) -> str:
    return escape(str(value), quote=True)


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _detail_section(label: str, value: Any, *, ordered: bool = False) -> str:
    if ordered:
        items = "".join(f"<li>{_text(item)}</li>" for item in value)
        body = f'<ol class="detail-list">{items}</ol>'
    else:
        body = f'<div class="detail-copy">{_text(value)}</div>'
    return (
        '<section class="detail-section">'
        f'<h3>{_text(label)}</h3>{body}'
        "</section>"
    )


def _render_details(task: dict[str, Any], *, hidden: bool) -> str:
    task_id = str(task.get("task_id", ""))
    status = str(task.get("status", ""))
    status_label, status_icon = STATUS_META.get(status, (status or "未知", "?"))
    sections: list[str] = []
    for field, label in (("objective", "目标"), ("background", "背景")):
        if field in task:
            sections.append(_detail_section(label, task[field]))
    if "acceptance_criteria" in task and isinstance(task["acceptance_criteria"], list):
        sections.append(_detail_section("验收标准", task["acceptance_criteria"], ordered=True))
    if "required_skills" in task and isinstance(task["required_skills"], list):
        required_skills = task["required_skills"]
        sections.append(
            _detail_section("必需 Skills", required_skills, ordered=True)
            if required_skills
            else _detail_section("必需 Skills", "无")
        )
    if "dependencies" in task and isinstance(task["dependencies"], list):
        dependencies = task["dependencies"]
        sections.append(_detail_section("依赖", "、".join(map(str, dependencies)) or "无"))
    for field, label in (
        ("run_id", "当前 run ID"),
        ("summary", "结果摘要"),
        ("artifact_id", "Artifact ID"),
        ("reason", "阻塞或失败原因"),
        ("completed_at", "完成时间"),
    ):
        if field in task:
            sections.append(_detail_section(label, task[field]))
    hidden_attribute = " hidden" if hidden else ""
    return (
        f'<article class="task-detail" data-detail-id="{_text(task_id)}"{hidden_attribute}>'
        '<div class="detail-heading">'
        f'<div class="detail-task-id">{_text(task_id)}</div>'
        f'<h2>{_text(task.get("title", ""))}</h2>'
        f'<div class="detail-status" data-status="{_text(status)}">'
        f'<span aria-hidden="true">{_text(status_icon)}</span>{_text(status_label)}'
        "</div></div>"
        + "".join(sections)
        + "</article>"
    )


def _render_node(task: dict[str, Any], layout: LayoutResult) -> str:
    task_id = str(task.get("task_id", ""))
    status = str(task.get("status", ""))
    status_label, status_icon = STATUS_META.get(status, (status or "未知", "?"))
    position = layout.positions[task_id]
    return (
        '<button type="button" class="task-node" '
        f'data-task-id="{_text(task_id)}" data-status="{_text(status)}" '
        f'style="left:{position.left}px;top:{position.top}px" '
        f'aria-label="{_text(task_id)} {_text(task.get("title", ""))}，{_text(status_label)}">'
        '<span class="node-topline">'
        f'<span class="node-icon" aria-hidden="true">{_text(status_icon)}</span>'
        f'<span class="node-id">{_text(task_id)}</span>'
        f'<span class="node-status">{_text(status_label)}</span>'
        "</span>"
        f'<span class="node-title">{_text(task.get("title", ""))}</span>'
        "</button>"
    )


def _render_edges(layout: LayoutResult) -> str:
    return "".join(
        '<path class="dependency-edge" d="" '
        f'data-from="{_text(source)}" data-to="{_text(target)}" '
        'marker-end="url(#dependency-arrow)" />'
        for source, target in layout.edges
    )


def _render_stat(label: str, value: int, group: str) -> str:
    return (
        f'<div class="summary-stat" data-group="{group}">'
        f'<span class="summary-stat-label">{_text(label)}</span>'
        f'<strong>{value}</strong></div>'
    )


def render_project_state(meta: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    """Return a complete HTML document built only from the supplied facts."""

    ordered_tasks = sorted(tasks, key=lambda task: task_id_key(str(task.get("task_id", ""))))
    layout = compute_layout(ordered_tasks)
    status_counts = {status: 0 for status in STATUS_META}
    for task in ordered_tasks:
        status = task.get("status")
        if status in status_counts:
            status_counts[status] += 1

    waiting = status_counts["pending"] + status_counts["ready"]
    active = status_counts["running"] + status_counts["submitted"]
    abnormal = status_counts["blocked"] + status_counts["failed"]
    stats = "".join(
        (
            _render_stat("待执行", waiting, "waiting"),
            _render_stat("进行中", active, "active"),
            _render_stat("已完成", status_counts["completed"], "completed"),
            _render_stat("异常", abnormal, "abnormal"),
        )
    )

    nodes = "".join(_render_node(task, layout) for task in ordered_tasks)
    details = "".join(
        _render_details(task, hidden=index != 0) for index, task in enumerate(ordered_tasks)
    )
    if not ordered_tasks:
        details = (
            '<div class="empty-detail"><h2>尚无任务</h2>'
            "<p>创建任务后，可在这里查看目标、依赖和运行结果。</p></div>"
        )

    replacements = {
        "{{PAGE_TITLE}}": _text(f"任务状态 · {meta.get('task_name', '')}"),
        "{{TASK_NAME}}": _text(meta.get("task_name", "")),
        "{{GOAL}}": _text(meta.get("goal", "")),
        "{{STATUS_STATS}}": stats,
        "{{CANVAS_WIDTH}}": str(layout.width),
        "{{CANVAS_HEIGHT}}": str(layout.height),
        "{{EDGES}}": _render_edges(layout),
        "{{NODES}}": nodes,
        "{{DETAILS}}": details,
        "{{INLINE_CSS}}": _read_source(CSS_PATH),
        "{{INLINE_JS}}": _read_source(JS_PATH),
    }
    html = _read_source(TEMPLATE_PATH)
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    unresolved = [placeholder for placeholder in replacements if placeholder in html]
    if unresolved:
        raise ValueError(f"unresolved UI template placeholders: {unresolved}")
    return html
