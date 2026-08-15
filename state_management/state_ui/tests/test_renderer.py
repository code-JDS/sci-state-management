from __future__ import annotations

import unittest

from state_management.state_ui.renderer import render_project_state


def complete_task(task_id: str, status: str, dependencies: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "title": f"Task {task_id}",
        "objective": "Produce the result.",
        "background": "Use exact evidence.",
        "acceptance_criteria": ["Result is complete.", "Checks pass."],
        "dependencies": dependencies or [],
        "status": status,
    }


class RendererTests(unittest.TestCase):
    def test_html_is_self_contained_and_has_one_node_and_edge_per_fact(self) -> None:
        statuses = [
            "pending",
            "ready",
            "running",
            "submitted",
            "completed",
            "blocked",
            "failed",
        ]
        tasks = []
        for index, status in enumerate(statuses, start=1):
            dependencies = [f"T-{index - 1:03d}"] if index > 1 else []
            tasks.append(complete_task(f"T-{index:03d}", status, dependencies))
        html = render_project_state(
            {"task_name": "display-test", "goal": "Show every state."}, tasks
        )
        self.assertEqual(html.count('class="task-node"'), len(tasks))
        self.assertEqual(html.count('class="dependency-edge"'), len(tasks) - 1)
        self.assertEqual(html.count('class="task-detail"'), len(tasks))
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("localStorage", html)
        self.assertNotIn("indexedDB", html)
        self.assertIn("setPointerCapture", html)
        self.assertIn("requestAnimationFrame", html)
        self.assertIn("updateConnectedEdges", html)
        for status in statuses:
            self.assertIn(f'data-status="{status}"', html)

    def test_user_text_is_escaped_and_never_inserted_by_javascript(self) -> None:
        malicious = '</button><script>globalThis.injected = true</script><b title="x">bad</b>'
        task = complete_task("T-001", "ready")
        task["title"] = malicious
        task["objective"] = malicious
        html = render_project_state(
            {"task_name": malicious, "goal": malicious}, [task]
        )
        self.assertNotIn(malicious, html)
        self.assertNotIn("globalThis.injected = true</script>", html)
        self.assertIn("&lt;/button&gt;&lt;script&gt;globalThis.injected = true&lt;/script&gt;", html)
        self.assertNotIn("innerHTML", html)

    def test_details_include_only_fields_present_on_the_task(self) -> None:
        task = complete_task("T-001", "completed")
        task.update(
            {
                "required_skills": ["methods/libra/SKILL.md"],
                "run_id": "R-002",
                "summary": "Checked result",
                "artifact_id": "A-003",
                "completed_at": "2026-08-10T10:00:00+00:00",
            }
        )
        html = render_project_state({"task_name": "x", "goal": "g"}, [task])
        self.assertIn("当前 run ID", html)
        self.assertIn("必需 Skills", html)
        self.assertIn("methods/libra/SKILL.md", html)
        self.assertIn("结果摘要", html)
        self.assertIn("Artifact ID", html)
        self.assertIn("完成时间", html)
        self.assertNotIn("阻塞或失败原因", html)


if __name__ == "__main__":
    unittest.main()
