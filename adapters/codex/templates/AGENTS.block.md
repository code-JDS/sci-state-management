<!-- statemng:begin -->
## Persistent Scientific Research State Management

1. Multi-step scientific research must load the `persistent-scientific-research` Skill.
2. Every state operation must use the `statemng` MCP tools.
3. Internal execution tasks must use the restricted `persistent_research_worker` custom agent.
4. If the `statemng` tools are unavailable, stop the state-managed task; do not use the CLI or edit `.state-management/` directly.
<!-- statemng:end -->
