# sci-state-management

`sci-state-management` provides project-local persistent state management for multi-step scientific research. It includes the `statemng` CLI, a STDIO MCP Server, a static state UI, a general research Skill, and a restricted Codex execution Agent.

## Supported environment

- macOS or Linux;
- Python 3.10 or newer;
- a trusted Codex project;
- Codex Local sessions.

## Install

From the root of the host project:

```bash
git submodule add https://github.com/code-JDS/sci-state-management.git state-management
python3 state-management/manage.py install
```

The installer:

- installs a machine-local launcher under `$HOME/.codex/statemng/[PROJECT_NAME]/mcp`;
- registers the `statemng` MCP Server in `.codex/config.toml`;
- installs the `persistent-scientific-research` Skill;
- installs the restricted `persistent_research_worker` configuration;
- adds a managed state-management block to the root `AGENTS.md`;
- adds `.state-management/` to `.gitignore`;
- checks MCP `initialize` and `tools/list`.

After installation, start a new Codex Local session so that Codex loads the MCP tools, Skill, and custom Agent.

## Use

Ask Codex for a multi-step scientific research result in natural language. The main Agent loads the installed Skill, initializes or resumes the overall task, dispatches internal tasks to `persistent_research_worker`, reviews submitted artifacts, and accepts completed work.

State is stored only under:

```text
[HOST_PROJECT]/.state-management/[TASK_NAME]/
```

Users do not run the internal `statemng` CLI or edit state JSON directly.

## Update

From the host project:

```bash
python3 state-management/manage.py update
```

The command fetches the latest `origin/main`, checks it out in the submodule, reruns installation, and repeats the MCP self-check. It does not modify `.state-management/` or commit changes in the host project.

## Tests

From `state-management/`:

```bash
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
python3 -B -m unittest discover -s state_management/state_ui/tests -p 'test_*.py' -v
```
