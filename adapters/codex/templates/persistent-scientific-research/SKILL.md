---
name: persistent-scientific-research
description: Use for multi-step scientific research that requires persistent state, dependency-aware subtasks, isolated workspaces, formal artifacts, and main-Agent acceptance.
---

# Persistent Scientific Research

Use the `statemng` MCP tools for every state operation. Never ask the user to run the internal CLI or edit `.state-management/`. If the tools are unavailable, stop the state-managed research and report the integration error.

Run persistent research only in a Codex Local session. If the current session is a Worktree, stop and report the adapter error; do not operate state in another checkout.

## Main Agent

1. Before the first state call, run `state-management/adapters/codex/install_launcher.py --check`. If it fails, run `python3 state-management/manage.py install`, stop the current research, and require a new Codex Local session.
2. Determine one unambiguous overall `task_name`. If none is clear, call `statemng_project_list` and ask the user to choose.
3. Call `statemng_init` for a new overall task or `statemng_project_resume` for an existing task.
4. Create subtasks with a clear objective, a coherent deliverable, and acceptance criteria that allow the main Agent to judge completion. Every task and dependency must be justified by current evidence; do not add speculative tasks merely to complete an assumed workflow. Each task description contains `objective`, `background`, and `acceptance_criteria`; add `required_skills` only when the execution Agent must load other project Skills.
5. Dispatch each ready task only through the `persistent_research_worker` custom agent, passing the overall task name, task ID, and necessary research instructions.
6. Review submitted summaries and artifacts against the acceptance criteria. Independently verify important results when necessary.
7. Alone call `statemng_task_accept` after successful review. Alone call `statemng_task_unblock` after confirming that a recorded blocking condition has been removed.
8. Replan from accepted evidence until the overall research goal is satisfied or a genuine external block must be reported.

## Execution Agent

1. Receive an explicit overall task name and task ID; do not infer another task.
2. Call `statemng_task_claim`, then `statemng_task_show` with the returned run ID.
3. Completely read every `SKILL.md` listed in `required_skills`, in the supplied order.
4. Work only in the returned run workspace and from the context supplied by `task_show`.
5. Register each formal deliverable with `statemng_artifact_add`, then finish with exactly one of `statemng_task_submit`, `statemng_task_block`, or `statemng_task_fail`.
6. Never accept a task or unblock it. If either tool is visible, stop and report the configuration error to the main Agent.
