# CongeriesCore

CongeriesCore is a lightweight, business-independent runtime core for Agents
and Workflows. Version 0.2 currently implements shared runtime types, the Run
lifecycle, the generic Scope authorization foundation, and the complete Runtime
Event delivery subsystem defined by the project specifications.

## Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/)

## Development

```bash
uv sync --all-groups
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
uv build
```

The runtime package has no third-party production dependencies. Application
workflows, providers, plugins, tools, MCP integration, and infrastructure remain
outside the current implementation boundary.

The generic authorization dispatcher is available, while end-to-end Tool,
Provider, and MCP enforcement remains in progress. See [tasks.md](tasks.md) for
the current delivery snapshot and next milestone.

Start with the [architecture overview](docs/overview.md), then use
[agents.md](agents.md) for the authoritative document map and contribution
workflow.
