# Pi configuration

This directory contains the project-level [Pi](https://pi.dev) configuration. After cloning, start Pi from the repository root and approve the project when prompted. Project settings supplement or override the user configuration in `~/.pi/agent/`.

## Standard locations

| Path | Purpose |
|---|---|
| `.pi/settings.json` | Project settings and package declarations |
| `.pi/mcp.json` | Project MCP servers |
| `.pi/extensions/` | Project extensions |
| `.pi/skills/` | Project skills |
| `.pi/prompts/` | Project prompt templates |
| `.pi/themes/` | Project themes |
| `~/.pi/agent/` | Global Pi configuration, credentials, and models |

## RENCI Azure models

Pi does not load `models.json` from a project `.pi/` directory. Copy or merge this repository's `.pi/models.json` into the global location:

```bash
cp .pi/models.json ~/.pi/agent/models.json
export RENCI_AZURE_API_KEY="<your-api-key>"
```

If `~/.pi/agent/models.json` already contains custom providers, merge the `renci-azure` provider instead of overwriting the file. Persist the environment variable using your shell or secret manager; never commit the API key.

## Subagents

`.pi/settings.json` declares the pinned `@tintinweb/pi-subagents` package. Pi installs it after project trust is granted. To reconcile packages manually, run:

```bash
pi update --extensions --approve
```

The example custom agent is `.pi/agents/example-reviewer.md`. View and run available agents from `/agents` in an interactive Pi session.
