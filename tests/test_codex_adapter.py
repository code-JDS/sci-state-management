from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_TEMPLATE = (
    ROOT
    / "adapters"
    / "codex"
    / "templates"
    / "persistent-scientific-research"
    / "SKILL.md"
)
WORKER_TEMPLATE = (
    ROOT / "adapters" / "codex" / "templates" / "persistent-research-worker.toml"
)
AGENTS_TEMPLATE = ROOT / "adapters" / "codex" / "templates" / "AGENTS.block.md"


def copy_tool(host: Path) -> Path:
    destination = host / "state-management"
    shutil.copytree(
        ROOT,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    return destination


def run_manage(tool: Path, home: Path, command: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(tool / "manage.py"), command],
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )


def run_git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *arguments],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stdout + completed.stderr)
    return completed.stdout.strip()


class CodexAdapterTests(unittest.TestCase):
    def test_general_skill_is_self_contained(self) -> None:
        skill = SKILL_TEMPLATE.read_text(encoding="utf-8")
        lines = skill.splitlines()
        self.assertEqual(lines[0], "---")
        self.assertEqual(lines[1], "name: persistent-scientific-research")
        self.assertTrue(lines[2].startswith("description: Use for multi-step scientific research"))
        self.assertEqual(lines[3], "---")
        self.assertIn("## Main Agent", skill)
        self.assertIn("## Execution Agent", skill)
        self.assertIn("persistent_research_worker", skill)
        self.assertIn("statemng_task_accept", skill)
        self.assertIn("statemng_task_unblock", skill)
        for forbidden in ("CANON", "epsilon-form", "workflow.py", "applicable root"):
            self.assertNotIn(forbidden, skill)

    def test_worker_template_enforces_main_only_tools(self) -> None:
        rendered = WORKER_TEMPLATE.read_text(encoding="utf-8").replace(
            "__PROJECT_NAME__", "sample-project"
        )
        worker = tomllib.loads(rendered)
        self.assertEqual(worker["name"], "persistent_research_worker")
        self.assertIn("required_skills", worker["developer_instructions"])
        self.assertEqual(
            worker["mcp_servers"]["statemng"]["disabled_tools"],
            ["statemng_task_accept", "statemng_task_unblock"],
        )
        self.assertEqual(
            worker["mcp_servers"]["statemng"]["args"],
            ["-c", 'exec "$HOME/.codex/statemng/sample-project/mcp"'],
        )

    def test_install_is_complete_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            host = base / "sample-project"
            host.mkdir()
            tool = copy_tool(host)
            home = base / "home"
            home.mkdir()

            (host / "AGENTS.md").write_text("# Existing rules\n", encoding="utf-8")
            (host / ".codex").mkdir()
            (host / ".codex" / "config.toml").write_text(
                '[unrelated]\nvalue = "keep"\n', encoding="utf-8"
            )
            (host / ".gitignore").write_text("existing/\n", encoding="utf-8")

            first = run_manage(tool, home, "install")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            first_payload = json.loads(first.stdout)
            self.assertTrue(first_payload["ok"], first_payload)
            self.assertTrue(first_payload["restart_required"])
            self.assertEqual(
                set(first_payload["changed"]),
                {
                    ".agents/skills/persistent-scientific-research/SKILL.md",
                    ".codex/agents/persistent-research-worker.toml",
                    "AGENTS.md",
                    ".codex/config.toml",
                    ".gitignore",
                },
            )

            launcher = home / ".codex" / "statemng" / "sample-project" / "mcp"
            self.assertTrue(launcher.is_file())
            self.assertTrue(launcher.stat().st_mode & stat.S_IXUSR)
            self.assertIn(str(tool / "mcp_server.py"), launcher.read_text(encoding="utf-8"))

            installed_skill = (
                host
                / ".agents"
                / "skills"
                / "persistent-scientific-research"
                / "SKILL.md"
            )
            self.assertEqual(
                installed_skill.read_text(encoding="utf-8"),
                SKILL_TEMPLATE.read_text(encoding="utf-8"),
            )
            worker = tomllib.loads(
                (host / ".codex" / "agents" / "persistent-research-worker.toml").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                worker["mcp_servers"]["statemng"]["disabled_tools"],
                ["statemng_task_accept", "statemng_task_unblock"],
            )
            config = tomllib.loads(
                (host / ".codex" / "config.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(config["unrelated"]["value"], "keep")
            self.assertEqual(
                config["mcp_servers"]["statemng"]["args"],
                ["-c", 'exec "$HOME/.codex/statemng/sample-project/mcp"'],
            )
            agents = (host / "AGENTS.md").read_text(encoding="utf-8")
            self.assertTrue(agents.startswith("# Existing rules\n"))
            self.assertEqual(agents.count("<!-- statemng:begin -->"), 1)
            self.assertEqual(agents.count("<!-- statemng:end -->"), 1)
            self.assertEqual(
                (host / ".gitignore").read_text(encoding="utf-8").splitlines(),
                ["existing/", ".state-management/"],
            )
            self.assertFalse((host / ".state-management").exists())

            tracked = {
                relative: (host / relative).read_bytes()
                for relative in first_payload["changed"]
            }
            second = run_manage(tool, home, "install")
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertEqual(second_payload["changed"], [])
            for relative, content in tracked.items():
                self.assertEqual((host / relative).read_bytes(), content)

    def test_install_updates_an_existing_statemng_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            host = base / "sample-project"
            host.mkdir()
            tool = copy_tool(host)
            home = base / "home"
            home.mkdir()
            (host / ".codex").mkdir()
            config_path = host / ".codex" / "config.toml"
            config_path.write_text(
                "[mcp_servers.statemng]\n"
                "enabled = false\n"
                'command = "old"\n'
                "\n"
                "[unrelated]\n"
                'value = "keep"\n',
                encoding="utf-8",
            )

            completed = run_manage(tool, home, "install")
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            config_text = config_path.read_text(encoding="utf-8")
            self.assertEqual(config_text.count("[mcp_servers.statemng]"), 1)
            config = tomllib.loads(config_text)
            self.assertTrue(config["mcp_servers"]["statemng"]["enabled"])
            self.assertEqual(config["unrelated"]["value"], "keep")

    def test_update_fetches_main_reinstalls_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            upstream = base / "upstream"
            shutil.copytree(
                ROOT,
                upstream,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            run_git(upstream, "init", "-b", "main")
            run_git(upstream, "config", "user.name", "State Management Test")
            run_git(upstream, "config", "user.email", "test@example.invalid")
            run_git(upstream, "config", "commit.gpgsign", "false")
            run_git(upstream, "add", ".")
            run_git(upstream, "commit", "-m", "initial")
            initial_commit = run_git(upstream, "rev-parse", "HEAD")

            bare = base / "remote.git"
            completed = subprocess.run(
                ["git", "clone", "--bare", str(upstream), str(bare)],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            run_git(upstream, "remote", "add", "origin", str(bare))

            agents_template = upstream / "adapters" / "codex" / "templates" / "AGENTS.block.md"
            agents_template.write_text(
                agents_template.read_text(encoding="utf-8").replace(
                    "Multi-step scientific research",
                    "Persistent multi-step scientific research",
                ),
                encoding="utf-8",
            )
            run_git(upstream, "add", "adapters/codex/templates/AGENTS.block.md")
            run_git(upstream, "commit", "-m", "update template")
            updated_commit = run_git(upstream, "rev-parse", "HEAD")
            run_git(upstream, "push", "origin", "main")

            host = base / "sample-project"
            host.mkdir()
            tool = host / "state-management"
            completed = subprocess.run(
                ["git", "clone", str(bare), str(tool)],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            run_git(tool, "checkout", "--detach", initial_commit)
            home = base / "home"
            home.mkdir()
            state_file = host / ".state-management" / "existing" / "keep.txt"
            state_file.parent.mkdir(parents=True)
            state_file.write_text("unchanged\n", encoding="utf-8")

            installed = run_manage(tool, home, "install")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            self.assertNotIn(
                "Persistent multi-step scientific research",
                (host / "AGENTS.md").read_text(encoding="utf-8"),
            )
            updated = run_manage(tool, home, "update")
            self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
            self.assertEqual(run_git(tool, "rev-parse", "HEAD"), updated_commit)
            self.assertIn(
                "Persistent multi-step scientific research",
                (host / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(state_file.read_text(encoding="utf-8"), "unchanged\n")

    def test_readme_documents_the_complete_public_flow(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("code-JDS/sci-state-management.git", readme)
        self.assertIn("python3 state-management/manage.py install", readme)
        self.assertIn("python3 state-management/manage.py update", readme)
        self.assertIn("new Codex Local session", readme)
        self.assertIn("persistent-scientific-research", readme)
        self.assertIn("persistent_research_worker", readme)
        self.assertNotIn("CANON", readme)
        self.assertNotIn("epsilon-form", readme)


if __name__ == "__main__":
    unittest.main()
