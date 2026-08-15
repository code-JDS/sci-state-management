from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


STATE_MANAGEMENT_ROOT = Path(__file__).resolve().parents[3]
CLI = STATE_MANAGEMENT_ROOT / "statemng"


class UIIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> dict:
        completed = subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=self.project,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return json.loads(completed.stdout)

    def test_cli_rebuilds_markdown_and_html_through_lifecycle_and_resume(self) -> None:
        (self.project / "goal.md").write_text("# Goal\n\nBuild a checked result.\n", encoding="utf-8")
        self.invoke("init", "ui-study", "--goal-file", "goal.md")
        task_root = self.project / ".state-management" / "ui-study"
        markdown = task_root / "views" / "PROJECT_STATE.md"
        html_path = task_root / "views" / "ui" / "PROJECT_STATE.html"
        self.assertTrue(markdown.is_file())
        self.assertTrue(html_path.is_file())
        self.assertNotIn("src=", html_path.read_text(encoding="utf-8"))

        description = {
            "objective": "Derive the result.",
            "background": "Use exact inputs.",
            "acceptance_criteria": ["The result is verified."],
        }
        (self.project / "description.json").write_text(
            json.dumps(description), encoding="utf-8"
        )
        created = self.invoke(
            "task",
            "create",
            "--task-name",
            "ui-study",
            "--title",
            "Derive result",
            "--description-file",
            "description.json",
        )
        task_id = created["task_id"]
        html = html_path.read_text(encoding="utf-8")
        self.assertIn(f'data-task-id="{task_id}"', html)
        self.assertIn('data-status="ready"', html)

        claimed = self.invoke("task", "claim", task_id, "--task-name", "ui-study")
        html = html_path.read_text(encoding="utf-8")
        self.assertIn('data-status="running"', html)
        self.assertIn(claimed["run_id"], html)

        markdown.unlink()
        html_path.unlink()
        self.invoke("project", "resume", "--task-name", "ui-study")
        self.assertTrue(markdown.is_file())
        self.assertTrue(html_path.is_file())
        self.assertFalse((task_root / "views" / "UI_LAYOUT.json").exists())
        self.assertFalse((task_root / "views" / "ui" / "UI_LAYOUT.json").exists())


if __name__ == "__main__":
    unittest.main()
