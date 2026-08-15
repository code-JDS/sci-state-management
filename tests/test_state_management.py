from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from state_management.cli import main


REPOSITORY = Path(__file__).resolve().parents[1]
CLI = REPOSITORY / "statemng"


class StateManagementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name)
        self.previous_cwd = Path.cwd()
        os.chdir(self.project)

    def tearDown(self) -> None:
        os.chdir(self.previous_cwd)
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(list(arguments))
        return code, json.loads(output.getvalue())

    def write(self, relative: str, content: str) -> Path:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def initialize(self, task_name: str = "family-a") -> Path:
        self.write("goal.md", "# Goal\n\nConstruct and verify the result.\n")
        code, result = self.invoke("init", task_name, "--goal-file", "goal.md")
        self.assertEqual(code, 0, result)
        return self.project / ".state-management" / task_name

    def description(
        self,
        filename: str,
        objective: str,
        background: str = "",
        criteria: list[str] | None = None,
        required_skills: list[str] | None = None,
    ) -> Path:
        description = {
            "objective": objective,
            "background": background,
            "acceptance_criteria": criteria or ["The result is verified."],
        }
        if required_skills is not None:
            description["required_skills"] = required_skills
        return self.write(
            filename,
            json.dumps(description),
        )

    def create_task(
        self,
        title: str = "First task",
        depends_on: str | None = None,
        description_file: str = "description.json",
    ) -> dict:
        self.description(description_file, f"Objective for {title}")
        arguments = [
            "task",
            "create",
            "--task-name",
            "family-a",
            "--title",
            title,
            "--description-file",
            description_file,
        ]
        if depends_on is not None:
            arguments.extend(["--depends-on", depends_on])
        code, result = self.invoke(*arguments)
        self.assertEqual(code, 0, result)
        return result

    def claim(self, task_id: str) -> dict:
        code, result = self.invoke(
            "task", "claim", task_id, "--task-name", "family-a"
        )
        self.assertEqual(code, 0, result)
        return result

    def submit_result(self, task_id: str, run_id: str, text: str = "Verified result") -> str:
        task_root = self.project / ".state-management" / "family-a"
        workspace = task_root / "runs" / run_id / "workspace"
        (workspace / "result.md").write_text(text, encoding="utf-8")
        (workspace / "summary.md").write_text(f"Summary: {text}", encoding="utf-8")
        code, artifact = self.invoke(
            "artifact",
            "add",
            "--task-name",
            "family-a",
            "--task",
            task_id,
            "--run-id",
            run_id,
            "--path",
            f"runs/{run_id}/workspace/result.md",
        )
        self.assertEqual(code, 0, artifact)
        code, submitted = self.invoke(
            "task",
            "submit",
            task_id,
            "--task-name",
            "family-a",
            "--run-id",
            run_id,
            "--summary-file",
            f"runs/{run_id}/workspace/summary.md",
            "--artifact",
            artifact["artifact_id"],
        )
        self.assertEqual(code, 0, submitted)
        return artifact["artifact_id"]


class InitializationTests(StateManagementTestCase):
    def test_project_list_is_empty_before_initialization(self) -> None:
        code, result = self.invoke("project", "list")
        self.assertEqual(code, 0)
        self.assertEqual(result, {"ok": True, "task_names": []})
        self.assertFalse((self.project / ".state-management").exists())

    def test_init_creates_only_the_documented_layout_and_preserves_goal_text(self) -> None:
        task_root = self.initialize()
        self.assertEqual(
            {path.name for path in task_root.iterdir()},
            {"meta.json", "tasks", "runs", "artifacts", ".locks", "views"},
        )
        meta = json.loads((task_root / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(
            meta,
            {
                "schema_version": 1,
                "task_name": "family-a",
                "goal": "# Goal\n\nConstruct and verify the result.\n",
            },
        )
        self.assertTrue((task_root / "views" / "PROJECT_STATE.md").is_file())

    def test_invalid_goal_and_existing_task_do_not_overwrite_state(self) -> None:
        self.write("empty.md", "")
        code, result = self.invoke("init", "family-a", "--goal-file", "empty.md")
        self.assertEqual(code, 1)
        self.assertFalse((self.project / ".state-management").exists())

        task_root = self.initialize()
        original = (task_root / "meta.json").read_bytes()
        self.write("goal.md", "replacement")
        code, result = self.invoke("init", "family-a", "--goal-file", "goal.md")
        self.assertEqual(code, 1)
        self.assertEqual((task_root / "meta.json").read_bytes(), original)

    def test_task_names_and_symbolic_state_roots_cannot_escape(self) -> None:
        self.write("goal.md", "Goal")
        code, _ = self.invoke("init", "../escape", "--goal-file", "goal.md")
        self.assertEqual(code, 1)
        self.assertFalse((self.project.parent / "escape").exists())

        outside = self.project / "outside"
        outside.mkdir()
        (self.project / ".state-management").symlink_to(outside, target_is_directory=True)
        code, result = self.invoke("init", "family-a", "--goal-file", "goal.md")
        self.assertEqual(code, 1)
        self.assertIn("symbolic link", result["error"])

    def test_project_list_finds_nearest_root_without_reading_task_json(self) -> None:
        task_root = self.initialize()
        (task_root / "meta.json").write_text("not JSON", encoding="utf-8")
        nested = self.project / "some" / "nested" / "directory"
        nested.mkdir(parents=True)
        os.chdir(nested)
        code, result = self.invoke("project", "list")
        self.assertEqual(code, 0)
        self.assertEqual(result["task_names"], ["family-a"])


class TaskLifecycleTests(StateManagementTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.task_root = self.initialize()

    def test_description_schema_is_exact_and_invalid_input_creates_no_task(self) -> None:
        self.write(
            "bad.json",
            json.dumps(
                {
                    "objective": "Do work",
                    "background": "",
                    "acceptance_criteria": ["Verified"],
                    "priority": 1,
                }
            ),
        )
        code, _ = self.invoke(
            "task",
            "create",
            "--task-name",
            "family-a",
            "--title",
            "Bad task",
            "--description-file",
            "bad.json",
        )
        self.assertEqual(code, 1)
        self.assertEqual(list((self.task_root / "tasks").iterdir()), [])

    def test_old_description_without_required_skills_remains_compatible(self) -> None:
        created = self.create_task()
        task_path = self.task_root / "tasks" / f"{created['task_id']}.json"
        persisted = json.loads(task_path.read_text(encoding="utf-8"))
        self.assertNotIn("required_skills", persisted)

        claimed = self.claim(created["task_id"])
        code, shown = self.invoke(
            "task",
            "show",
            created["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            claimed["run_id"],
        )
        self.assertEqual(code, 0, shown)
        self.assertNotIn("required_skills", shown)

    def test_required_skills_round_trip_through_lifecycle_and_rebuild(self) -> None:
        required_skills = [
            "skills/canon-check-core/SKILL.md",
            ".agents/skills/persistent-scientific-research/SKILL.md",
        ]
        for path in required_skills:
            self.write(path, f"# {path}\n")
        self.description(
            "with-skills.json",
            "Use the required methods.",
            required_skills=required_skills,
        )

        code, created = self.invoke(
            "task",
            "create",
            "--task-name",
            "family-a",
            "--title",
            "Skilled task",
            "--description-file",
            "with-skills.json",
        )
        self.assertEqual(code, 0, created)
        self.assertEqual(created["required_skills"], required_skills)
        task_path = self.task_root / "tasks" / f"{created['task_id']}.json"
        persisted = json.loads(task_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["required_skills"], required_skills)

        claimed = self.claim(created["task_id"])
        code, shown = self.invoke(
            "task",
            "show",
            created["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            claimed["run_id"],
        )
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["required_skills"], required_skills)

        markdown_path = self.task_root / "views" / "PROJECT_STATE.md"
        html_path = self.task_root / "views" / "ui" / "PROJECT_STATE.html"
        markdown_path.unlink()
        html_path.unlink()
        code, resumed = self.invoke("project", "resume", "--task-name", "family-a")
        self.assertEqual(code, 0, resumed)
        resumed_task = next(
            task for task in resumed["tasks"] if task["task_id"] == created["task_id"]
        )
        self.assertEqual(resumed_task["required_skills"], required_skills)
        markdown = markdown_path.read_text(encoding="utf-8")
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("Required Skills", markdown)
        self.assertIn(required_skills[0], markdown)
        self.assertIn("必需 Skills", html)
        self.assertIn(required_skills[1], html)

    def test_required_skills_reject_invalid_paths_types_and_duplicates(self) -> None:
        valid = "skills/canon-check-core/SKILL.md"
        self.write(valid, "# Valid skill\n")
        invalid_values = [
            valid,
            [1],
            [valid, valid],
            ["/tmp/SKILL.md"],
            [r"C:\\skills\\foreign\\SKILL.md"],
            ["../outside/SKILL.md"],
            ["skills/canon-check-core/README.md"],
            ["skills/missing/SKILL.md"],
        ]
        for index, required_skills in enumerate(invalid_values):
            with self.subTest(required_skills=required_skills):
                description = {
                    "objective": "Do work",
                    "background": "",
                    "acceptance_criteria": ["Verified"],
                    "required_skills": required_skills,
                }
                filename = f"bad-skills-{index}.json"
                self.write(filename, json.dumps(description))
                code, _ = self.invoke(
                    "task",
                    "create",
                    "--task-name",
                    "family-a",
                    "--title",
                    "Bad skill task",
                    "--description-file",
                    filename,
                )
                self.assertEqual(code, 1)
        self.assertEqual(list((self.task_root / "tasks").iterdir()), [])

    def test_dependency_unlock_and_complete_research_handoff(self) -> None:
        first = self.create_task("Establish input")
        second = self.create_task("Use input", depends_on=first["task_id"], description_file="second.json")
        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["status"], "pending")
        ready = self.invoke("task", "ready", "--task-name", "family-a")[1]
        self.assertEqual([task["task_id"] for task in ready["tasks"]], [first["task_id"]])
        pending = self.invoke(
            "task", "list", "--task-name", "family-a", "--status", "pending"
        )[1]
        self.assertEqual([task["task_id"] for task in pending["tasks"]], [second["task_id"]])

        claim = self.claim(first["task_id"])
        code, shown = self.invoke(
            "task",
            "show",
            first["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            claim["run_id"],
        )
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["objective"], "Objective for Establish input")
        self.assertEqual(shown["dependencies"], [])
        self.assertEqual(
            Path(shown["workspace"]),
            (self.task_root / "runs" / claim["run_id"] / "workspace").resolve(),
        )

        artifact_id = self.submit_result(first["task_id"], claim["run_id"])
        code, accepted = self.invoke(
            "task",
            "accept",
            first["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            claim["run_id"],
        )
        self.assertEqual(code, 0, accepted)
        self.assertEqual(accepted["unlocked_tasks"], [second["task_id"]])

        code, accepted_again = self.invoke(
            "task",
            "accept",
            first["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            claim["run_id"],
        )
        self.assertEqual(code, 0, accepted_again)
        self.assertEqual(accepted_again["unlocked_tasks"], [])

        second_claim = self.claim(second["task_id"])
        code, shown = self.invoke(
            "task",
            "show",
            second["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            second_claim["run_id"],
        )
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["dependencies"][0]["artifact_id"], artifact_id)
        self.assertIn("Verified result", shown["dependencies"][0]["summary"])

    def test_artifact_add_and_submit_are_idempotent_for_the_same_result(self) -> None:
        task = self.create_task()
        claim = self.claim(task["task_id"])
        workspace = self.task_root / "runs" / claim["run_id"] / "workspace"
        (workspace / "result.md").write_text("result", encoding="utf-8")
        arguments = (
            "artifact",
            "add",
            "--task-name",
            "family-a",
            "--task",
            task["task_id"],
            "--run-id",
            claim["run_id"],
            "--path",
            f"runs/{claim['run_id']}/workspace/result.md",
        )
        code, first = self.invoke(*arguments)
        self.assertEqual(code, 0, first)
        code, second = self.invoke(*arguments)
        self.assertEqual(code, 0, second)
        self.assertEqual(first["artifact_id"], second["artifact_id"])
        self.assertEqual(len(list((self.task_root / "artifacts").iterdir())), 1)
        listed = self.invoke("artifact", "list", "--task-name", "family-a")[1]
        self.assertEqual([item["artifact_id"] for item in listed["artifacts"]], [first["artifact_id"]])

        (workspace / "summary.md").write_text("summary", encoding="utf-8")
        submit = (
            "task",
            "submit",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            claim["run_id"],
            "--summary-file",
            f"runs/{claim['run_id']}/workspace/summary.md",
            "--artifact",
            first["artifact_id"],
        )
        self.assertEqual(self.invoke(*submit)[0], 0)
        self.assertEqual(self.invoke(*submit)[0], 0)
        code, after_submit = self.invoke(*arguments)
        self.assertEqual(code, 0, after_submit)
        self.assertEqual(after_submit["artifact_id"], first["artifact_id"])

    def test_accept_rejects_a_changed_artifact(self) -> None:
        task = self.create_task()
        claim = self.claim(task["task_id"])
        artifact_id = self.submit_result(task["task_id"], claim["run_id"])
        record = self.invoke(
            "artifact", "show", artifact_id, "--task-name", "family-a"
        )[1]
        artifact_path = self.project / record["stored_path"]
        artifact_path.chmod(0o644)
        artifact_path.write_text("tampered", encoding="utf-8")
        code, result = self.invoke(
            "task",
            "accept",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            claim["run_id"],
        )
        self.assertEqual(code, 1)
        self.assertIn("hash mismatch", result["error"])
        status = self.invoke(
            "task", "status", task["task_id"], "--task-name", "family-a"
        )[1]
        self.assertEqual(status["status"], "submitted")

    def test_wrong_run_and_artifact_path_escape_are_rejected(self) -> None:
        task = self.create_task()
        claim = self.claim(task["task_id"])
        outside = self.write("outside.md", "outside")
        code, result = self.invoke(
            "artifact",
            "add",
            "--task-name",
            "family-a",
            "--task",
            task["task_id"],
            "--run-id",
            claim["run_id"],
            "--path",
            str(outside),
        )
        self.assertEqual(code, 1)
        self.assertIn("escapes", result["error"])

        workspace = self.task_root / "runs" / claim["run_id"] / "workspace"
        (workspace / "summary.md").write_text("summary", encoding="utf-8")
        code, result = self.invoke(
            "task",
            "submit",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            "R-999",
            "--summary-file",
            f"runs/{claim['run_id']}/workspace/summary.md",
            "--artifact",
            "A-999",
        )
        self.assertEqual(code, 1)
        self.assertIn("run_id", result["error"])

    def test_retryable_failure_returns_ready_and_block_preserves_workspace(self) -> None:
        task = self.create_task()
        first_run = self.claim(task["task_id"])["run_id"]
        workspace = self.task_root / "runs" / first_run / "workspace"
        (workspace / "partial.txt").write_text("valuable", encoding="utf-8")
        (workspace / "failure.md").write_text(
            "Method failed; partial result retained.", encoding="utf-8"
        )
        code, failed = self.invoke(
            "task",
            "fail",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            first_run,
            "--reason-file",
            f"runs/{first_run}/workspace/failure.md",
            "--retryable",
            "true",
        )
        self.assertEqual(code, 0, failed)
        self.assertEqual(failed["status"], "ready")
        self.assertTrue((workspace / "partial.txt").is_file())

        second_run = self.claim(task["task_id"])["run_id"]
        second_workspace = self.task_root / "runs" / second_run / "workspace"
        (second_workspace / "blocker.md").write_text("Waiting for input", encoding="utf-8")
        code, blocked = self.invoke(
            "task",
            "block",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            second_run,
            "--reason-file",
            f"runs/{second_run}/workspace/blocker.md",
        )
        self.assertEqual(code, 0, blocked)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(second_workspace.is_dir())
        status = self.invoke(
            "task", "status", task["task_id"], "--task-name", "family-a"
        )[1]
        self.assertEqual(status["reason"], "Waiting for input")

        code, wrong_run = self.invoke(
            "task",
            "unblock",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            "R-999",
        )
        self.assertEqual(code, 1, wrong_run)
        self.assertIn("run_id", wrong_run["error"])

        run_path = self.task_root / "runs" / second_run / "run.json"
        inconsistent_run = json.loads(run_path.read_text(encoding="utf-8"))
        inconsistent_run["status"] = "running"
        run_path.write_text(json.dumps(inconsistent_run), encoding="utf-8")
        code, inconsistent = self.invoke(
            "task",
            "unblock",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            second_run,
        )
        self.assertEqual(code, 1, inconsistent)
        self.assertIn("inconsistent", inconsistent["error"])
        inconsistent_run["status"] = "blocked"
        run_path.write_text(json.dumps(inconsistent_run), encoding="utf-8")

        code, unblocked = self.invoke(
            "task",
            "unblock",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            second_run,
        )
        self.assertEqual(code, 0, unblocked)
        self.assertEqual(unblocked["status"], "ready")
        task_json = json.loads(
            (self.task_root / "tasks" / f"{task['task_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(task_json["status"], "ready")
        for field in ("run_id", "reason", "summary", "artifact_id"):
            self.assertNotIn(field, task_json)
        old_run = json.loads(
            (self.task_root / "runs" / second_run / "run.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(old_run["status"], "blocked")
        self.assertEqual(old_run["reason"], "Waiting for input")
        self.assertTrue(second_workspace.is_dir())
        third_run = self.claim(task["task_id"])["run_id"]
        self.assertNotEqual(third_run, second_run)

        final_task = self.create_task("Non-retryable", description_file="final.json")
        final_run = self.claim(final_task["task_id"])["run_id"]
        final_workspace = self.task_root / "runs" / final_run / "workspace"
        (final_workspace / "failure.md").write_text("No valid route", encoding="utf-8")
        code, failed = self.invoke(
            "task",
            "fail",
            final_task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            final_run,
            "--reason-file",
            f"runs/{final_run}/workspace/failure.md",
            "--retryable",
            "false",
        )
        self.assertEqual(code, 0, failed)
        self.assertEqual(failed["status"], "failed")

    def test_unblock_clears_submitted_fields_but_preserves_run_and_artifact(self) -> None:
        task = self.create_task()
        run_id = self.claim(task["task_id"])["run_id"]
        artifact_id = self.submit_result(task["task_id"], run_id)
        workspace = self.task_root / "runs" / run_id / "workspace"
        (workspace / "blocker.md").write_text("Need review input", encoding="utf-8")
        code, blocked = self.invoke(
            "task",
            "block",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            run_id,
            "--reason-file",
            f"runs/{run_id}/workspace/blocker.md",
        )
        self.assertEqual(code, 0, blocked)

        code, unblocked = self.invoke(
            "task",
            "unblock",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            run_id,
        )
        self.assertEqual(code, 0, unblocked)
        task_status = self.invoke(
            "task", "status", task["task_id"], "--task-name", "family-a"
        )[1]
        self.assertEqual(task_status["status"], "ready")
        self.assertNotIn("summary", task_status)
        self.assertNotIn("artifact_id", task_status)
        old_run = json.loads(
            (self.task_root / "runs" / run_id / "run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(old_run["status"], "blocked")
        self.assertIn("Summary: Verified result", old_run["summary"])
        self.assertEqual(old_run["artifact_id"], artifact_id)
        artifact = self.invoke(
            "artifact", "show", artifact_id, "--task-name", "family-a"
        )[1]
        self.assertEqual(artifact["run_id"], run_id)
        view = (self.task_root / "views" / "PROJECT_STATE.md").read_text(encoding="utf-8")
        self.assertNotIn("## Submitted Results", view)

        code, repeated = self.invoke(
            "task",
            "unblock",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            run_id,
        )
        self.assertEqual(code, 1, repeated)

    def test_missing_task_name_uses_required_error(self) -> None:
        code, result = self.invoke("task", "ready")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "TASK_NAME_REQUIRED")


class RecoveryAndDoctorTests(StateManagementTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.task_root = self.initialize()

    def test_resume_repairs_ready_state_rebuilds_view_and_lists_unfinished_run(self) -> None:
        first = self.create_task("First")
        run = self.claim(first["task_id"])["run_id"]
        self.submit_result(first["task_id"], run)
        self.invoke(
            "task", "accept", first["task_id"], "--task-name", "family-a", "--run-id", run
        )
        second = self.create_task("Second", depends_on=first["task_id"], description_file="second.json")
        second_path = self.task_root / "tasks" / f"{second['task_id']}.json"
        second_json = json.loads(second_path.read_text(encoding="utf-8"))
        second_json["status"] = "pending"
        second_path.write_text(json.dumps(second_json), encoding="utf-8")
        (self.task_root / "views" / "PROJECT_STATE.md").unlink()

        code, resumed = self.invoke("project", "resume", "--task-name", "family-a")
        self.assertEqual(code, 0, resumed)
        statuses = {task["task_id"]: task["status"] for task in resumed["tasks"]}
        self.assertEqual(statuses[second["task_id"]], "ready")
        self.assertEqual(resumed["incomplete_runs"], [])
        self.assertTrue((self.task_root / "views" / "PROJECT_STATE.md").is_file())

        second_run = self.claim(second["task_id"])["run_id"]
        orphan = self.task_root / "runs" / "R-999"
        (orphan / "workspace").mkdir(parents=True)
        resumed = self.invoke("project", "resume", "--task-name", "family-a")[1]
        self.assertEqual(
            [item["run_id"] for item in resumed["incomplete_runs"]],
            [second_run, "R-999"],
        )
        self.assertTrue((self.task_root / "runs" / second_run).is_dir())
        self.assertTrue(orphan.is_dir())

    def test_doctor_detects_hash_temp_dependency_and_incomplete_run_problems(self) -> None:
        task = self.create_task()
        run = self.claim(task["task_id"])["run_id"]
        artifact_id = self.submit_result(task["task_id"], run)
        artifact = self.invoke(
            "artifact", "show", artifact_id, "--task-name", "family-a"
        )[1]
        path = self.project / artifact["stored_path"]
        path.chmod(0o644)
        path.write_text("changed", encoding="utf-8")
        (self.task_root / "tasks" / ".tmp-left.tmp").write_text("partial", encoding="utf-8")
        task_path = self.task_root / "tasks" / f"{task['task_id']}.json"
        task_json = json.loads(task_path.read_text(encoding="utf-8"))
        task_json["dependencies"] = ["T-999"]
        task_path.write_text(json.dumps(task_json), encoding="utf-8")

        code, result = self.invoke("doctor", "--task-name", "family-a")
        self.assertEqual(code, 0, result)
        joined = "\n".join(result["issues"])
        self.assertIn("hash mismatch", joined)
        self.assertIn("temporary file remains", joined)
        self.assertIn("invalid dependency T-999", joined)
        self.assertIn(f"unfinished run: {run}", joined)

    def test_doctor_reports_invalid_json_schema_cycles_and_run_mismatch(self) -> None:
        first = self.create_task("First")
        second = self.create_task("Second", description_file="second.json")
        run = self.claim(first["task_id"])["run_id"]

        first_path = self.task_root / "tasks" / f"{first['task_id']}.json"
        second_path = self.task_root / "tasks" / f"{second['task_id']}.json"
        first_json = json.loads(first_path.read_text(encoding="utf-8"))
        second_json = json.loads(second_path.read_text(encoding="utf-8"))
        first_json["dependencies"] = [second["task_id"]]
        first_json["run_id"] = "R-999"
        second_json["dependencies"] = [first["task_id"]]
        first_path.write_text(json.dumps(first_json), encoding="utf-8")
        second_path.write_text(json.dumps(second_json), encoding="utf-8")
        (self.task_root / "tasks" / "T-999.json").write_text("not JSON", encoding="utf-8")

        meta_path = self.task_root / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["schema_version"] = 2
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        code, result = self.invoke("doctor", "--task-name", "family-a")
        self.assertEqual(code, 0, result)
        joined = "\n".join(result["issues"])
        self.assertIn("incompatible schema version", joined)
        self.assertIn("cannot read JSON", joined)
        self.assertIn("cyclic dependency", joined)
        self.assertIn(f"{first['task_id']}: current run is missing", joined)
        self.assertIn(f"unfinished run: {run}", joined)


class EndToEndConcurrencyTests(StateManagementTestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=self.project,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_two_processes_cannot_claim_the_same_task(self) -> None:
        self.write("goal.md", "Goal")
        initialized = self.run_cli("init", "family-a", "--goal-file", "goal.md")
        self.assertEqual(initialized.returncode, 0, initialized.stderr or initialized.stdout)
        self.description("description.json", "Concurrent claim")
        created = self.run_cli(
            "task",
            "create",
            "--task-name",
            "family-a",
            "--title",
            "Concurrent",
            "--description-file",
            "description.json",
        )
        self.assertEqual(created.returncode, 0, created.stderr or created.stdout)
        task_id = json.loads(created.stdout)["task_id"]
        command = [
            sys.executable,
            str(CLI),
            "task",
            "claim",
            task_id,
            "--task-name",
            "family-a",
        ]
        first = subprocess.Popen(command, cwd=self.project, text=True, stdout=subprocess.PIPE)
        second = subprocess.Popen(command, cwd=self.project, text=True, stdout=subprocess.PIPE)
        first_stdout, _ = first.communicate(timeout=10)
        second_stdout, _ = second.communicate(timeout=10)
        results = [json.loads(first_stdout), json.loads(second_stdout)]
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 1])
        self.assertEqual(sum(result.get("status") == "running" for result in results), 1)
        self.assertEqual(sum(result.get("error") == "TASK_ALREADY_CLAIMED" for result in results), 1)
        runs = list((self.project / ".state-management" / "family-a" / "runs").glob("R-*"))
        self.assertEqual(len(runs), 1)

    def test_two_processes_cannot_unblock_the_same_task_twice(self) -> None:
        task_root = self.initialize()
        task = self.create_task()
        run_id = self.claim(task["task_id"])["run_id"]
        workspace = task_root / "runs" / run_id / "workspace"
        (workspace / "blocker.md").write_text("Waiting", encoding="utf-8")
        blocked = self.run_cli(
            "task",
            "block",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            run_id,
            "--reason-file",
            f"runs/{run_id}/workspace/blocker.md",
        )
        self.assertEqual(blocked.returncode, 0, blocked.stdout)
        command = [
            sys.executable,
            str(CLI),
            "task",
            "unblock",
            task["task_id"],
            "--task-name",
            "family-a",
            "--run-id",
            run_id,
        ]
        first = subprocess.Popen(command, cwd=self.project, text=True, stdout=subprocess.PIPE)
        second = subprocess.Popen(command, cwd=self.project, text=True, stdout=subprocess.PIPE)
        first_stdout, _ = first.communicate(timeout=10)
        second_stdout, _ = second.communicate(timeout=10)
        results = [json.loads(first_stdout), json.loads(second_stdout)]
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 1])
        self.assertEqual(sum(result.get("status") == "ready" for result in results), 1)
        task_json = json.loads(
            (task_root / "tasks" / f"{task['task_id']}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(task_json["status"], "ready")
        self.assertNotIn("run_id", task_json)


if __name__ == "__main__":
    unittest.main()
